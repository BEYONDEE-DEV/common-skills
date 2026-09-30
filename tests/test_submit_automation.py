from __future__ import annotations

import json
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests import test_submit_bundle as fixtures

git, submit, verifier = fixtures.git, fixtures.submit, fixtures.verifier


class SubmitAutomationTests(unittest.TestCase):
    setUp = fixtures.SubmitTests.setUp
    local_identity = fixtures.SubmitTests.local_identity
    add_repo = fixtures.SubmitTests.add_repo
    change = fixtures.SubmitTests.change
    plan = fixtures.SubmitTests.plan
    runner = fixtures.SubmitTests.runner
    run_plan = fixtures.SubmitTests.run_plan
    assertState = fixtures.SubmitTests.assertState
    advance_source = fixtures.SubmitTests.advance_source

    def prepare(self):
        result = self.runner().prepare()
        path = Path(result["plan_path"])
        return result["repositories"][0], json.loads(path.read_text())["repositories"]["web"]

    def test_prepare_collects_committed_staged_unstaged_and_untracked_content(self):
        repo = self.repos["web"]
        (repo / "base.txt").write_text("committed\n")
        git(repo, "add", "base.txt")
        git(repo, "commit", "-m", "기능 변경")
        (repo / "base.txt").write_text("staged\n")
        git(repo, "add", "base.txt")
        (repo / "base.txt").write_text("unstaged\n")
        self.change(filename="new.txt", text="new content\n")
        row, plan = self.prepare()
        packet = json.loads(Path(row["review_path"]).read_text())
        self.assertIn("+committed", packet["committed"]["content"])
        self.assertIn("+staged", packet["staged"]["content"])
        self.assertIn("+unstaged", packet["unstaged"]["content"])
        self.assertIn("+unstaged", packet["tracked"]["content"])
        self.assertEqual(packet["untracked"][0]["content"], "new content\n")
        self.assertEqual(row["required_fields"], ["task_summary"])
        self.assertEqual(plan["paths"], ["base.txt", "new.txt"])

    def test_large_and_binary_review_material_is_not_silently_truncated(self):
        repo = self.repos["web"]
        text = "long text\n" * 10000
        (repo / "large.txt").write_text(text)
        (repo / "binary.bin").write_bytes(b"\0binary\xff")
        row, _ = self.prepare()
        packet = json.loads(Path(row["review_path"]).read_text())
        entries = {entry["file"]: entry for entry in packet["untracked"]}
        self.assertNotIn("content", entries["large.txt"])
        self.assertEqual(Path(entries["large.txt"]["path"]).read_text(), text)
        self.assertNotIn("content", entries["binary.bin"])
        self.assertEqual(Path(entries["binary.bin"]["path"]).read_bytes(), b"\0binary\xff")

    def test_prepare_does_not_accept_a_change_during_material_collection(self):
        self.change()
        collect = submit.review.write_material
        def concurrent_change(*args, **kwargs):
            result = collect(*args, **kwargs)
            self.change(text="changed after capture\n")
            return result
        with mock.patch.object(submit.review, "write_material", side_effect=concurrent_change):
            result = self.runner().prepare()
        self.assertEqual(result["repositories"][0]["state"], "BLOCKED")
        self.assertNotIn("plan_path", result)
        self.assertNotIn("review_path", result["repositories"][0])

    def test_one_summary_supplies_prose_without_claiming_tests_ran(self):
        self.change()
        _, plan = self.prepare()
        plan["task_summary"] = "feat: 검색 조건 유지"
        result = self.runner().execute({"web": plan})["repositories"][0]
        self.assertState(result, "SUBMITTED")
        pr = self.api.prs[("owner/web", 1)]
        self.assertEqual(pr["title"], plan["task_summary"])
        self.assertEqual(git(self.repos["web"], "show", "-s", "--format=%s", "HEAD"), plan["task_summary"])
        self.assertIn("검증 명령을 실행하지 않았습니다", pr["body"])
        self.assertNotIn("모두 성공", pr["body"])

    def test_generated_body_reports_only_commands_that_actually_ran(self):
        self.change()
        _, plan = self.prepare()
        plan.update(task_summary="검색 개선", validation=[[sys.executable, "-c", "pass"]])
        result = self.runner(test=True).execute({"web": plan})["repositories"][0]
        self.assertState(result, "SUBMITTED")
        self.assertIn("모두 성공", self.api.prs[("owner/web", 1)]["body"])
        self.assertIn(sys.executable, self.api.prs[("owner/web", 1)]["body"])

    def test_explicit_prose_overrides_summary_for_different_scopes(self):
        self.change()
        _, plan = self.prepare()
        plan.update(task_summary="전체 검색 개선", commit_message="fix: 검색 조건 수정",
                    pr_title="검색 흐름 개선", pr_body="검색 조건과 결과를 정리합니다.")
        self.assertState(self.runner().execute({"web": plan})["repositories"][0], "SUBMITTED")
        self.assertEqual(git(self.repos["web"], "show", "-s", "--format=%s", "HEAD"), plan["commit_message"])
        self.assertEqual(self.api.prs[("owner/web", 1)]["title"], plan["pr_title"])
        self.assertEqual(self.api.prs[("owner/web", 1)]["body"], plan["pr_body"])

    def test_open_wait_needs_no_prose_or_committed_continuation_review(self):
        self.change()
        self.assertState(self.run_plan(), "SUBMITTED")
        self.change(filename="next.txt")
        git(self.repos["web"], "add", "next.txt")
        git(self.repos["web"], "commit", "-m", "후속 기능")
        with mock.patch.object(submit.review, "write_material", side_effect=AssertionError("unnecessary review")):
            row, plan = self.prepare()
        self.assertEqual(row["action"], "WAIT")
        self.assertEqual(row["required_fields"], [])
        self.assertFalse(row["content_review_required"])
        self.assertState(self.runner().execute({"web": plan})["repositories"][0], "WAITING")

    def test_dirty_open_reviews_only_uncommitted_work_and_only_needs_commit_prose(self):
        self.change()
        self.assertState(self.run_plan(), "SUBMITTED")
        self.change(filename="next.txt")
        row, plan = self.prepare()
        self.assertEqual(row["action"], "COMMIT_AND_WAIT")
        self.assertEqual(row["required_fields"], ["task_summary"])
        packet = json.loads(Path(row["review_path"]).read_text())
        self.assertEqual(packet["committed"]["bytes"], 0)
        plan["commit_message"] = "후속 기능 추가"
        self.assertState(self.runner().execute({"web": plan})["repositories"][0], "WAITING")
        self.assertEqual(self.api.create_calls, 1)

    def test_open_to_merged_change_requires_a_new_continuation_review(self):
        self.change()
        self.assertState(self.run_plan(), "SUBMITTED")
        self.change(filename="next.txt")
        git(self.repos["web"], "add", "next.txt")
        git(self.repos["web"], "commit", "-m", "후속 기능")
        _, plan = self.prepare()
        pr = self.api.prs[("owner/web", 1)]
        pr.update(state="closed", merged_at="2026-09-30T00:00:00Z", merge_commit_sha=pr["head"]["sha"])
        before = git(self.repos["web"], "rev-parse", "HEAD")
        result = self.runner().execute({"web": plan})["repositories"][0]
        self.assertState(result, "FAILED")
        self.assertIn("prepare and review", result["error"])
        self.assertEqual(before, git(self.repos["web"], "rev-parse", "HEAD"))

    def test_publication_recovery_does_not_read_or_commit_dirty_future_work(self):
        self.change()
        self.api.fail_after_create = True
        failed = self.run_plan()
        self.assertState(failed, "FAILED")
        self.assertIn("resume_plan_path", failed)
        self.change(filename="next.txt")
        with mock.patch.object(submit, "worktree_fingerprint", side_effect=AssertionError("future content hash")), \
                mock.patch.object(submit.review, "write_material", side_effect=AssertionError("future content review")):
            row, plan = self.prepare()
        self.assertEqual(row["action"], "RECOVER_PUBLICATION")
        self.assertEqual(row["required_fields"], [])
        self.assertState(self.runner().execute({"web": plan})["repositories"][0], "SUBMITTED")
        self.assertIn("?? next.txt", git(self.repos["web"], "status", "--porcelain"))
        self.assertEqual(self.api.create_calls, 1)

    def test_required_validation_retry_keeps_prose_and_exact_rotated_snapshot(self):
        self.change()
        self.advance_source()
        failed = self.run_plan(self.plan(validation=[]))
        self.assertEqual(failed["error_code"], "REVALIDATION_REQUIRED")
        self.assertEqual(failed["required_fields"], ["validation"])
        plan = json.loads(Path(failed["resume_plan_path"]).read_text())["repositories"]["web"]
        self.assertEqual(plan["expected_head"], git(self.repos["web"], "rev-parse", "HEAD"))
        self.assertEqual(plan["pr_title"], "기능 추가")
        with mock.patch.object(submit.review, "write_material", side_effect=AssertionError("repeat review")):
            row, refreshed = self.prepare()
        self.assertFalse(row["content_review_required"])
        self.assertEqual(row["required_fields"], ["validation"])
        refreshed["validation"] = [[sys.executable, "-c", "pass"]]
        self.assertState(self.runner().execute({"web": refreshed})["repositories"][0], "SUBMITTED")

    def test_new_content_after_failure_invalidates_reused_review(self):
        self.change()
        self.advance_source()
        self.run_plan(self.plan(validation=[]))
        self.change(filename="later.txt")
        row, _ = self.prepare()
        self.assertTrue(row["content_review_required"])
        self.assertIn("review_path", row)

    def test_new_clean_commit_after_failure_invalidates_reused_review(self):
        self.change()
        self.advance_source()
        self.run_plan(self.plan(validation=[]))
        self.change(filename="later.txt")
        git(self.repos["web"], "add", "later.txt")
        git(self.repos["web"], "commit", "-m", "추가 기능")
        row, _ = self.prepare()
        self.assertTrue(row["content_review_required"])
        self.assertIn("review_path", row)

    def test_missing_publication_prose_retry_reports_only_needed_input(self):
        self.change()
        failed = self.run_plan(self.plan(pr_title=""))
        self.assertState(failed, "FAILED")
        self.assertEqual(failed["required_fields"], ["task_summary"])
        plan = json.loads(Path(failed["resume_plan_path"]).read_text())["repositories"]["web"]
        plan["task_summary"] = "기능 추가"
        self.assertState(self.runner().execute({"web": plan})["repositories"][0], "SUBMITTED")

    def test_invalid_multiline_summary_stops_before_commit(self):
        self.change()
        _, plan = self.prepare()
        plan["task_summary"] = "검색 개선\n다른 제목"
        before = git(self.repos["web"], "rev-parse", "HEAD")
        self.assertState(self.runner().execute({"web": plan})["repositories"][0], "FAILED")
        self.assertEqual(before, git(self.repos["web"], "rev-parse", "HEAD"))
        self.assertEqual(git(self.repos["web"], "diff", "--cached", "--name-only"), "")

    def test_no_diff_needs_no_publication_prose(self):
        git(self.repos["web"], "commit", "--allow-empty", "-m", "메타데이터 변경")
        row, plan = self.prepare()
        self.assertEqual(row["action"], "SYNC_ONLY")
        self.assertEqual(row["required_fields"], [])
        self.assertState(self.runner().execute({"web": plan})["repositories"][0], "NO_DIFF")

    def test_candidate_remote_inspection_runs_concurrently(self):
        self.add_repo("api")
        self.change()
        self.change("api")
        barrier = threading.Barrier(2)
        find = self.api.find
        def simultaneous_lookup(repository, branch):
            barrier.wait(timeout=5)
            return find(repository, branch)
        with mock.patch.object(self.api, "find", side_effect=simultaneous_lookup):
            result = self.runner().prepare()
        self.assertEqual([row["state"] for row in result["repositories"]], ["NONE", "NONE"])

    def test_candidate_remote_failure_does_not_suppress_healthy_sibling_plan(self):
        self.add_repo("api")
        self.change()
        self.change("api")
        find = self.api.find
        def fail_one(repository, branch):
            if repository == "owner/api":
                raise submit.SubmitError("API unavailable")
            return find(repository, branch)
        with mock.patch.object(self.api, "find", side_effect=fail_one):
            result = self.runner().prepare()
        plans = json.loads(Path(result["plan_path"]).read_text())["repositories"]
        self.assertEqual(set(plans), {"web"})
        self.assertEqual(result["repositories"][0]["state"], "BLOCKED")

    def test_parallel_pr_proofs_keep_final_identity_check(self):
        self.change()
        self.assertState(self.run_plan(), "SUBMITTED")
        identity = self.runner().identity("web")
        repo = self.repos["web"]
        source = git(repo, "rev-parse", "main")
        head = git(repo, "rev-parse", "HEAD")
        barrier = threading.Barrier(4)
        api = self.api.api
        def simultaneous_proof(*args, **kwargs):
            barrier.wait(timeout=5)
            return api(*args, **kwargs)
        with mock.patch.object(self.api, "api", side_effect=simultaneous_proof), \
                mock.patch.object(verifier, "check_identity", wraps=verifier.check_identity) as check:
            result = verifier.verify(repo, "owner/web", 1, "main", identity["branch"], source, head, self.api)
        self.assertTrue(result["verified"])
        self.assertEqual(check.call_count, 2)


if __name__ == "__main__":
    unittest.main()
