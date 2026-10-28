#!/usr/bin/env python3
"""Status, sync, submit and promote for a folder of independent Git repositories.

Every command prints one JSON document on stdout. Per-repository outcomes are
reported in ``repositories[].status``; repositories run concurrently and one
failing never stops the others. The script never force-pushes, resets,
rebases or merges PRs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

REMOTE = "origin"
SOURCE_CONFIG = "multiRepoSource"  # git config branch.<name>.multiRepoSource
DEFAULT_CHAIN = ("dev", "dev2", "prod")
PROTECTED = {"main", "master", *DEFAULT_CHAIN}
MAX_WORKERS = 8
DIFF_LIMIT = 12000  # characters of working-tree diff returned by submit --prepare

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


def run(args: list[str], cwd: Path, check: bool = True, input: str | None = None) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            args, cwd=cwd, env=ENV, input=input, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
    except FileNotFoundError as error:
        raise Failure("COMMAND_MISSING", f"{args[0]} is not installed") from error
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise Failure("COMMAND_FAILED", f"{' '.join(args[:3])} failed: {detail}")
    return proc


def git(repo: Path, *args: str, check: bool = True, input: str | None = None) -> subprocess.CompletedProcess[str]:
    return run(["git", "-c", "core.quotePath=false", *args], repo, check, input)


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
    """Paths with uncommitted or untracked changes; renames list both sides."""
    fields = git(repo, "status", "--porcelain", "-z", "--untracked-files=all").stdout.split("\0")
    paths, index = [], 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if not entry:
            continue
        paths.append(entry[3:])
        if entry[0] in "RC":
            paths.append(fields[index])
            index += 1
    return paths


def merging(repo: Path) -> bool:
    return git(repo, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode == 0


def conflict_paths(repo: Path) -> list[str]:
    return [p for p in out(repo, "diff", "--name-only", "--diff-filter=U").splitlines() if p]


def ahead_behind(repo: Path, base: str, head: str = "HEAD") -> tuple[int, int]:
    behind, ahead = out(repo, "rev-list", "--left-right", "--count", f"{base}...{head}").split()
    return int(ahead), int(behind)


def remote_heads(repo: Path, *branches: str) -> list[str]:
    refs = [f"refs/heads/{b}" for b in branches]
    lines = out(repo, "ls-remote", "--heads", REMOTE, *refs).splitlines()
    return [line.split("refs/heads/", 1)[1] for line in lines if "refs/heads/" in line]


def fetch_branches(repo: Path, *branches: str) -> set[str]:
    """Fetch only these branches; forget remote-tracking refs of branches deleted on the remote."""
    wanted = list(dict.fromkeys(b for b in branches if b))
    refspecs = [f"+refs/heads/{b}:refs/remotes/{REMOTE}/{b}" for b in wanted]
    if git(repo, "fetch", "--no-tags", REMOTE, *refspecs, check=False).returncode == 0:
        return set(wanted)
    present = set(remote_heads(repo, *wanted))
    for branch in set(wanted) - present:
        git(repo, "update-ref", "-d", f"refs/remotes/{REMOTE}/{branch}", check=False)
    if present:
        git(repo, "fetch", "--no-tags", REMOTE, *[s for b, s in zip(wanted, refspecs) if b in present])
    return present


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


def pr_material(repo: Path, source: str, prs: list[dict[str, Any]]) -> dict[str, Any]:
    """Commits and diff a new PR from HEAD into the source would contain."""
    merged = [pr for pr in prs if pr.get("state") == "MERGED" and pr.get("headRefOid")]
    boundary = max(merged, key=lambda pr: pr["number"])["headRefOid"] if merged else None
    # Squash merges leave the old task commits off the source; skip everything the last merged PR carried.
    exclude = [f"^{boundary}"] if boundary and is_ancestor(repo, boundary, "HEAD") else []
    commits = out(repo, "log", "--no-merges", "--format=%s", "HEAD", f"^{REMOTE}/{source}", *exclude)
    return {"commits": commits.splitlines(), "diffstat": out(repo, "diff", "--stat", f"{REMOTE}/{source}...HEAD")}


# --- shared submit context ----------------------------------------------------


def submit_context(repo: Path, source_arg: str | None, need_gh: bool = True) -> dict[str, Any]:
    """Resolve branch, PR and source, fetch them, and refuse unsafe branches."""
    branch = current_branch(repo)
    if not branch:
        raise Failure("DETACHED", "HEAD is detached; check out the task branch first")
    if branch in PROTECTED:
        raise Failure("PROTECTED_BRANCH", f"refusing to submit from {branch}; use a task branch", branch=branch)
    gh_ok = True
    try:
        prs = list_prs(repo, branch)
    except Failure:
        if need_gh:
            raise
        gh_ok, prs = False, []  # sync still works locally; it only skips the remote cleanup
    pr = choose_pr(prs)
    source, origin = resolve_source(repo, branch, source_arg, pr)
    if not source:
        raise Failure("SOURCE_REQUIRED", "source branch is unknown; ask the user and pass --source",
                      branch=branch, remote_branches=sorted(remote_heads(repo)))
    if branch == source:
        raise Failure("PROTECTED_BRANCH", f"refusing to submit from {branch}; use a task branch", branch=branch)
    if source not in fetch_branches(repo, branch, source):
        raise Failure("SOURCE_NOT_FOUND", f"{REMOTE}/{source} does not exist", source=source)
    git(repo, "config", f"branch.{branch}.{SOURCE_CONFIG}", source)
    open_pr = pr if pr and pr["state"] == "OPEN" else None
    return {"branch": branch, "source": source, "source_from": origin, "prs": prs, "open_pr": open_pr, "gh_ok": gh_ok}


def public(ctx: dict[str, Any]) -> dict[str, Any]:
    return {key: ctx[key] for key in ("branch", "source", "source_from")}


# --- status -----------------------------------------------------------------


def status_one(repo: Path, args: argparse.Namespace) -> dict[str, Any]:
    branch = current_branch(repo)
    result: dict[str, Any] = {"status": "OK" if branch else "DETACHED", "branch": branch,
                              "dirty": dirty_paths(repo), "merging": merging(repo)}
    if not branch:
        return result
    pr = None
    try:
        pr = choose_pr(list_prs(repo, branch))
    except Failure as error:
        result["pr_error"] = str(error)
    result["pr"] = pr
    source, origin = resolve_source(repo, branch, args.source, pr)
    result["source"], result["source_from"] = source, origin
    if not args.offline:
        fetch_branches(repo, branch, source or "")
    if source and remote_sha(repo, source):
        result["ahead"], result["behind"] = ahead_behind(repo, f"{REMOTE}/{source}")
    pushed = remote_sha(repo, branch)
    result["unpushed"] = ahead_behind(repo, pushed)[0] if pushed else None
    return result


# --- submit -----------------------------------------------------------------


def prepare_one(repo: Path, args: argparse.Namespace) -> dict[str, Any]:
    """Everything the agent needs to write one plan entry, without changing the branch."""
    ctx = submit_context(repo, args.source)
    result: dict[str, Any] = {**public(ctx), "status": "READY", "pr": ctx["open_pr"]}
    if merging(repo) and conflict_paths(repo):
        return {**result, "status": "CONFLICT", "conflicts": conflict_paths(repo)}
    dirty = dirty_paths(repo)
    result["dirty"] = dirty
    if dirty:
        untracked = set(out(repo, "ls-files", "--others", "--exclude-standard", "-z").split("\0")) - {""}
        diff = git(repo, "diff", "HEAD", "--no-color", "--no-ext-diff").stdout
        result.update(
            untracked=sorted(untracked),
            dirty_diffstat=out(repo, "diff", "HEAD", "--stat"),
            dirty_diff=diff[:DIFF_LIMIT],
            dirty_diff_truncated=len(diff) > DIFF_LIMIT,
        )
    result["needs_pr_text"] = ctx["open_pr"] is None
    if result["needs_pr_text"]:
        result.update(pr_material(repo, ctx["source"], ctx["prs"]))
    return result


def contained_in(repo: Path, commit: str, source: str) -> bool:
    """True when ``commit`` adds nothing to ``source``: merging it would leave the source tree unchanged.

    This is how squash-merged work is recognised, since its commits never become ancestors of the source.
    """
    if is_ancestor(repo, commit, source):
        return True
    proc = git(repo, "merge-tree", "--write-tree", source, commit, check=False)
    return proc.returncode == 0 and proc.stdout.split()[0] == out(repo, "rev-parse", f"{source}^{{tree}}")


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


def commit_entry(repo: Path, commit: dict[str, Any]) -> bool:
    """Stage exactly the planned paths and commit them; a rerun with nothing left is a no-op."""
    paths, message = commit.get("paths") or [], (commit.get("message") or "").strip()
    if not paths:
        return False
    if not message:
        raise Failure("PLAN_INVALID", "commit.message is required when commit.paths is set")
    # Paths already gone from both disk and index (e.g. a staged `git mv` source) need no add.
    known = set(out(repo, "ls-files", "-z", "--", *paths).split("\0"))
    present = [p for p in paths if p in known or (repo / p).exists()]
    if present:
        git(repo, "add", "-A", "--", *present)
    if git(repo, "diff", "--quiet", "HEAD", "--", *paths, check=False).returncode == 0:
        return False
    # Commit only the planned paths, even if something else was staged beforehand.
    git(repo, "commit", "-F", "-", "--", *paths, input=message + "\n")
    return True


def integrate(repo: Path, ctx: dict[str, Any], base: dict[str, Any]) -> tuple[str | None, bool, dict[str, Any] | None]:
    """Bring teammates' pushes and the latest source into HEAD. Returns (remote head, stale, conflict)."""
    branch, source = ctx["branch"], ctx["source"]
    source_ref, source_sha = f"{REMOTE}/{source}", remote_sha(repo, source)
    # A remote task branch whose work is already in the source (its PR was squash-merged) is stale:
    # never merge it back, and replace it on push instead of stacking new work on top of it.
    remote_head = remote_sha(repo, branch)
    stale_remote = bool(remote_head) and contained_in(repo, remote_head, source_ref)
    if remote_head and not stale_remote:
        conflict = merge_into_head(repo, f"{REMOTE}/{branch}")
        if conflict:
            return remote_head, stale_remote, conflict

    head = out(repo, "rev-parse", "HEAD")
    if head != source_sha and contained_in(repo, head, source_ref):
        # Everything on the branch is already in the source: restart it at the source tip instead of
        # adding a merge commit. Nothing is lost (the source holds all of it); `switch -C` carries
        # uncommitted changes along and refuses to overwrite them or untracked files.
        git(repo, "switch", "-q", "-C", branch, source_sha)
        base["moved_to_source"] = head
        return remote_head, stale_remote, None
    return remote_head, stale_remote, merge_into_head(repo, source_ref)


