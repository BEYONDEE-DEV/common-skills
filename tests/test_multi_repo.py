from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "skills" / "multi-repo"
SCRIPT = SKILL_DIR / "scripts" / "multi_repo.py"

FAKE_GH = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import fcntl, json, os, sys
    from pathlib import Path

    state = Path(os.environ["FAKE_GH_STATE"])
    lock = open(str(state) + ".lock", "w")
    fcntl.flock(lock, fcntl.LOCK_EX)  # the runner calls gh for several repositories concurrently
    prs = json.loads(state.read_text(encoding="utf-8")) if state.exists() else []
    args = sys.argv[1:]

    def opt(name):
        return args[args.index(name) + 1] if name in args else None

    if args[:2] == ["pr", "list"]:
        head, base, state = opt("--head"), opt("--base"), opt("--state")
        print(json.dumps([p for p in prs if head in (None, p["headRefName"]) and base in (None, p["baseRefName"])
                          and state in (None, "all", p["state"].lower())]))
    elif args[:2] == ["pr", "create"]:
        number = len(prs) + 1
        source = opt("--body-file")
        body = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8") if source else opt("--body")
        prs.append({"number": number, "state": "OPEN", "url": f"https://github.com/o/r/pull/{number}",
                    "baseRefName": opt("--base"), "headRefName": opt("--head"), "title": opt("--title"),
                    "body": body, "headRefOid": None})
        state.write_text(json.dumps(prs), encoding="utf-8")
        print(f"https://github.com/o/r/pull/{number}")
    else:
        sys.exit("unsupported: " + " ".join(args))
    """
)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class Workspace:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.remote = base / "remote.git"
        self.bundle = base / "bundle"
        self.seed = base / "seed"
        self.bin = base / "bin"
        self.gh_state = base / "prs.json"
        self.bundle.mkdir()
        self.bin.mkdir()
        gh = self.bin / "gh"
        gh.write_text(FAKE_GH, encoding="utf-8")
        gh.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "FAKE_GH_STATE": str(self.gh_state),
            "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        }
        os.environ.update({k: v for k, v in self.env.items() if k.startswith("GIT_")})

        git(base, "init", "-q", "--bare", "-b", "main", str(self.remote))
        git(base, "clone", "-q", str(self.remote), str(self.seed))
        self.commit(self.seed, "base.txt", "base\n", "init")
        for branch in ("main", "dev", "dev2", "prod"):
            git(self.seed, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
        self.api = self.clone("api", "feature")

    def clone(self, name: str, branch: str) -> Path:
        path = self.bundle / name
        git(self.bundle, "clone", "-q", str(self.remote), name)
        git(path, "switch", "-q", "-c", branch, "origin/dev")
        return path

    def commit(self, repo: Path, name: str, text: str, message: str) -> str:
        (repo / name).write_text(text, encoding="utf-8")
        git(repo, "add", name)
        git(repo, "commit", "-q", "-m", message)
        return git(repo, "rev-parse", "HEAD")

    def advance(self, branch: str, name: str, text: str, message: str) -> str:
        git(self.seed, "fetch", "-q", "origin")
        git(self.seed, "switch", "-q", "-C", branch, f"origin/{branch}")
        sha = self.commit(self.seed, name, text, message)
        git(self.seed, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
        return sha

    def run(self, *args: str) -> dict:
        proc = subprocess.run([sys.executable, str(SCRIPT), "--root", str(self.bundle), *args],
                              env=self.env, capture_output=True, text=True, encoding="utf-8")
        return json.loads(proc.stdout)

    def repo_result(self, *args: str) -> dict:
        return self.run(*args)["repositories"][0]

    def prs(self) -> list[dict]:
        return json.loads(self.gh_state.read_text(encoding="utf-8")) if self.gh_state.exists() else []

    def remote_sha(self, branch: str) -> str:
        return git(self.remote, "rev-parse", branch)

    def close(self) -> None:
        self.tmp.cleanup()


class MultiRepoTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = Workspace()
        self.addCleanup(self.ws.close)


class SubmitTests(MultiRepoTestCase):
    def test_unknown_source_asks_for_it(self) -> None:
        self.ws.commit(self.ws.api, "a.txt", "a\n", "A 작업")
        result = self.ws.repo_result("submit")
        self.assertEqual(result["status"], "SOURCE_REQUIRED")
        self.assertIn("dev", result["remote_branches"])

    def test_first_submit_pushes_then_creates_pr_with_text(self) -> None:
        head = self.ws.commit(self.ws.api, "a.txt", "a\n", "A 작업")
        first = self.ws.repo_result("submit", "--source", "dev")
        self.assertEqual(first["status"], "PR_TEXT_REQUIRED")
        self.assertEqual(first["commits"], ["A 작업"])
        self.assertEqual(self.ws.remote_sha("feature"), head)

        body = self.ws.bundle.parent / "body.md"
        body.write_text("## 변경\n- A\n", encoding="utf-8")
        second = self.ws.repo_result("--repo", "api", "submit", "--title", "A 기능", "--body-file", str(body))
        self.assertEqual(second["status"], "CREATED")
        self.assertEqual(second["source_from"], "config", "source is remembered after the first submit")
        pr = self.ws.prs()[0]
        self.assertEqual((pr["baseRefName"], pr["headRefName"], pr["title"]), ("dev", "feature", "A 기능"))
        self.assertEqual(pr["body"], "## 변경\n- A\n")

    def test_dirty_repository_is_not_touched(self) -> None:
        self.ws.commit(self.ws.api, "a.txt", "a\n", "A")
        (self.ws.api / "scratch.txt").write_text("x", encoding="utf-8")
        result = self.ws.repo_result("submit", "--source", "dev")
        self.assertEqual(result["status"], "DIRTY")
        self.assertEqual(result["dirty"], ["scratch.txt"])
        self.assertNotIn("feature", git(self.ws.remote, "branch"))

    def test_ignored_files_do_not_count_as_dirty(self) -> None:
        (self.ws.api / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
        git(self.ws.api, "add", ".gitignore")
        git(self.ws.api, "commit", "-q", "-m", "ignore")
        (self.ws.api / "node_modules").mkdir()
        (self.ws.api / "node_modules" / "x.js").write_text("", encoding="utf-8")
        self.assertEqual(self.ws.repo_result("submit", "--source", "dev")["status"], "PR_TEXT_REQUIRED")

    def test_submit_merges_advanced_source_and_updates_open_pr(self) -> None:
        self.ws.commit(self.ws.api, "a.txt", "a\n", "A")
        self.ws.repo_result("submit", "--source", "dev", "--title", "A")
        source = self.ws.advance("dev", "other.txt", "o\n", "다른 작업")
        result = self.ws.repo_result("submit")
        self.assertEqual(result["status"], "UPDATED")
        self.assertTrue(result["pushed"])
        git(self.ws.remote, "merge-base", "--is-ancestor", source, "feature")

    def test_conflict_is_reported_then_resumed_after_resolution(self) -> None:
        self.ws.commit(self.ws.api, "base.txt", "mine\n", "내 변경")
        self.ws.repo_result("submit", "--source", "dev", "--title", "A")
        self.ws.advance("dev", "base.txt", "theirs\n", "상대 변경")

        conflict = self.ws.repo_result("submit")
        self.assertEqual(conflict["status"], "CONFLICT")
        self.assertEqual(conflict["conflicts"], ["base.txt"])
        self.assertEqual(self.ws.repo_result("submit")["status"], "CONFLICT", "unresolved conflict stays reported")

        (self.ws.api / "base.txt").write_text("mine and theirs\n", encoding="utf-8")
        git(self.ws.api, "add", "base.txt")
        resumed = self.ws.repo_result("submit")
        self.assertEqual(resumed["status"], "UPDATED")
        self.assertEqual(git(self.ws.remote, "show", "feature:base.txt"), "mine and theirs")

    def test_after_squash_merge_next_submit_needs_only_new_work(self) -> None:
        self.ws.commit(self.ws.api, "a.txt", "a\n", "A 작업")
        self.ws.repo_result("submit", "--source", "dev", "--title", "A")
        merged_head = self.ws.remote_sha("feature")
        # GitHub squash-merges PR 1 into dev.
        self.ws.advance("dev", "a.txt", "a\n", "A 기능 (#1)")
        prs = self.ws.prs()
        prs[0].update(state="MERGED", headRefOid=merged_head)
        self.ws.gh_state.write_text(json.dumps(prs), encoding="utf-8")

        self.assertEqual(self.ws.repo_result("submit")["status"], "NO_CHANGES")
        self.ws.commit(self.ws.api, "b.txt", "b\n", "B 작업")
        result = self.ws.repo_result("submit")
        self.assertEqual(result["status"], "PR_TEXT_REQUIRED")
        self.assertEqual(result["commits"], ["B 작업"])
        self.assertIn("b.txt", result["diffstat"])
        self.assertNotIn("a.txt", result["diffstat"])

    def squash_merge_first_pr(self, *later: tuple[str, str, str]) -> str:
        """Submit A, then squash-merge its PR into dev (plus any later commits). Returns the merged head."""
        self.ws.commit(self.ws.api, "a.txt", "a\n", "A 작업")
        self.ws.repo_result("submit", "--source", "dev", "--title", "A")
        merged_head = self.ws.remote_sha("feature")
        self.ws.advance("dev", "a.txt", "a\n", "A 기능 (#1)")
        for name, text, message in later:
            self.ws.advance("dev", name, text, message)
        prs = self.ws.prs()
        prs[0].update(state="MERGED", headRefOid=merged_head)
        self.ws.gh_state.write_text(json.dumps(prs), encoding="utf-8")
        return merged_head

    def test_merged_branch_moves_to_source_tip_without_merge_commit(self) -> None:
        merged_head = self.squash_merge_first_pr(("c.txt", "c\n", "다른 PR (#2)"))
        result = self.ws.repo_result("submit")
        self.assertEqual((result["status"], result["moved_to_source"]), ("NO_CHANGES", merged_head))
        self.assertEqual(git(self.ws.api, "rev-parse", "HEAD"), self.ws.remote_sha("dev"))
        self.assertEqual(git(self.ws.api, "status", "--porcelain"), "")
        self.assertEqual((self.ws.api / "c.txt").read_text(encoding="utf-8"), "c\n")
        self.assertEqual(git(self.ws.api, "rev-list", "--merges", "--count", "HEAD"), "0")

    def test_existing_merge_commit_on_merged_branch_is_dropped(self) -> None:
        self.squash_merge_first_pr(("c.txt", "c\n", "다른 PR (#2)"))
        git(self.ws.api, "fetch", "-q", "origin")
        git(self.ws.api, "merge", "-q", "--no-edit", "origin/dev")
        self.assertEqual(self.ws.repo_result("submit")["status"], "NO_CHANGES")
        self.assertEqual(git(self.ws.api, "rev-parse", "HEAD"), self.ws.remote_sha("dev"))

    def test_new_work_after_squash_replaces_stale_remote_branch(self) -> None:
        merged_head = self.squash_merge_first_pr(("c.txt", "c\n", "다른 PR (#2)"))
        self.ws.repo_result("submit")
        head = self.ws.commit(self.ws.api, "b.txt", "b\n", "B 작업")
        result = self.ws.repo_result("submit")
        self.assertEqual(result["status"], "PR_TEXT_REQUIRED")
        self.assertEqual(result["replaced_stale_remote"], merged_head)
        self.assertEqual(result["commits"], ["B 작업"])
        self.assertEqual(self.ws.remote_sha("feature"), head)
        self.assertEqual(git(self.ws.api, "rev-parse", "HEAD^"), self.ws.remote_sha("dev"))

    def test_teammate_commits_on_remote_branch_are_merged_not_replaced(self) -> None:
        self.ws.commit(self.ws.api, "a.txt", "a\n", "A")
        self.ws.repo_result("submit", "--source", "dev", "--title", "A")
        teammate = self.ws.advance("feature", "t.txt", "t\n", "동료 작업")
        self.ws.commit(self.ws.api, "b.txt", "b\n", "B")
        result = self.ws.repo_result("submit")
        self.assertEqual(result["status"], "UPDATED")
        self.assertNotIn("replaced_stale_remote", result)
        git(self.ws.remote, "merge-base", "--is-ancestor", teammate, "feature")

    def test_protected_and_source_branches_are_refused(self) -> None:
        git(self.ws.api, "switch", "-q", "-c", "dev2-copy")
        self.assertEqual(self.ws.repo_result("submit", "--source", "dev2-copy")["status"], "PROTECTED_BRANCH")
        git(self.ws.api, "switch", "-q", "dev2")
        self.assertEqual(self.ws.repo_result("submit", "--source", "dev")["status"], "PROTECTED_BRANCH")

    def test_one_failing_repository_does_not_stop_others(self) -> None:
        web = self.ws.clone("web", "feature-web")
        self.ws.commit(web, "w.txt", "w\n", "W")
        (self.ws.api / "dirty.txt").write_text("x", encoding="utf-8")
        results = {r["repo"]: r["status"] for r in self.ws.run("submit", "--source", "dev")["repositories"]}
        self.assertEqual(results, {"api": "DIRTY", "web": "PR_TEXT_REQUIRED"})


class PlannedSubmitTests(MultiRepoTestCase):
    def write_plan(self, entries: dict) -> str:
        path = self.ws.bundle.parent / "plan.json"
        path.write_text(json.dumps({"repositories": entries}, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def test_prepare_reports_work_without_changing_anything(self) -> None:
        self.ws.commit(self.ws.api, "a.txt", "a\n", "A 작업")
        (self.ws.api / "base.txt").write_text("changed\n", encoding="utf-8")
        (self.ws.api / "new.txt").write_text("n\n", encoding="utf-8")
        head = git(self.ws.api, "rev-parse", "HEAD")

        result = self.ws.repo_result("submit", "--source", "dev", "--prepare")
        self.assertEqual(result["status"], "READY")
        self.assertEqual(sorted(result["dirty"]), ["base.txt", "new.txt"])
        self.assertEqual(result["untracked"], ["new.txt"])
        self.assertIn("+changed", result["dirty_diff"])
        self.assertTrue(result["needs_pr_text"])
        self.assertEqual(result["commits"], ["A 작업"])
        self.assertEqual(git(self.ws.api, "rev-parse", "HEAD"), head)
        self.assertNotIn("feature", git(self.ws.remote, "branch"))

    def test_plan_commits_and_opens_prs_for_every_repository_in_one_call(self) -> None:
        web = self.ws.clone("web", "feature-web")
        (self.ws.api / "a.txt").write_text("a\n", encoding="utf-8")
        (web / "w.txt").write_text("w\n", encoding="utf-8")
        plan = self.write_plan({
            "api": {"commit": {"message": "A 기능 추가\n\n상세 설명", "paths": ["a.txt"]},
                    "pr": {"title": "A 기능", "body": "본문 A"}},
            "web": {"commit": {"message": "W 기능 추가", "paths": ["w.txt"]},
                    "pr": {"title": "W 기능", "body": "본문 W"}},
        })
        results = {r["repo"]: r for r in self.ws.run("submit", "--source", "dev", "--plan", plan)["repositories"]}
        self.assertEqual({k: v["status"] for k, v in results.items()}, {"api": "CREATED", "web": "CREATED"})
        self.assertEqual(git(self.ws.remote, "log", "-1", "--format=%B", "feature").strip(), "A 기능 추가\n\n상세 설명")
        self.assertEqual({(p["headRefName"], p["title"], p["body"]) for p in self.ws.prs()},
                         {("feature", "A 기능", "본문 A"), ("feature-web", "W 기능", "본문 W")})

        rerun = {r["repo"]: r for r in self.ws.run("submit", "--plan", plan)["repositories"]}
        self.assertEqual({k: (v["status"], v["committed"]) for k, v in rerun.items()},
                         {"api": ("UPDATED", False), "web": ("UPDATED", False)})
        self.assertEqual(len(self.ws.prs()), 2, "rerunning a plan never duplicates PRs")

    def test_plan_only_touches_listed_repositories_and_paths(self) -> None:
        web = self.ws.clone("web", "feature-web")
        (web / "w.txt").write_text("w\n", encoding="utf-8")
        (self.ws.api / "a.txt").write_text("a\n", encoding="utf-8")
        (self.ws.api / "unrelated.txt").write_text("u\n", encoding="utf-8")
        plan = self.write_plan({"api": {"commit": {"message": "A", "paths": ["a.txt"]}, "pr": {"title": "A"}}})
        results = self.ws.run("submit", "--source", "dev", "--plan", plan)["repositories"]
        self.assertEqual([(r["repo"], r["status"], r["dirty"]) for r in results], [("api", "DIRTY", ["unrelated.txt"])])
        self.assertTrue(results[0]["committed"])
        self.assertEqual((web / "w.txt").exists() and git(web, "status", "--porcelain"), "?? w.txt")

    def test_plan_commits_renames_and_deletions(self) -> None:
        git(self.ws.api, "mv", "base.txt", "moved.txt")
        plan = self.write_plan({"api": {"commit": {"message": "이동", "paths": ["base.txt", "moved.txt"]},
                                        "pr": {"title": "이동"}}})
        self.assertEqual(self.ws.repo_result("submit", "--source", "dev", "--plan", plan)["status"], "CREATED")
        self.assertEqual(git(self.ws.remote, "ls-tree", "--name-only", "feature"), "moved.txt")

    def test_plan_naming_unknown_repository_is_rejected(self) -> None:
        plan = self.write_plan({"nope": {}})
        self.assertEqual(self.ws.run("submit", "--plan", plan)["status"], "UNKNOWN_REPOSITORY")

    def test_branch_deleted_on_merge_is_pushed_again(self) -> None:
        self.ws.commit(self.ws.api, "a.txt", "a\n", "A")
        self.ws.repo_result("submit", "--source", "dev", "--title", "A")
        merged_head = self.ws.remote_sha("feature")
        self.ws.advance("dev", "a.txt", "a\n", "A (#1)")
        git(self.ws.remote, "branch", "-D", "feature")
        prs = self.ws.prs()
        prs[0].update(state="MERGED", headRefOid=merged_head)
        self.ws.gh_state.write_text(json.dumps(prs), encoding="utf-8")

        self.ws.commit(self.ws.api, "b.txt", "b\n", "B")
        result = self.ws.repo_result("submit", "--title", "B")
        self.assertEqual(result["status"], "CREATED")
        self.assertEqual(self.ws.remote_sha("feature"), git(self.ws.api, "rev-parse", "HEAD"))


class StatusTests(MultiRepoTestCase):
    def test_status_reports_branch_source_and_divergence(self) -> None:
        self.ws.commit(self.ws.api, "a.txt", "a\n", "A")
        self.ws.repo_result("submit", "--source", "dev", "--title", "A")
        self.ws.advance("dev", "o.txt", "o\n", "O")
        result = self.ws.repo_result("status")
        self.assertEqual((result["branch"], result["source"], result["source_from"]), ("feature", "dev", "config"))
        self.assertEqual((result["ahead"], result["behind"], result["unpushed"]), (1, 1, 0))
        self.assertEqual(result["pr"]["state"], "OPEN")


class SyncTests(MultiRepoTestCase):
    def test_sync_merges_source_locally_without_pushing(self) -> None:
        self.ws.commit(self.ws.api, "a.txt", "a\n", "A")
        self.ws.repo_result("submit", "--source", "dev", "--title", "A")
        pushed = self.ws.remote_sha("feature")
        source = self.ws.advance("dev", "d.txt", "d\n", "D")
        result = self.ws.repo_result("sync")
        self.assertEqual((result["status"], result["unpushed"]), ("SYNCED", 2))
        git(self.ws.api, "merge-base", "--is-ancestor", source, "HEAD")
        self.assertEqual(self.ws.remote_sha("feature"), pushed)
        self.assertEqual(len(self.ws.prs()), 1)
        self.assertEqual(self.ws.repo_result("sync")["status"], "UP_TO_DATE")

    def test_sync_skips_repository_with_uncommitted_changes(self) -> None:
        self.ws.commit(self.ws.api, "a.txt", "a\n", "A")
        before = git(self.ws.api, "rev-parse", "HEAD")
        (self.ws.api / "wip.txt").write_text("wip\n", encoding="utf-8")
        self.ws.advance("dev", "d.txt", "d\n", "D")
        result = self.ws.repo_result("sync", "--source", "dev")
        self.assertEqual((result["status"], result["dirty"]), ("DIRTY", ["wip.txt"]))
        self.assertEqual(git(self.ws.api, "rev-parse", "HEAD"), before)

    def test_sync_conflict_is_resumed_after_resolution(self) -> None:
        self.ws.commit(self.ws.api, "base.txt", "mine\n", "내 변경")
        self.ws.advance("dev", "base.txt", "theirs\n", "상대 변경")
        self.assertEqual(self.ws.repo_result("sync", "--source", "dev")["conflicts"], ["base.txt"])
        (self.ws.api / "base.txt").write_text("both\n", encoding="utf-8")
        git(self.ws.api, "add", "base.txt")
        self.assertEqual(self.ws.repo_result("sync")["status"], "SYNCED")
        self.assertEqual(git(self.ws.api, "status", "--porcelain"), "")
        self.assertIn("상대 변경", git(self.ws.api, "log", "--format=%s"))

    def squash_merge_pr(self) -> None:
        self.ws.commit(self.ws.api, "a.txt", "a\n", "A")
        self.ws.repo_result("submit", "--source", "dev", "--title", "A")
        self.tip = self.ws.advance("dev", "a.txt", "a\n", "A (#1)")
        prs = self.ws.prs()
        prs[0].update(state="MERGED", headRefOid=self.ws.remote_sha("feature"))
        self.ws.gh_state.write_text(json.dumps(prs), encoding="utf-8")

    def test_sync_after_merge_restarts_at_source_and_deletes_remote_branch(self) -> None:
        self.squash_merge_pr()
        merged = self.ws.remote_sha("feature")
        result = self.ws.repo_result("sync")
        self.assertEqual((result["status"], result["head"], result["deleted_remote"]), ("SYNCED", self.tip, merged))
        self.assertIn("moved_to_source", result)
        self.assertNotIn("feature", git(self.ws.remote, "branch"))
        self.assertEqual(self.ws.repo_result("sync")["status"], "UP_TO_DATE")

    def test_sync_keeps_remote_branch_of_open_pr_or_with_new_work(self) -> None:
        self.ws.commit(self.ws.api, "a.txt", "a\n", "A")
        self.ws.repo_result("submit", "--source", "dev", "--title", "A")
        self.ws.advance("dev", "d.txt", "d\n", "D")
        result = self.ws.repo_result("sync")
        self.assertNotIn("deleted_remote", result)
        self.assertIn("feature", git(self.ws.remote, "branch"))

        self.ws.advance("dev", "a.txt", "a\n", "A (#1)")  # content is in dev but the PR is still open
        result = self.ws.repo_result("sync")
        self.assertEqual(result["remote_kept"], "PR #1 is still open")
        self.assertIn("feature", git(self.ws.remote, "branch"))


    def test_sync_never_deletes_a_shared_source_branch(self) -> None:
        self.squash_merge_pr()
        git(self.ws.api, "config", "branch.other.multiRepoSource", "feature")  # another task builds on it
        result = self.ws.repo_result("sync")
        self.assertEqual(result["remote_kept"], "feature is the source of other branches or open PRs")
        self.assertIn("feature", git(self.ws.remote, "branch"))

    def test_sync_without_merged_pr_keeps_remote_branch(self) -> None:
        self.ws.commit(self.ws.api, "a.txt", "a\n", "A")
        self.ws.repo_result("submit", "--source", "dev")  # pushed, no PR
        self.ws.advance("dev", "a.txt", "a\n", "A")
        result = self.ws.repo_result("sync")
        self.assertEqual(result["remote_kept"], "no merged PR from this branch")


class PromoteTests(MultiRepoTestCase):
    def test_plan_then_execute_fast_forwards_each_stage(self) -> None:
        tip = self.ws.advance("dev", "d.txt", "d\n", "배포할 변경")
        plan = self.ws.run("promote", "--from", "dev")
        steps = plan["repositories"][0]["steps"]
        self.assertEqual([(s["to"], s["action"]) for s in steps], [("dev2", "PUSH"), ("prod", "PUSH")])
        self.assertTrue(steps[0]["commits"][0].endswith("배포할 변경"))

        done = self.ws.run("promote", "--from", "dev", "--execute", plan["plan_id"])
        self.assertEqual(done["repositories"][0]["promoted"], ["dev2", "prod"])
        self.assertEqual((self.ws.remote_sha("dev2"), self.ws.remote_sha("prod")), (tip, tip))
        again = self.ws.run("promote", "--from", "dev")
        self.assertEqual({s["action"] for s in again["repositories"][0]["steps"]}, {"UP_TO_DATE"})

    def test_feature_source_starts_the_chain(self) -> None:
        self.ws.commit(self.ws.api, "a.txt", "a\n", "A")
        self.ws.repo_result("submit", "--source", "dev", "--title", "A")
        plan = self.ws.run("promote", "--from", "feature")
        self.assertEqual(plan["repositories"][0]["stages"], ["feature", "dev", "dev2", "prod"])

    def test_task_branch_goes_straight_into_dev(self) -> None:
        self.ws.commit(self.ws.api, "a.txt", "a\n", "A")
        self.ws.repo_result("submit", "--source", "dev", "--title", "A")
        tip = git(self.ws.api, "rev-parse", "HEAD")
        plan = self.ws.run("promote", "--to", "dev")
        result = plan["repositories"][0]
        self.assertEqual((result["stages"], result["task"]), (["feature", "dev"], "feature"))
        self.assertEqual([(s["to"], s["action"]) for s in result["steps"]], [("dev", "PUSH")])

        prod = self.ws.remote_sha("prod")
        done = self.ws.run("promote", "--to", "dev", "--execute", plan["plan_id"])
        self.assertEqual(done["repositories"][0]["promoted"], ["dev"])
        self.assertEqual((self.ws.remote_sha("dev"), self.ws.remote_sha("prod")), (tip, prod))

    def test_unsubmitted_task_branch_requires_submit(self) -> None:
        (self.ws.api / "a.txt").write_text("a\n", encoding="utf-8")
        result = self.ws.repo_result("promote")
        self.assertEqual((result["status"], result["reason"]), ("SUBMIT_REQUIRED", "uncommitted"))

        git(self.ws.api, "add", "a.txt")
        git(self.ws.api, "commit", "-q", "-m", "A")
        result = self.ws.repo_result("promote")
        self.assertEqual((result["status"], result["reason"]), ("SUBMIT_REQUIRED", "unpushed"))

        self.ws.repo_result("submit", "--source", "dev", "--title", "A")
        self.ws.advance("dev", "d.txt", "d\n", "D")
        result = self.ws.repo_result("promote")
        self.assertEqual((result["status"], result["reason"]), ("SUBMIT_REQUIRED", "behind"))

    def test_task_branch_without_new_work_starts_at_its_source(self) -> None:
        self.ws.advance("dev", "d.txt", "d\n", "D")
        result = self.ws.repo_result("promote")
        self.assertEqual((result["status"], result["stages"], result["task"]), ("PLANNED", ["dev", "dev2", "prod"], None))

    def test_stale_plan_pushes_nothing(self) -> None:
        self.ws.advance("dev", "d.txt", "d\n", "D")
        plan = self.ws.run("promote", "--from", "dev")
        self.ws.advance("dev", "e.txt", "e\n", "E")
        before = self.ws.remote_sha("prod")
        result = self.ws.run("promote", "--from", "dev", "--execute", plan["plan_id"])
        self.assertEqual(result["status"], "PLAN_CHANGED")
        self.assertEqual(self.ws.remote_sha("prod"), before)

    def test_diverged_stage_blocks_that_repository(self) -> None:
        self.ws.advance("dev", "d.txt", "d\n", "D")
        self.ws.advance("dev2", "x.txt", "x\n", "hotfix")
        result = self.ws.run("promote", "--from", "dev")["repositories"][0]
        self.assertEqual(result["status"], "DIVERGED")

    def test_ahead_stage_is_carried_forward(self) -> None:
        hotfix = self.ws.advance("dev2", "x.txt", "x\n", "hotfix")
        steps = self.ws.run("promote", "--from", "dev")["repositories"][0]["steps"]
        self.assertEqual([(s["to"], s["action"], s["after"]) for s in steps],
                         [("dev2", "AHEAD", hotfix), ("prod", "PUSH", hotfix)])


class SkillContractTests(unittest.TestCase):
    def test_skill_is_explicit_only_and_points_at_the_runner(self) -> None:
        text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("name: multi-repo", text)
        self.assertIn("disable-model-invocation: true", text)
        self.assertIn("$common-skills:multi-repo", text)
        self.assertIn("/common-skills:multi-repo", text)
        self.assertIn("scripts/multi_repo.py", text)
        for status in ("SOURCE_REQUIRED", "DIRTY", "CONFLICT", "PR_TEXT_REQUIRED", "CREATED", "UPDATED",
                       "NO_CHANGES", "PROTECTED_BRANCH", "PUSH_REJECTED", "DIVERGED", "PLAN_CHANGED",
                       "SUBMIT_REQUIRED", "SYNCED"):
            self.assertIn(status, text)
        yaml = (SKILL_DIR / "agents" / "openai.yaml").read_text(encoding="utf-8")
        self.assertIn("allow_implicit_invocation: false", yaml)


if __name__ == "__main__":
    unittest.main()
