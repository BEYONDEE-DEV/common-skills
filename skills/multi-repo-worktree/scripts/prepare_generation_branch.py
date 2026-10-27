#!/usr/bin/env python3
"""Prepare and verify one generation branch without rewriting refs."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


class TransitionError(RuntimeError):
    pass


class GenerationConflict(TransitionError):
    def __init__(self, context: dict[str, Any]):
        self.context = context
        super().__init__("generation tree conflicts; no branch was changed: " + ", ".join(context["conflict_paths"]))


GENERATION_WORKTREES_RELATIVE_PATH = Path(".multi-repo-worktree") / "generation-worktrees"
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


def git(repo: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown Git error"
        raise TransitionError(f"git {' '.join(arguments)} failed: {detail}")
    return result


def object_id(repo: Path, revision: str, suffix: str = "") -> str:
    result = git(repo, "rev-parse", "--verify", f"{revision}{suffix}")
    return result.stdout.strip()


def ref_target(repo: Path, branch: str) -> str | None:
    result = git(repo, "rev-parse", "--verify", f"refs/heads/{branch}", check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def expected_target(branch_family: str, target_generation: int) -> str:
    if not branch_family or target_generation <= 1:
        raise TransitionError("target generation must be greater than one")
    return f"{branch_family}-mrw-g{target_generation}"


def validate_target(repo: Path, branch: str) -> None:
    result = git(repo, "check-ref-format", "--branch", branch, check=False)
    if result.returncode != 0:
        raise TransitionError(f"invalid deterministic target branch: {branch}")


def normalize_transfer_subject(value: str | None, *, reject_placeholder: bool = True) -> str:
    if value is None:
        raise TransitionError("non-empty transfer requires a commit subject")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise TransitionError("transfer subject must not contain control characters")
    normalized = value.strip(" ")
    if not normalized:
        raise TransitionError("transfer subject must not be blank")
    folded = normalized.casefold()
    if reject_placeholder and any(
        placeholder in folded for placeholder in KNOWN_TRANSFER_SUBJECT_PLACEHOLDERS
    ):
        raise TransitionError("transfer subject contains an unresolved workflow placeholder")
    return normalized


def commit_subject(repo: Path, commit: str) -> str:
    return git(repo, "show", "-s", "--format=%s", commit).stdout.rstrip("\n")


def compatible_target_shape(
    repo: Path,
    commit: str,
    source_sha: str,
    result_tree_sha: str,
) -> bool:
    if object_id(repo, commit, "^{tree}") != result_tree_sha:
        return False
    if result_tree_sha == object_id(repo, source_sha, "^{tree}"):
        return commit == source_sha
    parents = git(repo, "show", "-s", "--format=%P", commit).stdout.strip().split()
    return parents == [source_sha]


def compatible_target(
    repo: Path,
    commit: str,
    source_sha: str,
    result_tree_sha: str,
    transfer_subject: str | None,
) -> bool:
    if not compatible_target_shape(repo, commit, source_sha, result_tree_sha):
        return False
    source_tree = object_id(repo, source_sha, "^{tree}")
    if result_tree_sha == source_tree:
        return transfer_subject is None
    return (
        transfer_subject is not None
        and commit_subject(repo, commit) == transfer_subject
    )


def registered_worktrees(repo: Path) -> set[Path]:
    fields = git(repo, "worktree", "list", "--porcelain", "-z").stdout.split("\0")
    return {
        Path(field.removeprefix("worktree ")).resolve()
        for field in fields
        if field.startswith("worktree ")
    }


def expected_temporary_worktree(workspace_root: Path, slot: str, repository: str, target: str) -> Path:
    identity = "\0".join((slot, repository, target)).encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()
    return workspace_root.resolve() / GENERATION_WORKTREES_RELATIVE_PATH / f"{digest}.worktree"


def validate_temporary_worktree(
    repo: Path,
    value: Path,
    workspace_root: Path,
    slot: str,
    repository: str,
    target: str,
) -> Path:
    if not value.is_absolute():
        raise TransitionError("temporary generation worktree path must be absolute")
    if not workspace_root.is_absolute() or not slot or not repository:
        raise TransitionError("temporary generation worktree ownership is incomplete")
    temporary = value.resolve()
    expected = expected_temporary_worktree(workspace_root, slot, repository, target)
    if str(value) != str(expected) or temporary != expected:
        raise TransitionError(
            f"temporary generation worktree does not match its generation journal identity: {expected}"
        )
    if temporary == repo or repo in temporary.parents or temporary in repo.parents:
        raise TransitionError("temporary generation worktree must be outside the repository worktree")
    return temporary


def has_untracked(repo: Path) -> bool:
    return bool(git(repo, "ls-files", "--others").stdout.strip())


def prepare_transfer_commit(
    repo: Path,
    temporary: Path,
    source: str,
    result_tree: str,
    transfer_subject: str,
) -> str:
    registered = registered_worktrees(repo)
    if temporary in registered:
        if not temporary.is_dir():
            raise TransitionError(
                f"journaled temporary worktree is registered but missing: {temporary}"
            )
    else:
        if temporary.exists():
            raise TransitionError(
                f"journaled temporary path exists but is not a registered worktree: {temporary}"
            )
        temporary.parent.mkdir(parents=True, exist_ok=True)
        git(repo, "worktree", "add", "--detach", str(temporary), source)

    if git(temporary, "symbolic-ref", "-q", "HEAD", check=False).returncode == 0:
        raise TransitionError(
            f"journaled temporary worktree is not detached and will not be modified: {temporary}"
        )

    head = object_id(temporary, "HEAD")
    if head == source:
        source_tree = object_id(repo, source, "^{tree}")
        index_tree = object_id(temporary, git(temporary, "write-tree").stdout.strip(), "^{tree}")
        if index_tree not in {source_tree, result_tree}:
            raise TransitionError(
                f"journaled temporary worktree has an unexpected index tree: {temporary}"
            )
        if git(temporary, "diff", "--quiet", check=False).returncode != 0 or has_untracked(temporary):
            raise TransitionError(
                f"journaled temporary worktree has unexpected unstaged or untracked content: {temporary}"
            )
        git(
            temporary,
            "restore",
            "--source",
            result_tree,
            "--staged",
            "--worktree",
            "--",
            ":/",
        )
        if git(temporary, "write-tree").stdout.strip() != result_tree:
            raise TransitionError("temporary generation index does not match the journaled result tree")
        if git(temporary, "diff", "--quiet", check=False).returncode != 0 or has_untracked(temporary):
            raise TransitionError("temporary generation worktree does not match its exact index")
        git(temporary, "commit", "-m", transfer_subject)
        commit = object_id(temporary, "HEAD")
    elif compatible_target(repo, head, source, result_tree, transfer_subject):
        if git(temporary, "status", "--porcelain").stdout.strip() or has_untracked(temporary):
            raise TransitionError(
                f"completed temporary generation commit has additional changes: {temporary}"
            )
        commit = head
    else:
        raise TransitionError(
            f"journaled temporary worktree HEAD does not match source or result: {temporary}"
        )

    if not compatible_target(repo, commit, source, result_tree, transfer_subject):
        actual_subject = commit_subject(repo, commit)
        raise TransitionError(
            "normal Git commit did not produce the journaled generation commit "
            f"subject: expected {transfer_subject!r}, found {actual_subject!r}"
        )
    if git(temporary, "status", "--porcelain").stdout.strip() or has_untracked(temporary):
        raise TransitionError("temporary generation worktree is not clean after commit")
    remove = git(repo, "worktree", "remove", str(temporary), check=False)
    if remove.returncode != 0:
        detail = remove.stderr.strip() or remove.stdout.strip() or "unknown cleanup error"
        raise TransitionError(
            f"temporary generation worktree cleanup failed: {detail}; preserved at {temporary}"
        )
    return commit


def analyze(args: argparse.Namespace) -> dict[str, Any]:
    repo = args.repo.resolve()
    from_head = object_id(repo, args.from_head)
    boundary = object_id(repo, args.boundary)
    source = object_id(repo, args.source)
    current = object_id(repo, "HEAD")
    if current != from_head:
        raise TransitionError(f"current HEAD changed: expected {from_head}, found {current}")
    ancestor = git(repo, "merge-base", "--is-ancestor", boundary, from_head, check=False)
    if ancestor.returncode != 0:
        raise TransitionError(
            "continuation boundary is not an ancestor of the current development head; "
            "no branch was changed"
        )
    target = expected_target(args.branch_family, args.target_generation)
    validate_target(repo, target)
    merge = git(
        repo,
        "merge-tree",
        "--write-tree",
        "--messages",
        "-z",
        "--merge-base",
        boundary,
        source,
        from_head,
        check=False,
    )
    fields = merge.stdout.split("\0")
    if merge.returncode == 1 and fields and len(fields[0]) in (40, 64):
        entries = []
        for field in fields[1:]:
            if not field:
                break
            entries.append(field)
        if entries:
            object_id(repo, fields[0], "^{tree}")
            raise GenerationConflict({"from_head_sha": from_head, "boundary_sha": boundary,
                "source_sha": source, "source_tree_sha": object_id(repo, source, "^{tree}"),
                "target_branch": target, "target_branch_generation": args.target_generation,
                "conflict_tree_sha": fields[0], "conflict_entries": entries,
                "conflict_paths": sorted({entry.split("\t", 1)[1] for entry in entries})})
    if merge.returncode != 0:
        detail = merge.stdout.strip() or merge.stderr.strip() or "3-way tree conflict"
        raise TransitionError(f"generation tree conflicts; no branch was changed: {detail}")
    lines = fields
    if not lines:
        raise TransitionError("merge-tree returned no result tree")
    result_tree = lines[0].strip()
    object_id(repo, result_tree, "^{tree}")
    source_tree = object_id(repo, source, "^{tree}")
    existing = ref_target(repo, target)
    if existing is not None and not compatible_target_shape(repo, existing, source, result_tree):
        raise TransitionError(f"target branch already exists with a different tree: {target}")
    return {
        "repo": str(repo),
        "from_head_sha": from_head,
        "boundary_sha": boundary,
        "source_sha": source,
        "source_tree_sha": source_tree,
        "target_branch": target,
        "target_branch_generation": args.target_generation,
        "result_tree_sha": result_tree,
        "target_status": "existing-compatible" if existing is not None else "absent",
        "target_sha": existing,
        "target_subject": (
            commit_subject(repo, existing)
            if existing is not None and result_tree != source_tree
            else None
        ),
    }


def create(args: argparse.Namespace) -> dict[str, Any]:
    repo = args.repo.resolve()
    source = object_id(repo, args.source)
    source_tree = object_id(repo, source, "^{tree}")
    journaled_source_tree = object_id(repo, args.source_tree)
    if journaled_source_tree != source_tree:
        raise TransitionError(
            "journaled source tree does not match the exact source commit"
        )
    journaled_result = object_id(repo, args.result_tree)
    result_tree = object_id(repo, journaled_result, "^{tree}")
    if journaled_result != result_tree:
        raise TransitionError("journaled result tree is not an exact tree object")
    target = expected_target(args.branch_family, args.target_generation)
    validate_target(repo, target)
    temporary = validate_temporary_worktree(
        repo,
        args.temporary_worktree,
        args.workspace_root,
        args.slot,
        args.repository,
        target,
    )
    existing = ref_target(repo, target)
    if result_tree == source_tree:
        if args.message is not None:
            raise TransitionError("empty transfer must not have a commit subject")
        transfer_subject = None
    else:
        transfer_subject = normalize_transfer_subject(args.message)
    if existing is not None:
        if not compatible_target(repo, existing, source, result_tree, transfer_subject):
            raise TransitionError(
                f"target branch already exists with a different tree or subject: {target}"
            )
        if temporary in registered_worktrees(repo) or temporary.exists():
            raise TransitionError(
                f"compatible target exists but the journaled temporary worktree also remains: {temporary}"
            )
        return {
            "target_branch": target,
            "target_sha": existing,
            "transfer_subject": transfer_subject,
            "created": False,
        }

    prepared = getattr(args, "prepared_commit", None)
    if prepared is not None:
        import generation_conflicts
        import slot_manifest
        token = getattr(args, "token", None)
        slot_manifest.require_operation_lock(args.workspace_root, args.slot, token)
        state = slot_manifest.require_slot(slot_manifest.load_manifest(slot_manifest.manifest_path(args.workspace_root)), args.slot)
        receipt = generation_conflicts.for_rotation(args.workspace_root, args.slot, args.repository,
                                                   token, state["repositories"][args.repository]["rotation"])
        if receipt is None or receipt["commit_sha"] != prepared:
            raise TransitionError("prepared commit requires its exact recorded conflict recovery")
    if prepared is not None and not compatible_target(repo, prepared, source, result_tree, transfer_subject):
        raise TransitionError("prepared resolution commit does not match source/tree/subject")
    if prepared is not None:
        if temporary in registered_worktrees(repo) or temporary.exists():
            raise TransitionError("prepared resolution has an unexpected generation worktree")
        commit = prepared
    elif result_tree == source_tree:
        if temporary in registered_worktrees(repo) or temporary.exists():
            raise TransitionError(
                f"empty transfer has an unexpected journaled temporary worktree: {temporary}"
            )
        commit = source
    else:
        try:
            commit = prepare_transfer_commit(
                repo,
                temporary,
                source,
                result_tree,
                transfer_subject,
            )
        except TransitionError as exc:
            raise TransitionError(f"{exc}; journaled temporary worktree: {temporary}") from exc
    zero = "0" * len(commit)
    update = git(
        repo,
        "update-ref",
        f"refs/heads/{target}",
        commit,
        zero,
        check=False,
    )
    if update.returncode != 0:
        existing = ref_target(repo, target)
        if existing is None or not compatible_target(
            repo,
            existing,
            source,
            result_tree,
            transfer_subject,
        ):
            detail = update.stderr.strip() or "target ref changed concurrently"
            raise TransitionError(f"cannot create target branch {target}: {detail}")
        commit = existing
        created = False
    else:
        created = True
    return {
        "target_branch": target,
        "target_sha": commit,
        "transfer_subject": transfer_subject,
        "created": created,
    }


def verify(args: argparse.Namespace) -> dict[str, Any]:
    repo = args.repo.resolve()
    source = object_id(repo, args.source)
    source_tree = object_id(repo, source, "^{tree}")
    journaled_source_tree = object_id(repo, args.source_tree)
    if journaled_source_tree != source_tree:
        raise TransitionError(
            "journaled source tree does not match the exact source commit"
        )
    journaled_result = object_id(repo, args.result_tree)
    result_tree = object_id(repo, journaled_result, "^{tree}")
    if journaled_result != result_tree:
        raise TransitionError("journaled result tree is not an exact tree object")
    target = expected_target(args.branch_family, args.target_generation)
    validate_target(repo, target)
    commit = ref_target(repo, target)
    if commit is None:
        raise TransitionError(f"target branch does not exist: {target}")
    if result_tree == source_tree:
        if args.message is not None:
            raise TransitionError("empty transfer must not have a commit subject")
        transfer_subject = None
    else:
        transfer_subject = normalize_transfer_subject(args.message)
    if not compatible_target(repo, commit, source, result_tree, transfer_subject):
        raise TransitionError(f"target branch does not match the journal: {target}")
    return {
        "target_branch": target,
        "target_sha": commit,
        "transfer_subject": transfer_subject,
        "verified": True,
    }


def inspect_legacy_repository_state(
    *,
    repo: Path,
    source_revision: str,
    result_tree_revision: str,
    branch_family: str,
    target_generation: int,
    temporary_worktree: Path,
    workspace_root: Path,
    slot: str,
    repository: str,
) -> dict[str, Any]:
    repo = repo.resolve()
    source = object_id(repo, source_revision)
    journaled_result = object_id(repo, result_tree_revision)
    result_tree = object_id(repo, journaled_result, "^{tree}")
    if journaled_result != result_tree:
        raise TransitionError("journaled result tree is not an exact tree object")
    source_tree = object_id(repo, source, "^{tree}")
    target = expected_target(branch_family, target_generation)
    validate_target(repo, target)
    temporary = validate_temporary_worktree(
        repo,
        temporary_worktree,
        workspace_root,
        slot,
        repository,
        target,
    )
    development_head = object_id(repo, "HEAD")
    symbolic = git(repo, "symbolic-ref", "--short", "-q", "HEAD", check=False)
    development_branch = symbolic.stdout.strip() if symbolic.returncode == 0 else None
    development_clean = not (
        git(repo, "status", "--porcelain").stdout.strip() or has_untracked(repo)
    )

    target_head = ref_target(repo, target)
    if target_head is not None and not compatible_target_shape(
        repo,
        target_head,
        source,
        result_tree,
    ):
        raise TransitionError(f"legacy target branch does not match its journal: {target}")
    target_subject = (
        commit_subject(repo, target_head)
        if target_head is not None and result_tree != source_tree
        else None
    )

    registered = registered_worktrees(repo)
    temporary_registered = temporary in registered
    if temporary_registered and not temporary.is_dir():
        raise TransitionError(f"journaled temporary worktree is registered but missing: {temporary}")
    if not temporary_registered and temporary.exists():
        raise TransitionError(
            f"journaled temporary path exists but is not a registered worktree: {temporary}"
        )

    temporary_status = "absent"
    temporary_head: str | None = None
    temporary_subject: str | None = None
    if temporary_registered:
        if result_tree == source_tree:
            raise TransitionError(
                f"empty legacy transfer has an unexpected temporary worktree: {temporary}"
            )
        if git(temporary, "symbolic-ref", "-q", "HEAD", check=False).returncode == 0:
            raise TransitionError(
                f"journaled temporary worktree is not detached: {temporary}"
            )
        temporary_head = object_id(temporary, "HEAD")
        if temporary_head == source:
            index_tree = git(temporary, "write-tree").stdout.strip()
            if index_tree not in {source_tree, result_tree}:
                raise TransitionError(
                    f"journaled temporary worktree has an unexpected index tree: {temporary}"
                )
            if (
                git(temporary, "diff", "--quiet", check=False).returncode != 0
                or has_untracked(temporary)
            ):
                raise TransitionError(
                    f"journaled temporary worktree has unstaged or untracked content: {temporary}"
                )
            temporary_status = (
                "source" if index_tree == source_tree else "source-indexed-result"
            )
        elif compatible_target_shape(repo, temporary_head, source, result_tree):
            if (
                git(temporary, "status", "--porcelain").stdout.strip()
                or has_untracked(temporary)
            ):
                raise TransitionError(
                    f"completed temporary generation commit has additional changes: {temporary}"
                )
            temporary_status = "completed"
            temporary_subject = commit_subject(repo, temporary_head)
        else:
            raise TransitionError(
                f"journaled temporary worktree HEAD does not match source or result: {temporary}"
            )

    return {
        "source_sha": source,
        "source_tree_sha": source_tree,
        "result_tree_sha": result_tree,
        "target_branch": target,
        "target_status": "existing" if target_head is not None else "absent",
        "target_sha": target_head,
        "target_subject": target_subject,
        "temporary_worktree": str(temporary),
        "temporary_status": temporary_status,
        "temporary_head_sha": temporary_head,
        "temporary_subject": temporary_subject,
        "development_branch": development_branch,
        "development_head_sha": development_head,
        "development_clean": development_clean,
    }


def inspect_legacy_state(args: argparse.Namespace) -> dict[str, Any]:
    return inspect_legacy_repository_state(
        repo=args.repo,
        source_revision=args.source,
        result_tree_revision=args.result_tree,
        branch_family=args.branch_family,
        target_generation=args.target_generation,
        temporary_worktree=args.temporary_worktree,
        workspace_root=args.workspace_root,
        slot=args.slot,
        repository=args.repository,
    )


def inspect_legacy(args: argparse.Namespace) -> dict[str, Any]:
    return inspect_legacy_state(args)


def _discard_legacy_temporary(args: argparse.Namespace) -> dict[str, Any]:
    expected_subject = normalize_transfer_subject(
        args.expected_subject,
        reject_placeholder=False,
    )
    import slot_manifest

    manifest_file = slot_manifest.manifest_path(args.workspace_root)
    with slot_manifest.manifest_lock(manifest_file):
        manifest = slot_manifest.load_manifest(manifest_file)
        slot_manifest.require_operation_lock(args.workspace_root, args.slot, args.token)
        slot_state = slot_manifest.require_slot(manifest, args.slot)
        if args.repository not in slot_state["repositories"]:
            raise TransitionError(
                f"slot {args.slot!r} has no repository binding for: {args.repository}"
            )
        expected_repo = (Path(slot_state["path"]) / args.repository).resolve()
        if args.repo.resolve() != expected_repo:
            raise TransitionError(
                "legacy temporary recovery repository does not match its manifest binding"
            )
        identity = slot_state["repositories"][args.repository]
        rotation = identity.get("rotation")
        if identity.get("legacy_schema") != 4 or rotation is None:
            raise TransitionError("repository has no schema 4 rotation journal")
        if args.expected_phase != "planned" or rotation["phase"] != args.expected_phase:
            raise TransitionError(
                "legacy temporary commit may only be discarded while persisted "
                "rotation phase is planned"
            )
        target = expected_target(args.branch_family, args.target_generation)
        expected_fields = {
            "source_sha": object_id(args.repo.resolve(), args.source),
            "target_branch": target,
            "target_branch_generation": args.target_generation,
            "result_tree_sha": object_id(
                args.repo.resolve(),
                args.result_tree,
                "^{tree}",
            ),
            "temporary_worktree": str(args.temporary_worktree),
        }
        if any(rotation.get(field) != value for field, value in expected_fields.items()):
            raise TransitionError(
                "legacy temporary recovery does not match its persisted rotation journal"
            )
        state = inspect_legacy_state(args)
        if state["development_branch"] != rotation["from_branch"]:
            raise TransitionError(
                "legacy planned rotation development branch changed"
            )
        if state["development_head_sha"] != rotation["from_head_sha"]:
            raise TransitionError("legacy planned rotation development HEAD changed")
        if not state["development_clean"]:
            raise TransitionError("legacy planned rotation development worktree is not clean")
        if state["target_status"] != "absent":
            raise TransitionError(
                "legacy temporary commit cannot be discarded after target publication"
            )
        if state["temporary_status"] != "completed":
            raise TransitionError("legacy temporary worktree is not an exact completed commit")
        if state["temporary_subject"] != expected_subject:
            raise TransitionError("legacy temporary commit subject changed during recovery")
        temporary = Path(state["temporary_worktree"])
        remove = git(args.repo.resolve(), "worktree", "remove", str(temporary), check=False)
        if remove.returncode != 0:
            detail = remove.stderr.strip() or remove.stdout.strip() or "unknown cleanup error"
            raise TransitionError(
                f"legacy temporary worktree cleanup failed: {detail}; preserved at {temporary}"
            )
    return {
        **state,
        "temporary_status": "discarded",
        "discarded": True,
    }


def discard_legacy_temporary(args: argparse.Namespace) -> dict[str, Any]:
    import slot_manifest

    try:
        return _discard_legacy_temporary(args)
    except slot_manifest.ManifestError as exc:
        raise TransitionError(str(exc)) from exc


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    subparsers = result.add_subparsers(dest="command", required=True)

    analyze_parser = subparsers.add_parser("analyze", help="Compute a 3-way result tree without refs")
    analyze_parser.add_argument("--repo", required=True, type=Path)
    analyze_parser.add_argument("--from-head", required=True)
    analyze_parser.add_argument("--boundary", required=True)
    analyze_parser.add_argument("--source", required=True)
    analyze_parser.add_argument("--branch-family", required=True)
    analyze_parser.add_argument("--target-generation", required=True, type=int)

    for name in ("create", "verify"):
        subparser = subparsers.add_parser(name)
        subparser.add_argument("--repo", required=True, type=Path)
        subparser.add_argument("--source", required=True)
        subparser.add_argument("--source-tree", required=True)
        subparser.add_argument("--result-tree", required=True)
        subparser.add_argument("--branch-family", required=True)
        subparser.add_argument("--target-generation", required=True, type=int)
        subparser.add_argument("--message")
        if name == "create":
            subparser.add_argument("--prepared-commit")
            subparser.add_argument("--token")
            subparser.add_argument("--temporary-worktree", required=True, type=Path)
            subparser.add_argument("--workspace-root", required=True, type=Path)
            subparser.add_argument("--slot", required=True)
            subparser.add_argument("--repository", required=True)

    for name in ("inspect-legacy", "discard-legacy-temporary"):
        subparser = subparsers.add_parser(name)
        subparser.add_argument("--repo", required=True, type=Path)
        subparser.add_argument("--source", required=True)
        subparser.add_argument("--result-tree", required=True)
        subparser.add_argument("--branch-family", required=True)
        subparser.add_argument("--target-generation", required=True, type=int)
        subparser.add_argument("--temporary-worktree", required=True, type=Path)
        subparser.add_argument("--workspace-root", required=True, type=Path)
        subparser.add_argument("--slot", required=True)
        subparser.add_argument("--repository", required=True)
        if name == "discard-legacy-temporary":
            subparser.add_argument("--expected-phase", required=True)
            subparser.add_argument("--expected-subject", required=True)
            subparser.add_argument("--token", required=True)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "analyze":
            payload = analyze(args)
        elif args.command == "create":
            payload = create(args)
        elif args.command == "verify":
            payload = verify(args)
        elif args.command == "inspect-legacy":
            payload = inspect_legacy(args)
        else:
            payload = discard_legacy_temporary(args)
        print(json.dumps({"ok": True, "result": payload}, ensure_ascii=False, sort_keys=True))
        return 0
    except TransitionError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