def finish_merge(repo: Path) -> list[str]:
    """Commit a merge whose conflicts were resolved; return the paths still conflicted."""
    if merging(repo):
        conflicts = conflict_paths(repo)
        if conflicts:
            return conflicts
        git(repo, "commit", "--no-edit")
    return []


def is_source_branch(repo: Path, branch: str) -> bool:
    """True when a local branch merges from ``branch`` or an open PR targets it."""
    configured = git(repo, "config", "--get-regexp", rf"^branch\..*\.{SOURCE_CONFIG}$", check=False).stdout
    if any(line.split()[-1] == branch for line in configured.splitlines() if line.strip()):
        return True
    proc = run(["gh", "pr", "list", "--base", branch, "--state", "open", "--limit", "1", "--json", "number"],
               repo, check=False)
    return proc.returncode != 0 or proc.stdout.strip() not in ("", "[]")


def delete_merged_remote(repo: Path, ctx: dict[str, Any], sha: str) -> dict[str, Any]:
    """Delete the remote task branch whose work is all in the source, unless a PR still uses it."""
    branch = ctx["branch"]
    if branch in PROTECTED:
        return {"remote_kept": f"{branch} is a protected branch"}
    if not ctx["gh_ok"]:
        return {"remote_kept": "gh is unavailable, so an open PR cannot be ruled out"}
    if ctx["open_pr"]:
        return {"remote_kept": f"PR #{ctx['open_pr']['number']} is still open"}
    # Only a task branch whose own PR was merged goes; shared branches others build on stay.
    if not any(pr.get("state") == "MERGED" for pr in ctx["prs"]):
        return {"remote_kept": "no merged PR from this branch"}
    if is_source_branch(repo, branch):
        return {"remote_kept": f"{branch} is the source of other branches or open PRs"}
    # The lease makes the deletion fail if anyone pushed to the branch since it was fetched.
    proc = git(repo, "push", f"--force-with-lease=refs/heads/{branch}:{sha}", REMOTE, f":refs/heads/{branch}",
               check=False)
    if proc.returncode != 0:
        return {"remote_kept": (proc.stderr or proc.stdout).strip()}
    git(repo, "branch", "--unset-upstream", check=False)
    return {"deleted_remote": sha}


