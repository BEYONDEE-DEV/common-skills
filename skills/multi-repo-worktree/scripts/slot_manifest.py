#!/usr/bin/env python3
"""Atomically manage permanent multi-repo worktree generations."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

if os.name == "nt":
    import msvcrt
else:
    import fcntl

SCHEMA_VERSION = 5
LEGACY_PERMANENT_SCHEMA_VERSIONS = {2, 3, 4}
MANIFEST_RELATIVE_PATH = Path(".multi-repo-worktree") / "slots.json"
LOCKS_RELATIVE_PATH = Path(".multi-repo-worktree") / "operation-locks"
GENERATION_WORKTREES_RELATIVE_PATH = Path(".multi-repo-worktree") / "generation-worktrees"


class ManifestError(RuntimeError):
    pass


def manifest_path(root: Path) -> Path:
    return root.resolve() / MANIFEST_RELATIVE_PATH


def operation_lock_path(root: Path, slot: str) -> Path:
    digest = hashlib.sha256(slot.encode("utf-8")).hexdigest()
    return root.resolve() / LOCKS_RELATIVE_PATH / f"{digest}.json"


def generation_worktree_path(root: Path, slot: str, repo: str, target: str) -> Path:
    identity = "\0".join((slot, repo, target)).encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()
    return root.resolve() / GENERATION_WORKTREES_RELATIVE_PATH / f"{digest}.worktree"


def empty_manifest() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "slots": {}}


def is_object_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) in {40, 64}
        and all(character in "0123456789abcdef" for character in value)
    )


KNOWN_TRANSFER_SUBJECT_PLACEHOLDERS = (
    "<source-branch>",
    "<source_branch>",
    "<branch-family>",
    "<target>",
    "<repo>",
    "<repository>",
    "<source>",
    "<source-tree>",
    "<result-tree>",
    "<task-summary>",
    "<task_summary>",
)


def normalize_transfer_subject(value: Any) -> str:
    if not isinstance(value, str):
        raise ManifestError("transfer subject must be a string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ManifestError("transfer subject must not contain control characters")
    normalized = value.strip(" ")
    if not normalized:
        raise ManifestError("transfer subject must not be blank")
    folded = normalized.casefold()
    if any(placeholder in folded for placeholder in KNOWN_TRANSFER_SUBJECT_PLACEHOLDERS):
        raise ManifestError("transfer subject contains an unresolved workflow placeholder")
    return normalized


def validate_submission(name: str, identity: dict[str, Any], slot_name: str) -> None:
    submission = identity["submission"]
    generation = identity["generation"]
    if submission is None:
        if generation > 0 and "legacy_schema" not in identity:
            raise ManifestError(
                f"slot {slot_name!r} repository {name!r} generation requires submission evidence"
            )
        return
    if not isinstance(submission, dict) or set(submission) != {
        "generation",
        "head_branch",
        "continuation_boundary_sha",
        "observed_head_sha",
    }:
        raise ManifestError(f"slot {slot_name!r} repository {name!r} has invalid submission evidence")
    if (
        type(submission["generation"]) is not int
        or submission["generation"] <= 0
        or submission["generation"] != generation
        or not isinstance(submission["head_branch"], str)
        or not submission["head_branch"]
        or not is_object_id(submission["continuation_boundary_sha"])
        or not is_object_id(submission["observed_head_sha"])
    ):
        raise ManifestError(f"slot {slot_name!r} repository {name!r} has invalid submission evidence")


def validate_rotation(
    name: str,
    rotation: Any,
    slot_name: str,
    legacy_schema: int | None = None,
) -> None:
    legacy_fields = {
        "phase",
        "from_branch",
        "from_branch_generation",
        "from_head_sha",
        "boundary_sha",
        "source_sha",
        "target_branch",
        "target_branch_generation",
        "result_tree_sha",
        "temporary_worktree",
    }
    current_fields = legacy_fields | {"source_tree_sha", "transfer_subject"}
    current_with_target = current_fields | {"target_head_sha"}
    allowed_field_sets = (
        {frozenset(legacy_fields)}
        if legacy_schema == 4
        else {frozenset(current_fields), frozenset(current_with_target)}
    )
    if not isinstance(rotation, dict) or frozenset(rotation) not in allowed_field_sets:
        raise ManifestError(f"slot {slot_name!r} repository {name!r} has invalid rotation journal")
    phases = {"planned", "branch-created", "switched"}
    if legacy_schema != 4:
        phases.add("retired")
    if rotation["phase"] not in phases:
        raise ManifestError(f"slot {slot_name!r} repository {name!r} has invalid rotation phase")
    if (
        not isinstance(rotation["from_branch"], str)
        or not rotation["from_branch"]
        or type(rotation["from_branch_generation"]) is not int
        or rotation["from_branch_generation"] <= 0
        or not isinstance(rotation["target_branch"], str)
        or not rotation["target_branch"]
        or not isinstance(rotation["temporary_worktree"], str)
        or not rotation["temporary_worktree"]
        or not Path(rotation["temporary_worktree"]).is_absolute()
        or type(rotation["target_branch_generation"]) is not int
        or rotation["target_branch_generation"] != rotation["from_branch_generation"] + 1
        or not all(
            is_object_id(rotation[field])
            for field in (
                "from_head_sha",
                "boundary_sha",
                "source_sha",
                "result_tree_sha",
            )
        )
    ):
        raise ManifestError(f"slot {slot_name!r} repository {name!r} has invalid rotation journal")
    if legacy_schema == 4:
        return
    if "target_head_sha" in rotation and not is_object_id(rotation["target_head_sha"]):
        raise ManifestError(f"slot {slot_name!r} repository {name!r} has invalid target head")
    if rotation["phase"] == "retired" and "target_head_sha" not in rotation:
        raise ManifestError(f"slot {slot_name!r} repository {name!r} retired without target head")
    if not is_object_id(rotation["source_tree_sha"]):
        raise ManifestError(f"slot {slot_name!r} repository {name!r} has invalid rotation journal")
    subject = rotation["transfer_subject"]
    empty_transfer = rotation["source_tree_sha"] == rotation["result_tree_sha"]
    if empty_transfer:
        if subject is not None:
            raise ManifestError(
                f"slot {slot_name!r} repository {name!r} empty rotation must not have a subject"
            )
    elif subject is None:
        raise ManifestError(
            f"slot {slot_name!r} repository {name!r} non-empty rotation requires a subject"
        )
    elif normalize_transfer_subject(subject) != subject:
        raise ManifestError(
            f"slot {slot_name!r} repository {name!r} transfer subject is not normalized"
        )


def validate_repository(name: str, identity: Any, slot_name: str) -> None:
    if not isinstance(name, str) or not name or not isinstance(identity, dict):
        raise ManifestError(f"slot {slot_name!r} has an invalid repository identity")
    required_fields = {
        "branch_family",
        "branch",
        "branch_generation",
        "branch_base_sha",
        "source_branch",
        "remote",
        "generation",
        "pr",
        "submission",
    }
    if not required_fields.issubset(identity) or set(identity) - (
        required_fields
        | {
            "rotation",
            "last_rotation",
            "last_reconciliation",
            "pending_remote_cleanups",
            "legacy_schema",
        }
    ):
        raise ManifestError(f"slot {slot_name!r} repository {name!r} has unexpected fields")
    if not isinstance(identity["branch_family"], str) or not identity["branch_family"]:
        raise ManifestError(f"slot {slot_name!r} repository {name!r} has an invalid branch family")
    if not isinstance(identity["branch"], str) or not identity["branch"]:
        raise ManifestError(f"slot {slot_name!r} repository {name!r} has an invalid branch")
    if type(identity["branch_generation"]) is not int or identity["branch_generation"] <= 0:
        raise ManifestError(f"slot {slot_name!r} repository {name!r} has an invalid branch generation")
    if identity["branch_base_sha"] is not None and not is_object_id(identity["branch_base_sha"]):
        raise ManifestError(f"slot {slot_name!r} repository {name!r} has an invalid branch base SHA")
    source_branch = identity["source_branch"]
    remote = identity["remote"]
    if (source_branch is None) != (remote is None):
        raise ManifestError(
            f"slot {slot_name!r} repository {name!r} must bind source branch and remote together"
        )
    if source_branch is not None and (
        not isinstance(source_branch, str)
        or not source_branch
        or not isinstance(remote, str)
        or not remote
    ):
        raise ManifestError(f"slot {slot_name!r} repository {name!r} has invalid source context")
    if type(identity["generation"]) is not int or identity["generation"] < 0:
        raise ManifestError(f"slot {slot_name!r} repository {name!r} has an invalid generation")
    pr = identity["pr"]
    if pr is not None and (not isinstance(pr, str) or not pr):
        raise ManifestError(f"slot {slot_name!r} repository {name!r} has an invalid PR")
    if identity["generation"] == 0 and pr is not None:
        raise ManifestError(f"slot {slot_name!r} repository {name!r} generation 0 must not have a PR")
    if identity["generation"] > 0 and pr is None:
        raise ManifestError(f"slot {slot_name!r} repository {name!r} generation requires a PR")
    if "legacy_schema" in identity:
        if identity["legacy_schema"] not in LEGACY_PERMANENT_SCHEMA_VERSIONS:
            raise ManifestError(f"slot {slot_name!r} repository {name!r} has invalid legacy evidence")
    elif identity["branch_base_sha"] is None:
        raise ManifestError(f"slot {slot_name!r} repository {name!r} requires a branch base SHA")
    validate_submission(name, identity, slot_name)
    if identity["generation"] == 0 and identity["submission"] is not None:
        raise ManifestError(f"slot {slot_name!r} repository {name!r} generation 0 has a submission")
    if "rotation" in identity:
        validate_rotation(name, identity["rotation"], slot_name, identity.get("legacy_schema"))
        rotation = identity["rotation"]
        if (
            rotation["from_branch"] != identity["branch"]
            or rotation["from_branch_generation"] != identity["branch_generation"]
        ):
            raise ManifestError(f"slot {slot_name!r} repository {name!r} rotation source mismatch")
    if "last_rotation" in identity:
        receipt = identity["last_rotation"]
        legacy_receipt_fields = {
            "target_branch",
            "target_branch_generation",
            "source_sha",
            "target_head_sha",
            "result_tree_sha",
        }
        receipt_fields = legacy_receipt_fields | {
            "from_branch",
            "from_branch_generation",
            "from_head_sha",
        }
        invalid_source_evidence = False
        if isinstance(receipt, dict) and set(receipt) == receipt_fields:
            invalid_source_evidence = (
                not isinstance(receipt["from_branch"], str)
                or not receipt["from_branch"]
                or receipt["from_branch"] == receipt["target_branch"]
                or type(receipt["from_branch_generation"]) is not int
                or receipt["from_branch_generation"] <= 0
                or receipt["from_branch_generation"] + 1
                != receipt["target_branch_generation"]
                or not is_object_id(receipt["from_head_sha"])
            )
        if (
            not isinstance(receipt, dict)
            or frozenset(receipt)
            not in {frozenset(legacy_receipt_fields), frozenset(receipt_fields)}
            or not isinstance(receipt["target_branch"], str)
            or not receipt["target_branch"]
            or type(receipt["target_branch_generation"]) is not int
            or receipt["target_branch_generation"] <= 1
            or not all(
                is_object_id(receipt[field])
                for field in ("source_sha", "target_head_sha", "result_tree_sha")
            )
            or receipt["target_branch"] != identity["branch"]
            or receipt["target_branch_generation"] != identity["branch_generation"]
            or receipt["source_sha"] != identity["branch_base_sha"]
            or invalid_source_evidence
        ):
            raise ManifestError(
                f"slot {slot_name!r} repository {name!r} has an invalid rotation receipt"
            )
    if "last_reconciliation" in identity:
        receipt = identity["last_reconciliation"]
        legacy_receipt_fields = {
            "from_generation",
            "from_pr",
            "to_generation",
            "to_pr",
        }
        evidence_fields = legacy_receipt_fields | {
            "head_branch",
            "continuation_boundary_sha",
            "observed_head_sha",
        }
        if (
            not isinstance(receipt, dict)
            or frozenset(receipt) not in {frozenset(legacy_receipt_fields), frozenset(evidence_fields)}
        ):
            raise ManifestError(
                f"slot {slot_name!r} repository {name!r} has an invalid reconciliation receipt"
            )
        from_generation = receipt["from_generation"]
        to_generation = receipt["to_generation"]
        from_pr = receipt["from_pr"]
        to_pr = receipt["to_pr"]
        invalid_evidence = False
        if set(receipt) == evidence_fields:
            invalid_evidence = (
                identity["submission"] is None
                or receipt["head_branch"] != identity["submission"]["head_branch"]
                or receipt["continuation_boundary_sha"]
                != identity["submission"]["continuation_boundary_sha"]
                or receipt["observed_head_sha"] != identity["submission"]["observed_head_sha"]
            )
        elif identity.get("legacy_schema") not in {2, 3}:
            invalid_evidence = True
        if (
            type(from_generation) is not int
            or from_generation <= 0
            or type(to_generation) is not int
            or to_generation != from_generation + 1
            or not isinstance(from_pr, str)
            or not from_pr
            or not isinstance(to_pr, str)
            or not to_pr
            or from_pr == to_pr
            or identity["generation"] != to_generation
            or pr != to_pr
            or invalid_evidence
        ):
            raise ManifestError(
                f"slot {slot_name!r} repository {name!r} has an invalid reconciliation receipt"
            )
    if "pending_remote_cleanups" in identity:
        pending_cleanups = identity["pending_remote_cleanups"]
        if not isinstance(pending_cleanups, list) or not pending_cleanups:
            raise ManifestError(
                f"slot {slot_name!r} repository {name!r} has invalid pending remote cleanup"
            )
        seen_cleanup_prs: set[str] = set()
        for cleanup in pending_cleanups:
            if not isinstance(cleanup, dict) or set(cleanup) != {"pr", "submission", "last_rotation"}:
                raise ManifestError(
                    f"slot {slot_name!r} repository {name!r} has invalid pending remote cleanup"
                )
            cleanup_pr = cleanup["pr"]
            submission = cleanup["submission"]
            receipt = cleanup["last_rotation"]
            legacy_cleanup_receipt_fields = {
                "target_branch",
                "target_branch_generation",
                "source_sha",
                "target_head_sha",
                "result_tree_sha",
            }
            cleanup_receipt_fields = legacy_cleanup_receipt_fields | {
                "from_branch",
                "from_branch_generation",
                "from_head_sha",
            }
            invalid_cleanup_receipt = receipt is not None and (
                not isinstance(receipt, dict)
                or frozenset(receipt)
                not in {
                    frozenset(legacy_cleanup_receipt_fields),
                    frozenset(cleanup_receipt_fields),
                }
                or not isinstance(receipt["target_branch"], str)
                or not receipt["target_branch"]
                or type(receipt["target_branch_generation"]) is not int
                or receipt["target_branch_generation"] <= 1
                or not all(
                    is_object_id(receipt[field])
                    for field in ("source_sha", "target_head_sha", "result_tree_sha")
                )
                or (
                    set(receipt) == cleanup_receipt_fields
                    and (
                        not isinstance(receipt["from_branch"], str)
                        or not receipt["from_branch"]
                        or type(receipt["from_branch_generation"]) is not int
                        or receipt["from_branch_generation"] <= 0
                        or not is_object_id(receipt["from_head_sha"])
                    )
                )
            )
            if (
                not isinstance(cleanup_pr, str)
                or not cleanup_pr
                or cleanup_pr == identity["pr"]
                or cleanup_pr in seen_cleanup_prs
                or not isinstance(submission, dict)
                or set(submission)
                != {"generation", "head_branch", "continuation_boundary_sha", "observed_head_sha"}
                or type(submission["generation"]) is not int
                or submission["generation"] <= 0
                or not isinstance(submission["head_branch"], str)
                or not submission["head_branch"]
                or not is_object_id(submission["continuation_boundary_sha"])
                or not is_object_id(submission["observed_head_sha"])
                or invalid_cleanup_receipt
            ):
                raise ManifestError(
                    f"slot {slot_name!r} repository {name!r} has invalid pending remote cleanup"
                )
            seen_cleanup_prs.add(cleanup_pr)


def validate_manifest_v5(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ManifestError("manifest must be a JSON object")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ManifestError(f"manifest must use schema {SCHEMA_VERSION}")
    if set(data) != {"schema_version", "slots"}:
        raise ManifestError("manifest has unexpected top-level fields")
    slots = data.get("slots")
    if not isinstance(slots, dict):
        raise ManifestError("manifest slots must be a JSON object")
    for name, slot in slots.items():
        if not isinstance(name, str) or not name or not isinstance(slot, dict):
            raise ManifestError("manifest contains an invalid slot entry")
        if set(slot) != {"path", "repositories"}:
            raise ManifestError(f"slot {name!r} has unexpected fields")
        if (
            not isinstance(slot["path"], str)
            or not slot["path"]
            or not Path(slot["path"]).is_absolute()
        ):
            raise ManifestError(f"slot {name!r} has an invalid path")
        repositories = slot["repositories"]
        if not isinstance(repositories, dict) or not repositories:
            raise ManifestError(f"slot {name!r} must have repository bindings")
        for repo, identity in repositories.items():
            validate_repository(repo, identity, name)
    return data


def promote_legacy_manifest(data: Any, version: int) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ManifestError("manifest must be a JSON object")
    if set(data) != {"schema_version", "slots"}:
        raise ManifestError("manifest has unexpected top-level fields")
    slots = data.get("slots")
    if not isinstance(slots, dict):
        raise ManifestError("manifest slots must be a JSON object")

    promoted: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "slots": {}}
    for name, slot in slots.items():
        if not isinstance(name, str) or not name or not isinstance(slot, dict):
            raise ManifestError("manifest contains an invalid slot entry")
        if set(slot) != {"path", "repositories"}:
            raise ManifestError(f"slot {name!r} has unexpected fields")
        if (
            not isinstance(slot["path"], str)
            or not slot["path"]
            or not Path(slot["path"]).is_absolute()
        ):
            raise ManifestError(f"slot {name!r} has an invalid path")
        repositories = slot["repositories"]
        if not isinstance(repositories, dict) or not repositories:
            raise ManifestError(f"slot {name!r} must have repository bindings")
        promoted_repositories: dict[str, Any] = {}
        for repo, identity in repositories.items():
            if not isinstance(repo, str) or not repo or not isinstance(identity, dict):
                raise ManifestError(f"slot {name!r} has an invalid repository identity")
            if version == 4:
                candidate = dict(identity)
                if "legacy_schema" not in candidate:
                    candidate["legacy_schema"] = 4
                validate_repository(repo, candidate, name)
                promoted_repositories[repo] = candidate
                continue
            if version == 2:
                if set(identity) != {"branch", "generation", "pr"}:
                    raise ManifestError(f"slot {name!r} repository {repo!r} has unexpected fields")
                source_branch = None
                remote = None
                receipt = None
            else:
                required = {"branch", "source_branch", "remote", "generation", "pr"}
                if not required.issubset(identity) or set(identity) - (required | {"last_reconciliation"}):
                    raise ManifestError(f"slot {name!r} repository {repo!r} has unexpected fields")
                source_branch = identity.get("source_branch")
                remote = identity.get("remote")
                receipt_present = "last_reconciliation" in identity
                receipt = identity.get("last_reconciliation")
            candidate = {
                "branch_family": identity.get("branch"),
                "branch": identity.get("branch"),
                "branch_generation": max(1, identity.get("generation", 0)),
                "branch_base_sha": None,
                "source_branch": source_branch,
                "remote": remote,
                "generation": identity.get("generation"),
                "pr": identity.get("pr"),
                "submission": None,
                "legacy_schema": version,
            }
            if version == 3 and receipt_present:
                candidate["last_reconciliation"] = receipt
            validate_repository(repo, candidate, name)
            promoted_repositories[repo] = candidate
        promoted["slots"][name] = {
            "path": slot["path"],
            "repositories": promoted_repositories,
        }
    return validate_manifest_v5(promoted)


def validate_manifest(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ManifestError("manifest must be a JSON object")
    version = data.get("schema_version")
    if type(version) is int and version == SCHEMA_VERSION:
        return validate_manifest_v5(data)
    if type(version) is int and version in LEGACY_PERMANENT_SCHEMA_VERSIONS:
        return promote_legacy_manifest(data, version)
    raise ManifestError(
        f"unsupported manifest schema: {version!r}; "
        "legacy active/released manifests require manual correction"
    )


def load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return empty_manifest()
    try:
        return validate_manifest(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read manifest {path}: {exc}") from exc


@contextmanager
def manifest_lock(path: Path, timeout_seconds: float = 3.0) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(path.suffix + ".lock")
    deadline = time.monotonic() + timeout_seconds
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        if os.name == "nt" and os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
    except OSError as exc:
        os.close(descriptor)
        raise ManifestError(f"cannot initialize manifest lock {lock}: {exc}") from exc
    while not acquired:
        try:
            if os.name == "nt":
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as exc:
            if os.name != "nt" and not isinstance(exc, BlockingIOError):
                os.close(descriptor)
                raise ManifestError(f"cannot lock manifest {lock}: {exc}") from exc
            if time.monotonic() >= deadline:
                os.close(descriptor)
                raise ManifestError(f"manifest is locked: {lock}")
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            if os.name == "nt":
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def write_json_atomic(path: Path, data: Any, prefix: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def write_manifest(path: Path, data: dict[str, Any]) -> None:
    validate_manifest_v5(data)
    write_json_atomic(path, data, "slots.")


def require_slot(data: dict[str, Any], name: str) -> dict[str, Any]:
    slot = data["slots"].get(name)
    if slot is None:
        raise ManifestError(f"slot does not exist: {name}")
    return slot


def require_complete_context(slot: dict[str, Any], name: str) -> None:
    incomplete = sorted(
        repo
        for repo, identity in slot["repositories"].items()
        if identity["source_branch"] is None or identity["remote"] is None
    )
    if incomplete:
        raise ManifestError(
            f"slot {name!r} requires source context for: {', '.join(incomplete)}"
        )


def require_generation_evidence(identity: dict[str, Any], repo: str) -> None:
    if "legacy_schema" in identity or identity["branch_base_sha"] is None:
        raise ManifestError(f"repository {repo!r} requires proven generation migration")
    if identity["generation"] > 0 and identity["submission"] is None:
        raise ManifestError(f"repository {repo!r} requires proven submission migration")


def parse_repository_bindings(values: list[list[str]]) -> dict[str, dict[str, str]]:
    bindings: dict[str, dict[str, str]] = {}
    for value in values:
        if len(value) != 5:
            raise ManifestError("repository binding requires REPO BRANCH SOURCE REMOTE SOURCE_SHA")
        repo, branch, source_branch, remote, source_sha = value
        if not repo or not branch or not source_branch or not remote:
            raise ManifestError("repository binding values must not be empty")
        if not is_object_id(source_sha):
            raise ManifestError(f"repository {repo!r} has invalid source SHA")
        if repo in bindings:
            raise ManifestError(f"duplicate repository binding: {repo}")
        bindings[repo] = {
            "branch": branch,
            "source_branch": source_branch,
            "remote": remote,
            "source_sha": source_sha,
        }
    if not bindings:
        raise ManifestError("at least one repository binding is required")
    return bindings


def parse_repository_contexts(values: list[list[str]]) -> dict[str, dict[str, str]]:
    contexts: dict[str, dict[str, str]] = {}
    for repo, source_branch, remote in values:
        if not repo or not source_branch or not remote:
            raise ManifestError("repository context values must not be empty")
        if repo in contexts:
            raise ManifestError(f"duplicate repository context: {repo}")
        contexts[repo] = {"source_branch": source_branch, "remote": remote}
    if not contexts:
        raise ManifestError("at least one repository context is required")
    return contexts


def parse_repository_updates(values: list[list[str]]) -> list[tuple[str, int, str, str, str, str]]:
    updates: list[tuple[str, int, str, str, str, str]] = []
    for value in values:
        if len(value) != 6:
            raise ManifestError(
                "repository update requires REPO GENERATION PR HEAD_BRANCH BOUNDARY_SHA OBSERVED_SHA"
            )
        repo, expected_generation_text, pr = value[:3]
        try:
            expected_generation = int(expected_generation_text)
        except ValueError as exc:
            raise ManifestError(
                f"repository {repo!r} expected generation must be an integer"
            ) from exc
        head_branch, boundary_sha, observed_sha = value[3:]
        if not head_branch or not is_object_id(boundary_sha) or not is_object_id(observed_sha):
            raise ManifestError(f"repository {repo!r} has invalid submission update")
        updates.append((repo, expected_generation, pr, head_branch, boundary_sha, observed_sha))
    return updates


def parse_repository_recoveries(values: list[list[str]]) -> list[tuple[str, int, str, str, str, str, str]]:
    recoveries: list[tuple[str, int, str, str, str, str, str]] = []
    for value in values:
        if len(value) != 7:
            raise ManifestError(
                "repository recovery requires REPO GENERATION CURRENT_PR RECOVERED_PR HEAD BOUNDARY OBSERVED"
            )
        repo, expected_generation_text, expected_pr, recovered_pr = value[:4]
        try:
            expected_generation = int(expected_generation_text)
        except ValueError as exc:
            raise ManifestError(
                f"repository {repo!r} expected generation must be an integer"
            ) from exc
        head_branch, boundary_sha, observed_sha = value[4:]
        if not head_branch or not is_object_id(boundary_sha) or not is_object_id(observed_sha):
            raise ManifestError(f"repository {repo!r} has invalid recovery submission evidence")
        recoveries.append(
            (repo, expected_generation, expected_pr, recovered_pr, head_branch, boundary_sha, observed_sha)
        )
    return recoveries


def parse_repository_migrations(values: list[list[str]]) -> list[tuple[str, int, str | None, str, int, str, dict[str, Any] | None]]:
    migrations: list[tuple[str, int, str | None, str, int, str, dict[str, Any] | None]] = []
    for value in values:
        if len(value) != 9:
            raise ManifestError(
                "repository migration requires REPO GENERATION PR_OR_DASH FAMILY BRANCH_GENERATION BASE_SHA HEAD_OR_DASH BOUNDARY_OR_DASH OBSERVED_OR_DASH"
            )
        repo, generation_text, pr_text, family, branch_generation_text, base_sha, head, boundary, observed = value
        try:
            generation = int(generation_text)
            branch_generation = int(branch_generation_text)
        except ValueError as exc:
            raise ManifestError(f"repository {repo!r} migration generations must be integers") from exc
        pr = None if pr_text == "-" else pr_text
        if generation == 0:
            if any(item != "-" for item in (head, boundary, observed)):
                raise ManifestError(f"repository {repo!r} generation 0 migration has submission evidence")
            submission = None
        else:
            if "-" in {head, boundary, observed}:
                raise ManifestError(f"repository {repo!r} migration requires submission evidence")
            submission = {
                "generation": generation,
                "head_branch": head,
                "continuation_boundary_sha": boundary,
                "observed_head_sha": observed,
            }
        migrations.append((repo, generation, pr, family, branch_generation, base_sha, submission))
    return migrations


def parse_repository_corrections(values: list[list[str]]) -> list[tuple[str, int, str, str, str]]:
    corrections: list[tuple[str, int, str, str, str]] = []
    for repo, generation_text, pr, expected_head, new_head in values:
        try:
            generation = int(generation_text)
        except ValueError as exc:
            raise ManifestError(f"repository {repo!r} generation must be an integer") from exc
        corrections.append((repo, generation, pr, expected_head, new_head))
    return corrections


def parse_repository_rotations(
    values: list[list[str]],
) -> list[tuple[str, str, int, str, str, str, str, str, int, str, str | None, str]]:
    rotations: list[
        tuple[str, str, int, str, str, str, str, str, int, str, str | None, str]
    ] = []
    for value in values:
        if len(value) != 12:
            raise ManifestError(
                "repository rotation requires REPO FROM_BRANCH FROM_GENERATION FROM_HEAD "
                "BOUNDARY SOURCE SOURCE_TREE TARGET TARGET_GENERATION RESULT_TREE "
                "SUBJECT_OR_DASH TEMPORARY_WORKTREE"
            )
        (
            repo,
            from_branch,
            from_generation_text,
            from_head,
            boundary,
            source,
            source_tree,
            target,
            target_generation_text,
            result_tree,
            subject_text,
            temporary_worktree,
        ) = value
        try:
            from_generation = int(from_generation_text)
            target_generation = int(target_generation_text)
        except ValueError as exc:
            raise ManifestError(f"repository {repo!r} rotation generations must be integers") from exc
        rotations.append(
            (
                repo,
                from_branch,
                from_generation,
                from_head,
                boundary,
                source,
                source_tree,
                target,
                target_generation,
                result_tree,
                None if subject_text == "-" else normalize_transfer_subject(subject_text),
                temporary_worktree,
            )
        )
    return rotations


def parse_repository_rotation_subjects(
    values: list[list[str]],
) -> list[tuple[str, str, int, str, str, str, str, str, int, str, str | None, str]]:
    return parse_repository_rotations(values)


def migrate_batch(
    data: dict[str, Any],
    name: str,
    migrations: list[tuple[str, int, str | None, str, int, str, dict[str, Any] | None]],
) -> dict[str, Any]:
    if not migrations:
        raise ManifestError("at least one repository migration is required")
    slot = require_slot(data, name)
    require_complete_context(slot, name)
    repositories = slot["repositories"]
    seen: set[str] = set()
    pending: list[tuple[str, dict[str, Any]]] = []
    for repo, generation, pr, family, branch_generation, base_sha, submission in migrations:
        if repo in seen:
            raise ManifestError(f"duplicate repository migration: {repo}")
        seen.add(repo)
        if repo not in repositories:
            raise ManifestError(f"slot {name!r} has no repository binding for: {repo}")
        identity = repositories[repo]
        if identity["generation"] != generation or identity["pr"] != pr:
            raise ManifestError(f"repository {repo!r} migration identity mismatch")
        if identity.get("legacy_schema") == 4 and "rotation" in identity:
            raise ManifestError(
                f"repository {repo!r} active rotation requires migrate-rotation-subject"
            )
        if not family or type(branch_generation) is not int or branch_generation <= 0 or not is_object_id(base_sha):
            raise ManifestError(f"repository {repo!r} has invalid migration evidence")
        candidate = dict(identity)
        candidate.update(
            branch_family=family,
            branch_generation=branch_generation,
            branch_base_sha=base_sha,
            submission=submission,
        )
        reconciliation = candidate.get("last_reconciliation")
        if reconciliation is not None and set(reconciliation) == {
            "from_generation",
            "from_pr",
            "to_generation",
            "to_pr",
        }:
            if submission is None:
                raise ManifestError(
                    f"repository {repo!r} reconciliation migration requires submission evidence"
                )
            upgraded_reconciliation = dict(reconciliation)
            upgraded_reconciliation.update(
                head_branch=submission["head_branch"],
                continuation_boundary_sha=submission["continuation_boundary_sha"],
                observed_head_sha=submission["observed_head_sha"],
            )
            candidate["last_reconciliation"] = upgraded_reconciliation
        candidate.pop("legacy_schema", None)
        validate_repository(repo, candidate, name)
        if candidate == identity:
            continue
        if "legacy_schema" not in identity:
            raise ManifestError(f"repository {repo!r} migration is already complete with different evidence")
        pending.append((repo, candidate))
    for repo, candidate in pending:
        repositories[repo] = candidate
    return slot


def migrate_rotation_subjects(
    data: dict[str, Any],
    name: str,
    migrations: list[
        tuple[str, str, int, str, str, str, str, str, int, str, str | None, str]
    ],
    root: Path,
) -> dict[str, Any]:
    if not migrations:
        raise ManifestError("at least one rotation subject migration is required")
    slot = require_slot(data, name)
    require_complete_context(slot, name)
    repositories = slot["repositories"]
    seen: set[str] = set()
    pending: list[tuple[str, dict[str, Any]]] = []
    for (
        repo,
        from_branch,
        from_generation,
        from_head,
        boundary,
        source,
        source_tree,
        target,
        target_generation,
        result_tree,
        transfer_subject,
        temporary_worktree,
    ) in migrations:
        if repo in seen:
            raise ManifestError(f"duplicate rotation subject migration: {repo}")
        seen.add(repo)
        if repo not in repositories:
            raise ManifestError(f"slot {name!r} has no repository binding for: {repo}")
        identity = repositories[repo]
        rotation = identity.get("rotation")
        if rotation is None:
            raise ManifestError(f"repository {repo!r} has no rotation journal")
        expected_temporary = generation_worktree_path(root, name, repo, target)
        if temporary_worktree != str(expected_temporary):
            raise ManifestError(
                f"repository {repo!r} rotation temporary worktree is not deterministic"
            )
        old_identity = {
            "from_branch": from_branch,
            "from_branch_generation": from_generation,
            "from_head_sha": from_head,
            "boundary_sha": boundary,
            "source_sha": source,
            "target_branch": target,
            "target_branch_generation": target_generation,
            "result_tree_sha": result_tree,
            "temporary_worktree": temporary_worktree,
        }
        if any(rotation.get(field) != value for field, value in old_identity.items()):
            raise ManifestError(f"repository {repo!r} rotation subject migration identity mismatch")
        migrated_rotation = dict(rotation)
        migrated_rotation["source_tree_sha"] = source_tree
        migrated_rotation["transfer_subject"] = transfer_subject
        validate_rotation(repo, migrated_rotation, name)
        if "legacy_schema" not in identity:
            if rotation == migrated_rotation:
                continue
            raise ManifestError(
                f"repository {repo!r} rotation subject migration is already complete "
                "with different evidence"
            )
        if identity["legacy_schema"] != 4:
            raise ManifestError(
                f"repository {repo!r} requires generation evidence migration before rotation"
            )
        repository_path = (Path(slot["path"]) / repo).resolve()
        try:
            from prepare_generation_branch import (
                TransitionError,
                inspect_legacy_repository_state,
            )

            git_state = inspect_legacy_repository_state(
                repo=repository_path,
                source_revision=source,
                result_tree_revision=result_tree,
                branch_family=identity["branch_family"],
                target_generation=target_generation,
                temporary_worktree=Path(temporary_worktree),
                workspace_root=root,
                slot=name,
                repository=repo,
            )
        except (TransitionError, OSError) as exc:
            raise ManifestError(
                f"repository {repo!r} rotation Git evidence is invalid: {exc}"
            ) from exc
        if git_state["source_tree_sha"] != source_tree:
            raise ManifestError(
                f"repository {repo!r} source tree does not match the exact source commit"
            )
        if not git_state["development_clean"]:
            raise ManifestError(
                f"repository {repo!r} development worktree is not clean during migration"
            )
        phase = rotation["phase"]
        target_exists = git_state["target_status"] == "existing"
        expected_subject = transfer_subject
        if target_exists:
            if git_state["temporary_status"] != "absent":
                raise ManifestError(
                    f"repository {repo!r} target and temporary worktree coexist"
                )
            if git_state["target_subject"] != expected_subject:
                raise ManifestError(
                    f"repository {repo!r} published target subject is not suitable "
                    "for migration"
                )
        elif phase != "planned":
            raise ManifestError(
                f"repository {repo!r} persisted rotation phase requires an exact target"
            )
        elif (
            git_state["temporary_status"] == "completed"
            and git_state["temporary_subject"] != expected_subject
        ):
            raise ManifestError(
                f"repository {repo!r} temporary commit subject requires safe recreation"
            )

        old_development = (
            git_state["development_branch"] == from_branch
            and git_state["development_head_sha"] == from_head
        )
        target_development = (
            target_exists
            and git_state["development_branch"] == target
            and git_state["development_head_sha"] == git_state["target_sha"]
        )
        if phase == "planned" and not old_development:
            raise ManifestError(
                f"repository {repo!r} planned rotation development state changed"
            )
        if phase == "branch-created" and not (old_development or target_development):
            raise ManifestError(
                f"repository {repo!r} branch-created development state is invalid"
            )
        if phase == "switched" and not target_development:
            raise ManifestError(
                f"repository {repo!r} switched development state is invalid"
            )
        candidate = dict(identity)
        candidate["rotation"] = migrated_rotation
        candidate.pop("legacy_schema")
        validate_repository(repo, candidate, name)
        pending.append((repo, candidate))
    for repo, candidate in pending:
        repositories[repo] = candidate
    return slot


def record_corrections(
    data: dict[str, Any],
    name: str,
    corrections: list[tuple[str, int, str, str, str]],
) -> dict[str, Any]:
    if not corrections:
        raise ManifestError("at least one repository correction is required")
    slot = require_slot(data, name)
    repositories = slot["repositories"]
    seen: set[str] = set()
    pending: list[tuple[str, str]] = []
    for repo, generation, pr, expected_head, new_head in corrections:
        if repo in seen:
            raise ManifestError(f"duplicate repository correction: {repo}")
        seen.add(repo)
        if repo not in repositories:
            raise ManifestError(f"slot {name!r} has no repository binding for: {repo}")
        identity = repositories[repo]
        require_generation_evidence(identity, repo)
        submission = identity["submission"]
        if identity["generation"] != generation or identity["pr"] != pr or submission is None:
            raise ManifestError(f"repository {repo!r} correction identity mismatch")
        if not is_object_id(expected_head) or not is_object_id(new_head):
            raise ManifestError(f"repository {repo!r} correction has invalid head SHA")
        if submission["observed_head_sha"] == new_head:
            continue
        if submission["observed_head_sha"] != expected_head:
            raise ManifestError(f"repository {repo!r} correction head mismatch")
        pending.append((repo, new_head))
    for repo, new_head in pending:
        repositories[repo]["submission"]["observed_head_sha"] = new_head
    return slot


def begin_rotations(
    data: dict[str, Any],
    name: str,
    rotations: list[
        tuple[str, str, int, str, str, str, str, str, int, str, str | None, str]
    ],
    root: Path,
) -> dict[str, Any]:
    if not rotations:
        raise ManifestError("at least one repository rotation is required")
    slot = require_slot(data, name)
    repositories = slot["repositories"]
    seen: set[str] = set()
    pending: list[tuple[str, dict[str, Any]]] = []
    for (
        repo,
        from_branch,
        from_generation,
        from_head,
        boundary,
        source,
        source_tree,
        target,
        target_generation,
        result_tree,
        transfer_subject,
        temporary_worktree,
    ) in rotations:
        if repo in seen:
            raise ManifestError(f"duplicate repository rotation: {repo}")
        seen.add(repo)
        if repo not in repositories:
            raise ManifestError(f"slot {name!r} has no repository binding for: {repo}")
        identity = repositories[repo]
        require_generation_evidence(identity, repo)
        rotation = {
            "phase": "planned",
            "from_branch": from_branch,
            "from_branch_generation": from_generation,
            "from_head_sha": from_head,
            "boundary_sha": boundary,
            "source_sha": source,
            "source_tree_sha": source_tree,
            "target_branch": target,
            "target_branch_generation": target_generation,
            "result_tree_sha": result_tree,
            "transfer_subject": transfer_subject,
            "temporary_worktree": temporary_worktree,
        }
        validate_rotation(repo, rotation, name)
        if identity.get("rotation") == rotation:
            continue
        if "rotation" in identity:
            raise ManifestError(f"repository {repo!r} already has a different rotation journal")
        if identity["branch"] != from_branch or identity["branch_generation"] != from_generation:
            raise ManifestError(f"repository {repo!r} rotation source mismatch")
        expected_target = f"{identity['branch_family']}-mrw-g{target_generation}"
        if target != expected_target:
            raise ManifestError(f"repository {repo!r} rotation target is not deterministic")
        expected_temporary = generation_worktree_path(root, name, repo, target)
        if temporary_worktree != str(expected_temporary):
            raise ManifestError(f"repository {repo!r} rotation temporary worktree is not deterministic")
        pending.append((repo, rotation))
    for repo, rotation in pending:
        repositories[repo]["rotation"] = rotation
    return slot


def advance_rotation(
    data: dict[str, Any],
    name: str,
    repo: str,
    expected: str,
    target: str,
    target_head_sha: str | None = None,
) -> dict[str, Any]:
    transitions = {
        ("planned", "branch-created"),
        ("branch-created", "switched"),
    }
    if (expected, target) not in transitions:
        raise ManifestError("invalid rotation phase transition")
    slot = require_slot(data, name)
    if repo not in slot["repositories"]:
        raise ManifestError(f"slot {name!r} has no repository binding for: {repo}")
    identity = slot["repositories"][repo]
    require_generation_evidence(identity, repo)
    rotation = identity.get("rotation")
    if rotation is None:
        raise ManifestError(f"repository {repo!r} has no rotation journal")
    if (expected, target) == ("planned", "branch-created"):
        if target_head_sha is None or not is_object_id(target_head_sha):
            raise ManifestError(f"repository {repo!r} branch creation requires exact target head")
        journaled_target = rotation.get("target_head_sha")
        if journaled_target is not None and journaled_target != target_head_sha:
            raise ManifestError(f"repository {repo!r} target head mismatch")
        rotation["target_head_sha"] = target_head_sha
    elif target_head_sha is not None:
        raise ManifestError("target head is valid only while recording branch creation")
    if rotation["phase"] == target:
        return slot
    if rotation["phase"] != expected:
        raise ManifestError(f"repository {repo!r} rotation phase mismatch")
    if (expected, target) == ("branch-created", "switched") and "target_head_sha" not in rotation:
        raise ManifestError(f"repository {repo!r} target head has not been recorded")
    rotation["phase"] = target
    return slot


def git_value(repo: Path, *arguments: str, allow_absent: bool = False) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if allow_absent and result.returncode == 1:
        return None
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown Git error"
        raise ManifestError(f"git {' '.join(arguments)} failed: {detail}")
    return result.stdout.strip()


def complete_rotation(
    data: dict[str, Any],
    name: str,
    repo: str,
    target_branch: str,
    target_generation: int,
    source_sha: str,
    target_head_sha: str,
    result_tree_sha: str,
) -> dict[str, Any]:
    slot = require_slot(data, name)
    if repo not in slot["repositories"]:
        raise ManifestError(f"slot {name!r} has no repository binding for: {repo}")
    identity = slot["repositories"][repo]
    require_generation_evidence(identity, repo)
    if (
        not is_object_id(source_sha)
        or not is_object_id(target_head_sha)
        or not is_object_id(result_tree_sha)
    ):
        raise ManifestError(f"repository {repo!r} rotation completion has invalid object evidence")
    requested_receipt = {
        "target_branch": target_branch,
        "target_branch_generation": target_generation,
        "source_sha": source_sha,
        "target_head_sha": target_head_sha,
        "result_tree_sha": result_tree_sha,
    }
    rotation = identity.get("rotation")
    if rotation is None:
        receipt = identity.get("last_rotation")
        if (
            identity["branch"] == target_branch
            and identity["branch_generation"] == target_generation
            and identity["branch_base_sha"] == source_sha
            and isinstance(receipt, dict)
            and all(receipt.get(field) == value for field, value in requested_receipt.items())
        ):
            repository_path = (Path(slot["path"]) / repo).resolve()
            actual_root = Path(
                git_value(repository_path, "rev-parse", "--show-toplevel") or ""
            ).resolve()
            if actual_root != repository_path:
                raise ManifestError(f"repository {repo!r} manifest path resolves elsewhere")
            if git_value(repository_path, "branch", "--show-current") != target_branch:
                raise ManifestError(f"repository {repo!r} target is not checked out")
            if (
                git_value(
                    repository_path,
                    "rev-parse",
                    "--verify",
                    f"refs/heads/{target_branch}",
                )
                != target_head_sha
                or git_value(repository_path, "rev-parse", "--verify", "HEAD")
                != target_head_sha
            ):
                raise ManifestError(f"repository {repo!r} completed target ref moved")
            if git_value(repository_path, "status", "--porcelain", "--untracked-files=all"):
                raise ManifestError(f"repository {repo!r} development worktree is dirty")
            return slot
        raise ManifestError(f"repository {repo!r} has no matching completed rotation")
    if rotation["phase"] != "retired":
        raise ManifestError(f"repository {repo!r} rotation has not retired its old branch")
    if (
        rotation["target_branch"] != target_branch
        or rotation["target_branch_generation"] != target_generation
        or rotation["source_sha"] != source_sha
        or rotation["result_tree_sha"] != result_tree_sha
        or rotation.get("target_head_sha") != target_head_sha
    ):
        raise ManifestError(f"repository {repo!r} rotation completion mismatch")
    repository_path = (Path(slot["path"]) / repo).resolve()
    actual_root = Path(git_value(repository_path, "rev-parse", "--show-toplevel") or "").resolve()
    if actual_root != repository_path:
        raise ManifestError(f"repository {repo!r} manifest path resolves elsewhere")
    if git_value(repository_path, "branch", "--show-current") != target_branch:
        raise ManifestError(f"repository {repo!r} target is not checked out")
    if (
        git_value(repository_path, "rev-parse", "--verify", f"refs/heads/{target_branch}")
        != target_head_sha
        or git_value(repository_path, "rev-parse", "--verify", "HEAD") != target_head_sha
    ):
        raise ManifestError(f"repository {repo!r} target ref moved before completion")
    if git_value(
        repository_path,
        "rev-parse",
        "--verify",
        "--quiet",
        f"refs/heads/{rotation['from_branch']}",
        allow_absent=True,
    ) is not None:
        raise ManifestError(f"repository {repo!r} old branch still exists")
    if git_value(repository_path, "status", "--porcelain", "--untracked-files=all"):
        raise ManifestError(f"repository {repo!r} development worktree is dirty")
    receipt = {
        "from_branch": rotation["from_branch"],
        "from_branch_generation": rotation["from_branch_generation"],
        "from_head_sha": rotation["from_head_sha"],
        **requested_receipt,
    }
    identity["branch"] = rotation["target_branch"]
    identity["branch_generation"] = rotation["target_branch_generation"]
    identity["branch_base_sha"] = rotation["source_sha"]
    identity["last_rotation"] = receipt
    identity.pop("rotation")
    return slot


def initialize(
    data: dict[str, Any],
    name: str,
    path: str,
    bindings: dict[str, dict[str, str]],
) -> dict[str, Any]:
    requested_path = str(Path(path).resolve())
    requested = {
        "path": requested_path,
        "repositories": {
            repo: {
                "branch_family": binding["branch"],
                "branch": binding["branch"],
                "branch_generation": 1,
                "branch_base_sha": binding["source_sha"],
                "source_branch": binding["source_branch"],
                "remote": binding["remote"],
                "generation": 0,
                "pr": None,
                "submission": None,
            }
            for repo, binding in sorted(bindings.items())
        },
    }
    current = data["slots"].get(name)
    if current is not None:
        if current["path"] != requested_path or set(current["repositories"]) != set(bindings):
            raise ManifestError(f"permanent slot binding mismatch for {name}")
        for repo, binding in bindings.items():
            identity = current["repositories"][repo]
            if identity["branch"] != binding["branch"]:
                raise ManifestError(f"permanent slot binding mismatch for {name}")
            if identity["branch_family"] != binding["branch"]:
                raise ManifestError(f"permanent slot binding mismatch for {name}")
            current_context = (identity["source_branch"], identity["remote"])
            requested_context = (binding["source_branch"], binding["remote"])
            if current_context == (None, None):
                identity["source_branch"], identity["remote"] = requested_context
            elif current_context != requested_context:
                raise ManifestError(f"permanent slot binding mismatch for {name}")
            requested_sha = binding["source_sha"]
            if identity["branch_base_sha"] != requested_sha:
                raise ManifestError(f"permanent slot binding mismatch for {name}")
        return current
    data["slots"][name] = requested
    return requested


def bind_context(
    data: dict[str, Any],
    name: str,
    contexts: dict[str, dict[str, str]],
) -> dict[str, Any]:
    slot = require_slot(data, name)
    repositories = slot["repositories"]
    if set(contexts) != set(repositories):
        missing = sorted(set(repositories) - set(contexts))
        extra = sorted(set(contexts) - set(repositories))
        details = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if extra:
            details.append(f"unknown: {', '.join(extra)}")
        raise ManifestError(f"slot {name!r} context must cover every repository ({'; '.join(details)})")

    for repo, context in contexts.items():
        identity = repositories[repo]
        current = (identity["source_branch"], identity["remote"])
        requested = (context["source_branch"], context["remote"])
        if current != (None, None) and current != requested:
            raise ManifestError(f"repository {repo!r} source context mismatch")

    for repo, context in contexts.items():
        repositories[repo]["source_branch"] = context["source_branch"]
        repositories[repo]["remote"] = context["remote"]
    return slot


def load_operation_lock(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read operation lock {path}: {exc}") from exc
    if (
        not isinstance(data, dict)
        or set(data) != {"slot", "token", "pid", "created_at"}
        or not isinstance(data.get("slot"), str)
        or not data["slot"]
        or not isinstance(data.get("token"), str)
        or not data["token"]
        or type(data.get("pid")) is not int
        or not isinstance(data.get("created_at"), str)
        or not data["created_at"]
    ):
        raise ManifestError(f"operation lock is malformed: {path}")
    return data


def acquire_operation_lock(root: Path, slot: str, token: str) -> dict[str, Any]:
    if not token:
        raise ManifestError("operation lock token must not be empty")
    path = operation_lock_path(root, slot)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "slot": slot,
        "token": token,
        "pid": os.getpid(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    descriptor, candidate_name = tempfile.mkstemp(
        prefix=f"{path.stem}.",
        suffix=".tmp",
        dir=path.parent,
    )
    candidate = Path(candidate_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(candidate, path)
        except FileExistsError:
            current = load_operation_lock(path)
            if current is not None and current["slot"] == slot and current["token"] == token:
                return current
            raise ManifestError(f"slot operation is locked: {slot}") from None
        except OSError as exc:
            raise ManifestError(f"cannot publish operation lock {path}: {exc}") from exc
    finally:
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass
    return payload


def require_operation_lock(root: Path, slot: str, token: str) -> dict[str, Any]:
    current = load_operation_lock(operation_lock_path(root, slot))
    if current is None:
        raise ManifestError(f"slot operation is not locked: {slot}")
    if current["slot"] != slot or current["token"] != token:
        raise ManifestError(f"slot operation lock token mismatch: {slot}")
    return current


def release_operation_lock(root: Path, slot: str, token: str) -> dict[str, Any]:
    path = operation_lock_path(root, slot)
    current = require_operation_lock(root, slot, token)
    try:
        path.unlink()
    except FileNotFoundError as exc:
        raise ManifestError(f"slot operation is not locked: {slot}") from exc
    return current


def record_batch(
    data: dict[str, Any],
    name: str,
    repository_updates: list[tuple[str, int, str, str, str, str]],
) -> dict[str, Any]:
    if not repository_updates:
        raise ManifestError("at least one repository PR update is required")
    updates: dict[str, tuple[int, str, str, str, str]] = {}
    for repo, expected_generation, pr, head_branch, boundary_sha, observed_sha in repository_updates:
        if not repo or not pr:
            raise ManifestError("repository and PR must not be empty")
        if type(expected_generation) is not int or expected_generation < 0:
            raise ManifestError("expected generation must be a non-negative integer")
        if repo in updates:
            raise ManifestError(f"duplicate repository PR update: {repo}")
        updates[repo] = (expected_generation, pr, head_branch, boundary_sha, observed_sha)

    slot = require_slot(data, name)
    require_complete_context(slot, name)
    repositories = slot["repositories"]
    missing = sorted(set(updates) - set(repositories))
    if missing:
        raise ManifestError(f"slot {name!r} has no repository binding for: {', '.join(missing)}")

    pending: list[tuple[str, int, str, dict[str, Any] | None]] = []
    for repo, (expected_generation, pr, head_branch, boundary_sha, observed_sha) in updates.items():
        identity = repositories[repo]
        submission = {
            "generation": expected_generation + 1,
            "head_branch": head_branch,
            "continuation_boundary_sha": boundary_sha,
            "observed_head_sha": observed_sha,
        }
        if (
            identity["generation"] == expected_generation + 1
            and identity["pr"] == pr
            and identity["submission"] == submission
        ):
            continue
        if identity["generation"] != expected_generation:
            raise ManifestError(
                f"repository {repo!r} generation mismatch: "
                f"expected {expected_generation}, found {identity['generation']}"
            )
        if expected_generation == 0 and identity["pr"] is not None:
            raise ManifestError(f"repository {repo!r} initial generation already has a PR")
        if expected_generation > 0 and identity["pr"] is None:
            raise ManifestError(f"repository {repo!r} generation has no current PR")
        if expected_generation > 0 and identity["pr"] == pr:
            raise ManifestError(f"repository {repo!r} new PR matches its current PR")
        require_generation_evidence(identity, repo)
        if head_branch != identity["branch"]:
            raise ManifestError(f"repository {repo!r} submitted head is not its active branch")
        if boundary_sha != observed_sha:
            raise ManifestError(f"repository {repo!r} new submission boundary must equal its head")
        pending.append((repo, expected_generation, pr, submission))

    for repo, expected_generation, pr, submission in pending:
        identity = repositories[repo]
        if expected_generation > 0:
            cleanup = {
                "pr": identity["pr"],
                "submission": identity["submission"],
                "last_rotation": identity.get("last_rotation"),
            }
            cleanups = identity.setdefault("pending_remote_cleanups", [])
            if cleanup not in cleanups:
                cleanups.append(cleanup)
        repositories[repo]["generation"] = expected_generation + 1
        repositories[repo]["pr"] = pr
        repositories[repo]["submission"] = submission
        repositories[repo].pop("last_reconciliation", None)
    return slot


def reconcile_batch(
    data: dict[str, Any],
    name: str,
    repository_recoveries: list[
        tuple[str, int, str, str, str, str, str]
    ],
) -> dict[str, Any]:
    if not repository_recoveries:
        raise ManifestError("at least one repository PR recovery is required")
    recoveries: dict[
        str, tuple[int, str, str, str, str, str]
    ] = {}
    for (
        repo,
        expected_generation,
        expected_pr,
        recovered_pr,
        head_branch,
        boundary_sha,
        observed_sha,
    ) in repository_recoveries:
        if not repo or not expected_pr or not recovered_pr:
            raise ManifestError("repository and PR values must not be empty")
        if type(expected_generation) is not int or expected_generation <= 0:
            raise ManifestError("recovery expected generation must be a positive integer")
        if expected_pr == recovered_pr:
            raise ManifestError(f"repository {repo!r} recovered PR matches its current PR")
        if repo in recoveries:
            raise ManifestError(f"duplicate repository PR recovery: {repo}")
        recoveries[repo] = (
            expected_generation,
            expected_pr,
            recovered_pr,
            head_branch,
            boundary_sha,
            observed_sha,
        )

    slot = require_slot(data, name)
    require_complete_context(slot, name)
    repositories = slot["repositories"]
    missing = sorted(set(recoveries) - set(repositories))
    if missing:
        raise ManifestError(f"slot {name!r} has no repository binding for: {', '.join(missing)}")

    pending: list[tuple[str, int, str, dict[str, Any] | None, dict[str, Any]]] = []
    for repo, recovery in recoveries.items():
        (
            expected_generation,
            expected_pr,
            recovered_pr,
            head_branch,
            boundary_sha,
            observed_sha,
        ) = recovery
        identity = repositories[repo]
        submission = {
            "generation": expected_generation + 1,
            "head_branch": head_branch,
            "continuation_boundary_sha": boundary_sha,
            "observed_head_sha": observed_sha,
        }
        expected_receipt = {
            "from_generation": expected_generation,
            "from_pr": expected_pr,
            "to_generation": expected_generation + 1,
            "to_pr": recovered_pr,
        }
        expected_receipt.update(
            head_branch=head_branch,
            continuation_boundary_sha=boundary_sha,
            observed_head_sha=observed_sha,
        )
        if identity["generation"] == expected_generation + 1 and identity["pr"] == recovered_pr:
            if (
                identity.get("last_reconciliation") != expected_receipt
                or identity["submission"] != submission
            ):
                raise ManifestError(f"repository {repo!r} recovery retry does not match its receipt")
            continue
        if identity["generation"] != expected_generation:
            raise ManifestError(
                f"repository {repo!r} generation mismatch: "
                f"expected {expected_generation}, found {identity['generation']}"
            )
        if identity["pr"] != expected_pr:
            raise ManifestError(
                f"repository {repo!r} current PR mismatch: "
                f"expected {expected_pr!r}, found {identity['pr']!r}"
            )
        require_generation_evidence(identity, repo)
        pending.append((repo, expected_generation, recovered_pr, submission, expected_receipt))

    for repo, expected_generation, recovered_pr, submission, receipt in pending:
        repositories[repo]["generation"] = expected_generation + 1
        repositories[repo]["pr"] = recovered_pr
        repositories[repo]["submission"] = submission
        repositories[repo]["last_reconciliation"] = receipt
    return slot


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--root", required=True, type=Path, help="Workspace root")
    subparsers = result.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status", help="Read permanent binding state")
    status.add_argument("--slot")

    initialize_parser = subparsers.add_parser("initialize", help="Initialize one permanent slot binding")
    initialize_parser.add_argument("--slot", required=True)
    initialize_parser.add_argument("--path", required=True)
    initialize_parser.add_argument(
        "--repository",
        action="append",
        required=True,
        nargs=5,
        metavar=("REPO", "TASK_BRANCH", "SOURCE_BRANCH", "REMOTE", "SOURCE_SHA"),
        help="REPO TASK_BRANCH SOURCE_BRANCH REMOTE SOURCE_SHA; repeat for every repository",
    )

    bind_context_parser = subparsers.add_parser(
        "bind-context",
        help="Atomically fill one legacy slot's permanent source context",
    )
    bind_context_parser.add_argument("--slot", required=True)
    bind_context_parser.add_argument(
        "--repository-context",
        action="append",
        required=True,
        nargs=3,
        metavar=("REPO", "SOURCE_BRANCH", "REMOTE"),
        help="Repository source branch and remote; repeat for every repository in the slot",
    )

    lock_parser = subparsers.add_parser("lock", help="Acquire a transient slot operation lock")
    lock_parser.add_argument("--slot", required=True)
    lock_parser.add_argument("--token", required=True)

    lock_status = subparsers.add_parser("lock-status", help="Read a slot operation lock")
    lock_status.add_argument("--slot", required=True)

    unlock_parser = subparsers.add_parser("unlock", help="Release an exact slot operation lock")
    unlock_parser.add_argument("--slot", required=True)
    unlock_parser.add_argument("--token", required=True)

    record = subparsers.add_parser("record-batch", help="Atomically advance repository PR generations")
    record.add_argument("--slot", required=True)
    record.add_argument("--token", required=True)
    record.add_argument(
        "--repository-update",
        action="append",
        required=True,
        nargs=6,
        metavar=("REPO", "GENERATION", "PR", "HEAD_BRANCH", "BOUNDARY_SHA", "OBSERVED_SHA"),
        help="REPO GENERATION PR HEAD_BRANCH BOUNDARY_SHA OBSERVED_SHA; repeat per repository",
    )

    migrate = subparsers.add_parser(
        "migrate-batch",
        help="Atomically record exactly proven generation evidence for legacy repositories",
    )
    migrate.add_argument("--slot", required=True)
    migrate.add_argument("--token", required=True)
    migrate.add_argument(
        "--repository-migration",
        action="append",
        required=True,
        nargs=9,
        metavar=("REPO", "GENERATION", "PR", "FAMILY", "BRANCH_GENERATION", "BASE_SHA", "HEAD", "BOUNDARY", "OBSERVED"),
    )

    migrate_subject = subparsers.add_parser(
        "migrate-rotation-subject",
        help="Atomically backfill exact schema 4 rotation tree and subject evidence",
    )
    migrate_subject.add_argument("--slot", required=True)
    migrate_subject.add_argument("--token", required=True)
    migrate_subject.add_argument(
        "--repository-subject",
        action="append",
        required=True,
        nargs=12,
        metavar=(
            "REPO",
            "FROM_BRANCH",
            "FROM_GENERATION",
            "FROM_HEAD",
            "BOUNDARY",
            "SOURCE",
            "SOURCE_TREE",
            "TARGET",
            "TARGET_GENERATION",
            "RESULT_TREE",
            "SUBJECT_OR_DASH",
            "TEMPORARY_WORKTREE",
        ),
    )

    correction = subparsers.add_parser(
        "record-correction",
        help="Atomically advance observed heads without moving continuation boundaries",
    )
    correction.add_argument("--slot", required=True)
    correction.add_argument("--token", required=True)
    correction.add_argument(
        "--repository-correction",
        action="append",
        required=True,
        nargs=5,
        metavar=("REPO", "GENERATION", "PR", "EXPECTED_HEAD", "NEW_HEAD"),
    )

    begin = subparsers.add_parser("begin-rotation", help="Journal generation rotations before refs change")
    begin.add_argument("--slot", required=True)
    begin.add_argument("--token", required=True)
    begin.add_argument(
        "--repository-rotation",
        action="append",
        required=True,
        nargs=12,
        metavar=(
            "REPO",
            "FROM_BRANCH",
            "FROM_GENERATION",
            "FROM_HEAD",
            "BOUNDARY",
            "SOURCE",
            "SOURCE_TREE",
            "TARGET",
            "TARGET_GENERATION",
            "RESULT_TREE",
            "SUBJECT_OR_DASH",
            "TEMPORARY_WORKTREE",
        ),
    )

    advance = subparsers.add_parser("advance-rotation", help="Advance one exact rotation journal phase")
    advance.add_argument("--slot", required=True)
    advance.add_argument("--token", required=True)
    advance.add_argument("--repository", required=True)
    advance.add_argument("--expected-phase", required=True)
    advance.add_argument("--target-phase", required=True)
    advance.add_argument("--target-head-sha")

    complete = subparsers.add_parser("complete-rotation", help="Commit one retired generation binding")
    complete.add_argument("--slot", required=True)
    complete.add_argument("--token", required=True)
    complete.add_argument("--repository", required=True)
    complete.add_argument("--target-branch", required=True)
    complete.add_argument("--target-generation", required=True, type=int)
    complete.add_argument("--source-sha", required=True)
    complete.add_argument("--target-head-sha", required=True)
    complete.add_argument("--result-tree-sha", required=True)

    reconcile = subparsers.add_parser(
        "reconcile-batch",
        help="Atomically recover exactly proven missing merged PR generations",
    )
    reconcile.add_argument("--slot", required=True)
    reconcile.add_argument("--token", required=True)
    reconcile.add_argument(
        "--repository-recovery",
        action="append",
        required=True,
        nargs=7,
        metavar=("REPO", "GENERATION", "CURRENT_PR", "RECOVERED_PR", "HEAD", "BOUNDARY", "OBSERVED"),
        help="REPO GENERATION CURRENT_PR RECOVERED_PR HEAD BOUNDARY OBSERVED; repeat as needed",
    )
    return result


def main() -> int:
    args = parser().parse_args()
    path = manifest_path(args.root)
    try:
        if args.command == "status":
            data = load_manifest(path)
            payload: Any = data if args.slot is None else require_slot(data, args.slot)
        elif args.command == "lock":
            data = load_manifest(path)
            slot = require_slot(data, args.slot)
            require_complete_context(slot, args.slot)
            payload = acquire_operation_lock(args.root, args.slot, args.token)
        elif args.command == "lock-status":
            data = load_manifest(path)
            require_slot(data, args.slot)
            payload = load_operation_lock(operation_lock_path(args.root, args.slot))
        elif args.command == "unlock":
            data = load_manifest(path)
            require_slot(data, args.slot)
            payload = release_operation_lock(args.root, args.slot, args.token)
        else:
            with manifest_lock(path):
                data = load_manifest(path)
                if args.command == "initialize":
                    bindings = parse_repository_bindings(args.repository)
                    payload = initialize(data, args.slot, args.path, bindings)
                elif args.command == "bind-context":
                    contexts = parse_repository_contexts(args.repository_context)
                    payload = bind_context(data, args.slot, contexts)
                elif args.command == "record-batch":
                    require_operation_lock(args.root, args.slot, args.token)
                    payload = record_batch(
                        data,
                        args.slot,
                        parse_repository_updates(args.repository_update),
                    )
                elif args.command == "migrate-batch":
                    require_operation_lock(args.root, args.slot, args.token)
                    payload = migrate_batch(
                        data,
                        args.slot,
                        parse_repository_migrations(args.repository_migration),
                    )
                elif args.command == "migrate-rotation-subject":
                    require_operation_lock(args.root, args.slot, args.token)
                    payload = migrate_rotation_subjects(
                        data,
                        args.slot,
                        parse_repository_rotation_subjects(args.repository_subject),
                        args.root,
                    )
                elif args.command == "record-correction":
                    require_operation_lock(args.root, args.slot, args.token)
                    payload = record_corrections(
                        data,
                        args.slot,
                        parse_repository_corrections(args.repository_correction),
                    )
                elif args.command == "begin-rotation":
                    require_operation_lock(args.root, args.slot, args.token)
                    payload = begin_rotations(
                        data,
                        args.slot,
                        parse_repository_rotations(args.repository_rotation),
                        args.root,
                    )
                elif args.command == "advance-rotation":
                    require_operation_lock(args.root, args.slot, args.token)
                    payload = advance_rotation(
                        data,
                        args.slot,
                        args.repository,
                        args.expected_phase,
                        args.target_phase,
                        args.target_head_sha,
                    )
                elif args.command == "complete-rotation":
                    require_operation_lock(args.root, args.slot, args.token)
                    payload = complete_rotation(
                        data,
                        args.slot,
                        args.repository,
                        args.target_branch,
                        args.target_generation,
                        args.source_sha,
                        args.target_head_sha,
                        args.result_tree_sha,
                    )
                else:
                    require_operation_lock(args.root, args.slot, args.token)
                    payload = reconcile_batch(
                        data,
                        args.slot,
                        parse_repository_recoveries(args.repository_recovery),
                    )
                write_manifest(path, data)
        print(json.dumps({"ok": True, "result": payload}, ensure_ascii=False, sort_keys=True))
        return 0
    except ManifestError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
