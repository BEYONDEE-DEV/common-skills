from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

from tests import test_submit_bundle as fixtures
from tests import test_sync_bundle as sync_fixtures
import generation_conflicts as conflicts
import prepare_generation_branch as generation

git, manifest, submit = fixtures.git, fixtures.manifest, fixtures.submit


class ConflictTests(unittest.TestCase):
    setUp = fixtures.SubmitTests.setUp
    local_identity = fixtures.SubmitTests.local_identity
    add_repo = fixtures.SubmitTests.add_repo
    runner = fixtures.SubmitTests.runner
    plan = fixtures.SubmitTests.plan
    change = fixtures.SubmitTests.change
    run_plan = fixtures.SubmitTests.run_plan
    assertState = fixtures.SubmitTests.assertState

    def conflict(self, *, extras=True):
        self.change(filename="base.txt", text="local DOM assertion\n")
        if extras:
            self.change(filename="local-only.txt", text="local feature\n")
        original = self.sources["web"]
        (original / "base.txt").write_text("upstream validation assertion\n")
        if extras:
            (original / "source-only.txt").write_text("upstream feature\n")
        git(original, "add", "-A")
        git(original, "commit", "-m", "원본 검증 보강")
        git(original, "push", "origin", "main")

    def fail_submit(self, *, validation=None):
        self.conflict()
        plan = self.plan(validation=validation or [[sys.executable, "-c", "pass"]])
        result = self.run_plan(plan)
        self.assertState(result, "FAILED")
        self.assertEqual(result["error_code"], "CONFLICT_RESOLUTION_REQUIRED", result)
        return result, json.loads(Path(result["resume_plan_path"]).read_text())["repositories"]["web"]

    def resolve(self, recovery, *, text="upstream validation assertion\nlocal DOM assertion\n"):
        temporary = Path(recovery["worktree"])
        (temporary / "base.txt").write_text(text)
        git(temporary, "add", "base.txt")
        return conflicts.record_resolution(self.root, "slot-3", "web", Path(recovery["receipt_path"]),
                                           "기능 추가", "원본 검증과 DOM 테스트를 함께 유지")

    def test_submit_exposes_isolated_conflicts_preserving_development_and_original(self):
        self.conflict()
        original = self.sources["web"]
        before = git(original, "rev-parse", "HEAD"), git(original, "status", "--porcelain")
        result = self.run_plan()
        self.assertEqual(result["error_code"], "CONFLICT_RESOLUTION_REQUIRED", result)
        recovery = result["conflict_recovery"]
        temporary = Path(recovery["worktree"])
        self.assertEqual(recovery["conflict_paths"], ["base.txt"])
        self.assertTrue(git(temporary, "ls-files", "-u"))
        self.assertIn("<<<<<<<", (temporary / "base.txt").read_text())
        self.assertTrue((temporary / "local-only.txt").is_file())
        self.assertTrue((temporary / "source-only.txt").is_file())
        self.assertEqual(git(self.repos["web"], "show", "HEAD:base.txt"), "local DOM assertion")
        self.assertEqual(git(self.repos["web"], "status", "--porcelain"), "")
        self.assertEqual(before, (git(original, "rev-parse", "HEAD"), git(original, "status", "--porcelain")))
        self.assertIsNone(self.runner().identity("web").get("rotation"))
        self.assertEqual(self.api.create_calls, 0)
        self.assertIsNone(manifest.load_operation_lock(manifest.operation_lock_path(self.root, "slot-3")))

    def test_resolved_submit_reuses_hooked_commit_validates_and_publishes(self):
        failure, plan = self.fail_submit()
        prepared = self.resolve(failure["conflict_recovery"])
        self.assertEqual(git(Path(prepared["worktree"]), "status", "--porcelain"), "")
        result = self.run_plan(plan)
        self.assertState(result, "SUBMITTED")
        self.assertEqual(git(self.repos["web"], "rev-parse", "HEAD"), prepared["commit_sha"])
        self.assertEqual(git(self.repos["web"], "show", "-s", "--format=%P", "HEAD"), prepared["source_sha"])
        self.assertEqual((self.repos["web"] / "base.txt").read_text(), "upstream validation assertion\nlocal DOM assertion\n")
        self.assertTrue(next(p for p in result["phases"] if p["phase"] == "validation")["executed"])
        self.assertFalse(Path(prepared["worktree"]).exists())
        self.assertEqual(self.api.create_calls, 1)

    def test_marker_only_staging_and_unresolved_stages_are_rejected(self):
        failure, _ = self.fail_submit()
        recovery = failure["conflict_recovery"]
        path = Path(recovery["receipt_path"])
        with self.assertRaisesRegex(generation.TransitionError, "unresolved conflict stages"):
            conflicts.record_resolution(self.root, "slot-3", "web", path, "기능 추가", "양쪽 보존")
        git(Path(recovery["worktree"]), "add", "base.txt")
        with self.assertRaisesRegex(generation.TransitionError, "leftover conflict marker"):
            conflicts.record_resolution(self.root, "slot-3", "web", path, "기능 추가", "양쪽 보존")

    def test_nonconflicting_merge_is_not_overwritten_by_resolution(self):
        failure, _ = self.fail_submit()
        recovery = failure["conflict_recovery"]
        temporary = Path(recovery["worktree"])
        (temporary / "source-only.txt").write_text("lost upstream meaning\n")
        git(temporary, "add", "source-only.txt")
        with self.assertRaisesRegex(generation.TransitionError, "non-conflicting paths"):
            self.resolve(recovery)
        self.assertEqual(self.api.create_calls, 0)

    def test_missing_validation_cannot_be_skipped_after_rotation_or_retry(self):
        failure, plan = self.fail_submit()
        self.resolve(failure["conflict_recovery"])
        plan["validation"] = []
        result = self.run_plan(plan)
        self.assertEqual(result["error_code"], "REVALIDATION_REQUIRED", result)
        self.assertEqual(self.api.create_calls, 0)
        resumed = json.loads(Path(result["resume_plan_path"]).read_text())["repositories"]["web"]
        self.assertEqual(self.run_plan(resumed)["error_code"], "REVALIDATION_REQUIRED")
        resumed["validation"] = [[sys.executable, "-c", "pass"]]
        self.assertState(self.run_plan(resumed), "SUBMITTED")

    def test_failed_validation_publishes_nothing_and_retry_can_finish(self):
        failure, plan = self.fail_submit()
        self.resolve(failure["conflict_recovery"])
        plan["validation"] = [[sys.executable, "-c", "raise SystemExit(9)"]]
        failed = self.run_plan(plan)
        self.assertEqual(failed["error_code"], "VALIDATION_FAILED", failed)
        self.assertEqual(self.api.create_calls, 0)
        resumed = json.loads(Path(failed["resume_plan_path"]).read_text())["repositories"]["web"]
        resumed["validation"] = [[sys.executable, "-c", "pass"]]
        self.assertState(self.run_plan(resumed), "SUBMITTED")

    def test_source_drift_preserves_resolution_and_requires_fresh_context(self):
        failure, plan = self.fail_submit()
        recovery = failure["conflict_recovery"]
        temporary = Path(recovery["worktree"])
        (temporary / "base.txt").write_text("unfinished semantic work\n")
        original = self.sources["web"]
        (original / "new-source.txt").write_text("new source\n")
        git(original, "add", "new-source.txt")
        git(original, "commit", "-m", "원본 후속 변경")
        git(original, "push", "origin", "main")
        with self.assertRaisesRegex(generation.TransitionError, "source advanced"):
            self.resolve(recovery)
        next_failure = self.run_plan(plan)
        self.assertEqual(next_failure["error_code"], "CONFLICT_RESOLUTION_REQUIRED")
        self.assertNotEqual(next_failure["conflict_recovery"]["worktree"], recovery["worktree"])
        self.assertTrue(temporary.exists())

    def test_editing_retry_never_restores_over_agent_changes(self):
        failure, plan = self.fail_submit()
        recovery = failure["conflict_recovery"]
        temporary = Path(recovery["worktree"])
        (temporary / "base.txt").write_text("partial agent resolution\n")
        again = self.run_plan(plan)
        self.assertEqual(again["error_code"], "CONFLICT_RESOLUTION_REQUIRED")
        self.assertEqual((temporary / "base.txt").read_text(), "partial agent resolution\n")

    def test_interrupted_preparation_preserves_edits_on_retry(self):
        self.conflict()
        plan = self.plan()
        real_write = manifest.write_json_atomic

        def interrupt(path, data, prefix):
            if prefix == "conflict." and data.get("state") == "editing":
                raise OSError("interrupted after conflict index creation")
            return real_write(path, data, prefix)

        with mock.patch.object(manifest, "write_json_atomic", side_effect=interrupt):
            failed = self.run_plan(plan)
        self.assertState(failed, "FAILED")
        receipt = next((manifest.manifest_path(self.root).parent / "conflict-resolutions").glob("*.json"))
        temporary = Path(json.loads(receipt.read_text())["worktree"])
        (temporary / "base.txt").write_text("partially recovered after interruption\n")
        resume = json.loads(Path(failed["resume_plan_path"]).read_text())["repositories"]["web"]
        again = self.run_plan(resume)
        self.assertEqual(again["error_code"], "CONFLICT_RESOLUTION_REQUIRED", again)
        self.assertEqual((temporary / "base.txt").read_text(), "partially recovered after interruption\n")

    def test_recorded_resolution_retry_is_idempotent(self):
        failure, _ = self.fail_submit()
        recovery = failure["conflict_recovery"]
        first = self.resolve(recovery)
        second = conflicts.record_resolution(self.root, "slot-3", "web", Path(recovery["receipt_path"]),
                                             "  기능 추가  ", "원본 검증과 DOM 테스트를 함께 유지")
        self.assertEqual(first["commit_sha"], second["commit_sha"])

    def test_attached_resolution_branch_is_not_adopted_or_deleted(self):
        failure, plan = self.fail_submit()
        prepared = self.resolve(failure["conflict_recovery"])
        temporary = Path(prepared["worktree"])
        git(temporary, "switch", "-c", "keep-resolution")
        failed = self.run_plan(plan)
        self.assertState(failed, "FAILED")
        self.assertIn("attached", failed["error"])
        self.assertEqual(git(temporary, "branch", "--show-current"), "keep-resolution")
        self.assertEqual(self.api.create_calls, 0)

    def test_modified_delete_conflict_can_keep_the_intended_local_file(self):
        self.change(filename="base.txt", text="local retained behavior\n")
        original = self.sources["web"]
        git(original, "rm", "base.txt")
        git(original, "commit", "-m", "이전 파일 삭제")
        git(original, "push", "origin", "main")
        failure = self.run_plan()
        self.assertEqual(failure["error_code"], "CONFLICT_RESOLUTION_REQUIRED", failure)
        self.resolve(failure["conflict_recovery"], text="local retained behavior\n")
        plan = json.loads(Path(failure["resume_plan_path"]).read_text())["repositories"]["web"]
        self.assertState(self.run_plan(plan), "SUBMITTED")
        self.assertEqual((self.repos["web"] / "base.txt").read_text(), "local retained behavior\n")

    def test_empty_record_retry_has_no_extra_commit(self):
        self.conflict(extras=False)
        failure = self.run_plan()
        record = self.resolve(failure["conflict_recovery"], text="upstream validation assertion\n")
        again = conflicts.record_resolution(self.root, "slot-3", "web", Path(record["receipt_path"]),
                                            "기능 추가", "원본 검증과 DOM 테스트를 함께 유지")
        self.assertEqual(record["commit_sha"], record["source_sha"])
        self.assertEqual(record["commit_sha"], again["commit_sha"])

    def test_attached_or_dirty_resolution_is_preserved(self):
        failure, plan = self.fail_submit()
        recovery = failure["conflict_recovery"]
        prepared = self.resolve(recovery)
        temporary = Path(prepared["worktree"])
        (temporary / "extra.txt").write_text("user data\n")
        failed = self.run_plan(plan)
        self.assertState(failed, "FAILED")
        self.assertIn("worktree changed", failed["error"])
        self.assertTrue((temporary / "extra.txt").exists())
        self.assertEqual(self.api.create_calls, 0)

    def test_ignored_extra_content_is_not_removed_or_published(self):
        failure, plan = self.fail_submit()
        prepared = self.resolve(failure["conflict_recovery"])
        temporary = Path(prepared["worktree"])
        exclusions = self.root / "recovery-excludes"
        exclusions.write_text("*.cache\n")
        git(temporary, "config", "core.excludesFile", str(exclusions))
        extra = temporary / "private.cache"
        extra.write_text("preserve ignored user content\n")
        self.assertEqual(git(temporary, "status", "--porcelain"), "")
        failed = self.run_plan(plan)
        self.assertState(failed, "FAILED")
        self.assertIn("worktree changed", failed["error"])
        self.assertEqual(extra.read_text(), "preserve ignored user content\n")
        self.assertEqual(self.api.create_calls, 0)

    def test_commit_hooks_run_and_hook_changed_tree_is_not_adopted(self):
        failure, _ = self.fail_submit()
        recovery = failure["conflict_recovery"]
        hooks = self.root / "hooks"
        hooks.mkdir()
        hook = hooks / "pre-commit"
        hook.write_text("#!/bin/sh\nprintf 'changed by hook\\n' >> base.txt\ngit add base.txt\n")
        hook.chmod(0o755)
        git(self.repos["web"], "config", "core.hooksPath", str(hooks))
        with self.assertRaisesRegex(generation.TransitionError, "hook changed"):
            self.resolve(recovery)
        self.assertEqual(self.api.create_calls, 0)
        self.assertTrue(Path(recovery["worktree"]).exists())

    def test_record_response_loss_adopts_exact_normal_commit(self):
        failure, plan = self.fail_submit()
        recovery = failure["conflict_recovery"]
        real_write = manifest.write_json_atomic

        def lose(path, data, prefix):
            if prefix == "conflict." and data.get("state") == "ready":
                raise OSError("lost record response after normal commit")
            return real_write(path, data, prefix)

        with mock.patch.object(manifest, "write_json_atomic", side_effect=lose):
            with self.assertRaises(OSError):
                self.resolve(recovery)
        temporary = Path(recovery["worktree"])
        committed = git(temporary, "rev-parse", "HEAD")
        record = conflicts.record_resolution(self.root, "slot-3", "web", Path(recovery["receipt_path"]),
                                             "기능 추가", "원본 검증과 DOM 테스트를 함께 유지")
        self.assertEqual(record["commit_sha"], committed)
        self.assertState(self.run_plan(plan), "SUBMITTED")

    def test_conflicting_repository_does_not_stop_sibling_submission(self):
        self.add_repo("api")
        self.conflict()
        self.change("api", "api.txt", "api feature\n")
        result = self.runner().execute({"web": self.plan(), "api": self.plan("api")})["repositories"]
        self.assertEqual([r["state"] for r in result], ["FAILED", "SUBMITTED"])
        self.assertEqual(result[0]["error_code"], "CONFLICT_RESOLUTION_REQUIRED")

    def test_empty_resolution_still_validates_before_no_diff(self):
        self.conflict(extras=False)
        failure = self.run_plan()
        recovery = failure["conflict_recovery"]
        self.resolve(recovery, text="upstream validation assertion\n")
        plan = json.loads(Path(failure["resume_plan_path"]).read_text())["repositories"]["web"]
        plan["validation"] = []
        failed = self.run_plan(plan)
        self.assertEqual(failed["error_code"], "REVALIDATION_REQUIRED", failed)
        plan = json.loads(Path(failed["resume_plan_path"]).read_text())["repositories"]["web"]
        plan["validation"] = [[sys.executable, "-c", "pass"]]
        result = self.run_plan(plan)
        self.assertState(result, "NO_DIFF")
        self.assertTrue(next(p for p in result["phases"] if p["phase"] == "validation")["executed"])


