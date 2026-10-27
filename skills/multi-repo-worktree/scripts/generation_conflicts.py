#!/usr/bin/env python3
"""Isolated, agent-edited recovery of an exact generation three-way conflict.

The runner prepares the worktree. The agent reconciles both intents and stages
the recorded paths, then this CLI creates a normal hooked commit for the runner.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import uuid
from pathlib import Path

import inspect_worktrees as inspector
import prepare_generation_branch as generation
import slot_manifest as manifest


class ConflictResolutionRequired(RuntimeError):
    def __init__(self, recovery: dict):
        self.recovery = recovery
        super().__init__("CONFLICT_RESOLUTION_REQUIRED: reconcile and stage the isolated conflict paths, then record and resume")


def locations(root: Path, slot: str, name: str, target: str, head: str, source: str) -> tuple[Path, Path]:
    key = hashlib.sha256("\0".join((slot, name, target, head, source)).encode()).hexdigest()
    directory = manifest.manifest_path(root.resolve()).parent
    return directory / "conflict-resolutions" / (key + ".json"), directory / "conflict-worktrees" / (key + ".worktree")


def binding(root: Path, slot: str, name: str) -> tuple[Path, dict]:
    state = manifest.require_slot(manifest.load_manifest(manifest.manifest_path(root)), slot)
    base = Path(state["path"]).resolve()
    repo = base / name
    if name not in state["repositories"] or not repo.is_relative_to(base) or repo == base or repo.resolve() != repo or \
            Path(generation.git(repo, "rev-parse", "--show-toplevel").stdout.strip()).resolve() != repo:
        raise generation.TransitionError("conflict recovery repository is not the manifest-bound worktree")
    return repo, state["repositories"][name]


def owned(root: Path, slot: str, name: str, record: dict) -> tuple[Path, Path, Path]:
    repo, identity = binding(root, slot, name)
    path, temporary = locations(root, slot, name, record["target_branch"], record["from_head_sha"], record["source_sha"])
    if record["repo"] != str(repo) or record["worktree"] != str(temporary) or \
            record["receipt_path"] != str(path) or temporary.resolve() != temporary:
        raise generation.TransitionError("conflict worktree ownership does not match its exact inputs")
    import submit_bundle as shared
    if shared.remote_identity(repo, identity["remote"])[1] != record["remote_key"]:
        raise generation.TransitionError("conflict recovery remote mapping changed")
    if inspector.git_common_dir(repo) is None:
        raise generation.TransitionError("conflict recovery Git repository is missing")
    return repo, path, temporary


def check_input(root: Path, slot: str, name: str, record: dict) -> tuple[Path, Path, Path]:
    import submit_bundle as shared
    repo, path, temporary = owned(root, slot, name, record)
    identity = binding(root, slot, name)[1]
    if shared.digest(identity) != record["identity_fingerprint"] or \
            shared.g(repo, "branch", "--show-current") != record["from_branch"] or \
            shared.g(repo, "rev-parse", "HEAD") != record["from_head_sha"]:
        raise generation.TransitionError("development input changed during conflict recovery; preserve the isolated work")
    shared.clean(repo)
    refs = shared.remote_heads(repo, identity["remote"], identity["source_branch"])
    if refs.get(identity["source_branch"]) != record["source_sha"]:
        raise generation.TransitionError("source advanced during conflict recovery; preserve the isolated work and prepare again")
    return repo, path, temporary


def check_temporary(repo: Path, temporary: Path, head: str) -> None:
    if temporary not in generation.registered_worktrees(repo) or not temporary.is_dir() or \
            inspector.git_common_dir(temporary) != inspector.git_common_dir(repo) or \
            generation.git(temporary, "symbolic-ref", "-q", "HEAD", check=False).returncode == 0 or \
            generation.object_id(temporary, "HEAD") != head:
        raise generation.TransitionError("conflict worktree is missing, foreign, attached, or moved")


def prepare(root: Path, slot: str, name: str, token: str, context: dict) -> dict:
    import submit_bundle as shared
    manifest.require_operation_lock(root, slot, token)
    repo, identity = binding(root, slot, name)
    path, temporary = locations(root, slot, name, context["target_branch"], context["from_head_sha"], context["source_sha"])
    expected = {**context, "repo": str(repo), "receipt_path": str(path), "worktree": str(temporary),
                "from_branch": identity["branch"], "identity_fingerprint": shared.digest(identity),
                "remote_key": shared.remote_identity(repo, identity["remote"])[1]}
    if path.exists():
        record = json.loads(path.read_text())
        if any(record.get(key) != value for key, value in expected.items()):
            raise generation.TransitionError("existing conflict recovery has different inputs; preserve it")
    else:
        if temporary.exists() or temporary in generation.registered_worktrees(repo):
            raise generation.TransitionError("unowned conflict worktree already exists; preserve it")
        record = {**expected, "state": "creating"}
        manifest.write_json_atomic(path, record, "conflict.")
    check_input(root, slot, name, record)
    if record["state"] == "creating":
        if temporary not in generation.registered_worktrees(repo):
            if temporary.exists():
                raise generation.TransitionError("unregistered conflict worktree path exists")
            temporary.parent.mkdir(parents=True, exist_ok=True)
            generation.git(repo, "worktree", "add", "--detach", str(temporary), record["source_sha"])
        check_temporary(repo, temporary, record["source_sha"])
        if generation.has_untracked(temporary):
            raise generation.TransitionError("conflict preparation has unexpected untracked content")
        # Only exact source or exact initial marker/stage state can be restored
        # after an interrupted preparation. Agent edits are never overwritten.
        unmerged = generation.git(temporary, "ls-files", "-u", "-z").stdout
        if generation.git(temporary, "diff", "--quiet", check=False).returncode != 0 and not unmerged:
            raise generation.TransitionError("interrupted conflict preparation contains unstaged edits")
        if unmerged:
            expected_entries = set(record["conflict_entries"])
            if set(unmerged.rstrip("\0").split("\0")) != expected_entries:
                raise generation.TransitionError("interrupted conflict index changed")
            # An initialized index is preserved, including possible edits made
            # before the last receipt write.
        else:
            tree = generation.git(temporary, "write-tree").stdout.strip()
            if tree not in {record["source_tree_sha"], record["conflict_tree_sha"]}:
                raise generation.TransitionError("interrupted conflict preparation index changed")
            generation.git(temporary, "restore", "--source", record["conflict_tree_sha"], "--staged", "--worktree", "--", ":/")
            removal = "".join(f"0 {'0' * len(record['source_sha'])}\t{p}\0" for p in record["conflict_paths"])
            entries = removal + "\0".join(record["conflict_entries"]) + "\0"
            result = subprocess.run(["git", "-C", str(temporary), "update-index", "-z", "--index-info"],
                                    input=entries.encode("utf-8", errors="surrogateescape"), capture_output=True)
            if result.returncode:
                raise generation.TransitionError(result.stderr.decode(errors="replace"))
        record["state"] = "editing"
        manifest.write_json_atomic(path, record, "conflict.")
    return record


def record_resolution(root: Path, slot: str, name: str, receipt: Path, subject: str, summary: str) -> dict:
    if not summary or not summary.strip():
        raise generation.TransitionError("describe how both sides' intent was preserved")
    token = uuid.uuid4().hex
    manifest.acquire_operation_lock(root, slot, token)
    try:
        record = json.loads(receipt.read_text())
        repo, path, temporary = check_input(root, slot, name, record)
        if receipt.resolve() != path:
            raise generation.TransitionError("conflict receipt path is not its deterministic owner")
        if record["state"] == "ready":
            expected_subject = generation.normalize_transfer_subject(subject) if record["transfer_subject"] is not None else None
            if record["summary"] != summary or record["transfer_subject"] != expected_subject:
                raise generation.TransitionError("recorded resolution differs; do not rewrite it")
            check_temporary(repo, temporary, record["commit_sha"])
            return record
        if record["state"] == "editing":
            check_temporary(repo, temporary, record["source_sha"])
            if generation.git(temporary, "ls-files", "-u").stdout:
                raise generation.TransitionError("unresolved conflict stages remain")
            if generation.git(temporary, "diff", "--quiet", check=False).returncode != 0 or generation.has_untracked(temporary):
                raise generation.TransitionError("stage the exact resolution; preserve unstaged or untracked content")
            generation.git(temporary, "diff", "--cached", "--check")
            tree = generation.git(temporary, "write-tree").stdout.strip()
            changed = generation.git(repo, "diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z",
                                     record["conflict_tree_sha"], tree, "--").stdout
            if set(changed.split("\0")) - {""} - set(record["conflict_paths"]):
                raise generation.TransitionError("resolution changed non-conflicting paths; preserve their automatic merge")
            transfer = generation.normalize_transfer_subject(subject) if tree != record["source_tree_sha"] else None
            record.update(state="committing", result_tree_sha=tree, transfer_subject=transfer, summary=summary)
            manifest.write_json_atomic(path, record, "conflict.")
        if record["state"] != "committing":
            raise generation.TransitionError("conflict preparation must complete before recording")
        if record["summary"] != summary or record["transfer_subject"] != (
                generation.normalize_transfer_subject(subject) if record["result_tree_sha"] != record["source_tree_sha"] else None):
            raise generation.TransitionError("resolution commit inputs changed")
        head = generation.object_id(temporary, "HEAD")
        check_temporary(repo, temporary, head)
        if generation.git(temporary, "diff", "--quiet", check=False).returncode != 0 or generation.has_untracked(temporary):
            raise generation.TransitionError("resolution worktree changed before commit")
        if head == record["source_sha"]:
            if generation.git(temporary, "write-tree").stdout.strip() != record["result_tree_sha"]:
                raise generation.TransitionError("resolution index changed before commit")
            if record["transfer_subject"] is not None:
                generation.git(temporary, "commit", "-m", record["transfer_subject"])
            head = generation.object_id(temporary, "HEAD")
        if not generation.compatible_target(repo, head, record["source_sha"], record["result_tree_sha"], record["transfer_subject"]):
            raise generation.TransitionError("resolution hook changed the tree, parent or subject; preserve it")
        if generation.git(temporary, "status", "--porcelain").stdout or generation.has_untracked(temporary):
            raise generation.TransitionError("resolution hook left additional content; preserve it")
        record.update(state="ready", commit_sha=head)
        manifest.write_json_atomic(path, record, "conflict.")
        return record
    finally:
        manifest.release_operation_lock(root, slot, token)


def for_rotation(root: Path, slot: str, name: str, token: str, rotation: dict) -> dict | None:
    manifest.require_operation_lock(root, slot, token)
    path, temporary = locations(root, slot, name, rotation["target_branch"], rotation["from_head_sha"], rotation["source_sha"])
    if not path.exists():
        return None
    record = json.loads(path.read_text())
    repo, _, _ = owned(root, slot, name, record)
    keys = ("target_branch", "from_head_sha", "source_sha", "boundary_sha", "result_tree_sha", "transfer_subject")
    if record["state"] != "ready" or any(record.get(key) != rotation.get(key) for key in keys) or \
            not generation.compatible_target(repo, record["commit_sha"], record["source_sha"],
                                             record["result_tree_sha"], record["transfer_subject"]):
        raise generation.TransitionError("resolved conflict does not match the exact rotation")
    if temporary.exists() or temporary in generation.registered_worktrees(repo):
        check_temporary(repo, temporary, record["commit_sha"])
        if generation.git(temporary, "status", "--porcelain").stdout or generation.has_untracked(temporary):
            raise generation.TransitionError("recorded resolution worktree changed; preserve it")
    elif generation.ref_target(repo, rotation["target_branch"]) != record["commit_sha"]:
        raise generation.TransitionError("resolution anchor is missing before target publication")
    return record


def cleanup(root: Path, slot: str, name: str, token: str, rotation: dict) -> None:
    record = for_rotation(root, slot, name, token, rotation)
    if record is None:
        return
    repo, _, temporary = owned(root, slot, name, record)
    if generation.ref_target(repo, rotation["target_branch"]) != record["commit_sha"]:
        raise generation.TransitionError("resolution target moved before cleanup; preserve its anchor")
    if temporary in generation.registered_worktrees(repo):
        generation.git(repo, "worktree", "remove", str(temporary))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--slot", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--summary", required=True)
    args = parser.parse_args()
    try:
        result = record_resolution(args.root.resolve(), args.slot, args.repository, args.receipt, args.subject, args.summary)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (RuntimeError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
