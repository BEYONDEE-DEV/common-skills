#!/usr/bin/env python3
"""Retire one journal-proven local generation branch with compare-and-delete."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from slot_manifest import (
    ManifestError,
    is_object_id,
    load_manifest,
    manifest_lock,
    manifest_path,
    normalize_transfer_subject,
    require_operation_lock,
    require_slot,
    write_manifest,
)


class RetirementError(RuntimeError):
    pass


def git(repo: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown Git error"
        raise RetirementError(f"git {' '.join(arguments)} failed: {detail}")
    return result


def object_id(repo: Path, revision: str) -> str | None:
    result = git(repo, "rev-parse", "--verify", "--quiet", revision, check=False)
    if result.returncode == 1:
        return None
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown Git error"
        raise RetirementError(f"cannot inspect {revision!r}: {detail}")
    value = result.stdout.strip()
    if not is_object_id(value):
        raise RetirementError(f"{revision!r} does not resolve to one exact object ID")
    return value


def checked_out_branches(repo: Path) -> dict[str, list[str]]:
    fields = git(repo, "worktree", "list", "--porcelain", "-z").stdout.split("\0")
    result: dict[str, list[str]] = {}
    worktree: str | None = None
    for field in fields:
        if field.startswith("worktree "):
            worktree = field.removeprefix("worktree ")
        elif field.startswith("branch refs/heads/") and worktree is not None:
            result.setdefault(field.removeprefix("branch refs/heads/"), []).append(worktree)
    return result


def require_direct_ref(repo: Path, ref: str) -> None:
    symbolic = git(repo, "symbolic-ref", "-q", ref, check=False)
    if symbolic.returncode == 0:
        raise RetirementError(f"symbolic branch ref is foreign and was preserved: {ref}")
    if symbolic.returncode != 1:
        detail = symbolic.stderr.strip() or symbolic.stdout.strip() or "unknown Git error"
        raise RetirementError(f"cannot verify direct ref {ref!r}: {detail}")


def require_clean_target(repo: Path, target_branch: str, target_head: str) -> None:
    actual_root = Path(git(repo, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if actual_root != repo:
        raise RetirementError(f"manifest repository path resolves elsewhere: {actual_root}")
    actual_branch = git(repo, "branch", "--show-current").stdout.strip()
    if actual_branch != target_branch:
        raise RetirementError(
            f"development branch changed: expected {target_branch!r}, found {actual_branch!r}"
        )
    if object_id(repo, "HEAD") != target_head:
        raise RetirementError("development HEAD does not match the exact target ref")
    if git(repo, "status", "--porcelain", "--untracked-files=all").stdout:
        raise RetirementError("development worktree is dirty; old branch was preserved")
    for name in (
        "MERGE_HEAD",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
        "rebase-merge",
        "rebase-apply",
        "BISECT_LOG",
    ):
        git_path = Path(git(repo, "rev-parse", "--git-path", name).stdout.strip())
        if not git_path.is_absolute():
            git_path = repo / git_path
        if git_path.exists():
            raise RetirementError(f"development worktree has an in-progress Git operation: {name}")


def require_target_shape(repo: Path, rotation: dict[str, Any], target_head: str) -> None:
    source = rotation["source_sha"]
    source_tree = object_id(repo, f"{source}^{{tree}}")
    target_tree = object_id(repo, f"{target_head}^{{tree}}")
    if source_tree != rotation["source_tree_sha"] or target_tree != rotation["result_tree_sha"]:
        raise RetirementError("target/source tree no longer matches the rotation journal")
    if rotation["result_tree_sha"] == rotation["source_tree_sha"]:
        if target_head != source or rotation["transfer_subject"] is not None:
            raise RetirementError("empty rotation target no longer matches its exact source")
        return
    parents = git(repo, "show", "-s", "--format=%P", target_head).stdout.strip().split()
    subject = git(repo, "show", "-s", "--format=%s", target_head).stdout.rstrip("\n")
    if parents != [source] or subject != normalize_transfer_subject(rotation["transfer_subject"]):
        raise RetirementError("target commit parent or subject no longer matches the journal")


def repository_context(
    args: argparse.Namespace,
    data: dict[str, Any],
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    require_operation_lock(args.root, args.slot, args.token)
    slot = require_slot(data, args.slot)
    if args.repository not in slot["repositories"]:
        raise RetirementError(
            f"slot {args.slot!r} has no repository binding for: {args.repository}"
        )
    identity = slot["repositories"][args.repository]
    if "legacy_schema" in identity:
        raise RetirementError("legacy repository requires evidence migration before retirement")
    rotation = identity.get("rotation")
    if not isinstance(rotation, dict) or rotation.get("phase") not in {"switched", "retired"}:
        raise RetirementError("repository has no switched retirement journal")
    slot_path = Path(slot["path"]).resolve()
    repo = (slot_path / args.repository).resolve()
    if not repo.is_relative_to(slot_path):
        raise RetirementError("manifest repository path escapes the permanent slot")
    return repo, identity, rotation


def retire_locked(
    args: argparse.Namespace,
    path: Path,
    data: dict[str, Any],
) -> dict[str, Any]:
    repo, identity, rotation = repository_context(args, data)
    old_branch = rotation["from_branch"]
    target_branch = rotation["target_branch"]
    expected_old_head = rotation["from_head_sha"]
    expected_target = f"{identity['branch_family']}-mrw-g{rotation['target_branch_generation']}"
    if target_branch != expected_target:
        raise RetirementError("rotation target is not deterministic for the manifest family")
    if identity["branch"] != old_branch or identity["branch_generation"] != rotation["from_branch_generation"]:
        raise RetirementError("top-level manifest binding no longer matches the rotation source")
    if old_branch in {target_branch, identity["source_branch"]}:
        raise RetirementError("rotation source is still an active target or protected source branch")
    for branch in (old_branch, target_branch):
        if git(repo, "check-ref-format", "--branch", branch, check=False).returncode != 0:
            raise RetirementError(f"journal contains an invalid branch name: {branch!r}")

    target_head = object_id(repo, f"refs/heads/{target_branch}")
    if target_head is None:
        raise RetirementError("exact target branch is absent")
    require_clean_target(repo, target_branch, target_head)
    require_direct_ref(repo, f"refs/heads/{target_branch}")
    require_target_shape(repo, rotation, target_head)
    journaled_target = rotation.get("target_head_sha")
    if journaled_target is None:
        rotation["target_head_sha"] = target_head
        write_manifest(path, data)
    elif journaled_target != target_head:
        raise RetirementError(
            f"target branch moved: expected {journaled_target}, found {target_head}; old branch preserved"
        )

    checkouts = checked_out_branches(repo)
    old_locations = checkouts.get(old_branch, [])
    if old_locations:
        raise RetirementError(
            f"old branch is still checked out and was preserved: {', '.join(old_locations)}"
        )
    target_locations = checkouts.get(target_branch, [])
    if str(repo) not in target_locations:
        raise RetirementError("target branch is not checked out in its manifest development worktree")

    old_ref = f"refs/heads/{old_branch}"
    require_direct_ref(repo, old_ref)
    old_head = object_id(repo, old_ref)
    if rotation["phase"] == "retired":
        if old_head is not None:
            raise RetirementError("retired journal conflicts with a present old branch")
        status = "already-retired"
    elif old_head is None:
        status = "already-absent"
    elif old_head != expected_old_head:
        raise RetirementError(
            f"old branch moved: expected {expected_old_head}, found {old_head}; preserved"
        )
    else:
        transaction = "".join(
            (
                "start\n",
                f"verify refs/heads/{target_branch} {target_head}\n",
                f"delete {old_ref} {expected_old_head}\n",
                "prepare\n",
                "commit\n",
            )
        )
        deleted = subprocess.run(
            ["git", "-C", str(repo), "update-ref", "--stdin"],
            input=transaction,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        remaining = object_id(repo, old_ref)
        if deleted.returncode != 0 or remaining is not None:
            detail = deleted.stderr.strip() or deleted.stdout.strip() or "local ref still exists"
            raise RetirementError(
                f"atomic target verification and old branch deletion failed; "
                f"old ref is {remaining or 'absent'}: {detail}"
            )
        status = "deleted"

    if rotation["phase"] == "switched":
        rotation["phase"] = "retired"
        write_manifest(path, data)

    return {
        "repository": args.repository,
        "from_branch": old_branch,
        "from_head_sha": expected_old_head,
        "target_branch": target_branch,
        "target_head_sha": target_head,
        "rotation_phase": rotation["phase"],
        "status": status,
    }


def retire(args: argparse.Namespace) -> dict[str, Any]:
    if not args.execute:
        raise RetirementError("generation retirement changes a ref and requires --execute")
    path = manifest_path(args.root)
    with manifest_lock(path):
        data = load_manifest(path)
        return retire_locked(args, path, data)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--root", required=True, type=Path, help="Workspace root")
    result.add_argument("--slot", required=True)
    result.add_argument("--repository", required=True)
    result.add_argument("--token", required=True, help="Exact held operation-lock token")
    result.add_argument("--execute", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        payload = retire(args)
        print(json.dumps({"ok": True, "result": payload}, ensure_ascii=False, sort_keys=True))
        return 0
    except (ManifestError, RetirementError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
