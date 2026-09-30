#!/usr/bin/env python3
"""Scoped submit preflight (default) and resumable per-repository execution (--execute).

The agent owns content selection and Korean prose. This runner owns mechanical sequencing.
No shell command strings, force push, remote merge, or remote branch deletion.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

import inspect_worktrees as inspector
import prepare_generation_branch as generation
import retire_generation_branch as retirement
import slot_manifest as manifest
import submit_review as review
import verify_pr as verifier


class SubmitError(RuntimeError):
    pass


class RevalidationRequired(SubmitError):
    pass


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def g(repo: Path, *args: str) -> str:
    return verifier.git(repo, *args).decode(errors="surrogateescape").strip()


def active_branch(identity: dict) -> str:
    rotation = identity.get("rotation", {})
    return rotation["target_branch"] if rotation.get("phase") in {"switched", "retired"} else identity["branch"]


def changed_paths(repo: Path, *, staged: bool = False) -> set[str]:
    # --no-renames yields both old and new names; all user pathspecs later use :(literal).
    args = ["diff", "--name-only", "--no-renames", "-z"]
    if staged:
        args.append("--cached")
    else:
        args.append("HEAD")
    raw = verifier.git(repo, *args, "--")
    if not staged:
        raw += verifier.git(repo, "ls-files", "--others", "--exclude-standard", "-z")
    return set(raw.decode(errors="surrogateescape").split("\0")) - {""}


def clean(repo: Path) -> None:
    if verifier.git(repo, "status", "--porcelain=v1", "--untracked-files=all"):
        raise SubmitError("worktree/index must be clean; unselected or generated changes remain")


def worktree_snapshot(repo: Path) -> tuple[str, str]:
    """Pin changed file bytes independently of whether they are staged yet."""
    value = hashlib.sha256()
    for name in sorted(changed_paths(repo)):
        path = repo / name
        value.update(hashlib.sha256(os.fsencode(name)).digest())
        if path.is_symlink():
            content, mode = os.fsencode(os.readlink(path)), path.lstat().st_mode
        elif path.is_file():
            content, mode = path.read_bytes(), path.stat().st_mode
        elif path.is_dir():
            content, mode = b"directory", path.stat().st_mode  # selection later rejects directories
        else:
            content, mode = b"deleted", 0
        value.update(hashlib.sha256(content).digest())
        value.update(str(mode).encode() + b"\0")
    content_key = value.hexdigest()
    index = verifier.git(repo, "diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv", "--")
    return content_key, digest([content_key, hashlib.sha256(index).hexdigest()])


def worktree_fingerprint(repo: Path) -> str:
    return worktree_snapshot(repo)[1]


def remote_identity(repo: Path, remote: str) -> tuple[str, str]:
    fetch = g(repo, "remote", "get-url", "--all", remote).splitlines()
    push = g(repo, "remote", "get-url", "--push", "--all", remote).splitlines()
    if len(fetch) != 1 or len(push) != 1:
        raise SubmitError("remote must have exactly one fetch and one push URL")
    repository = verifier.github_repository(fetch[0])
    if verifier.github_repository(push[0]) != repository:
        raise SubmitError("fetch/push repositories differ")
    return repository, digest([fetch, push])


def remote_heads(repo: Path, remote: str, *branches: str) -> dict[str, str]:
    refs = ["refs/heads/" + branch for branch in branches]
    raw = g(repo, "ls-remote", "--heads", remote, *refs)
    return {ref.removeprefix("refs/heads/"): sha
            for line in raw.splitlines() for sha, ref in [line.split("\t")] if ref in refs}


def korean(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or not re.search("[가-힣]", value):
        raise SubmitError(f"{label} must contain reviewed Korean text")
    return value


class Runner:
    def __init__(self, root: Path, bundle: str, api: verifier.GitHub | None = None,
                 test: bool = False):
        self.root = root.resolve()
        self.base, self.slot, _ = inspector.resolve_bundle(self.root, bundle)
        if self.slot is None:
            raise SubmitError("submit requires a manifest-bound slot")
        self.bundle = bundle
        self.path = manifest.manifest_path(self.root)
        self.receipt_path = self.root / ".stageflow-worktrees" / "submissions" / (digest(self.slot) + ".json")
        self.api = api or verifier.GitHub()
        self.token = uuid.uuid4().hex
        self.test = test
        self.validation_trees: dict[str, str] = {}
        self.receipts: dict[str, Any] = {}
        self.timings: list[dict] = []

    def identity(self, name: str) -> dict:
        slot = manifest.require_slot(manifest.load_manifest(self.path), self.slot)
        if Path(slot["path"]).resolve() != self.base:
            raise SubmitError("slot path changed")
        identity = slot["repositories"][name]
        manifest.require_generation_evidence(identity, name)
        return identity

    def load_receipts(self) -> None:
        self.receipts = json.loads(self.receipt_path.read_text()) if self.receipt_path.exists() else {}
        if not isinstance(self.receipts, dict):
            raise SubmitError("invalid submission receipt; preserve it for recovery")

    def save(self) -> None:
        manifest.require_operation_lock(self.root, self.slot, self.token)
        manifest.write_json_atomic(self.receipt_path, self.receipts, "submit.")

    def mutate(self, callback) -> None:
        with manifest.manifest_lock(self.path):
            manifest.require_operation_lock(self.root, self.slot, self.token)
            data = manifest.load_manifest(self.path)
            callback(data)
            manifest.write_manifest(self.path, data)

    @contextmanager
    def phase(self, name: str):
        started = time.monotonic()
        item = {"phase": name, "ok": False}
        self.timings.append(item)
        try:
            yield
            item["ok"] = True
        finally:
            item["seconds"] = round(time.monotonic() - started, 3)

    def classify(self, name: str, identity: dict, repository: str) -> str:
        pending = self.receipts.get(name, {}).get("pending")
        if pending:
            return "RECOVER"
        branch = active_branch(identity)
        if identity["pr"]:
            submission = identity["submission"]
            pr = self.api.view(repository, verifier.pr_number(identity["pr"], repository))
            state = "MERGED" if pr.get("merged_at") else str(pr.get("state", "")).upper()
            if state not in {"OPEN", "MERGED"}:
                raise SubmitError("recorded PR is neither OPEN nor MERGED")
            verifier.check_identity(pr, repository, identity["source_branch"], submission["head_branch"],
                                    submission["observed_head_sha"], state=state)
            if state == "OPEN" and (branch != submission["head_branch"] or identity.get("rotation")):
                raise SubmitError("OPEN PR has conflicting active branch/rotation")
            if branch == submission["head_branch"]:
                return state
        else:
            state = "NONE"
        if self.api.find(repository, branch):
            raise SubmitError("active branch has an unrecorded PR; exact publication recovery required")
        return state

    def preflight(self, *, content_snapshot: bool = True, skip_dirty: bool = False,
                  repositories: set[str] | None = None, local_only: bool = False,
                  candidates_only: bool = False) -> dict:
        started = time.monotonic()
        self.load_receipts()
        scan = inspector.inspect_bundle(self.root, self.bundle)
        if scan["errors"]:
            # A missing sibling is reported, not silently removed from the bundle.
            missing = scan["errors"]
        else:
            missing = []
        held = manifest.load_operation_lock(manifest.operation_lock_path(self.root, self.slot))
        if held:
            missing.append("slot operation lock is held; verify its owner before stale-lock recovery")
        rows = []
        candidates = []
        for item in scan["bundles"][0]["items"]:
            name, repo = item["repo"], Path(item["path"])
            row = {"repository": name, "path": str(repo), "dirty": item["dirty"]}
            try:
                if skip_dirty and item["dirty"] != "clean":
                    row.update(state="DIRTY", changed_paths=sorted(changed_paths(repo)))
                    rows.append(row)
                    continue
                identity = self.identity(name)
                if any(conflict["repository"] == name for conflict in scan["conflicts"]):
                    raise SubmitError("active branch is occupied by another worktree")
                if item["operation_state"] != "-":
                    raise SubmitError("unfinished Git operation")
                branch = g(repo, "branch", "--show-current")
                rotation = identity.get("rotation", {})
                allowed = {active_branch(identity)}
                if rotation.get("phase") == "branch-created":
                    allowed.add(rotation["target_branch"])  # switch succeeded before journal update
                if branch not in allowed:
                    raise SubmitError("checked-out branch disagrees with manifest")
                repository, remote_key = remote_identity(repo, identity["remote"])
                head = g(repo, "rev-parse", "HEAD")
                submission = identity.get("submission") or {}
                boundary = (submission.get("observed_head_sha") if branch == submission.get("head_branch")
                            else identity["branch_base_sha"])
                candidate = (item["dirty"] != "clean" or head != boundary or bool(rotation) or
                             bool(self.receipts.get(name, {}).get("pending")))
                row.update(branch=branch, head_sha=head, generation=identity["generation"],
                           github_repository=repository, remote_key=remote_key,
                           identity_fingerprint=digest(identity), rotation=rotation.get("phase"))
                if (repositories is not None and name not in repositories) or (
                        repositories is None and candidates_only and not candidate):
                    row.update(state="LOCAL_ONLY", reason="not selected for publication")
                    rows.append(row)
                    continue
                if local_only:
                    row.update(state="READY")
                    rows.append(row)
                    continue
                candidates.append((row, repo, identity, branch, head))
            except (RuntimeError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
                row.update(state="BLOCKED", error=str(exc))
            rows.append(row)

        def inspect_remote(candidate):
            row, repo, identity, branch, _ = candidate
            heads = remote_heads(repo, identity["remote"], identity["source_branch"], branch)
            source = heads.get(identity["source_branch"])
            if not source:
                raise SubmitError("bound remote source is missing")
            return heads, source, self.classify(row["repository"], identity, row["github_repository"])

        # Only read-only candidate lookups overlap; publication guards stay sequential.
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(inspect_remote, candidate) for candidate in candidates]
            for (row, repo, identity, branch, head), future in zip(candidates, futures):
                try:
                    heads, source, state = future.result()
                    row.update(state=state, source_sha=source, changed_paths=sorted(changed_paths(repo)),
                               remote_head_sha=heads.get(branch))
                    if content_snapshot:
                        self.prepare_content(row, repo, identity, branch, head)
                except (RuntimeError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
                    row.update(state="BLOCKED", error=str(exc))
        return {"mode": "preflight", "slot": self.slot, "bundle": str(self.base),
                "repositories": rows, "errors": missing, "seconds": round(time.monotonic() - started, 3)}

    def prepare_content(self, row: dict, repo: Path, identity: dict, branch: str, head: str) -> None:
        name, state = row["repository"], row["state"]
        plan = {"expected_head": head, "expected_generation": identity["generation"],
                "expected_branch": branch, "expected_remote": row["remote_key"],
                "expected_identity": digest(identity), "paths": row["changed_paths"],
                "prepared_state": state, "task_summary": ""}
        if state == "RECOVER":
            pending = self.receipts[name]["pending"]
            plan.update(pending.get("inputs", {}))
            row.update(action="RECOVER_PUBLICATION", required_fields=[], content_review_required=False,
                       publication={key: pending.get(key) for key in ("branch", "head_sha", "pr_url")}, plan=plan)
            return
        row["worktree_fingerprint"] = worktree_fingerprint(repo)
        plan["expected_worktree"] = row["worktree_fingerprint"]
        rotation = identity.get("rotation", {})
        submission = identity.get("submission") or {}
        boundary = rotation.get("boundary_sha") or (
            submission.get("continuation_boundary_sha") if branch == submission.get("head_branch")
            else identity["branch_base_sha"])
        review_head = rotation.get("from_head_sha", head)
        committed_paths = verifier.git(repo, "diff", "--name-only", "--no-renames", "-z", boundary, review_head, "--")
        committed = [p for p in committed_paths.decode(errors="surrogateescape").split("\0") if p]
        row["committed_review"] = {"boundary_sha": boundary, "head_sha": review_head, "changed_paths": committed}
        row["action"] = review.action(state, row["changed_paths"], committed)
        saved = self.resume_plan(name)
        if saved is not None and self.receipts[name].get("last_result", {}).get("state") == "FAILED":
            plan = saved
            row["content_review_required"] = False
        else:
            row["content_review_required"] = bool(row["changed_paths"] or (state != "OPEN" and committed))
        if rotation:
            plan["transfer_subject"] = rotation["transfer_subject"]
        row["required_fields"] = review.required_fields(state, row["changed_paths"], committed, plan,
            revalidation=bool(self.receipts.get(name, {}).get("revalidation_required")) or self.test or
                plan.get("validation_mode") == "always",
            rotation_required=bool(rotation) or row["source_sha"] != identity["branch_base_sha"])
        row["plan"] = plan

    def write_plan(self, plans: dict) -> Path:
        path = self.root / ".stageflow-worktrees" / "submit-plans" / (uuid.uuid4().hex + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8", errors="backslashreplace") as stream:
            json.dump({"repositories": plans}, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        return path

    def prepare(self, repositories: set[str] | None = None) -> dict:
        result = self.preflight(repositories=repositories, candidates_only=True)
        for row in result["repositories"]:
            if not row.get("content_review_required") or "plan" not in row:
                continue
            try:
                plan, committed = row["plan"], row["committed_review"]
                directory = self.root / ".stageflow-worktrees" / "submit-plans" / (uuid.uuid4().hex + ".review")
                row["review_path"] = review.write_material(Path(row["path"]), directory,
                    head=plan["expected_head"], boundary=committed["boundary_sha"],
                    committed_head=committed["head_sha"], include_committed=row["state"] != "OPEN")
                if g(Path(row["path"]), "rev-parse", "HEAD") != plan["expected_head"] or \
                        worktree_fingerprint(Path(row["path"])) != plan["expected_worktree"] or \
                        digest(self.identity(row["repository"])) != plan["expected_identity"]:
                    raise SubmitError("content changed while preparing review material; refresh preflight")
            except (RuntimeError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
                row.update(state="BLOCKED", error=str(exc))
                row.pop("plan", None)
                row.pop("review_path", None)
        plans = {row["repository"]: row["plan"] for row in result["repositories"] if "plan" in row}
        if plans:
            result["plan_path"] = str(self.write_plan(plans))
        for row in result["repositories"]:
            row.pop("plan", None)
        return result

    def checkpoint(self, name: str, plan: dict, head: str) -> None:
        identity = self.identity(name)
        repo = self.bound(name, identity, plan["expected_remote"])
        clean(repo)
        if g(repo, "rev-parse", "HEAD") != head or g(repo, "branch", "--show-current") != active_branch(identity):
            raise SubmitError("input moved before saving the retry snapshot")
        checkpoint = {**plan,
            "expected_head": head, "expected_branch": active_branch(identity),
            "expected_generation": identity["generation"], "expected_identity": digest(identity),
            "expected_worktree": worktree_fingerprint(repo), "paths": []}
        if self.receipts.setdefault(name, {}).get("resume_plan") != checkpoint:
            self.receipts[name]["resume_plan"] = checkpoint
            self.save()

    def resume_plan(self, name: str) -> dict | None:
        plan = self.receipts.get(name, {}).get("resume_plan")
        if not plan:
            return None
        identity = self.identity(name)
        repo = self.base / name
        if repo.resolve() != repo or Path(g(repo, "rev-parse", "--show-toplevel")).resolve() != repo or \
                inspector.operation_state(repo) != "-":
            return None
        if plan.get("expected_identity") != digest(identity) or \
                plan.get("expected_remote") != remote_identity(repo, identity["remote"])[1] or \
                plan.get("expected_head") != g(repo, "rev-parse", "HEAD") or \
                plan.get("expected_branch") != g(repo, "branch", "--show-current") or \
                plan.get("expected_worktree") != worktree_fingerprint(repo):
            return None
        return dict(plan)

    def bound(self, name: str, identity: dict, remote_key: str) -> Path:
        manifest.require_operation_lock(self.root, self.slot, self.token)
        if self.identity(name) != identity:
            raise SubmitError("manifest identity changed during execution")
        repo = self.base / name
        if repo.resolve() != repo or Path(g(repo, "rev-parse", "--show-toplevel")).resolve() != repo:
            raise SubmitError("repository path changed")
        if remote_identity(repo, identity["remote"])[1] != remote_key:
            raise SubmitError("remote mapping changed")
        if inspector.operation_state(repo) != "-":
            raise SubmitError("unfinished Git operation")
        return repo

    def commit(self, repo: Path, plan: dict) -> str:
        if g(repo, "rev-parse", "HEAD") != plan["expected_head"]:
            raise SubmitError("reviewed HEAD changed before commit")
        reviewed_content, reviewed_snapshot = worktree_snapshot(repo)
        if plan.get("expected_worktree") != reviewed_snapshot:
            raise SubmitError("reviewed worktree/index content changed; refresh preflight and review")
        paths = plan.get("paths", [])
        if not isinstance(paths, list) or any(not isinstance(p, str) or not p or
                PurePosixPath(p).is_absolute() or ".." in PurePosixPath(p).parts or
                PurePosixPath(p).as_posix() != p or p == "." for p in paths):
            raise SubmitError("paths must be exact repository-relative file names, not directories or patterns")
        selected = set(paths)
        for p in paths:
            if (repo / p).is_dir() and not (repo / p).is_symlink():
                raise SubmitError("directory path selections are not allowed")
        staged = changed_paths(repo, staged=True)
        if not staged <= selected:
            raise SubmitError("unrelated staged paths; refusing to include them in the task commit")
        changed = changed_paths(repo)
        if not changed <= selected:
            raise SubmitError("unselected changes remain; select exact reviewed files before execution")
        if not changed:
            return plan["expected_head"]
        message = korean(plan.get("commit_message"), "commit_message")
        old_head = plan["expected_head"]
        tracked = set(verifier.git(repo, "ls-files", "-z").decode(errors="surrogateescape").split("\0"))
        # An already-staged rename/deletion has removed the old name from the index.
        to_stage = [p for p in paths if p in tracked or (repo / p).exists() or (repo / p).is_symlink()]
        if to_stage:
            g(repo, "add", "-A", "--", *[":(literal)" + p for p in to_stage])
        if not changed_paths(repo, staged=True) <= selected:
            raise SubmitError("staging changed concurrently")
        if worktree_snapshot(repo)[0] != reviewed_content:
            raise SubmitError("reviewed file content changed while staging; no commit was created")
        g(repo, "diff", "--exit-code", "--no-ext-diff", "--no-textconv", "--",
          *[":(literal)" + p for p in paths])
        expected_tree = g(repo, "write-tree")
        g(repo, "diff", "--cached", "--check")
        if g(repo, "rev-parse", "HEAD") != old_head:
            raise SubmitError("HEAD changed while staging")
        g(repo, "commit", "-m", message)
        committed = g(repo, "rev-parse", "HEAD")
        if g(repo, "show", "-s", "--format=%P", committed) != old_head or g(repo, "rev-parse", committed + "^{tree}") != expected_tree:
            raise SubmitError("commit hook changed the reviewed tree/history; inspect local commit before publishing")
        clean(repo)
        return committed

    def fetch_source(self, repo: Path, identity: dict) -> str:
        branch, remote = identity["source_branch"], identity["remote"]
        g(repo, "fetch", "--no-tags", remote, f"refs/heads/{branch}:refs/remotes/{remote}/{branch}")
        source = g(repo, "rev-parse", f"refs/remotes/{remote}/{branch}^{{commit}}")
        g(repo, "merge-base", "--is-ancestor", identity["branch_base_sha"], source)
        if identity["pr"]:
            repository = remote_identity(repo, remote)[0]
            pr = self.api.view(repository, verifier.pr_number(identity["pr"], repository))
            verifier.check_identity(pr, repository, branch, identity["submission"]["head_branch"],
                                    identity["submission"]["observed_head_sha"], state="MERGED")
            g(repo, "merge-base", "--is-ancestor", pr["merge_commit_sha"], source)
        return source

    def rotate(self, name: str, source: str, subject: str | None, expected_head: str,
               *, before_change=None) -> str:
        identity, repo = self.identity(name), self.base / name
        clean(repo)
        if g(repo, "rev-parse", "HEAD") != expected_head:
            raise SubmitError("HEAD changed before rotation")
        rotation = identity.get("rotation")
        if not rotation:
            unrotated = identity["pr"] and identity["branch"] == identity["submission"]["head_branch"]
            boundary = identity["submission"]["continuation_boundary_sha"] if unrotated else identity["branch_base_sha"]
            if not unrotated and source == boundary:
                return expected_head
            a = generation.analyze(argparse.Namespace(repo=repo, from_head=expected_head,
                boundary=boundary, source=source, branch_family=identity["branch_family"],
                target_generation=identity["branch_generation"] + 1))
            transfer = generation.normalize_transfer_subject(subject) if a["source_tree_sha"] != a["result_tree_sha"] else None
            temporary = manifest.generation_worktree_path(self.root, self.slot, name, a["target_branch"])
            self.mutate(lambda data: manifest.begin_rotations(data, self.slot, [(name, identity["branch"],
                identity["branch_generation"], a["from_head_sha"], boundary, source, a["source_tree_sha"],
                a["target_branch"], a["target_branch_generation"], a["result_tree_sha"], transfer, str(temporary))], self.root))
            rotation = self.identity(name)["rotation"]
        if before_change is not None:
            before_change(rotation["result_tree_sha"])
        args = argparse.Namespace(repo=repo, source=rotation["source_sha"], source_tree=rotation["source_tree_sha"],
            result_tree=rotation["result_tree_sha"], branch_family=identity["branch_family"],
            target_generation=rotation["target_branch_generation"], message=rotation["transfer_subject"],
            temporary_worktree=Path(rotation["temporary_worktree"]), workspace_root=self.root,
            slot=self.slot, repository=name)
        if rotation["phase"] == "planned":
            if g(repo, "branch", "--show-current") != rotation["from_branch"] or g(repo, "rev-parse", "HEAD") != rotation["from_head_sha"]:
                raise SubmitError("rotation source moved")
            created = generation.create(args)
            self.mutate(lambda data: manifest.advance_rotation(data, self.slot, name, "planned", "branch-created", created["target_sha"]))
            rotation = self.identity(name)["rotation"]
        if rotation["phase"] == "branch-created":
            verified = generation.verify(args)
            if "target_head_sha" not in rotation:
                self.mutate(lambda data: manifest.advance_rotation(data, self.slot, name,
                    "planned", "branch-created", verified["target_sha"]))
                rotation = self.identity(name)["rotation"]
            branch = g(repo, "branch", "--show-current")
            if branch == rotation["from_branch"] and g(repo, "rev-parse", "HEAD") == rotation["from_head_sha"]:
                g(repo, "switch", "--no-overwrite-ignore", "--", rotation["target_branch"])
            elif branch != rotation["target_branch"]:
                raise SubmitError("rotation checkout moved")
            clean(repo)
            if g(repo, "rev-parse", "HEAD") != rotation["target_head_sha"]:
                raise SubmitError("rotation target moved")
            self.mutate(lambda data: manifest.advance_rotation(data, self.slot, name, "branch-created", "switched"))
            rotation = self.identity(name)["rotation"]
        if rotation["phase"] == "switched":
            retirement.retire(argparse.Namespace(root=self.root, slot=self.slot, repository=name,
                                                 token=self.token, execute=True))
            rotation = self.identity(name)["rotation"]
        self.mutate(lambda data: manifest.complete_rotation(data, self.slot, name,
            rotation["target_branch"], rotation["target_branch_generation"], rotation["source_sha"],
            rotation["target_head_sha"], rotation["result_tree_sha"]))
        return rotation["target_head_sha"]

    def rotate_for_submit(self, name: str, source: str, subject: str | None, expected_head: str) -> str:
        def mark_changed(tree):
            if tree != self.validation_trees[name]:
                self.receipts.setdefault(name, {})["revalidation_required"] = True
                self.save()  # Persist BEFORE branch creation/switch, including crash recovery.
        return self.rotate(name, source, subject, expected_head, before_change=mark_changed)

    def no_diff(self, name: str, branch: str) -> dict:
        # Nothing remains to publish; do not make a future independent development
        # change inherit a recheck requirement from this now-empty submission.
        self.receipts.setdefault(name, {}).pop("revalidation_required", None)
        self.save()
        return {"state": "NO_DIFF", "branch": branch}

    def validate(self, name: str, repo: Path, source: str, plan: dict, expected_head: str) -> None:
        clean(repo)
        if g(repo, "rev-parse", "HEAD") != expected_head:
            raise SubmitError("HEAD changed before validation")
        tree = g(repo, "rev-parse", expected_head + "^{tree}")
        mode = plan.get("validation_mode", "if-changed")
        if mode not in {"if-changed", "always"}:
            raise SubmitError("validation_mode must be if-changed or always")
        receipt = self.receipts.setdefault(name, {})
        changed = self.validation_trees.get(name) != tree
        required = changed or bool(receipt.get("revalidation_required"))
        item = self.timings[-1]
        item.update(mode=mode, tree_changed=changed)
        if not self.test and mode != "always" and not required:
            item.update(executed=False, reason="development validation assumed; submitted tree unchanged")
            return
        # Keep an interrupted/failed required recheck from becoming "already tested"
        # merely because the next attempt starts on the rotated commit.
        receipt["revalidation_required"] = True
        self.save()
        commands = plan.get("validation", [])
        if not isinstance(commands, list) or any(not isinstance(cmd, list) or not cmd or
                any(not isinstance(arg, str) or not arg for arg in cmd) for cmd in commands):
            raise SubmitError("validation must be a list of argv arrays (no shell strings)")
        if not commands:
            item.update(executed=False, reason="changed tree or explicit test request needs validation commands")
            raise RevalidationRequired("REVALIDATION_REQUIRED: provide focused validation commands for the changed tree "
                                       "(or the explicit test request); no PR was published")
        timeout = plan.get("validation_timeout_seconds", 900)
        if type(timeout) is not int or not 1 <= timeout <= 3600:
            raise SubmitError("validation_timeout_seconds must be between 1 and 3600")
        item.update(executed=True, reason="explicit request" if self.test or mode == "always" else "submitted tree changed")
        for command in commands:
            verifier.command(command, cwd=repo, timeout=timeout)
        clean(repo)
        if g(repo, "rev-parse", "HEAD") != expected_head:
            raise SubmitError("validation changed its input basis")
        self.validation_trees[name] = tree
        receipt.pop("revalidation_required", None)
        self.save()

    def check_publication(self, name: str, pending: dict, *, recovering: bool,
                          source_sha: str | None = None) -> tuple[Path, dict[str, str]]:
        identity = self.identity(name)
        if identity != pending["identity"]:
            raise SubmitError("publication manifest basis changed; preserve receipt for exact recovery")
        repo = self.bound(name, identity, pending["remote_key"])
        if g(repo, "branch", "--show-current") != pending["branch"]:
            raise SubmitError("publication branch changed")
        if recovering:
            # Continuation B may already exist; never stage/commit it while recovering A.
            g(repo, "merge-base", "--is-ancestor", pending["head_sha"], g(repo, "rev-parse", "HEAD"))
        else:
            clean(repo)
            if g(repo, "rev-parse", "HEAD") != pending["head_sha"]:
                raise SubmitError("publication HEAD changed")
        heads = remote_heads(repo, identity["remote"], identity["source_branch"], pending["branch"])
        if heads.get(identity["source_branch"]) != (source_sha or pending["source_sha"]):
            raise SubmitError("source advanced at publication boundary; pending submission preserved, no automatic rewrite")
        if heads.get(pending["branch"]) not in {None, pending["head_sha"]}:
            raise SubmitError("remote head differs; no overwrite or force push")
        return repo, heads

    def publish(self, name: str, pending: dict, *, recovering: bool = False) -> dict:
        repository, branch, identity = pending["repository"], pending["branch"], pending["identity"]
        matches = self.api.find(repository, branch)
        if len(matches) > 1:
            raise SubmitError("multiple PRs for the publication head")
        if matches:
            pr = matches[0]
            verifier.check_identity(pr, repository, identity["source_branch"], branch,
                                    pending["head_sha"], source_sha=pending["source_sha"])
        else:
            # Reuse the source+head query from the immediately-before-push guard.
            repo, heads = self.check_publication(name, pending, recovering=recovering)
            if not heads.get(branch):
                # Empty expected ref is a create-only lease; even a raced ancestor is not overwritten.
                with self.phase("push"):
                    g(repo, "push", "--force-with-lease=refs/heads/" + branch + ":", identity["remote"],
                      pending["head_sha"] + ":refs/heads/" + branch)
            self.check_publication(name, pending, recovering=recovering)
            # A concurrent ready PR may have appeared after push; never blindly create twice.
            matches = self.api.find(repository, branch)
            if len(matches) > 1:
                raise SubmitError("multiple PRs appeared after push")
            if matches:
                pr = matches[0]
            else:
                with self.phase("create_pr"):
                    url = self.api.create(repository, identity["source_branch"], branch, pending["title"], pending["body"])
                pr = {"number": verifier.pr_number(url, repository)}
        pending["pr_url"] = f"https://github.com/{repository}/pull/{pr['number']}"
        self.save()  # Creation succeeded even when the following verification fails.
        repo = self.base / name
        with self.phase("verify_pr"):
            result = verifier.verify(repo, repository, pr["number"], identity["source_branch"], branch,
                                     pending["source_sha"], pending["head_sha"], self.api)
        self.check_publication(name, pending, recovering=recovering)
        return self.record_publication(name, pending, result)

    def record_publication(self, name: str, pending: dict, result: dict) -> dict:
        identity, branch = pending["identity"], pending["branch"]
        with self.phase("record"):
            pending["verified_pr"] = result
            self.save()
            def record(data):
                if data["slots"][self.slot]["repositories"][name] != identity:
                    raise SubmitError("manifest changed immediately before record")
                manifest.record_batch(data, self.slot, [(name, identity["generation"], result["pr"], branch,
                                                        pending["head_sha"], pending["head_sha"])])
            self.mutate(record)
            self.receipts[name].pop("pending", None)
            self.save()
        return {"state": "RECORDED" if result.get("pr_state") == "MERGED" else "SUBMITTED", **result}

    def recover_merged(self, name: str, pending: dict) -> dict | None:
        """Record exactly the saved submission if the user merged it before recovery.

        No branch recreation, continuation commit, test run, or history rewrite.
        GitHub's merge evidence and the saved pre-merge patch are both required.
        """
        matches = self.api.find(pending["repository"], pending["branch"])
        if len(matches) > 1:
            raise SubmitError("multiple PRs for the pending publication")
        if not matches or not matches[0].get("merged_at"):
            return None
        pr, identity = matches[0], pending["identity"]
        verifier.check_identity(pr, pending["repository"], identity["source_branch"], pending["branch"],
                                pending["head_sha"], state="MERGED")
        pending["pr_url"] = pr["html_url"]
        self.save()
        repo = self.bound(name, identity, pending["remote_key"])
        source = self.fetch_source(repo, identity)
        g(repo, "merge-base", "--is-ancestor", pending["source_sha"], source)
        g(repo, "merge-base", "--is-ancestor", pr["merge_commit_sha"], source)
        basis = pr["base"]["sha"]
        if verifier.local_diff(repo, pending["source_sha"], pending["head_sha"]) != \
                verifier.local_diff(repo, basis, pending["head_sha"]):
            raise SubmitError("merged recovery changed the saved submission patch")
        result = verifier.verify(repo, pending["repository"], pr["number"], identity["source_branch"],
                                 pending["branch"], basis, pending["head_sha"], self.api, state="MERGED")
        self.check_publication(name, pending, recovering=True, source_sha=source)
        return self.record_publication(name, pending, result)

    def recover_source(self, name: str, pending: dict) -> bool:
        """Return True only when an unpublished intent can safely be replanned without B."""
        identity = pending["identity"]
        repo = self.bound(name, identity, pending["remote_key"])
        heads = remote_heads(repo, identity["remote"], identity["source_branch"], pending["branch"])
        if heads.get(identity["source_branch"]) == pending["source_sha"]:
            return False
        with self.phase("recover_source"):
            source = self.fetch_source(repo, identity)
            g(repo, "merge-base", "--is-ancestor", pending["source_sha"], source)
            matches = self.api.find(pending["repository"], pending["branch"])
            if heads.get(pending["branch"]) is None and not matches:
                clean(repo)
                if g(repo, "branch", "--show-current") != pending["branch"] or g(repo, "rev-parse", "HEAD") != pending["head_sha"]:
                    raise SubmitError("unpublished recovery has new continuation; do not mix it with the saved submission")
                self.receipts[name].pop("pending")
                self.save()
                return True
            if heads.get(pending["branch"]) != pending["head_sha"] or len(matches) > 1:
                raise SubmitError("publication recovery remote identity is ambiguous")
            # Published A is immutable. Normal source advancement may be adopted only if
            # its exact three-dot patch remains unchanged; never validate/commit dirty B.
            old_patch = verifier.local_diff(repo, pending["source_sha"], pending["head_sha"])
            new_patch = verifier.local_diff(repo, source, pending["head_sha"])
            if old_patch != new_patch:
                raise SubmitError("source advancement changed the published patch; explicit recovery needed")
            if matches:
                verifier.check_identity(matches[0], pending["repository"], identity["source_branch"],
                                        pending["branch"], pending["head_sha"], source_sha=source)
            pending.setdefault("original_source_sha", pending["source_sha"])
            pending["source_sha"] = source
            self.save()
            return False

    def execute_one(self, name: str, plan: dict, row: dict) -> dict:
        identity = self.identity(name)
        pending = self.receipts.get(name, {}).get("pending")
        if pending:
            # Manifest record may have succeeded before receipt clearing failed.
            submission = identity.get("submission") or {}
            if identity["generation"] == pending["identity"]["generation"] + 1 and \
                    submission.get("head_branch") == pending["branch"] and \
                    submission.get("continuation_boundary_sha") == pending["head_sha"] and \
                    submission.get("observed_head_sha") == pending["head_sha"]:
                result = pending.get("verified_pr")
                if not result or result["pr"] != identity["pr"] or result["head_sha"] != pending["head_sha"]:
                    raise SubmitError("recorded recovery lacks its exact verified publication receipt")
                # The manifest write already committed this success. PR/source may have
                # legitimately advanced since; clearing the stale receipt is not publication.
                self.receipts[name].pop("pending")
                self.save()
                return {"state": "RECORDED", **result}
            with self.phase("recover_publication"):
                merged = self.recover_merged(name, pending)
                if merged:
                    return merged
                if self.recover_source(name, pending):
                    # No remote publication and no continuation: the saved reviewed commit
                    # remains the sole work input. Replan it once using the normal path.
                    overrides = {key: plan[key] for key in ("validation", "validation_mode", "validation_timeout_seconds")
                                 if key in plan}
                    refreshed = {**pending.get("inputs", plan), **overrides, "expected_head": pending["head_sha"],
                        "expected_worktree": worktree_fingerprint(self.base / name),
                        "expected_branch": pending["branch"], "expected_generation": identity["generation"],
                        "expected_remote": pending["remote_key"], "expected_identity": digest(identity)}
                    return self.execute_one(name, refreshed, row)
                return self.publish(name, pending, recovering=True)
        if row["state"] == "BLOCKED":
            raise SubmitError(row["error"])
        plan = review.expand_prose(plan)
        repo = self.bound(name, identity, row["remote_key"])
        if plan.get("expected_head") != g(repo, "rev-parse", "HEAD") or \
                plan.get("expected_generation") != identity["generation"] or \
                plan.get("expected_remote") != row["remote_key"] or \
                plan.get("expected_identity") != digest(identity) or \
                plan.get("expected_branch") != g(repo, "branch", "--show-current"):
            raise SubmitError("reviewed plan snapshot no longer matches HEAD/branch/generation; refresh preflight")
        state = self.classify(name, identity, row["github_repository"])
        if plan.get("prepared_state") == "OPEN" and state != "OPEN":
            raise SubmitError("waiting PR changed state; prepare and review the continuation before submitting it")
        plan["prepared_state"] = state
        expected_head = plan["expected_head"]
        reviewed_head = identity.get("rotation", {}).get("from_head_sha", expected_head)
        self.validation_trees[name] = g(repo, "rev-parse", reviewed_head + "^{tree}")
        if identity.get("rotation"):
            with self.phase("resume_rotation"):
                expected_head = self.rotate_for_submit(name, identity["rotation"]["source_sha"], None, expected_head)
            if changed_paths(repo):
                raise SubmitError("rotation recovery cannot consume new dirty work")
        else:
            with self.phase("commit"):
                expected_head = self.commit(repo, plan)
            reviewed_head = expected_head
        self.checkpoint(name, plan, expected_head)
        self.validation_trees[name] = g(repo, "rev-parse", reviewed_head + "^{tree}")
        if state == "OPEN":
            return {"state": "WAITING", "pr": identity["pr"], "pushed": False}
        with self.phase("fetch_and_rotate"):
            identity = self.identity(name)
            source = self.fetch_source(repo, identity)
            expected_head = self.rotate_for_submit(name, source, plan.get("transfer_subject"), expected_head)
        self.checkpoint(name, plan, expected_head)
        identity = self.identity(name)
        if not verifier.git(repo, "diff", "--name-only", f"{source}...HEAD", "--"):
            return self.no_diff(name, identity["branch"])
        title, body = korean(plan.get("pr_title"), "pr_title"), korean(plan.get("pr_body"), "pr_body")
        with self.phase("validation"):
            self.validate(name, repo, source, plan, expected_head)
        # One compatible source advance can be absorbed before any publication intent is saved.
        if remote_heads(repo, identity["remote"], identity["source_branch"]).get(identity["source_branch"]) != source:
            with self.phase("source_replan"):
                refreshed = self.fetch_source(repo, identity)
                g(repo, "merge-base", "--is-ancestor", source, refreshed)
                expected_head = self.rotate_for_submit(name, refreshed, plan.get("transfer_subject"), expected_head)
                source, identity = refreshed, self.identity(name)
            self.checkpoint(name, plan, expected_head)
            if not verifier.git(repo, "diff", "--name-only", f"{source}...HEAD", "--"):
                return self.no_diff(name, identity["branch"])
            with self.phase("validation"):
                self.validate(name, repo, source, plan, expected_head)
        pending = {"identity": identity, "repository": row["github_repository"],
                   "remote_key": row["remote_key"], "branch": identity["branch"],
                   "head_sha": expected_head, "source_sha": source,
                   "title": title, "body": review.publication_body(plan, self.timings), "inputs": plan}
        # Validate before making a durable pending entry. Once pending exists, never add B.
        self.check_publication(name, pending, recovering=False)
        self.receipts.setdefault(name, {})["pending"] = pending
        self.save()
        with self.phase("publication"):
            return self.publish(name, pending)

    def execute(self, plans: dict[str, dict]) -> dict:
        bindings = manifest.require_slot(manifest.load_manifest(self.path), self.slot)["repositories"]
        if not isinstance(plans, dict) or not plans or set(plans) - set(bindings):
            raise SubmitError("plan must select one or more exact manifest repositories")
        # All bundle paths/occupancy still get checked; expensive remote queries run
        # only for selected repositories at their actual mutation boundaries.
        preflight = self.preflight(repositories=set(plans), local_only=True, content_snapshot=False)
        rows = {row["repository"]: row for row in preflight["repositories"]}
        results = []
        manifest.acquire_operation_lock(self.root, self.slot, self.token)
        try:
            self.load_receipts()
            for name, plan in plans.items():
                self.timings = []
                started = time.monotonic()
                receipt = self.receipts.setdefault(name, {})
                previous_failed = receipt.get("last_result", {}).get("state") == "FAILED"
                receipt["attempts"] = receipt.get("attempts", 0) + 1 if previous_failed or receipt.get("pending") else 1
                try:
                    self.save()
                    if name not in rows:
                        raise SubmitError("manifest repository is missing/mismatched in bundle inspection")
                    result = self.execute_one(name, plan, rows[name])
                except (RuntimeError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
                    failed_phase = next((p["phase"] for p in reversed(self.timings) if not p["ok"]), None)
                    result = {"state": "FAILED", "error": str(exc)[-2000:],
                              "recovery_pending": bool(receipt.get("pending")),
                              "pr": (receipt.get("pending") or {}).get("pr_url"),
                              "error_code": "REVALIDATION_REQUIRED" if isinstance(exc, RevalidationRequired) else
                                            "VERIFICATION_FAILED" if failed_phase == "verify_pr" else
                                            "VALIDATION_FAILED" if failed_phase == "validation" else "SUBMIT_FAILED",
                              "failed_phase": failed_phase}
                    try:
                        resume = (receipt["pending"].get("inputs", plan) if receipt.get("pending")
                                  else self.resume_plan(name))
                        if resume is not None:
                            result["resume_plan_path"] = str(self.write_plan({name: resume}))
                            result["content_review_required"] = False
                            if receipt.get("pending"):
                                result["required_fields"] = []
                            else:
                                identity = self.identity(name)
                                repo = self.base / name
                                boundary = identity["branch_base_sha"]
                                if identity["pr"] and identity["branch"] == identity["submission"]["head_branch"]:
                                    boundary = identity["submission"]["continuation_boundary_sha"]
                                committed = g(repo, "diff", "--name-only", boundary, resume["expected_head"], "--").splitlines()
                                result["required_fields"] = review.required_fields(resume["prepared_state"], [], committed, resume,
                                    revalidation=bool(receipt.get("revalidation_required")) or self.test or
                                        resume.get("validation_mode") == "always",
                                    rotation_required=failed_phase in {"fetch_and_rotate", "source_replan", "resume_rotation"})
                        else:
                            result["content_review_required"] = True
                    except (RuntimeError, OSError, ValueError, KeyError, subprocess.SubprocessError):
                        result["content_review_required"] = True
                result.update(repository=name, seconds=round(time.monotonic() - started, 3),
                              attempts=receipt["attempts"], retry_count=receipt["attempts"] - 1, phases=self.timings)
                receipt["last_result"] = result
                self.save()
                results.append(result)
        finally:
            manifest.release_operation_lock(self.root, self.slot, self.token)
        return {"mode": "execute", "slot": self.slot, "repositories": results,
                "errors": preflight["errors"], "preflight_seconds": preflight["seconds"],
                "receipt": str(self.receipt_path)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--plan", type=Path, help="Reviewed JSON object with repositories mapping")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--prepare", action="store_true", help="Create a new snapshot plan; never overwrite an existing plan")
    parser.add_argument("--repository", action="append", help="Limit remote preflight to exact manifest repository names")
    parser.add_argument("--test", action="store_true", help="Explicitly run the plan's validation commands during submit")
    args = parser.parse_args()
    try:
        runner = Runner(args.root, args.bundle, test=args.test)
        if args.prepare and args.execute:
            raise SubmitError("--prepare and --execute are separate operations")
        selected = set(args.repository) if args.repository else None
        if selected and selected - set(manifest.require_slot(manifest.load_manifest(runner.path), runner.slot)["repositories"]):
            raise SubmitError("--repository must name exact manifest repositories")
        if (args.test or args.plan) and not args.execute:
            raise SubmitError("--test and --plan require --execute")
        if args.execute:
            if not args.plan:
                raise SubmitError("--execute requires --plan")
            plans = json.loads(args.plan.read_text())["repositories"]
            if selected and set(plans) != selected:
                raise SubmitError("--repository must match the reviewed plan selection")
            result = runner.execute(plans)
        elif args.prepare:
            result = runner.prepare(selected)
        else:
            result = runner.preflight(repositories=selected, candidates_only=True)
        print(json.dumps(result, ensure_ascii=False))
        return int(bool(result["errors"]) or any(row["state"] in {"FAILED", "BLOCKED"} for row in result["repositories"]))
    except (RuntimeError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
