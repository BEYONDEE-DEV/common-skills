from __future__ import annotations

import unittest
from unittest import mock

from tests import test_submit_bundle as fixtures
import cleanup_merged_branch as cleanup
import sync_bundle as sync

git, manifest, submit = fixtures.git, fixtures.manifest, fixtures.submit


class SyncTests(unittest.TestCase):
    # Share only the isolated Git/GitHub fixture, not the submit test cases.
    setUp = fixtures.SubmitTests.setUp
    local_identity = fixtures.SubmitTests.local_identity
    add_repo = fixtures.SubmitTests.add_repo
    change = fixtures.SubmitTests.change
    plan = fixtures.SubmitTests.plan
    runner = fixtures.SubmitTests.runner
    run_plan = fixtures.SubmitTests.run_plan
    assertState = fixtures.SubmitTests.assertState

    def sync_runner(self):
        return sync.Runner(self.root, "worktrees/slot-3", api=self.api)

    def sync_plan(self, name="web"):
        row = next(row for row in self.sync_runner().preflight()["repositories"] if row["repository"] == name)
        return {**row.get("plan", {}), "transfer_subject": "후속 기능 유지"}

    def run_sync(self, name="web", plan=None):
        plans = {name: self.sync_plan(name) if plan is None else plan}
        return self.sync_runner().execute(plans)["repositories"][0]

    def local_commit(self, name="web", filename="next.txt", text="continuation\n"):
        self.change(name, filename, text)
        git(self.repos[name], "add", filename)
        git(self.repos[name], "commit", "-m", "후속 기능")

    def advance_source(self, name="web"):
        source = self.sources[name]
        (source / "source.txt").write_text("source advancement\n" + git(source, "rev-parse", "HEAD"))
        git(source, "add", "source.txt")
        git(source, "commit", "-m", "원본 변경")
        git(source, "push", "origin", "main")
        return git(source, "rev-parse", "HEAD")

    def publish_and_merge(self, name="web"):
        self.change(name)
        result = self.runner().execute({name: self.plan(name)})["repositories"][0]
        self.assertState(result, "SUBMITTED")
        return self.merge_current(name)

    def merge_current(self, name="web"):
        identity = self.runner().identity(name)
        source = self.sources[name]
        git(source, "merge", "--squash", identity["branch"])
        git(source, "commit", "-m", "기능 스쿼시 병합")
        git(source, "push", "origin", "main")
        merged = git(source, "rev-parse", "HEAD")
        number = int(identity["pr"].rsplit("/", 1)[1])
        self.api.prs[("owner/" + name, number)].update(state="closed", merged_at="2026-09-10T12:00:00Z", merge_commit_sha=merged)
        return merged

    def test_preflight_is_read_only_and_does_not_query_dirty_repo_api_or_contents(self):
        self.change()
        before = git(self.repos["web"], "show-ref"), git(self.repos["web"], "status", "--porcelain")
        metadata = {str(p): p.read_bytes() for p in (self.root / ".multi-repo-worktree").rglob("*") if p.is_file()}
        with mock.patch.object(self.api, "find", side_effect=AssertionError("dirty API query")), \
                mock.patch.object(self.api, "view", side_effect=AssertionError("dirty API query")), \
                mock.patch.object(submit, "worktree_fingerprint", side_effect=AssertionError("unnecessary content hash")):
            result = self.sync_runner().preflight()
        self.assertEqual(result["repositories"][0]["action"], "SKIP_DIRTY")
        self.assertEqual(before, (git(self.repos["web"], "show-ref"), git(self.repos["web"], "status", "--porcelain")))
        self.assertEqual(metadata, {str(p): p.read_bytes() for p in (self.root / ".multi-repo-worktree").rglob("*") if p.is_file()})

    def test_none_unchanged_noop_and_no_submit_actions(self):
        with mock.patch.object(submit.Runner, "commit", side_effect=AssertionError("task commit")), \
                mock.patch.object(submit.Runner, "publish", side_effect=AssertionError("publication")), \
                mock.patch.object(self.api, "create", side_effect=AssertionError("PR creation")):
            result = self.run_sync()
        self.assertState(result, "NOOP")
        self.assertEqual(result["rotations"], [])
        self.assertEqual(result["cleanups"], [])

    def test_none_advance_transfers_local_work_and_never_publishes(self):
        self.local_commit()
        source = self.advance_source()
        result = self.run_sync()
        self.assertState(result, "SYNCED")
        repo = self.repos["web"]
        self.assertEqual(result["branch"], "task-mrw-g2")
        self.assertEqual(git(repo, "show", "-s", "--format=%P", "HEAD"), source)
        self.assertEqual(git(repo, "diff", "--name-only", f"{source}...HEAD"), "next.txt")
        self.assertEqual(submit.remote_heads(repo, "origin", "task", "task-mrw-g2"), {})
        self.assertEqual(git(self.sources["web"], "rev-parse", "HEAD"), source)
        self.assertEqual(git(self.sources["web"], "status", "--porcelain"), "")
        self.assertEqual(self.api.create_calls, 0)

    def test_empty_source_rotation_needs_no_transfer_subject(self):
        source = self.advance_source()
        plan = self.sync_plan()
        plan["transfer_subject"] = None
        self.assertState(self.run_sync(plan=plan), "SYNCED")
        self.assertEqual(git(self.repos["web"], "rev-parse", "HEAD"), source)

    def test_ignored_file_is_not_overwritten_by_generation_switch(self):
        self.local_commit(filename=".gitignore", text="runtime.txt\n")
        repo = self.repos["web"]
        (repo / "runtime.txt").write_text("important local ignored data\n")
        source = self.sources["web"]
        (source / "runtime.txt").write_text("tracked upstream content\n")
        git(source, "add", "runtime.txt")
        git(source, "commit", "-m", "원본 파일 추가")
        git(source, "push", "origin", "main")
        old = git(repo, "rev-parse", "HEAD")
        result = self.run_sync()
        self.assertState(result, "FAILED")
        self.assertEqual((repo / "runtime.txt").read_text(), "important local ignored data\n")
        self.assertEqual(git(repo, "rev-parse", "HEAD"), old)
        self.assertEqual(git(repo, "branch", "--show-current"), "task")
        self.assertEqual(self.runner().identity("web")["rotation"]["phase"], "branch-created")

    def test_clean_open_only_fetches_and_waits_even_with_source_advance(self):
        self.change()
        self.assertState(self.run_plan(), "SUBMITTED")
        self.local_commit()
        source = self.advance_source()
        repo = self.repos["web"]
        before = git(repo, "rev-parse", "HEAD"), self.runner().identity("web"), git(self.remotes["web"], "show-ref")
        with mock.patch.object(submit.Runner, "rotate", side_effect=AssertionError("OPEN rotation")), \
                mock.patch.object(cleanup, "cleanup", side_effect=AssertionError("OPEN cleanup")):
            self.assertState(self.run_sync(), "WAITING")
        self.assertEqual(before, (git(repo, "rev-parse", "HEAD"), self.runner().identity("web"), git(self.remotes["web"], "show-ref")))
        self.assertEqual(git(repo, "rev-parse", "refs/remotes/origin/main"), source)

    def test_mixed_dirty_and_clean_success_are_independent(self):
        self.add_repo("api")
        self.change()
        self.advance_source("api")
        before = git(self.repos["web"], "show-ref"), git(self.repos["web"], "status", "--porcelain")
        results = self.sync_runner().execute({"web": {}, "api": self.sync_plan("api")})["repositories"]
        self.assertEqual([row["state"] for row in results], ["SKIPPED", "SYNCED"])
        self.assertEqual(before, (git(self.repos["web"], "show-ref"), git(self.repos["web"], "status", "--porcelain")))

    def test_merged_squash_transfers_only_B_and_deletes_exact_old_refs(self):
        merged = self.publish_and_merge()
        self.local_commit()
        result = self.run_sync()
        self.assertState(result, "SYNCED")
        repo = self.repos["web"]
        self.assertEqual(git(repo, "diff", "--name-only", f"{merged}...HEAD"), "next.txt")
        self.assertEqual(submit.remote_heads(repo, "origin", "task"), {})
        self.assertEqual(git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/task"), "")
        self.assertEqual(self.runner().identity("web")["generation"], 1)
        self.assertEqual(result["cleanups"][0]["status"], "deleted")
        self.assertState(self.run_sync(), "NOOP")

    def test_cleanup_failure_blocks_next_rotation_but_preserves_completed_rotation(self):
        self.publish_and_merge()
        self.local_commit()
        with mock.patch.object(cleanup, "cleanup", side_effect=cleanup.CleanupError("remote unavailable")):
            first = self.run_sync()
        self.assertState(first, "FAILED")
        self.assertEqual(self.runner().identity("web")["branch"], "task-mrw-g2")
        self.assertTrue(submit.remote_heads(self.repos["web"], "origin", "task"))
        self.advance_source()
        with mock.patch.object(submit.Runner, "rotate", side_effect=AssertionError("rotated before cleanup")), \
                mock.patch.object(cleanup, "cleanup", side_effect=cleanup.CleanupError("remote still unavailable")):
            self.assertState(self.run_sync(), "FAILED")
        last = self.run_sync()
        self.assertState(last, "SYNCED")
        self.assertEqual(last["branch"], "task-mrw-g3")
        self.assertEqual(last["retry_count"], 2)

    def test_remote_deletion_response_loss_retries_without_new_generation(self):
        self.publish_and_merge()
        original = cleanup.git
        def lose_response(repo, *args, **kwargs):
            result = original(repo, *args, **kwargs)
            if args[0] == "push":
                raise cleanup.CleanupError("response lost after actual remote deletion")
            return result
        with mock.patch.object(cleanup, "git", side_effect=lose_response):
            self.assertState(self.run_sync(), "FAILED")
        identity = self.runner().identity("web")
        self.assertEqual(submit.remote_heads(self.repos["web"], "origin", "task"), {})
        self.assertState(self.run_sync(), "NOOP")
        self.assertEqual(self.runner().identity("web"), identity)

    def test_rotation_switch_crash_resumes_exact_target(self):
        self.local_commit()
        self.advance_source()
        original = manifest.advance_rotation
        def crash(data, slot, name, expected, target, target_head_sha=None):
            if target == "switched":
                raise manifest.ManifestError("lost switch receipt")
            return original(data, slot, name, expected, target, target_head_sha)
        with mock.patch.object(manifest, "advance_rotation", side_effect=crash):
            self.assertState(self.run_sync(), "FAILED")
        head = git(self.repos["web"], "rev-parse", "HEAD")
        result = self.run_sync()
        self.assertState(result, "SYNCED")
        self.assertEqual(result["head_sha"], head)
        self.assertTrue(result["rotations"][0]["resumed"])

    def test_legacy_branch_created_missing_target_sha_is_verified_and_backfilled(self):
        self.local_commit()
        self.advance_source()
        original = submit.g
        def before_switch(repo, *args):
            if args[0] == "switch":
                raise submit.SubmitError("switch interrupted")
            return original(repo, *args)
        with mock.patch.object(submit, "g", side_effect=before_switch):
            self.assertState(self.run_sync(), "FAILED")
        path = manifest.manifest_path(self.root)
        data = manifest.load_manifest(path)
        rotation = data["slots"]["slot-3"]["repositories"]["web"]["rotation"]
        expected = rotation.pop("target_head_sha")
        manifest.write_manifest(path, data)
        result = self.run_sync()
        self.assertState(result, "SYNCED")
        self.assertEqual(result["head_sha"], expected)
        self.assertNotIn("rotation", self.runner().identity("web"))

    def test_pending_submit_receipt_blocks_sync_without_consuming_it(self):
        self.change()
        self.api.fail_after_create = True
        self.assertState(self.run_plan(), "FAILED")
        receipt = self.runner().receipt_path
        before = receipt.read_bytes(), git(self.repos["web"], "show-ref")
        result = self.run_sync()
        self.assertState(result, "FAILED")
        self.assertIn("submit recovery", result["error"])
        self.assertEqual(before, (receipt.read_bytes(), git(self.repos["web"], "show-ref")))

    def test_unrecorded_remote_branch_without_receipt_is_not_rotated(self):
        self.local_commit()
        git(self.repos["web"], "push", "origin", "task")
        self.advance_source()
        result = self.run_sync()
        self.assertState(result, "FAILED")
        self.assertIn("unrecorded remote", result["error"])
        self.assertEqual(self.runner().identity("web")["branch"], "task")

    def test_draft_and_moved_remote_are_not_cleaned(self):
        self.publish_and_merge()
        self.api.prs[("owner/web", 1)]["draft"] = True
        self.assertState(self.run_sync(), "FAILED")
        self.api.prs[("owner/web", 1)]["draft"] = False
        self.local_commit()
        git(self.repos["web"], "push", "origin", "task")
        before = git(self.remotes["web"], "show-ref")
        self.assertState(self.run_sync(), "FAILED")
        self.assertEqual(before, git(self.remotes["web"], "show-ref"))

    def test_reviewed_head_and_remote_changes_fail_before_fetch(self):
        plan = self.sync_plan()
        self.local_commit()
        with mock.patch.object(submit.Runner, "fetch_source", side_effect=AssertionError("fetch on stale plan")):
            self.assertState(self.run_sync(plan=plan), "FAILED")
        plan = self.sync_plan()
        with mock.patch.object(submit, "remote_identity", return_value=("owner/web", "moved-remote")):
            self.assertState(self.run_sync(plan=plan), "FAILED")

    def test_cleanup_rechecks_mapping_after_slow_remote_lookup(self):
        self.publish_and_merge()
        lookup = cleanup.remote_branch_target
        mapping = {"changed": False}
        original_identity = self.local_identity
        def drift_mapping(repo, remote, branch):
            result = lookup(repo, remote, branch)
            mapping["changed"] = True
            return result
        def identity(repo, remote):
            repository, key = original_identity(repo, remote)
            return repository, "changed-after-lookup" if mapping["changed"] else key
        with mock.patch.object(submit, "remote_identity", side_effect=identity), \
                mock.patch.object(cleanup, "remote_branch_target", side_effect=drift_mapping):
            result = self.run_sync()
        self.assertState(result, "FAILED")
        self.assertIn("remote mapping changed", result["error"])
        self.assertTrue(submit.remote_heads(self.repos["web"], "origin", "task"))

    def test_remote_deletion_lease_rejects_concurrently_moved_ref(self):
        self.publish_and_merge()
        original = cleanup.git
        raced_sha = git(self.sources["web"], "rev-parse", "HEAD")
        def race(repo, *args, **kwargs):
            if args[0] == "push":
                git(self.remotes["web"], "update-ref", "refs/heads/task", raced_sha)
            return original(repo, *args, **kwargs)
        with mock.patch.object(cleanup, "git", side_effect=race):
            self.assertState(self.run_sync(), "FAILED")
        self.assertEqual(submit.remote_heads(self.repos["web"], "origin", "task")["task"], raced_sha)

    def test_concurrent_clean_head_advance_after_fetch_stops_rotation(self):
        self.advance_source()
        original = submit.Runner.fetch_source
        def advance(ctx, repo, identity):
            source = original(ctx, repo, identity)
            self.local_commit(filename="external.txt")
            return source
        with mock.patch.object(submit.Runner, "fetch_source", new=advance):
            result = self.run_sync()
        self.assertState(result, "FAILED")
        self.assertIn("HEAD/branch changed", result["error"])
        self.assertNotIn("rotation", self.runner().identity("web"))

    def test_pending_cleanup_success_survives_later_cleanup_failure(self):
        self.publish_and_merge()
        self.change(filename="second.txt")
        self.assertState(self.run_plan(), "SUBMITTED")
        self.merge_current()
        original = cleanup.cleanup
        def fail_second(args, **kwargs):
            if args.github_number == 2:
                raise cleanup.CleanupError("second remote cleanup failed")
            return original(args, **kwargs)
        with mock.patch.object(cleanup, "cleanup", side_effect=fail_second):
            result = self.run_sync()
        self.assertState(result, "FAILED")
        self.assertEqual(len(result["cleanups"]), 1)
        self.assertNotIn("pending_remote_cleanups", self.runner().identity("web"))
        self.assertEqual(submit.remote_heads(self.repos["web"], "origin", "task"), {})
        self.assertTrue(submit.remote_heads(self.repos["web"], "origin", "task-mrw-g2"))
        self.assertState(self.run_sync(), "SYNCED")

    def test_merge_conflict_stops_only_one_repository_before_rotation(self):
        self.add_repo("api")
        self.local_commit(filename="base.txt", text="local edit\n")
        source = self.sources["web"]
        (source / "base.txt").write_text("source edit\n")
        git(source, "add", "base.txt")
        git(source, "commit", "-m", "원본 충돌")
        git(source, "push", "origin", "main")
        self.advance_source("api")
        result = self.sync_runner().execute({"web": self.sync_plan(), "api": self.sync_plan("api")})
        self.assertEqual([row["state"] for row in result["repositories"]], ["FAILED", "SYNCED"])
        self.assertEqual(self.runner().identity("web")["branch"], "task")
        self.assertNotIn("rotation", self.runner().identity("web"))

    def test_multiple_merged_cleanup_records_are_processed_individually(self):
        self.publish_and_merge()
        self.change(filename="second.txt")
        self.assertState(self.run_plan(), "SUBMITTED")
        self.merge_current()
        self.assertEqual(len(self.runner().identity("web")["pending_remote_cleanups"]), 1)
        result = self.run_sync()
        self.assertState(result, "SYNCED")
        self.assertEqual(len(result["cleanups"]), 2)
        self.assertNotIn("pending_remote_cleanups", self.runner().identity("web"))
        self.assertEqual(submit.remote_heads(self.repos["web"], "origin", "task", "task-mrw-g2"), {})

    def test_open_does_not_cleanup_older_pending_merged_records(self):
        self.publish_and_merge()
        self.change(filename="second.txt")
        self.assertState(self.run_plan(), "SUBMITTED")
        before = self.runner().identity("web"), git(self.remotes["web"], "show-ref")
        self.assertState(self.run_sync(), "WAITING")
        self.assertEqual(before, (self.runner().identity("web"), git(self.remotes["web"], "show-ref")))


if __name__ == "__main__":
    unittest.main()
