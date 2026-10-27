#!/usr/bin/env python3
"""Status, submit and promote for a folder of independent Git repositories.

Every command prints one JSON document on stdout. Per-repository outcomes are
reported in ``repositories[].status``; one repository failing never stops the
others. The script never force-pushes, resets, rebases or merges PRs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

REMOTE = "origin"
SOURCE_CONFIG = "multiRepoSource"  # git config branch.<name>.multiRepoSource
DEFAULT_CHAIN = ("dev", "dev2", "prod")
PROTECTED = {"main", "master", *DEFAULT_CHAIN}

ENV = {
    **os.environ,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_MERGE_AUTOEDIT": "no",
    "GH_PROMPT_DISABLED": "1",
    "GH_NO_UPDATE_NOTIFIER": "1",
}


class Failure(Exception):
    def __init__(self, status: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.extra = extra


def run(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            args, cwd=cwd, env=ENV, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
    except FileNotFoundError as error:
        raise Failure("COMMAND_MISSING", f"{args[0]} is not installed") from error
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise Failure("COMMAND_FAILED", f"{' '.join(args[:3])} failed: {detail}")
    return proc


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(["git", "-c", "core.quotePath=false", *args], repo, check)


def out(repo: Path, *args: str) -> str:
    return git(repo, *args).stdout.strip()


# --- repository discovery -------------------------------------------------


def is_repo_root(path: Path) -> bool:
    return (path / ".git").exists()


def discover(root: Path, selected: list[str]) -> list[Path]:
    root = root.resolve()
    if is_repo_root(root):
        repos = [root]
    else:
        repos = sorted(
            child for child in root.iterdir()
            if child.is_dir() and not child.name.startswith(".") and is_repo_root(child)
        )
    if not selected:
        if not repos:
            raise Failure("NO_REPOSITORIES", f"no Git repositories directly under {root}")
        return repos
    chosen = []
    for name in selected:
        match = [r for r in repos if r.name == name or r == (root / name).resolve()]
        if not match:
            raise Failure("UNKNOWN_REPOSITORY", f"{name} is not a repository under {root}")
        chosen.extend(m for m in match if m not in chosen)
    return chosen


# --- git state --------------------------------------------------------------


def current_branch(repo: Path) -> str | None:
    proc = git(repo, "symbolic-ref", "-q", "--short", "HEAD", check=False)
    return proc.stdout.strip() if proc.returncode == 0 else None


def remote_sha(repo: Path, branch: str) -> str | None:
    proc = git(repo, "rev-parse", "-q", "--verify", f"refs/remotes/{REMOTE}/{branch}^{{commit}}", check=False)
    return proc.stdout.strip() if proc.returncode == 0 else None


def is_ancestor(repo: Path, older: str, newer: str) -> bool:
    return git(repo, "merge-base", "--is-ancestor", older, newer, check=False).returncode == 0


def dirty_paths(repo: Path) -> list[str]:
    lines = git(repo, "status", "--porcelain", "--untracked-files=all").stdout.splitlines()
    return [line[3:] for line in lines if line.strip()]


def merging(repo: Path) -> bool:
    return git(repo, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode == 0


def conflict_paths(repo: Path) -> list[str]:
    return [p for p in out(repo, "diff", "--name-only", "--diff-filter=U").splitlines() if p]


def ahead_behind(repo: Path, base: str, head: str = "HEAD") -> tuple[int, int]:
    behind, ahead = out(repo, "rev-list", "--left-right", "--count", f"{base}...{head}").split()
    return int(ahead), int(behind)


def fetch(repo: Path) -> None:
    git(repo, "fetch", "--prune", "--no-tags", REMOTE)


# --- GitHub -----------------------------------------------------------------


def list_prs(repo: Path, branch: str) -> list[dict[str, Any]]:
    if not shutil.which("gh"):
        raise Failure("GH_MISSING", "GitHub CLI (gh) is not installed")
    proc = run(
        ["gh", "pr", "list", "--head", branch, "--state", "all", "--limit", "20",
         "--json", "number,state,url,baseRefName,title,headRefOid"],
        repo,
    )
    return json.loads(proc.stdout or "[]")


def choose_pr(prs: list[dict[str, Any]]) -> dict[str, Any] | None:
    open_prs = [pr for pr in prs if pr.get("state") == "OPEN"]
    pool = open_prs or prs
    return max(pool, key=lambda pr: pr["number"]) if pool else None


def resolve_source(repo: Path, branch: str, explicit: str | None, pr: dict[str, Any] | None) -> tuple[str | None, str | None]:
    if explicit:
        return explicit, "argument"
    configured = git(repo, "config", "--get", f"branch.{branch}.{SOURCE_CONFIG}", check=False).stdout.strip()
    if configured:
        return configured, "config"
    if pr:
        return pr["baseRefName"], "pr"
    return None, None


# --- status -----------------------------------------------------------------


def status_one(repo: Path, args: argparse.Namespace) -> dict[str, Any]:
    result: dict[str, Any] = {"status": "OK"}
    if not args.offline:
        fetch(repo)
    branch = current_branch(repo)
    result["branch"] = branch
    result["dirty"] = dirty_paths(repo)
    result["merging"] = merging(repo)
    if not branch:
        result["status"] = "DETACHED"
        return result
    pr = None
    try:
        pr = choose_pr(list_prs(repo, branch))
    except Failure as error:
        result["pr_error"] = str(error)
    result["pr"] = pr
    source, origin = resolve_source(repo, branch, args.source, pr)
    result["source"], result["source_from"] = source, origin
    if source and remote_sha(repo, source):
        result["ahead"], result["behind"] = ahead_behind(repo, f"{REMOTE}/{source}")
    pushed = remote_sha(repo, branch)
    result["unpushed"] = ahead_behind(repo, pushed)[0] if pushed else None
    return result


# --- submit -----------------------------------------------------------------


def merge_into_head(repo: Path, target: str) -> dict[str, Any] | None:
    """Merge ``target`` into HEAD. Returns a CONFLICT outcome or None on success."""
    if is_ancestor(repo, target, "HEAD"):
        return None
    proc = git(repo, "merge", "--no-edit", target, check=False)
    if proc.returncode == 0:
        return None
    conflicts = conflict_paths(repo)
    if conflicts:
        return {"status": "CONFLICT", "merging": target, "conflicts": conflicts}
    raise Failure("MERGE_FAILED", (proc.stderr or proc.stdout).strip())


def pr_material(repo: Path, source: str, prs: list[dict[str, Any]]) -> dict[str, Any]:
    """Commits and diff a new PR will contain. HEAD already includes the source."""
    merged = [pr for pr in prs if pr.get("state") == "MERGED" and pr.get("headRefOid")]
    boundary = max(merged, key=lambda pr: pr["number"])["headRefOid"] if merged else None
    # Squash merges leave the old task commits off the source; skip everything the last merged PR carried.
    exclude = [f"^{boundary}"] if boundary and is_ancestor(repo, boundary, "HEAD") else []
    commits = out(repo, "log", "--no-merges", "--format=%s", "HEAD", f"^{REMOTE}/{source}", *exclude)
    return {"commits": commits.splitlines(), "diffstat": out(repo, "diff", "--stat", f"{REMOTE}/{source}", "HEAD")}


def submit_one(repo: Path, args: argparse.Namespace) -> dict[str, Any]:
    branch = current_branch(repo)
    if not branch:
        raise Failure("DETACHED", "HEAD is detached; check out the task branch first")
    if branch in PROTECTED:
        raise Failure("PROTECTED_BRANCH", f"refusing to submit from {branch}; use a task branch", branch=branch)
    fetch(repo)
    prs = list_prs(repo, branch)
    pr = choose_pr(prs)
    source, origin = resolve_source(repo, branch, args.source, pr)
    if not source:
        remotes = [line.split("/", 1)[1] for line in out(repo, "branch", "-r", "--format=%(refname:short)").splitlines()
                   if line.startswith(f"{REMOTE}/") and line != f"{REMOTE}/HEAD"]
        raise Failure("SOURCE_REQUIRED", "source branch is unknown; ask the user and pass --source",
                      branch=branch, remote_branches=remotes)
    if branch == source:
        raise Failure("PROTECTED_BRANCH", f"refusing to submit from {branch}; use a task branch", branch=branch)
    if not remote_sha(repo, source):
        raise Failure("SOURCE_NOT_FOUND", f"{REMOTE}/{source} does not exist", source=source)
    git(repo, "config", f"branch.{branch}.{SOURCE_CONFIG}", source)
    base = {"branch": branch, "source": source, "source_from": origin}

    if merging(repo):
        conflicts = conflict_paths(repo)
        if conflicts:
            return {**base, "status": "CONFLICT", "conflicts": conflicts}
        git(repo, "commit", "--no-edit")
    dirty = dirty_paths(repo)
    if dirty:
        return {**base, "status": "DIRTY", "dirty": dirty}

    targets = [f"{REMOTE}/{branch}"] if remote_sha(repo, branch) else []
    for target in [*targets, f"{REMOTE}/{source}"]:
        conflict = merge_into_head(repo, target)
        if conflict:
            return {**base, **conflict}

    open_pr = pr if pr and pr["state"] == "OPEN" else None
    if git(repo, "diff", "--quiet", f"{REMOTE}/{source}", "HEAD", check=False).returncode == 0:
        return {**base, "status": "NO_CHANGES", "pr": open_pr}

    head = out(repo, "rev-parse", "HEAD")
    pushed = False
    if remote_sha(repo, branch) != head:
        proc = git(repo, "push", "-u", REMOTE, f"HEAD:refs/heads/{branch}", check=False)
        if proc.returncode != 0:
            raise Failure("PUSH_REJECTED", (proc.stderr or proc.stdout).strip())
        pushed = True
    base.update(head=head, pushed=pushed)

    if open_pr:
        if open_pr["baseRefName"] != source:
            base["warning"] = f"open PR targets {open_pr['baseRefName']}, not {source}"
        return {**base, "status": "UPDATED", "pr": open_pr}
    if not args.title:
        return {**base, "status": "PR_TEXT_REQUIRED", **pr_material(repo, source, prs)}
    create = ["gh", "pr", "create", "--base", source, "--head", branch, "--title", args.title]
    create += ["--body-file", str(args.body_file)] if args.body_file else ["--body", ""]
    url = run(create, repo).stdout.strip().splitlines()[-1]
    return {**base, "status": "CREATED", "pr": {"url": url, "title": args.title, "baseRefName": source}}


# --- promote ----------------------------------------------------------------


def promote_plan(repo: Path, args: argparse.Namespace) -> dict[str, Any]:
    if not args.offline:
        fetch(repo)
    start = args.start
    if not start:
        branch = current_branch(repo)
        start = resolve_source(repo, branch, None, None)[0] if branch else None
        if not start:
            raise Failure("SOURCE_REQUIRED", "promotion start branch is unknown; pass --from")
    chain = list(args.chain)
    stages = chain[chain.index(start):] if start in chain else [start, *chain]
    current = remote_sha(repo, stages[0])
    if not current:
        raise Failure("BRANCH_NOT_FOUND", f"{REMOTE}/{stages[0]} does not exist")
    steps = []
    for previous, stage in zip(stages, stages[1:]):
        before = remote_sha(repo, stage)
        if not before:
            raise Failure("BRANCH_NOT_FOUND", f"{REMOTE}/{stage} does not exist")
        step = {"from": previous, "to": stage, "before": before, "after": current}
        if before == current:
            step["action"] = "UP_TO_DATE"
        elif is_ancestor(repo, before, current):
            step["action"] = "PUSH"
            step["commits"] = out(repo, "log", "--format=%H %s", f"{before}..{current}").splitlines()
        elif is_ancestor(repo, current, before):
            step["action"] = "AHEAD"  # stage already contains everything; carry its tip forward
            step["after"] = current = before
        else:
            raise Failure("DIVERGED", f"{REMOTE}/{stage} has diverged from {REMOTE}/{previous}; integrate it manually",
                          steps=steps)
        steps.append(step)
    return {"status": "PLANNED", "stages": stages, "steps": steps}


def plan_id(results: list[dict[str, Any]]) -> str:
    material = [(r["repo"], r.get("status"), r.get("steps")) for r in results]
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()[:16]


def promote_execute(repo: Path, plan: dict[str, Any]) -> dict[str, Any]:
    if plan["status"] != "PLANNED":
        raise Failure(plan["status"], plan.get("error", ""))
    done = []
    for step in plan["steps"]:
        if step["action"] == "PUSH":
            proc = git(repo, "push", REMOTE, f"{step['after']}:refs/heads/{step['to']}", check=False)
            if proc.returncode != 0:
                raise Failure("PUSH_REJECTED", f"{step['to']}: {(proc.stderr or proc.stdout).strip()}", promoted=done)
            remote = out(repo, "ls-remote", REMOTE, f"refs/heads/{step['to']}").split()
            if not remote or remote[0] != step["after"]:
                raise Failure("VERIFY_FAILED", f"{REMOTE}/{step['to']} is not at {step['after']}", promoted=done)
            done.append(step["to"])
    return {**plan, "status": "PROMOTED" if done else "UP_TO_DATE", "promoted": done}


# --- entry point --------------------------------------------------------------


def each(repos: list[Path], action) -> list[dict[str, Any]]:
    results = []
    for repo in repos:
        try:
            result = action(repo)
        except Failure as error:
            result = {"status": error.status, "error": str(error), **error.extra}
        results.append({"repo": repo.name, "path": str(repo), **result})
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="bundle folder or a single repository")
    parser.add_argument("--repo", action="append", default=[], help="limit to this repository (repeatable)")
    commands = parser.add_subparsers(dest="command", required=True)

    status = commands.add_parser("status")
    status.add_argument("--source")
    status.add_argument("--offline", action="store_true", help="skip git fetch")

    submit = commands.add_parser("submit")
    submit.add_argument("--source", help="source branch to merge from and open the PR against")
    submit.add_argument("--title", help="PR title, used only when a new PR is created")
    submit.add_argument("--body-file", type=Path)

    promote = commands.add_parser("promote")
    promote.add_argument("--from", dest="start", help="first branch; defaults to the current branch's source")
    promote.add_argument("--chain", type=lambda v: v.split(","), default=list(DEFAULT_CHAIN))
    promote.add_argument("--execute", metavar="PLAN_ID", help="push the plan previously shown with this id")
    promote.add_argument("--offline", action="store_true")

    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        repos = discover(args.root, args.repo)
        if args.command == "submit" and args.title and len(repos) != 1:
            raise Failure("USAGE", "--title applies to exactly one --repo")
        if args.command == "status":
            report = {"repositories": each(repos, lambda r: status_one(r, args))}
        elif args.command == "submit":
            report = {"repositories": each(repos, lambda r: submit_one(r, args))}
        else:
            plans = each(repos, lambda r: promote_plan(r, args))
            report = {"plan_id": plan_id(plans), "repositories": plans}
            if args.execute:
                if args.execute != report["plan_id"]:
                    raise Failure("PLAN_CHANGED", "remote branches changed since the plan was shown; review again",
                                  plan_id=report["plan_id"])
                by_path = {p["path"]: p for p in plans}
                report["repositories"] = each(repos, lambda r: promote_execute(r, by_path[str(r)]))
    except Failure as error:
        print(json.dumps({"status": error.status, "error": str(error), **error.extra}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps({"command": args.command, **report}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
