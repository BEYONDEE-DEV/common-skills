#!/usr/bin/env python3
"""Promote remote source branches through dev -> dev2 -> prod without checkout.

Default inspection is read-only. --prepare fetches and writes a reviewable plan;
--execute publishes only the pinned fast-forward updates from that plan.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import uuid
from pathlib import Path

import inspect_worktrees as inspector
import slot_manifest as manifest
import submit_bundle as shared
import verify_pr as verifier


class PromotionError(RuntimeError):
    pass


STAGES = ("dev", "dev2", "prod")
ERRORS = (RuntimeError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError)


def route(source: str) -> list[str]:
    return list(STAGES[STAGES.index(source):]) if source in STAGES else [source, *STAGES]


def ancestor(repo: Path, before: str, after: str) -> bool:
    result = subprocess.run(["git", "--no-optional-locks", "-C", str(repo),
                             "merge-base", "--is-ancestor", before, after],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    if result.returncode not in (0, 1):
        raise PromotionError(result.stderr.decode(errors="replace")[-1500:])
    return result.returncode == 0


def steps(repo: Path, branches: list[str], refs: dict[str, str]) -> list[dict]:
    if set(refs) != set(branches) or not all(manifest.is_object_id(sha) for sha in refs.values()):
        raise PromotionError("all promotion branches must already exist with exact commit IDs")
    for sha in refs.values():
        if shared.g(repo, "rev-parse", "--verify", sha + "^{commit}") != sha:
            raise PromotionError("promotion object is not a commit; prepare again")
    result = []
    incoming = refs[branches[0]]
    for source, target in zip(branches, branches[1:]):
        previous = refs[target]
        if ancestor(repo, incoming, previous):
            action, after = "NOOP", previous
        elif ancestor(repo, previous, incoming):
            action, after = "FAST_FORWARD", incoming
        else:
            raise PromotionError(f"{source} -> {target} diverged; resolve integration separately")
        result.append({"source": source, "target": target, "source_sha": incoming,
                       "before": previous, "after": after, "action": action,
                       "commits": shared.g(repo, "log", "--format=%h %s", f"{previous}..{after}").splitlines(),
                       "changed_paths": shared.g(repo, "diff", "--no-ext-diff", "--no-textconv", "--name-only",
                                                 previous, after, "--").splitlines()})
        # An ahead target already contains the source; review its full tip before
        # promoting that branch to the next environment, never reset it to source.
        incoming = after
    return result


class Runner:
    def __init__(self, root: Path, bundle: str):
        self.root = root.resolve()
        self.base, self.slot, self.bindings = inspector.resolve_bundle(self.root, bundle)
        if self.slot is None:
            raise PromotionError("promote requires a manifest-bound slot")
        self.bundle = bundle
        self.token = uuid.uuid4().hex
        self.directory = self.root / ".multi-repo-worktree" / "promotions"

    def binding(self, name: str) -> tuple[Path, dict, str, str, str]:
        slot = manifest.require_slot(manifest.load_manifest(manifest.manifest_path(self.root)), self.slot)
        if Path(slot["path"]).resolve() != self.base:
            raise PromotionError("slot path changed")
        identity = slot["repositories"][name]
        repo = self.base / name
        if not repo.is_relative_to(self.base) or repo.resolve() != repo or repo == self.base or \
                Path(shared.g(repo, "rev-parse", "--show-toplevel")).resolve() != repo:
            raise PromotionError("repository path escapes or aliases the manifest bundle")
        source, remote = identity.get("source_branch"), identity.get("remote")
        if not isinstance(source, str) or not isinstance(remote, str) or \
                not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", remote):
            raise PromotionError("manifest must bind an exact source branch and remote")
        for branch in route(source):
            shared.g(repo, "check-ref-format", "refs/heads/" + branch)
        repository, remote_key = shared.remote_identity(repo, remote)
        mirror = subprocess.run(["git", "-C", str(repo), "config", "--bool", "--get", f"remote.{remote}.mirror"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        if mirror.returncode not in (0, 1) or mirror.stdout.strip() == b"true":
            raise PromotionError("promotion cannot use a mirror remote")
        common = str(inspector.git_common_dir(repo))
        return repo, identity, repository, remote_key, common

    def inspect(self, selected: list[str] | None = None, *, fetch: bool = False) -> dict:
        names = selected if selected is not None else list(self.bindings)
        if not names or len(set(names)) != len(names) or set(names) - set(self.bindings):
            raise PromotionError("select exact manifest repository names once each")
        rows = []
        for name in names:
            row = {"repository": name, "state": "BLOCKED"}
            try:
                repo, identity, repository, remote_key, common = self.binding(name)
                remote, source = identity["remote"], identity["source_branch"]
                branches = route(source)
                refs = shared.remote_heads(repo, remote, *branches)
                if set(refs) != set(branches):
                    raise PromotionError("missing remote branches: " + ", ".join(set(branches) - set(refs)))
                if fetch:
                    shared.g(repo, "fetch", "--no-tags", "--no-write-fetch-head", "--recurse-submodules=no", remote,
                             *[f"refs/heads/{b}:refs/remotes/{remote}/{b}" for b in branches])
                    fetched = {b: shared.g(repo, "rev-parse", f"refs/remotes/{remote}/{b}^{{commit}}") for b in branches}
                    if fetched != refs or shared.remote_heads(repo, remote, *branches) != refs:
                        raise PromotionError("remote changed during preparation; prepare again")
                row.update(path=str(repo), identity_fingerprint=shared.digest(identity),
                           github_repository=repository, remote_key=remote_key, common_git_dir=common,
                           source_branch=source, remote=remote, route=branches, refs=refs,
                           steps=steps(repo, branches, refs))
                row["state"] = "READY" if any(s["action"] == "FAST_FORWARD" for s in row["steps"]) else "NOOP"
            except ERRORS as exc:
                row["error"] = str(exc)[-2000:]
            rows.append(row)
        return {"schema_version": 1, "operation": "promote", "root": str(self.root),
                "slot": self.slot, "bundle": self.bundle, "repositories": rows}

    def prepare(self, selected: list[str] | None = None) -> dict:
        manifest.acquire_operation_lock(self.root, self.slot, self.token)
        try:
            plan = self.inspect(selected, fetch=True)
            plan["run_id"] = uuid.uuid4().hex
            plan["fingerprint"] = shared.digest(plan)
            path = self.directory / (plan["run_id"] + ".json")
            manifest.write_json_atomic(path, plan, "promote-plan.")
            return {"mode": "prepare", "plan_path": str(path), **plan}
        finally:
            manifest.release_operation_lock(self.root, self.slot, self.token)

    def validate_plan(self, plan: dict) -> None:
        if not isinstance(plan, dict):
            raise PromotionError("promotion plan must be an object")
        content = {key: value for key, value in plan.items() if key != "fingerprint"}
        if plan.get("schema_version") != 1 or plan.get("operation") != "promote" or \
                plan.get("root") != str(self.root) or plan.get("slot") != self.slot or \
                plan.get("bundle") != self.bundle or plan.get("fingerprint") != shared.digest(content) or \
                not re.fullmatch(r"[0-9a-f]{32}", plan.get("run_id", "")):
            raise PromotionError("invalid promotion plan or fingerprint")
        rows = plan.get("repositories")
        if not isinstance(rows, list) or not rows or any(
                not isinstance(row, dict) or not isinstance(row.get("repository"), str) for row in rows):
            raise PromotionError("plan has no repositories")
        names = [row["repository"] for row in rows]
        if len(set(names)) != len(names) or set(names) - set(self.bindings):
            raise PromotionError("plan repository scope differs from manifest")

    def guard(self, row: dict) -> Path:
        repo, identity, repository, remote_key, common = self.binding(row["repository"])
        if row["identity_fingerprint"] != shared.digest(identity) or row["remote_key"] != remote_key or \
                row["path"] != str(repo) or row["common_git_dir"] != common or \
                row["github_repository"] != repository or row["source_branch"] != identity["source_branch"] or \
                row["remote"] != identity["remote"] or row["route"] != route(identity["source_branch"]):
            raise PromotionError("promotion binding or remote changed; prepare again")
        if row["steps"] != steps(repo, row["route"], row["refs"]):
            raise PromotionError("promotion steps differ from the pinned Git graph")
        return repo

    @staticmethod
    def observed(repo: Path, row: dict) -> dict[str, str]:
        actual = shared.remote_heads(repo, row["remote"], *row["route"])
        allowed = {branch: {sha} for branch, sha in row["refs"].items()}
        for step in row["steps"]:
            allowed[step["target"]].add(step["after"])
        if set(actual) != set(allowed) or any(actual[b] not in allowed[b] for b in allowed):
            raise PromotionError("remote ref changed outside this plan; preserve progress and prepare again")
        return actual

    def execute(self, plan: dict) -> dict:
        self.validate_plan(plan)
        path = self.directory / (plan["run_id"] + ".progress.json")
        manifest.acquire_operation_lock(self.root, self.slot, self.token)
        try:
            progress = json.loads(path.read_text()) if path.exists() else {
                "fingerprint": plan["fingerprint"], "repositories": {}}
            if not isinstance(progress, dict) or progress.get("fingerprint") != plan["fingerprint"] or \
                    not isinstance(progress.get("repositories"), dict) or \
                    set(progress["repositories"]) - {row["repository"] for row in plan["repositories"]}:
                raise PromotionError("promotion progress belongs to another plan")

            def save() -> None:
                manifest.require_operation_lock(self.root, self.slot, self.token)
                manifest.write_json_atomic(path, progress, "promote-progress.")

            # Complete the bundle's read-only guards before any publication.
            errors = {}
            for row in plan["repositories"]:
                try:
                    if row["state"] not in {"READY", "NOOP"}:
                        raise PromotionError(row.get("error", "blocked repository; prepare again"))
                    self.observed(self.guard(row), row)
                except ERRORS as exc:
                    errors[row["repository"]] = str(exc)[-2000:]
            for row in plan["repositories"]:
                name = row["repository"]
                previous = progress["repositories"].get(name, {})
                record = {"repository": name, "state": "FAILED", "steps": previous.get("steps", []),
                          "attempts": previous.get("attempts", 0) + 1}
                progress["repositories"][name] = record
                repo_lock = None
                try:
                    if name in errors:
                        raise PromotionError(errors[name])
                    repo = self.guard(row)
                    # Shared across slots targeting the same remote repository.
                    repo_lock = "promote:" + row["github_repository"].lower()
                    manifest.acquire_operation_lock(self.root, repo_lock, self.token)
                    actual = self.observed(repo, row)
                    record["steps"] = []
                    save()
                    for step in row["steps"]:
                        manifest.require_operation_lock(self.root, repo_lock, self.token)
                        repo = self.guard(row)
                        actual = self.observed(repo, row)
                        if actual[step["source"]] != step["source_sha"]:
                            raise PromotionError("upstream stage does not match the planned source commit")
                        item = {**step, "state": "PUSHING"}
                        record["steps"].append(item)
                        save()  # Durable intent precedes each remote write.
                        if actual[step["target"]] != step["after"]:
                            if not ancestor(repo, actual[step["target"]], step["after"]):
                                raise PromotionError("promotion update is not a fast-forward")
                            # This lease is a compare-and-swap, never permission
                            # to rewrite history: ancestry is independently proven.
                            # It also prevents recreating a concurrently deleted ref.
                            shared.g(repo, "-c", "push.followTags=false", "push", "--porcelain",
                                     "--recurse-submodules=no",
                                     f"--force-with-lease=refs/heads/{step['target']}:{step['before']}", row["remote"],
                                     f"{step['after']}:refs/heads/{step['target']}")
                        actual = self.observed(repo, row)
                        if actual[step["target"]] != step["after"]:
                            raise PromotionError("push result not observed; retry this exact plan")
                        item["state"] = "PROMOTED" if step["action"] == "FAST_FORWARD" else "NOOP"
                        save()
                    actual = self.observed(repo, row)
                    if any(actual[s["target"]] != s["after"] for s in row["steps"]):
                        raise PromotionError("final promotion refs differ from the planned results")
                    record.update(state="PROMOTED" if row["state"] == "READY" else "NOOP",
                                  refs=actual, prod_sha=actual["prod"])
                except ERRORS as exc:
                    record["error"] = str(exc)[-2000:]
                finally:
                    if repo_lock is not None:
                        current = manifest.load_operation_lock(manifest.operation_lock_path(self.root, repo_lock))
                        if current and current["token"] == self.token:
                            manifest.release_operation_lock(self.root, repo_lock, self.token)
                save()
            return {"mode": "execute", "operation": "promote", "slot": self.slot,
                    "repositories": list(progress["repositories"].values()), "report_path": str(path)}
        finally:
            manifest.release_operation_lock(self.root, self.slot, self.token)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--repository", action="append", help="Exact repository name; repeat to select a subset")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--prepare", action="store_true")
    mode.add_argument("--execute", type=Path, metavar="PLAN", help="Execute or resume the generated plan")
    args = parser.parse_args()
    try:
        runner = Runner(args.root, args.bundle)
        if args.execute:
            if args.repository:
                raise PromotionError("execution uses the plan's repository set; do not override it")
            result = runner.execute(json.loads(args.execute.read_text()))
        else:
            result = runner.prepare(args.repository) if args.prepare else runner.inspect(args.repository)
        print(json.dumps(result, ensure_ascii=False))
        return int(any(row["state"] in {"BLOCKED", "FAILED"} for row in result["repositories"]))
    except ERRORS as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