def sync_one(repo: Path, args: argparse.Namespace) -> dict[str, Any]:
    """Bring the checked-out task branch up to the latest source; commits and pushes nothing.

    A repository with uncommitted changes is skipped untouched. The remote task branch is deleted
    once everything on it is in the source (its PR was merged).
    """
    ctx = submit_context(repo, args.source, need_gh=False)
    base = public(ctx)
    before = out(repo, "rev-parse", "HEAD")
    conflicts = finish_merge(repo)
    if conflicts:
        return {**base, "status": "CONFLICT", "conflicts": conflicts}
    dirty = dirty_paths(repo)
    if dirty:
        return {**base, "status": "DIRTY", "dirty": dirty}
    remote_head, stale_remote, conflict = integrate(repo, ctx, base)
    if conflict:
        return {**base, **conflict}
    if stale_remote:
        base.update(delete_merged_remote(repo, ctx, remote_head))
    head = out(repo, "rev-parse", "HEAD")
    pushed = remote_sha(repo, ctx["branch"])
    return {**base, "status": "SYNCED" if head != before else "UP_TO_DATE", "head": head,
            "unpushed": ahead_behind(repo, pushed)[0] if pushed else None}


def submit_one(repo: Path, args: argparse.Namespace, entry: dict[str, Any] | None = None) -> dict[str, Any]:
    entry = entry or {}
    ctx = submit_context(repo, args.source)
    base = public(ctx)

    conflicts = finish_merge(repo)
    if conflicts:
        return {**base, "status": "CONFLICT", "conflicts": conflicts}
    if entry.get("commit"):
        base["committed"] = commit_entry(repo, entry["commit"])
    dirty = dirty_paths(repo)
    if dirty:
        return {**base, "status": "DIRTY", "dirty": dirty}

    branch, source = ctx["branch"], ctx["source"]
    source_ref = f"{REMOTE}/{source}"
    remote_head, stale_remote, conflict = integrate(repo, ctx, base)
    if conflict:
        return {**base, **conflict}

    open_pr = ctx["open_pr"]
    if git(repo, "diff", "--quiet", source_ref, "HEAD", check=False).returncode == 0:
        return {**base, "status": "NO_CHANGES", "pr": open_pr}

    head = out(repo, "rev-parse", "HEAD")
    pushed = False
    if remote_head != head:
        push = ["push", "-u", REMOTE, f"HEAD:refs/heads/{branch}"]
        if stale_remote and not is_ancestor(repo, remote_head, head):
            push.insert(1, f"--force-with-lease=refs/heads/{branch}:{remote_head}")
            base["replaced_stale_remote"] = remote_head
        proc = git(repo, *push, check=False)
        if proc.returncode != 0:
            raise Failure("PUSH_REJECTED", (proc.stderr or proc.stdout).strip())
        pushed = True
    base.update(head=head, pushed=pushed)

    if open_pr:
        if open_pr["baseRefName"] != source:
            base["warning"] = f"open PR targets {open_pr['baseRefName']}, not {source}"
        return {**base, "status": "UPDATED", "pr": open_pr}
    pr_text = entry.get("pr") or {}
    title = args.title or pr_text.get("title")
    if not title:
        return {**base, "status": "PR_TEXT_REQUIRED", **pr_material(repo, source, ctx["prs"])}
    body = args.body_file.read_text(encoding="utf-8") if args.body_file else pr_text.get("body", "")
    create = ["gh", "pr", "create", "--base", source, "--head", branch, "--title", title, "--body-file", "-"]
    url = run(create, repo, input=body).stdout.strip().splitlines()[-1]
    return {**base, "status": "CREATED", "pr": {"url": url, "title": title, "baseRefName": source}}