# Avoid inheriting all test cases solely to share the fixture helpers.
class SyncResolutionTests(unittest.TestCase):
    setUp = fixtures.SubmitTests.setUp
    local_identity = fixtures.SubmitTests.local_identity
    add_repo = fixtures.SubmitTests.add_repo
    runner = fixtures.SubmitTests.runner
    plan = fixtures.SubmitTests.plan
    change = fixtures.SubmitTests.change
    conflict = ConflictTests.conflict
    resolve = ConflictTests.resolve
    assertState = fixtures.SubmitTests.assertState
    sync_runner = sync_fixtures.SyncTests.sync_runner
    sync_plan = sync_fixtures.SyncTests.sync_plan
    run_sync = sync_fixtures.SyncTests.run_sync

    def test_sync_resolves_only_in_isolation_then_requires_focused_validation(self):
        self.conflict()
        git(self.repos["web"], "add", "-A")
        git(self.repos["web"], "commit", "-m", "작업 보존")
        failed = self.run_sync()
        self.assertEqual(failed["error_code"], "CONFLICT_RESOLUTION_REQUIRED", failed)
        self.resolve(failed["conflict_recovery"])
        failed = self.run_sync()
        self.assertEqual(failed["error_code"], "REVALIDATION_REQUIRED", failed)
        self.assertEqual(self.api.create_calls, 0)
        plan = self.sync_plan()
        plan["validation"] = [[sys.executable, "-c", "pass"]]
        succeeded = self.run_sync(plan=plan)
        self.assertIn(succeeded["state"], {"SYNCED", "NOOP"})
        self.assertTrue(next(p for p in succeeded["phases"] if p["phase"] == "validation")["executed"])
        self.assertEqual(self.api.create_calls, 0)


if __name__ == "__main__":
    unittest.main()
