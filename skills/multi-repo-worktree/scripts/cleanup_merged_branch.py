#!/usr/bin/env python3
"""Delete one exactly proven merged PR branch from a multi-repo-worktree slot."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from slot_manifest import (
    ManifestError,
    is_object_id,
    load_manifest,
    manifest_lock,
    manifest_path,
    require_operation_lock,
    require_slot,
    write_manifest,
)


class CleanupError(RuntimeError):
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
        raise CleanupError(f"git {' '.join(arguments)} failed: {detail}")
    return result


def ref_target(repo: Path, ref: str) -> str | None:
    result = git(repo, "rev-parse", "--verify", "--quiet", ref, check=False)
    if result.returncode == 1:
        return None
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown Git error"
        raise CleanupError(f"cannot inspect ref {ref!r}: {detail}")
    value = result.stdout.strip()
    if not is_object_id(value):
        raise CleanupError(f"ref {ref!r} does not resolve to one exact object ID")
    return value


def remote_branch_target(repo: Path, remote: str, branch: str) -> str | None:
    ref = f"refs/heads/{branch}"
    result = git(repo, "ls-remote", "--heads", "--exit-code", remote, ref, check=False)
    if result.returncode == 2 and not result.stdout.strip():
        return None
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown Git error"
        raise CleanupError(f"cannot inspect remote branch {remote}/{branch}: {detail}")
    rows = [line.split() for line in result.stdout.splitlines() if line.strip()]
    if len(rows) != 1 or len(rows[0]) != 2 or rows[0][1] != ref or not is_object_id(rows[0][0]):
        raise CleanupError(f"remote branch {remote}/{branch} did not resolve exactly")
    return rows[0][0]


def checked_out_branches(repo: Path) -> dict[str, list[str]]:
    fields = git(repo, "worktree", "list", "--porcelain", "-z").stdout.split("\0")
    result: dict[str, list[str]] = {}
    worktree: str | None = None
    for field in fields:
        if field.startswith("worktree "):
            worktree = field.removeprefix("worktree ")
        elif field.startswith("branch refs/heads/") and worktree is not None:
            branch = field.removeprefix("branch refs/heads/")
            result.setdefault(branch, []).append(worktree)
    return result


def require_direct_ref(repo: Path, ref: str) -> None:
    symbolic = git(repo, "symbolic-ref", "-q", ref, check=False)
    if symbolic.returncode == 0:
        raise CleanupError(f"symbolic branch ref is foreign and was preserved: {ref}")
    if symbolic.returncode != 1:
        detail = symbolic.stderr.strip() or symbolic.stdout.strip() or "unknown Git error"
        raise CleanupError(f"cannot verify direct ref {ref!r}: {detail}")


def require_clean_development_state(repo: Path, active_branch: str, active_head: str) -> None:
    actual_root = Path(git(repo, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if actual_root != repo:
        raise CleanupError(f"manifest repository path resolves to a different worktree: {actual_root}")
    actual_branch = git(repo, "branch", "--show-current").stdout.strip()
    if actual_branch != active_branch:
        raise CleanupError(
            f"development branch changed: expected {active_branch!r}, found {actual_branch!r}"
        )
    actual_head = ref_target(repo, "HEAD")
    if actual_head != active_head:
        raise CleanupError(
            f"development HEAD changed: expected {active_head}, found {actual_head or 'absent'}"
        )
    if git(repo, "status", "--porcelain", "--untracked-files=all").stdout:
        raise CleanupError("development worktree is dirty; no branch was deleted")
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
            raise CleanupError(f"development worktree has an in-progress Git operation: {name}")


def require_github_merge_evidence(
    args: argparse.Namespace,
    source_branch: str,
    pr: str,
    submission: dict[str, Any],
) -> None:
    if not isinstance(submission, dict):
        raise CleanupError("repository has no exact submission evidence to clean")
    recorded_pr = str(pr)
    if recorded_pr not in {str(args.github_number), args.github_url}:
        raise CleanupError(
            f"GitHub PR identity does not match recorded PR {recorded_pr!r}"
        )
    if args.github_state != "MERGED" or args.github_is_draft != "false":
        raise CleanupError("recorded PR is not an exact non-draft MERGED PR")
    if not args.github_merged_at:
        raise CleanupError("merged PR evidence has no mergedAt value")
    if not is_object_id(args.github_merge_commit):
        raise CleanupError("merged PR evidence has an invalid merge commit")
    if (
        args.github_base != source_branch
        or args.github_head != submission["head_branch"]
        or args.github_head_sha != submission["observed_head_sha"]
    ):
        raise CleanupError("GitHub PR base/head/head SHA does not match manifest submission evidence")


def require_merge_commit_on_source(
    repo: Path,
    remote: str,
    source_branch: str,
    merge_commit: str,
) -> str:
    source_ref = f"refs/remotes/{remote}/{source_branch}"
    source_head = ref_target(repo, source_ref)
    if source_head is None:
        raise CleanupError(f"fetched source ref is absent: {source_ref}")
    commit = git(repo, "cat-file", "-e", f"{merge_commit}^{{commit}}", check=False)
    if commit.returncode != 0:
        raise CleanupError("GitHub merge commit is not available in the fetched repository")
    contained = git(
        repo,
        "merge-base",
        "--is-ancestor",
        merge_commit,
        source_head,
        check=False,
    )
    if contained.returncode != 0:
        raise CleanupError("GitHub merge commit is not contained by the fetched source ref")
    return source_head


def require_local_transfer_proof(
    repo: Path,
    identity: dict[str, Any],
    receipt: dict[str, Any] | None,
    submission: dict[str, Any],
    old_branch: str,
    old_head: str,
) -> None:
    if not isinstance(receipt, dict):
        raise CleanupError("local merged branch has no completed rotation receipt")
    from_generation = receipt.get("from_branch_generation")
    target_generation = receipt.get("target_branch_generation")
    if (
        receipt.get("from_branch") != old_branch
        or receipt.get("from_head_sha") != old_head
        or type(from_generation) is not int
        or type(target_generation) is not int
        or from_generation + 1 != target_generation
        or receipt.get("target_branch") != identity["branch"]
        or target_generation != identity["branch_generation"]
        or receipt.get("source_sha") != identity["branch_base_sha"]
    ):
        raise CleanupError(
            "old local branch moved or completed rotation receipt does not match the active generation"
        )
    active_head = ref_target(repo, f"refs/heads/{identity['branch']}")
    if active_head != receipt.get("target_head_sha"):
        raise CleanupError("active generation ref moved after completed rotation")
    active_tree = ref_target(repo, f"{active_head}^{{tree}}") if active_head is not None else None
    if active_tree != receipt.get("result_tree_sha"):
        raise CleanupError("active generation tree does not match completed rotation")
    boundary = submission["continuation_boundary_sha"]
    source = receipt["source_sha"]
    ancestor = git(repo, "merge-base", "--is-ancestor", boundary, old_head, check=False)
    if ancestor.returncode != 0:
        raise CleanupError("submitted continuation boundary is not an ancestor of the old local branch")
    merge = git(
        repo,
        "merge-tree",
        "--write-tree",
        "--messages",
        "--merge-base",
        boundary,
        source,
        old_head,
        check=False,
    )
    if merge.returncode != 0:
        detail = merge.stdout.strip() or merge.stderr.strip() or "3-way tree conflict"
        raise CleanupError(f"old local branch no longer reproduces the completed rotation: {detail}")
    lines = merge.stdout.splitlines()
    if not lines or lines[0].strip() != receipt["result_tree_sha"]:
        raise CleanupError("old local branch contains work not proven in the active generation")
    locations = checked_out_branches(repo).get(old_branch, [])
    if locations:
        raise CleanupError(
            f"old local branch is still checked out and will not be deleted: {', '.join(locations)}"
        )


def repository_context(
    args: argparse.Namespace,
    data: dict[str, Any],
) -> tuple[Path, dict[str, Any]]:
    slot = require_slot(data, args.slot)
    require_operation_lock(args.root, args.slot, args.token)
    repositories = slot["repositories"]
    if args.repository not in repositories:
        raise CleanupError(
            f"slot {args.slot!r} has no repository binding for: {args.repository}"
        )
    identity = repositories[args.repository]
    if "legacy_schema" in identity or "rotation" in identity:
        raise CleanupError("repository requires a completed schema-5 rotation before cleanup")
    slot_path = Path(slot["path"]).resolve()
    repo = (slot_path / args.repository).resolve()
    if not repo.is_relative_to(slot_path):
        raise CleanupError("manifest repository path escapes the permanent slot")
    return repo, identity


def select_cleanup(
    args: argparse.Namespace,
    identity: dict[str, Any],
) -> tuple[str, dict[str, Any], dict[str, Any] | None, int | None]:
    candidates: list[tuple[str, dict[str, Any], dict[str, Any] | None, int | None]] = []
    if identity.get("pr") is not None and isinstance(identity.get("submission"), dict):
        candidates.append((identity["pr"], identity["submission"], identity.get("last_rotation"), None))
    for index, cleanup in enumerate(identity.get("pending_remote_cleanups", [])):
        candidates.append((cleanup["pr"], cleanup["submission"], cleanup["last_rotation"], index))
    matches = [
        candidate
        for candidate in candidates
        if str(candidate[0]) in {str(args.github_number), args.github_url}
    ]
    if len(matches) != 1:
        raise CleanupError("GitHub PR identity does not match one cleanup record")
    return matches[0]


def cleanup_locked(args: argparse.Namespace, path: Path, data: dict[str, Any],
                   before_delete: Callable[[], Any] | None = None) -> dict[str, Any]:
    if not args.execute:
        raise CleanupError("merged branch cleanup is destructive and requires --execute")
    repo, identity = repository_context(args, data)
    cleanup_pr, submission, receipt, pending_index = select_cleanup(args, identity)
    require_github_merge_evidence(args, identity["source_branch"], cleanup_pr, submission)
    old_branch = submission["head_branch"]
    active_branch = identity["branch"]
    source_branch = identity["source_branch"]
    remote = identity["remote"]
    if old_branch in {active_branch, source_branch}:
        raise CleanupError("merged PR branch is still active or is the source branch")
    if remote.startswith("-"):
        raise CleanupError("manifest remote name is unsafe")
    if git(repo, "check-ref-format", "--branch", old_branch, check=False).returncode != 0:
        raise CleanupError("manifest submission branch name is invalid")
    active_head = ref_target(repo, f"refs/heads/{active_branch}")
    if active_head is None:
        raise CleanupError("active generation branch is absent")
    require_clean_development_state(repo, active_branch, active_head)
    source_head = require_merge_commit_on_source(
        repo,
        remote,
        source_branch,
        args.github_merge_commit,
    )

    local_ref = f"refs/heads/{old_branch}"
    require_direct_ref(repo, f"refs/heads/{active_branch}")
    require_direct_ref(repo, local_ref)
    local_head = ref_target(repo, local_ref)
    expected_remote_head = submission["observed_head_sha"]
    remote_head = remote_branch_target(repo, remote, old_branch)
    if remote_head is not None and remote_head != expected_remote_head:
        raise CleanupError(
            f"remote branch advanced: expected {expected_remote_head}, found {remote_head}"
        )
    if local_head is not None:
        require_local_transfer_proof(repo, identity, receipt, submission, old_branch, local_head)
    locations = checked_out_branches(repo).get(old_branch, [])
    if locations:
        raise CleanupError(f"merged branch remains checked out; refs preserved: {', '.join(locations)}")

    remote_deleted = remote_head is None
    local_deleted = local_head is None
    if remote_head is not None:
        if before_delete is not None:
            before_delete()
        require_clean_development_state(repo, active_branch, active_head)
        if checked_out_branches(repo).get(old_branch):
            raise CleanupError("merged branch became checked out; refs preserved")
        lease = f"--force-with-lease=refs/heads/{old_branch}:{expected_remote_head}"
        deleted = git(repo, "push", lease, remote, f":refs/heads/{old_branch}", check=False)
        remaining = remote_branch_target(repo, remote, old_branch)
        if remaining is not None:
            detail = deleted.stderr.strip() or deleted.stdout.strip() or "remote ref still exists"
            raise CleanupError(f"exact remote branch deletion failed; local branch preserved: {detail}")
        remote_deleted = True

    if local_head is not None:
        try:
            if before_delete is not None:
                before_delete()
            require_clean_development_state(repo, active_branch, active_head)
        except RuntimeError as exc:
            raise CleanupError(f"remote branch is absent but local branch was preserved: {exc}") from exc
        locations = checked_out_branches(repo).get(old_branch, [])
        if locations:
            raise CleanupError(
                "remote branch was deleted but the local branch became checked out; "
                f"local branch preserved at {local_head}: {', '.join(locations)}"
            )
        deleted = git(repo, "update-ref", "-d", local_ref, local_head, check=False)
        remaining = ref_target(repo, local_ref)
        if remaining is not None:
            detail = deleted.stderr.strip() or deleted.stdout.strip() or "local ref still exists"
            raise CleanupError(
                "remote branch was deleted but exact local branch deletion failed; "
                f"local branch preserved at {remaining}: {detail}"
            )
        local_deleted = True

    if pending_index is not None:
        cleanups = identity["pending_remote_cleanups"]
        if cleanups[pending_index]["pr"] != cleanup_pr:
            raise CleanupError("pending remote cleanup changed before completion")
        cleanups.pop(pending_index)
        if not cleanups:
            identity.pop("pending_remote_cleanups")
        write_manifest(path, data)

    return {
        "repository": args.repository,
        "pr": cleanup_pr,
        "merged_branch": old_branch,
        "active_branch": active_branch,
        "source_head_sha": source_head,
        "remote_deleted": remote_deleted,
        "local_deleted": local_deleted,
        "status": "already-absent" if remote_head is None and local_head is None else "deleted",
    }


def cleanup(args: argparse.Namespace, *, before_delete: Callable[[], Any] | None = None) -> dict[str, Any]:
    path = manifest_path(args.root)
    with manifest_lock(path):
        data = load_manifest(path)
        return cleanup_locked(args, path, data, before_delete)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--root", required=True, type=Path, help="Workspace root")
    result.add_argument("--slot", required=True)
    result.add_argument("--repository", required=True)
    result.add_argument("--token", required=True, help="Exact held slot operation-lock token")
    result.add_argument("--github-number", required=True, type=int)
    result.add_argument("--github-url", required=True)
    result.add_argument("--github-state", required=True)
    result.add_argument("--github-is-draft", required=True, choices=("true", "false"))
    result.add_argument("--github-base", required=True)
    result.add_argument("--github-head", required=True)
    result.add_argument("--github-head-sha", required=True)
    result.add_argument("--github-merged-at", required=True)
    result.add_argument("--github-merge-commit", required=True)
    result.add_argument("--execute", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        payload = cleanup(args)
        print(json.dumps({"ok": True, "result": payload}, ensure_ascii=False, sort_keys=True))
        return 0
    except (CleanupError, ManifestError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
