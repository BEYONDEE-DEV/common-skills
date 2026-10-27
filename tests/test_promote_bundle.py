from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "multi-repo-worktree" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import promote_bundle as promote
import slot_manifest as manifest
import submit_bundle as shared


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.PIPE).decode().strip()


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = self.root / "worktrees" / "slot-1"
        self.base.mkdir(parents=True)
        self.data = manifest.empty_manifest()
        self.sources, self.tasks, self.remotes, self.initial, self.release = {}, {}, {}, {}, {}
        patcher = mock.patch.object(shared, "remote_identity", side_effect=lambda repo, remote: (
            "owner/" + repo.name, shared.digest([
                git(repo, "remote", "get-url", remote), git(repo, "remote", "get-url", "--push", remote)])))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.add_repo("web")

    def add_repo(self, name, source_branch="feature-custom"):
        repo, remote, task = self.root / name, self.root / (name + ".git"), self.base / name
        repo.mkdir()
        git(repo, "init", "-b", source_branch)
        git(repo, "config", "user.name", "Promotion Test")
        git(repo, "config", "user.email", "promote@example.invalid")
        git(repo, "config", "core.hooksPath", str(self.root / "empty-hooks"))
        (repo / "base.txt").write_text("base\n")
        git(repo, "add", "base.txt")
        git(repo, "commit", "-m", "기준")
        initial = git(repo, "rev-parse", "HEAD")
        git(repo, "init", "--bare", str(remote))
        git(repo, "remote", "add", "origin", str(remote))
        for branch in promote.STAGES:
            if branch != source_branch:
                git(repo, "branch", branch, initial)
        (repo / "release.txt").write_text("release A\n")
        git(repo, "add", "release.txt")
        git(repo, "commit", "-m", "작업 A")
        release = git(repo, "rev-parse", "HEAD")
        git(repo, "push", "origin", source_branch, *[b for b in promote.STAGES if b != source_branch])
        git(repo, "worktree", "add", "-b", "task", str(task))
        self.sources[name], self.tasks[name], self.remotes[name] = repo, task, remote
        self.initial[name], self.release[name] = initial, release
        identity = {"branch_family": "task", "branch": "task", "branch_generation": 1,
                    "branch_base_sha": release, "source_branch": source_branch, "remote": "origin",
                    "generation": 0, "pr": None, "submission": None}
        self.data["slots"].setdefault("slot-1", {"path": str(self.base), "repositories": {}})["repositories"][name] = identity
        manifest.write_manifest(manifest.manifest_path(self.root), self.data)

    def runner(self, bundle="slot-1"):
        return promote.Runner(self.root, bundle)

    def plan(self, selected=None):
        prepared = self.runner().prepare(selected)
        return json.loads(Path(prepared["plan_path"]).read_text())

    def execute(self, plan):
        return self.runner().execute(plan)["repositories"]

    def refs(self, name="web"):
        return shared.remote_heads(self.tasks[name], "origin", "feature-custom", *promote.STAGES)

    def commit(self, name="web", filename="more.txt"):
        repo = self.sources[name]
        (repo / filename).write_text(filename + "\n")
        git(repo, "add", filename)
        git(repo, "commit", "-m", "후속 변경")
        return git(repo, "rev-parse", "HEAD")

    def test_route_uses_variable_source_and_skips_prior_stages(self):
        self.assertEqual(promote.route("feature-a"), ["feature-a", "dev", "dev2", "prod"])
        self.assertEqual(promote.route("dev"), ["dev", "dev2", "prod"])
        self.assertEqual(promote.route("dev2"), ["dev2", "prod"])
        self.assertEqual(promote.route("prod"), ["prod"])

    def test_inspection_is_read_only_with_dirty_local_work(self):
        task = self.tasks["web"]
        (task / "future.txt").write_text("B stays local\n")
        before = git(task, "show-ref"), git(task, "status", "--porcelain"), self.refs()
        metadata = {str(p): p.read_bytes() for p in (self.root / ".multi-repo-worktree").rglob("*") if p.is_file()}
        result = self.runner().inspect()
        self.assertEqual(result["repositories"][0]["state"], "READY")
        self.assertEqual(before, (git(task, "show-ref"), git(task, "status", "--porcelain"), self.refs()))
        self.assertEqual(metadata, {str(p): p.read_bytes() for p in (self.root / ".multi-repo-worktree").rglob("*") if p.is_file()})

    def test_promote_preserves_task_B_original_worktree_and_manifest(self):
        task, original = self.tasks["web"], self.sources["web"]
        (task / "B.txt").write_text("local B\n")
        git(task, "add", "B.txt")
        git(task, "commit", "-m", "로컬 B")
        (task / "dirty.txt").write_text("in-progress B\n")
        (original / "original-dirty.txt").write_text("original work\n")
        before = [(git(p, "rev-parse", "HEAD"), git(p, "status", "--porcelain"),
                   git(p, "for-each-ref", "refs/heads")) for p in (task, original)]
        manifest_bytes = manifest.manifest_path(self.root).read_bytes()
        result = self.execute(self.plan())[0]
        self.assertEqual(result["state"], "PROMOTED", result)
        self.assertEqual(set(self.refs().values()), {self.release["web"]})
        self.assertEqual(before, [(git(p, "rev-parse", "HEAD"), git(p, "status", "--porcelain"),
                                  git(p, "for-each-ref", "refs/heads")) for p in (task, original)])
        self.assertEqual(manifest_bytes, manifest.manifest_path(self.root).read_bytes())
        self.assertEqual(git(self.remotes["web"], "ls-tree", "prod", "B.txt"), "")

    def test_dev_and_dev2_sources_start_at_bound_stage(self):
        for name, source in (("api", "dev"), ("client", "dev2")):
            self.add_repo(name, source)
            row = self.plan([name])["repositories"][0]
            self.assertEqual(row["route"], promote.route(source))
            result = self.execute(self.plan([name]))[0]
            self.assertEqual(result["prod_sha"], self.release[name])
            self.assertEqual(result["state"], "PROMOTED")

    def test_noop_and_repeat_of_same_plan(self):
        plan = self.plan()
        first = self.execute(plan)[0]
        before = git(self.remotes["web"], "show-ref")
        self.assertEqual(self.execute(plan)[0]["state"], "PROMOTED")
        self.assertEqual(before, git(self.remotes["web"], "show-ref"))
        self.assertEqual(first["prod_sha"], self.release["web"])
        self.assertEqual(self.execute(self.plan())[0]["state"], "NOOP")

    def test_ahead_stage_is_preserved_and_its_reviewed_tip_is_promoted(self):
        extra = self.commit()
        git(self.sources["web"], "push", "origin", f"{extra}:refs/heads/dev")
        plan = self.plan()
        row = plan["repositories"][0]
        self.assertEqual(row["steps"][0]["action"], "NOOP")
        self.assertEqual(row["steps"][1]["source_sha"], extra)
        self.assertIn("more.txt", row["steps"][1]["changed_paths"])
        self.assertEqual(self.execute(plan)[0]["prod_sha"], extra)
        self.assertEqual(self.refs()["feature-custom"], self.release["web"])

    def test_diverged_later_stage_blocks_repository_before_first_push(self):
        repo = self.sources["web"]
        git(repo, "switch", "dev2")
        other = self.commit(filename="other.txt")
        git(repo, "push", "origin", f"{other}:refs/heads/dev2")
        before = self.refs()
        plan = self.plan()
        self.assertEqual(plan["repositories"][0]["state"], "BLOCKED")
        self.assertIn("diverged", plan["repositories"][0]["error"])
        self.assertEqual(self.execute(plan)[0]["state"], "FAILED")
        self.assertEqual(before, self.refs())

    def test_missing_branch_is_not_created(self):
        git(self.remotes["web"], "update-ref", "-d", "refs/heads/prod")
        plan = self.plan()
        self.assertIn("missing remote branches", plan["repositories"][0]["error"])
        self.assertEqual(self.execute(plan)[0]["state"], "FAILED")
        self.assertNotIn("prod", self.refs())

    def test_source_changed_after_prepare_requires_new_plan(self):
        plan = self.plan()
        newer = self.commit()
        git(self.sources["web"], "push", "origin", f"{newer}:refs/heads/feature-custom")
        before = self.refs()
        result = self.execute(plan)[0]
        self.assertEqual(result["state"], "FAILED")
        self.assertIn("outside this plan", result["error"])
        self.assertEqual(before, self.refs())

    def test_push_rejection_preserves_progress_and_exact_retry_resumes(self):
        plan = self.plan()
        real_g = shared.g

        def rejecting(repo, *args):
            if "push" in args and args[-1].endswith(":refs/heads/dev2"):
                raise PromotionErrorForTest("simulated protection rejection")
            return real_g(repo, *args)

        with mock.patch.object(shared, "g", side_effect=rejecting):
            failed = self.execute(plan)[0]
        self.assertEqual(failed["state"], "FAILED")
        self.assertEqual(self.refs()["dev"], self.release["web"])
        self.assertEqual(self.refs()["dev2"], self.initial["web"])
        self.assertEqual(failed["steps"][0]["state"], "PROMOTED")
        self.assertEqual(self.execute(plan)[0]["state"], "PROMOTED")
        self.assertEqual(self.refs()["prod"], self.release["web"])

    def test_response_loss_after_push_is_adopted_without_pushing_it_again(self):
        plan = self.plan()
        real_g, lost = shared.g, [False]

        def losing(repo, *args):
            result = real_g(repo, *args)
            if "push" in args and not lost[0]:
                lost[0] = True
                raise PromotionErrorForTest("connection lost after accepted push")
            return result

        with mock.patch.object(shared, "g", side_effect=losing):
            self.assertEqual(self.execute(plan)[0]["state"], "FAILED")
        self.assertEqual(self.refs()["dev"], self.release["web"])
        with mock.patch.object(shared, "g", wraps=real_g) as calls:
            self.assertEqual(self.execute(plan)[0]["state"], "PROMOTED")
        pushed = [c.args[-1] for c in calls.call_args_list if "push" in c.args]
        self.assertEqual(pushed, [f"{self.release['web']}:refs/heads/dev2", f"{self.release['web']}:refs/heads/prod"])

    def test_failed_repository_does_not_stop_clean_sibling(self):
        self.add_repo("api", "dev")
        git(self.remotes["web"], "update-ref", "-d", "refs/heads/prod")
        rows = self.execute(self.plan())
        self.assertEqual({r["repository"]: r["state"] for r in rows}, {"web": "FAILED", "api": "PROMOTED"})
        self.assertEqual(self.refs("api")["prod"], self.release["api"])

    def test_selection_and_plan_scope_are_exact(self):
        with self.assertRaises(promote.PromotionError):
            self.runner().inspect(["../web"])
        with self.assertRaises(promote.PromotionError):
            self.runner().inspect(["web", "web"])
        plan = self.plan()
        plan["repositories"][0]["steps"][0]["after"] = self.initial["web"]
        with self.assertRaises(promote.PromotionError):
            self.execute(plan)

    def test_slot_and_shared_repository_locks_are_respected(self):
        plan = self.plan()
        manifest.acquire_operation_lock(self.root, "slot-1", "other")
        try:
            with self.assertRaises(manifest.ManifestError):
                self.execute(plan)
        finally:
            manifest.release_operation_lock(self.root, "slot-1", "other")
        key = "promote:owner/web"
        manifest.acquire_operation_lock(self.root, key, "other-slot")
        try:
            self.assertEqual(self.execute(plan)[0]["state"], "FAILED")
            self.assertEqual(self.refs()["dev"], self.initial["web"])
            self.assertEqual(manifest.load_operation_lock(manifest.operation_lock_path(self.root, key))["token"], "other-slot")
        finally:
            manifest.release_operation_lock(self.root, key, "other-slot")

    def test_remote_and_manifest_drift_fail_before_publication(self):
        plan = self.plan()
        self.data["slots"]["slot-1"]["repositories"]["web"]["source_branch"] = "dev"
        manifest.write_manifest(manifest.manifest_path(self.root), self.data)
        before = self.refs()
        self.assertEqual(self.execute(plan)[0]["state"], "FAILED")
        self.assertEqual(before, self.refs())

    def test_mirror_remote_is_blocked(self):
        git(self.tasks["web"], "config", "remote.origin.mirror", "yes")
        row = self.runner().inspect()["repositories"][0]
        self.assertEqual(row["state"], "BLOCKED")
        self.assertIn("mirror", row["error"])

    def test_concurrent_divergent_update_is_rejected_by_normal_git_push(self):
        plan = self.plan()
        git(self.sources["web"], "switch", "dev")
        concurrent = self.commit(filename="concurrent.txt")
        real_g, raced = shared.g, [False]

        def racing(repo, *args):
            if "push" in args and not raced[0]:
                raced[0] = True
                git(self.sources["web"], "push", "origin", f"{concurrent}:refs/heads/dev")
            return real_g(repo, *args)

        with mock.patch.object(shared, "g", side_effect=racing):
            result = self.execute(plan)[0]
        self.assertEqual(result["state"], "FAILED")
        self.assertEqual(self.refs()["dev"], concurrent)
        self.assertEqual(self.refs()["dev2"], self.initial["web"])
        self.assertEqual(self.refs()["prod"], self.initial["web"])

    def test_source_advancement_between_steps_stops_without_promoting_new_commits(self):
        plan = self.plan()
        newer = self.commit()
        real_g, changed = shared.g, [False]

        def advancing(repo, *args):
            result = real_g(repo, *args)
            if "push" in args and not changed[0]:
                changed[0] = True
                git(self.sources["web"], "push", "origin", f"{newer}:refs/heads/feature-custom")
            return result

        with mock.patch.object(shared, "g", side_effect=advancing):
            result = self.execute(plan)[0]
        self.assertEqual(result["state"], "FAILED")
        self.assertEqual(self.refs()["dev"], self.release["web"])
        self.assertEqual(self.refs()["feature-custom"], newer)
        self.assertEqual(self.refs()["prod"], self.initial["web"])

    def test_target_deleted_between_last_query_and_push_is_not_recreated(self):
        plan = self.plan()
        real_g, deleted = shared.g, [False]

        def deleting(repo, *args):
            if "push" in args and not deleted[0]:
                deleted[0] = True
                git(self.remotes["web"], "update-ref", "-d", "refs/heads/dev")
            return real_g(repo, *args)

        with mock.patch.object(shared, "g", side_effect=deleting):
            result = self.execute(plan)[0]
        self.assertEqual(result["state"], "FAILED")
        self.assertNotIn("dev", self.refs())
        self.assertEqual(self.refs()["dev2"], self.initial["web"])
        self.assertEqual(self.refs()["prod"], self.initial["web"])

    def test_prod_source_is_successful_noop(self):
        self.add_repo("api", "prod")
        result = self.execute(self.plan(["api"]))[0]
        self.assertEqual(result["state"], "NOOP")
        self.assertEqual(result["steps"], [])
        self.assertEqual(result["prod_sha"], self.release["api"])

    def test_cli_prepare_and_execute_use_generated_plan(self):
        # The real CLI rejects file remotes by default; keep that GitHub identity
        # boundary in place and test the callable CLI with the fixture identity.
        import contextlib
        import io
        output = io.StringIO()
        args = ["promote_bundle.py", "--root", str(self.root), "--bundle", "slot-1", "--prepare"]
        with mock.patch.object(sys, "argv", args), contextlib.redirect_stdout(output):
            self.assertEqual(promote.main(), 0)
        path = json.loads(output.getvalue())["plan_path"]
        output = io.StringIO()
        with mock.patch.object(sys, "argv", args[:-1] + ["--execute", path]), contextlib.redirect_stdout(output):
            self.assertEqual(promote.main(), 0)
        self.assertEqual(json.loads(output.getvalue())["repositories"][0]["prod_sha"], self.release["web"])


class PromotionErrorForTest(RuntimeError):
    pass


if __name__ == "__main__":
    unittest.main()