def load_plan(path: Path) -> dict[str, dict[str, Any]]:
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))["repositories"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise Failure("PLAN_INVALID", f"cannot read plan {path}: {error}") from error
    if not isinstance(plan, dict):
        raise Failure("PLAN_INVALID", "plan.repositories must map repository names to entries")
    return plan


# --- promote ----------------------------------------------------------------


def promote_stages(repo: Path, start: str, chain: list[str]) -> list[str]:
    if start in chain:
        return chain[chain.index(start):]
    source = resolve_source(repo, start, None, None)[0]
    if source in chain:
        return [start, *chain[chain.index(source):]]
    return [start, *([source] if source else []), *chain]


def promote_plan(repo: Path, args: argparse.Namespace) -> dict[str, Any]:
    branch = current_branch(repo)
    start = args.start or branch
    if not start:
        raise Failure("DETACHED", "HEAD is detached; pass --from")
    chain = list(args.chain)
    if not args.start and start in PROTECTED - set(chain):
        raise Failure("SOURCE_REQUIRED", f"{start} is not a promotion stage; pass --from")
    stages = promote_stages(repo, start, chain)
    if args.to:
        if args.to not in stages[1:]:
            raise Failure("USAGE", f"--to {args.to} is not a later stage of {' -> '.join(stages)}")
        stages = stages[:stages.index(args.to) + 1]
    # The checked-out task branch goes straight into the next stage, so it must be submitted first.
    task = start == branch and start not in chain
    if task and dirty_paths(repo):
        raise Failure("SUBMIT_REQUIRED", f"{start} has uncommitted changes; submit it first",
                      reason="uncommitted", stages=stages)
    if not args.offline:
        missing = set(stages) - fetch_branches(repo, *stages) - ({start} if task else set())
        if missing:
            raise Failure("BRANCH_NOT_FOUND", f"{REMOTE} has no {', '.join(sorted(missing))}")
    if task:
        head = out(repo, "rev-parse", "HEAD")
        if remote_sha(repo, stages[1]) and contained_in(repo, head, f"{REMOTE}/{stages[1]}"):
            stages = stages[1:]  # nothing left on the task branch that the next stage lacks
            task = False
        elif remote_sha(repo, start) != head:
            raise Failure("SUBMIT_REQUIRED", f"{start} has commits not pushed to {REMOTE}; submit it first",
                          reason="unpushed", stages=stages)
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
        elif task and not steps:
            raise Failure("SUBMIT_REQUIRED", f"{start} lacks the latest {REMOTE}/{stage}; submit it first",
                          reason="behind", stages=stages)
        else:
            raise Failure("DIVERGED", f"{REMOTE}/{stage} has diverged from {REMOTE}/{previous}; integrate it manually",
                          steps=steps)
        steps.append(step)
    return {"status": "PLANNED", "stages": stages, "task": stages[0] if task else None, "steps": steps}


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


def each(repos: list[Path], action: Callable[[Path], dict[str, Any]]) -> list[dict[str, Any]]:
    def one(repo: Path) -> dict[str, Any]:
        try:
            result = action(repo)
        except Failure as error:
            result = {"status": error.status, "error": str(error), **error.extra}
        return {"repo": repo.name, "path": str(repo), **result}

    if not repos:
        return []
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(repos))) as pool:
        return list(pool.map(one, repos))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="bundle folder or a single repository")
    parser.add_argument("--repo", action="append", default=[], help="limit to this repository (repeatable)")
    commands = parser.add_subparsers(dest="command", required=True)

    status = commands.add_parser("status")
    status.add_argument("--source")
    status.add_argument("--offline", action="store_true", help="skip git fetch")

    sync = commands.add_parser("sync")
    sync.add_argument("--source", help="source branch to merge from")

    submit = commands.add_parser("submit")
    submit.add_argument("--source", help="source branch to merge from and open the PR against")
    mode = submit.add_mutually_exclusive_group()
    mode.add_argument("--prepare", action="store_true", help="report what each repository needs; change nothing")
    mode.add_argument("--plan", type=Path, help="JSON plan with per-repository commit and PR text")
    submit.add_argument("--title", help="PR title for a single --repo, used only when a new PR is created")
    submit.add_argument("--body-file", type=Path)

    promote = commands.add_parser("promote")
    promote.add_argument("--from", dest="start", help="first branch; defaults to the current branch")
    promote.add_argument("--to", help="last stage to push (for example dev)")
    promote.add_argument("--chain", type=lambda v: v.split(","), default=list(DEFAULT_CHAIN))
    promote.add_argument("--execute", metavar="PLAN_ID", help="push the plan previously shown with this id")
    promote.add_argument("--offline", action="store_true")

    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        report = dispatch(args)
    except Failure as error:
        print(json.dumps({"status": error.status, "error": str(error), **error.extra}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps({"command": args.command, **report}, ensure_ascii=False, indent=2))
    return 0


def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    repos = discover(args.root, args.repo)
    if args.command == "status":
        return {"repositories": each(repos, lambda r: status_one(r, args))}
    if args.command == "sync":
        return {"repositories": each(repos, lambda r: sync_one(r, args))}
    if args.command == "submit":
        if (args.title or args.body_file) and (len(repos) != 1 or args.plan or args.prepare):
            raise Failure("USAGE", "--title/--body-file apply to exactly one --repo without --plan/--prepare")
        if args.prepare:
            return {"repositories": each(repos, lambda r: prepare_one(r, args))}
        if args.plan:
            plan = load_plan(args.plan)
            unknown = set(plan) - {r.name for r in repos}
            if unknown:
                raise Failure("UNKNOWN_REPOSITORY", f"plan names unknown repositories: {', '.join(sorted(unknown))}")
            planned = [r for r in repos if r.name in plan]
            return {"repositories": each(planned, lambda r: submit_one(r, args, plan[r.name] or {}))}
        return {"repositories": each(repos, lambda r: submit_one(r, args))}

    plans = each(repos, lambda r: promote_plan(r, args))
    report: dict[str, Any] = {"plan_id": plan_id(plans), "repositories": plans}
    if args.execute:
        if args.execute != report["plan_id"]:
            raise Failure("PLAN_CHANGED", "remote branches changed since the plan was shown; review again",
                          plan_id=report["plan_id"])
        by_path = {p["path"]: p for p in plans}
        report["repositories"] = each(repos, lambda r: promote_execute(r, by_path[str(r)]))
    return report


if __name__ == "__main__":
    raise SystemExit(main())
