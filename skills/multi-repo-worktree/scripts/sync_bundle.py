#!/usr/bin/env python3
"""Bundle-scoped sync preflight and journaled rotation/merged-ref cleanup.

Read-only by default. --execute requires a reviewed snapshot plan. No dirty task
commits, PR creation, branch publication, bulk branch pruning, or original-worktree pull.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
from typing import Any

import cleanup_merged_branch as cleanup
import generation_conflicts as conflicts
import slot_manifest as manifest
import submit_bundle as shared
import verify_pr as verifier


class SyncError(RuntimeError):
    pass


ERRORS = (RuntimeError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError)


class Runner:
    def __init__(self, root: Path, bundle: str, api: verifier.GitHub | None = None):
        # Reuse inspection, identity, timing, fetch, rotation, and required checks.
        # Never call submit.execute/commit/publish or change pending publication.
        # Conflict rotation may persist shared required-validation flags only.
        self.context = shared.Runner(root, bundle, api=api)
        self.report_path = self.context.root / ".multi-repo-worktree" / "sync-runs" / (shared.digest(self.context.slot) + ".json")

    def preflight(self) -> dict:
        result = self.context.preflight(content_snapshot=False, skip_dirty=True)
        result["operation"] = "sync"
        for row in result["repositories"]:
            row["action"] = "BLOCKED"
            if row["state"] == "DIRTY":
                row["action"] = "SKIP_DIRTY"
                continue
            if row["state"] == "RECOVER":
                row["action"] = "RECOVER_SUBMIT_FIRST"
                continue
            if row["state"] == "BLOCKED":
                continue
            try:
                identity = self.context.identity(row["repository"])
                self.check_remote_head(identity, row["branch"], row["remote_head_sha"], row["state"])
                unrotated = identity["pr"] and identity["branch"] == identity["submission"]["head_branch"]
                advanced = row["source_sha"] != identity["branch_base_sha"]
                row["action"] = ("WAIT" if row["state"] == "OPEN" else
                    "RESUME_ROTATION" if identity.get("rotation") else
                    "ROTATE_AND_CLEAN" if unrotated else
                    "CLEAN_AND_ROTATE" if row["state"] == "MERGED" and advanced else
                    "CLEAN" if row["state"] == "MERGED" else "ROTATE" if advanced else "NOOP")
                row["cleanup_candidates"] = [] if row["state"] == "OPEN" else self.cleanup_candidates(identity)
                row["branch_base_sha"] = identity["branch_base_sha"]
                if row["state"] != "OPEN":
                    rotation = identity.get("rotation")
                    boundary = (rotation["boundary_sha"] if rotation else
                        identity["submission"]["continuation_boundary_sha"] if unrotated else identity["branch_base_sha"])
                    review_head = rotation["from_head_sha"] if rotation else row["head_sha"]
                    paths = verifier.git(Path(row["path"]), "diff", "--name-only", "-z", boundary, review_head, "--")
                    row["transfer_review"] = {"boundary_sha": boundary, "head_sha": review_head,
                        "changed_paths": [path for path in paths.decode(errors="surrogateescape").split("\0") if path]}
                row["plan"] = {"expected_head": row["head_sha"], "expected_branch": row["branch"],
                    "expected_generation": row["generation"], "expected_identity": row["identity_fingerprint"],
                    "expected_remote": row["remote_key"], "expected_source": row["source_sha"],
                    "transfer_subject": identity.get("rotation", {}).get("transfer_subject")}
            except ERRORS as exc:
                row.update(state="BLOCKED", action="BLOCKED", error=str(exc))
        return result

    @staticmethod
    def cleanup_candidates(identity: dict) -> list[dict]:
        records = [{"pr": item["pr"], "branch": item["submission"]["head_branch"], "pending": True}
                   for item in identity.get("pending_remote_cleanups", [])]
        if identity["pr"]:
            records.append({"pr": identity["pr"], "branch": identity["submission"]["head_branch"], "pending": False})
        return records

    @staticmethod
    def check_remote_head(identity: dict, branch: str, remote_head: str | None, state: str) -> None:
        submission = identity.get("submission") or {}
        if branch == submission.get("head_branch"):
            expected = submission["observed_head_sha"]
            if remote_head != expected and (state == "OPEN" or remote_head is not None):
                raise SyncError("recorded PR remote head changed or is missing; no rotation/cleanup")
        elif remote_head is not None:
            raise SyncError("unrecorded remote active branch exists; recover submit before sync")

    def guard(self, name: str, identity: dict, plan: dict, head: str, branch: str,
              *, source: str | None = None) -> Path:
        ctx = self.context
        repo = ctx.bound(name, identity, plan["expected_remote"])
        shared.clean(repo)
        if shared.g(repo, "rev-parse", "HEAD") != head or shared.g(repo, "branch", "--show-current") != branch:
            raise SyncError("reviewed sync HEAD/branch changed")
        locations = cleanup.checked_out_branches(repo).get(branch, [])
        if any(Path(path).resolve() != repo for path in locations):
            raise SyncError("active branch is occupied by another worktree")
        if source is not None:
            refs = shared.remote_heads(repo, identity["remote"], identity["source_branch"])
            if refs.get(identity["source_branch"]) != source:
                raise SyncError("remote source advanced during sync; preserve progress and refresh preflight")
            if shared.g(repo, "rev-parse", f"refs/remotes/{identity['remote']}/{identity['source_branch']}") != source:
                raise SyncError("fetched source ref moved during sync")
        return repo

    def fetch(self, name: str, identity: dict, plan: dict, head: str, branch: str, state: str) -> str:
        ctx = self.context
        repo = self.guard(name, identity, plan, head, branch)
        if state == "OPEN":
            # Shared submit.fetch_source intentionally requires MERGED for a recorded PR.
            source_branch, remote = identity["source_branch"], identity["remote"]
            shared.g(repo, "fetch", "--no-tags", remote, f"refs/heads/{source_branch}:refs/remotes/{remote}/{source_branch}")
            source = shared.g(repo, "rev-parse", f"refs/remotes/{remote}/{source_branch}^{{commit}}")
        else:
            source = ctx.fetch_source(repo, identity)
        shared.g(repo, "merge-base", "--is-ancestor", plan["expected_source"], source)
        self.guard(name, identity, plan, head, branch, source=source)
        return source

    def clean_merged(self, name: str, plan: dict, source: str, head: str, branch: str,
                     progress: dict) -> None:
        ctx = self.context
        candidates = self.cleanup_candidates(ctx.identity(name))
        for candidate in candidates:
            identity = ctx.identity(name)
            if candidate["branch"] == identity["branch"]:
                raise SyncError("merged cleanup still targets active branch; rotation must finish first")
            repo = self.guard(name, identity, plan, head, branch, source=source)
            repository = shared.remote_identity(repo, identity["remote"])[0]
            number = verifier.pr_number(candidate["pr"], repository)
            pr = ctx.api.view(repository, number)
            records = list(identity.get("pending_remote_cleanups", []))
            if identity["pr"]:
                records.append({"pr": identity["pr"], "submission": identity["submission"]})
            matches = [item for item in records if item["pr"] == candidate["pr"]]
            if len(matches) != 1:
                raise SyncError("cleanup must resolve to one exact manifest record")
            submission = matches[0]["submission"]
            verifier.check_identity(pr, repository, identity["source_branch"], submission["head_branch"],
                                    submission["observed_head_sha"], state="MERGED")
            self.guard(name, identity, plan, head, branch, source=source)
            with ctx.phase("cleanup_merged"):
                args = argparse.Namespace(root=ctx.root, slot=ctx.slot, repository=name, token=ctx.token,
                    execute=True, github_number=number, github_url=pr["html_url"], github_state="MERGED",
                    github_is_draft="false", github_base=pr["base"]["ref"], github_head=pr["head"]["ref"],
                    github_head_sha=pr["head"]["sha"], github_merged_at=pr["merged_at"],
                    github_merge_commit=pr["merge_commit_sha"])
                result = cleanup.cleanup(args, before_delete=lambda: self.guard(
                    name, identity, plan, head, branch, source=source))
            progress["cleanups"].append(result)
            self.guard(name, ctx.identity(name), plan, head, branch, source=source)

    def execute_one(self, name: str, plan: dict, row: dict, progress: dict) -> dict:
        ctx = self.context
        if row["state"] == "DIRTY":
            return {"state": "SKIPPED", "reason": "dirty repository left unchanged", "changed_paths": row["changed_paths"]}
        if row["state"] in {"BLOCKED", "RECOVER"}:
            raise SyncError(row.get("error", "pending publication: run submit recovery first; sync left unchanged"))
        identity = ctx.identity(name)
        if not isinstance(plan, dict) or plan.get("expected_identity") != shared.digest(identity) or \
                plan.get("expected_generation") != identity["generation"] or \
                plan.get("expected_remote") != row["remote_key"] or \
                plan.get("expected_branch") != row["branch"] or plan.get("expected_head") != row["head_sha"] or \
                not manifest.is_object_id(plan.get("expected_source")):
            raise SyncError("reviewed sync snapshot changed; refresh preflight")
        if ctx.receipts.get(name, {}).get("pending"):
            raise SyncError("pending submit publication must be recovered before sync")
        head, branch = plan["expected_head"], plan["expected_branch"]
        repo = self.guard(name, identity, plan, head, branch)
        state = ctx.classify(name, identity, row["github_repository"])
        if state not in {"NONE", "OPEN", "MERGED"}:
            raise SyncError("repository no longer has a supported sync state")
        remote_head = shared.remote_heads(repo, identity["remote"], branch).get(branch)
        self.check_remote_head(identity, branch, remote_head, state)
        with ctx.phase("fetch"):
            source = self.fetch(name, identity, plan, head, branch, state)
        progress["source_sha"] = source
        if state == "OPEN":
            if ctx.classify(name, identity, row["github_repository"]) != "OPEN":
                raise SyncError("PR changed while waiting; refresh preflight")
            remote_head = shared.remote_heads(repo, identity["remote"], branch).get(branch)
            self.check_remote_head(identity, branch, remote_head, state)
            return {"state": "WAITING", "pr": identity["pr"], "branch": branch}

        if identity.get("rotation"):
            shared.g(repo, "merge-base", "--is-ancestor", identity["rotation"]["source_sha"], source)
            with ctx.phase("resume_rotation"):
                head = ctx.rotate(name, identity["rotation"]["source_sha"], None, head)
            identity = ctx.identity(name)
            branch = identity["branch"]
            progress["rotations"].append({"branch": branch, "head_sha": head, "resumed": True})
            self.guard(name, identity, plan, head, branch, source=source)

        self.validate_conflict(name, plan, source, head)

        unrotated = identity["pr"] and identity["branch"] == identity["submission"]["head_branch"]
        # Complete cleanup BEFORE replacing an already-completed rotation receipt.
        if not unrotated:
            self.clean_merged(name, plan, source, head, branch, progress)
            identity = ctx.identity(name)
        if unrotated or source != identity["branch_base_sha"]:
            self.guard(name, identity, plan, head, branch, source=source)
            with ctx.phase("rotate"):
                head = ctx.rotate(name, source, plan.get("transfer_subject"), head)
            identity = ctx.identity(name)
            branch = identity["branch"]
            progress["rotations"].append({"branch": branch, "head_sha": head, "resumed": False})
            self.guard(name, identity, plan, head, branch, source=source)
            self.validate_conflict(name, plan, source, head)
        if unrotated:
            self.clean_merged(name, plan, source, head, branch, progress)
        return {"state": "SYNCED" if progress["rotations"] or any(c["status"] == "deleted" for c in progress["cleanups"])
                else "NOOP", "branch": branch, "head_sha": head}

    def validate_conflict(self, name: str, plan: dict, source: str, head: str) -> None:
        ctx = self.context
        if ctx.receipts.get(name, {}).get("conflict_validation_required"):
            with ctx.phase("validation"):
                ctx.validate(name, ctx.base / name, source, plan, head)

    def execute(self, plans: dict[str, dict]) -> dict:
        ctx = self.context
        preflight = self.preflight()
        rows = {row["repository"]: row for row in preflight["repositories"]}
        bindings = manifest.require_slot(manifest.load_manifest(ctx.path), ctx.slot)["repositories"]
        if not isinstance(plans, dict) or not plans or set(plans) - set(bindings):
            raise SyncError("plan must select exact manifest repositories")
        manifest.acquire_operation_lock(ctx.root, ctx.slot, ctx.token)
        results = []
        try:
            ctx.load_receipts()  # Never overwrite submit recovery evidence.
            previous = json.loads(self.report_path.read_text()) if self.report_path.exists() else {}
            for name, plan in plans.items():
                ctx.timings = []
                started = time.monotonic()
                progress: dict[str, Any] = {"rotations": [], "cleanups": []}
                prior = previous.get(name, {})
                attempts = prior.get("attempts", 0) + 1 if prior.get("state") == "FAILED" else 1
                try:
                    if name not in rows:
                        raise SyncError("manifest repository is missing or mismatched")
                    result = self.execute_one(name, plan, rows[name], progress)
                except ERRORS as exc:
                    result = {"state": "FAILED", "error": str(exc)[-2000:]}
                    if isinstance(exc, conflicts.ConflictResolutionRequired):
                        result.update(error_code="CONFLICT_RESOLUTION_REQUIRED", conflict_recovery=exc.recovery)
                    elif isinstance(exc, shared.RevalidationRequired):
                        result["error_code"] = "REVALIDATION_REQUIRED"
                try:
                    rotation = ctx.identity(name).get("rotation", {})
                    result["rotation_phase"] = rotation.get("phase")
                    if rotation:
                        result["rotation_target"] = rotation["target_branch"]
                except ERRORS:
                    pass  # Keep the original failure when the binding itself cannot be read.
                result.update(repository=name, **progress, phases=ctx.timings, attempts=attempts,
                              retry_count=attempts - 1, seconds=round(time.monotonic() - started, 3))
                results.append(result)
                previous[name] = result
                manifest.require_operation_lock(ctx.root, ctx.slot, ctx.token)
                manifest.write_json_atomic(self.report_path, previous, "sync.")
        finally:
            manifest.release_operation_lock(ctx.root, ctx.slot, ctx.token)
        return {"mode": "execute", "operation": "sync", "slot": ctx.slot, "repositories": results,
                "errors": preflight["errors"], "preflight_seconds": preflight["seconds"], "report": str(self.report_path)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--plan", type=Path, help="Reviewed JSON object with repositories mapping")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    try:
        runner = Runner(args.root, args.bundle)
        if args.execute:
            if not args.plan:
                raise SyncError("--execute requires --plan")
            result = runner.execute(json.loads(args.plan.read_text())["repositories"])
        else:
            result = runner.preflight()
        print(json.dumps(result, ensure_ascii=False))
        return int(bool(result["errors"]) or any(row["state"] in {"FAILED", "BLOCKED", "SKIPPED", "DIRTY", "RECOVER"}
                                                for row in result["repositories"]))
    except ERRORS as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
