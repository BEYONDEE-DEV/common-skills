#!/usr/bin/env python3
"""Audit inactive generation branches without changing Git or the manifest."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from slot_manifest import ManifestError, is_object_id, load_manifest, manifest_path, require_slot


class AuditError(RuntimeError):
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
        raise AuditError(f"git {' '.join(arguments)} failed: {detail}")
    return result


def object_id(repo: Path, revision: str) -> str | None:
    result = git(repo, "rev-parse", "--verify", "--quiet", revision, check=False)
    if result.returncode == 1:
        return None
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown Git error"
        raise AuditError(f"cannot inspect {revision!r}: {detail}")
    value = result.stdout.strip()
    if not is_object_id(value):
        raise AuditError(f"{revision!r} does not resolve to one exact object ID")
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


def local_family_refs(repo: Path, family: str) -> list[tuple[str, str]]:
    pattern = re.compile(rf"^{re.escape(family)}(?:-mrw-g[0-9]+)?$")
    output = git(
        repo,
        "for-each-ref",
        "--format=%(refname:short)%00%(objectname)",
        "refs/heads/",
    ).stdout
    refs: list[tuple[str, str]] = []
    for row in output.splitlines():
        branch, separator, head = row.partition("\0")
        if separator and pattern.fullmatch(branch) and is_object_id(head):
            refs.append((branch, head))
    return sorted(refs)


def is_ancestor(repo: Path, candidate: str, protected: str) -> bool:
    return git(repo, "merge-base", "--is-ancestor", candidate, protected, check=False).returncode == 0


def effective_active(identity: dict[str, Any]) -> tuple[str, str]:
    rotation = identity.get("rotation")
    if isinstance(rotation, dict) and rotation.get("phase") in {"switched", "retired"}:
        return rotation["target_branch"], "rotation-target"
    return identity["branch"], "manifest-active"


def exact_journal_reason(identity: dict[str, Any], branch: str, head: str) -> str | None:
    rotation = identity.get("rotation")
    if (
        isinstance(rotation, dict)
        and rotation.get("from_branch") == branch
        and rotation.get("from_head_sha") == head
    ):
        return "current-rotation-source"
    receipt = identity.get("last_rotation")
    if (
        isinstance(receipt, dict)
        and receipt.get("from_branch") == branch
        and receipt.get("from_head_sha") == head
    ):
        return "last-rotation-source"
    return None


def parse_merged_evidence(values: list[list[str]]) -> dict[tuple[str, str, str], str]:
    result: dict[tuple[str, str, str], str] = {}
    for repository, pr, branch, head in values:
        if not repository or not pr or not branch or not is_object_id(head):
            raise AuditError("merged PR evidence must contain REPOSITORY PR BRANCH exact HEAD")
        key = (repository, branch, head)
        if key in result and result[key] != pr:
            raise AuditError(f"conflicting merged PR evidence for {repository}/{branch}")
        result[key] = pr
    return result


def audit_repository(
    slot_path: Path,
    repository: str,
    identity: dict[str, Any],
    merged: dict[tuple[str, str, str], str],
) -> dict[str, Any]:
    repo = (slot_path / repository).resolve()
    if not repo.is_relative_to(slot_path):
        raise AuditError("manifest repository path escapes the permanent slot")
    actual_root = Path(git(repo, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if actual_root != repo:
        raise AuditError(f"manifest repository path resolves elsewhere: {actual_root}")

    active_branch, active_basis = effective_active(identity)
    active_head = object_id(repo, f"refs/heads/{active_branch}")
    source_branches = {identity["source_branch"]} if identity.get("source_branch") else set()
    protected_heads: dict[str, str] = {}
    if active_head is not None:
        protected_heads[f"active:{active_branch}"] = active_head
    for source_branch in source_branches:
        for label, ref in (
            (f"source-local:{source_branch}", f"refs/heads/{source_branch}"),
            (
                f"source-remote:{identity['remote']}/{source_branch}",
                f"refs/remotes/{identity['remote']}/{source_branch}",
            ),
        ):
            head = object_id(repo, ref)
            if head is not None:
                protected_heads[label] = head

    checkouts = checked_out_branches(repo)
    rows: list[dict[str, Any]] = []
    for branch, head in local_family_refs(repo, identity["branch_family"]):
        reasons: list[str] = []
        classification = "unresolved"
        if branch == active_branch:
            classification = "protected"
            reasons.append(active_basis)
        if branch in source_branches:
            classification = "protected"
            reasons.append("source-branch")
        if branch in checkouts:
            classification = "protected"
            reasons.append("checked-out")
        if classification != "protected":
            reachable = [label for label, protected in protected_heads.items() if is_ancestor(repo, head, protected)]
            if reachable:
                classification = "reachable-from-protected"
                reasons.extend(reachable)
            else:
                journal_reason = exact_journal_reason(identity, branch, head)
                merged_pr = merged.get((repository, branch, head))
                if journal_reason is not None:
                    classification = "exact-journal-evidence"
                    reasons.append(journal_reason)
                elif merged_pr is not None:
                    classification = "exact-merged-pr-evidence"
                    reasons.append(f"merged-pr:{merged_pr}")
        rows.append(
            {
                "branch": branch,
                "head_sha": head,
                "active": branch == active_branch,
                "classification": classification,
                "reasons": reasons,
                "checked_out_at": checkouts.get(branch, []),
            }
        )

    inactive = [row for row in rows if not row["active"]]
    return {
        "repository": repository,
        "branch_family": identity["branch_family"],
        "effective_active_branch": active_branch,
        "effective_active_basis": active_basis,
        "matching_local_refs": len(rows),
        "inactive_local_refs": len(inactive),
        "unresolved_inactive_refs": sum(row["classification"] == "unresolved" for row in inactive),
        "branches": rows,
    }


def audit(args: argparse.Namespace) -> dict[str, Any]:
    data = load_manifest(manifest_path(args.root))
    slot = require_slot(data, args.slot)
    slot_path = Path(slot["path"]).resolve()
    merged = parse_merged_evidence(args.merged_pr_evidence)
    repositories = slot["repositories"]
    selected = [args.repository] if args.repository else sorted(repositories)
    unknown = [repository for repository in selected if repository not in repositories]
    if unknown:
        raise AuditError(f"slot {args.slot!r} has no repository binding for: {unknown[0]}")
    reports = [
        audit_repository(slot_path, repository, repositories[repository], merged)
        for repository in selected
    ]
    return {
        "slot": args.slot,
        "slot_path": str(slot_path),
        "read_only": True,
        "repositories": reports,
        "matching_local_refs": sum(report["matching_local_refs"] for report in reports),
        "inactive_local_refs": sum(report["inactive_local_refs"] for report in reports),
        "unresolved_inactive_refs": sum(report["unresolved_inactive_refs"] for report in reports),
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--root", required=True, type=Path, help="Workspace root")
    result.add_argument("--slot", required=True)
    result.add_argument("--repository")
    result.add_argument(
        "--merged-pr-evidence",
        action="append",
        nargs=4,
        default=[],
        metavar=("REPOSITORY", "PR", "BRANCH", "HEAD_SHA"),
        help="Externally verified exact MERGED PR identity; repeat as needed",
    )
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        payload = audit(args)
        print(json.dumps({"ok": True, "result": payload}, ensure_ascii=False, sort_keys=True))
        return 0
    except (ManifestError, AuditError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
