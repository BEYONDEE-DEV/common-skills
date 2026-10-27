from __future__ import annotations

import copy
import io
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "multi-repo-worktree" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import inspect_worktrees as inspector
import slot_manifest as manifest
import submit_bundle as submit
import verify_pr as verifier


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.PIPE).decode().strip()


class FakeGitHub:
    """Real local Git refs/patches with an in-memory GitHub boundary; no network writes."""
    def __init__(self, repos):
        self.repos = repos
        self.prs = {}
        self.create_calls = 0
        self.fail_after_create = False
        self.corrupt_patch = False

    def view(self, repository, number):
        pr = copy.deepcopy(self.prs[(repository, number)])
        repo = self.repos[repository]
        if not pr.get("merged_at"):
            pr["base"]["sha"] = submit.remote_heads(repo, "origin", pr["base"]["ref"])[pr["base"]["ref"]]
            heads = submit.remote_heads(repo, "origin", pr["head"]["ref"])
            pr["head"]["sha"] = heads.get(pr["head"]["ref"], pr["head"]["sha"])
        return pr

    def find(self, repository, branch):
        return [self.view(r, n) for (r, n), pr in self.prs.items()
                if r == repository and pr["head"]["ref"] == branch]

    def create(self, repository, base, head, title, body):
        self.create_calls += 1
        repo = self.repos[repository]
        refs = submit.remote_heads(repo, "origin", base, head)
        number = len(self.prs) + 1
        count = len(git(repo, "diff", "--name-only", f"{refs[base]}...{refs[head]}").splitlines())
        self.prs[(repository, number)] = {
            "number": number, "html_url": f"https://github.com/{repository}/pull/{number}",
            "state": "open", "draft": False, "merged_at": None, "merge_commit_sha": None,
            "base": {"repo": {"full_name": repository}, "ref": base, "sha": refs[base]},
            "head": {"repo": {"full_name": repository}, "ref": head, "sha": refs[head]},
            "changed_files": count, "title": title, "body": body,
        }
        if self.fail_after_create:
            self.fail_after_create = False
            raise submit.SubmitError("simulated response loss after PR creation")
        return self.prs[(repository, number)]["html_url"]

    def api(self, endpoint, *, paginate=False, diff=False):
        commit_match = re.fullmatch(r"repos/(.+)/git/commits/([0-9a-f]+)", endpoint)
        if commit_match:
            repository, oid = commit_match.groups()
            return {"sha": oid, "tree": {"sha": git(self.repos[repository], "rev-parse", oid + "^{tree}")}}
        match = re.match(r"repos/(.+)/pulls/(\d+)", endpoint)
        repository, number = match[1], int(match[2])
        pr = self.view(repository, number)
        repo, base, head = self.repos[repository], pr["base"]["sha"], pr["head"]["sha"]
        if diff:
            # Independently generate GitHub's display representation: binary changes
            # have a summary, never --binary literals. Do not call verifier.local_diff.
            patch = subprocess.check_output(["git", "-C", str(repo), "-c", "diff.algorithm=myers",
                "-c", "diff.indentHeuristic=true", "diff", "--no-ext-diff", "--no-textconv",
                "--no-color", "--find-renames=50%", "--unified=3", f"{base}...{head}", "--"])
            if self.corrupt_patch:
                patch += b"corrupt\n"
            # A realistic presentation-only difference from local Git output.
            return re.sub(rb"index ([0-9a-f]{7})[0-9a-f]+\.\.([0-9a-f]{7})[0-9a-f]+", rb"index \1..\2", patch)
        return [{"filename": name} for name in git(repo, "diff", "--name-only", f"{base}...{head}").splitlines()]


class SubmitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = self.root / "worktrees" / "slot-3"
        self.base.mkdir(parents=True)
        self.sources, self.repos, self.remotes = {}, {}, {}
        self.data = manifest.empty_manifest()
        self.api = FakeGitHub({})
        self.remote_patch = mock.patch.object(submit, "remote_identity", side_effect=self.local_identity)
        self.remote_patch.start()
        self.addCleanup(self.remote_patch.stop)
        self.add_repo("web")

    def local_identity(self, repo, remote):
        return "owner/" + repo.name, submit.digest([
            git(repo, "remote", "get-url", "--all", remote),
            git(repo, "remote", "get-url", "--push", "--all", remote)])

    def add_repo(self, name):
        source, remote, task = self.root / name, self.root / (name + ".git"), self.base / name
        source.mkdir()
        git(source, "init", "-b", "main")
        git(source, "config", "user.name", "Submit Test")
        git(source, "config", "user.email", "submit@example.invalid")
        (source / "base.txt").write_text("base\n")
        git(source, "add", "base.txt")
        git(source, "commit", "-m", "기준")
        git(self.root, "init", "--bare", str(remote))
        git(source, "remote", "add", "origin", str(remote))
        git(source, "push", "origin", "main")
        git(source, "worktree", "add", "-b", "task", str(task))
        self.sources[name], self.repos[name], self.remotes[name] = source, task, remote
        self.api.repos["owner/" + name] = task
        identity = {"branch_family": "task", "branch": "task", "branch_generation": 1,
            "branch_base_sha": git(source, "rev-parse", "HEAD"), "source_branch": "main",
            "remote": "origin", "generation": 0, "pr": None, "submission": None}
        self.data["slots"].setdefault("slot-3", {"path": str(self.base), "repositories": {}})["repositories"][name] = identity
        manifest.write_manifest(manifest.manifest_path(self.root), self.data)

    def runner(self, **kwargs):
        return submit.Runner(self.root, "worktrees/slot-3", api=self.api, **kwargs)

    def change(self, name="web", filename="feature.txt", text="new feature\n"):
        (self.repos[name] / filename).write_text(text)

    def plan(self, name="web", **overrides):
        repo = self.repos[name]
        identity = self.runner().identity(name)
        result = {"expected_head": git(repo, "rev-parse", "HEAD"), "expected_generation": identity["generation"],
            "expected_branch": git(repo, "branch", "--show-current"),
            "expected_remote": self.local_identity(repo, "origin")[1], "expected_identity": submit.digest(identity),
            "expected_worktree": submit.worktree_fingerprint(repo), "paths": sorted(submit.changed_paths(repo)),
            "commit_message": "기능 추가", "transfer_subject": "다음 기능 추가", "pr_title": "기능 추가",
            "pr_body": "기능을 추가하고 검증했습니다.", "validation": [[sys.executable, "-c", "pass"]]}
        result.update(overrides)
        return result

    def run_plan(self, plan=None):
        return self.runner().execute({"web": plan or self.plan()})["repositories"][0]

    def assertState(self, result, expected):
        self.assertEqual(result["state"], expected, result)

    def test_scoped_inspection_does_not_status_other_worktrees_or_discover_workspace(self):
        git(self.sources["web"], "worktree", "add", "-b", "unrelated", str(self.root / "other"))
        with mock.patch.object(inspector, "discover_candidates", side_effect=AssertionError("broad scan")), \
                mock.patch.object(inspector, "run_git", wraps=inspector.run_git) as calls:
            data = inspector.inspect_bundle(self.root, "worktrees/slot-3")
        self.assertEqual(data["repository_group_count"], 1)
        self.assertEqual(data["worktree_count"], 1)
        self.assertTrue(all(call.args[0] == self.repos["web"] for call in calls.call_args_list))
        self.assertNotIn(str(self.root / "other"), json.dumps(data))

    def test_preflight_is_read_only_even_when_source_advanced(self):
        self.change()
        before = git(self.repos["web"], "show-ref"), git(self.repos["web"], "status", "--porcelain")
        state_before = {str(p): p.read_bytes() for p in (self.root / ".multi-repo-worktree").rglob("*") if p.is_file()}
        result = self.runner().preflight()
        self.assertEqual(result["repositories"][0]["state"], "NONE")
        self.assertEqual(before, (git(self.repos["web"], "show-ref"), git(self.repos["web"], "status", "--porcelain")))
        self.assertEqual(state_before, {str(p): p.read_bytes() for p in (self.root / ".multi-repo-worktree").rglob("*") if p.is_file()})

    def test_submit_and_open_wait_preserve_remote_boundary(self):
        self.change()
        self.assertState(self.run_plan(), "SUBMITTED")
        before = self.runner().identity("web")
        remote_before = git(self.remotes["web"], "show-ref")
        self.change(filename="next.txt")
        result = self.run_plan()
        self.assertState(result, "WAITING")
        self.assertEqual(before, self.runner().identity("web"))
        self.assertEqual(remote_before, git(self.remotes["web"], "show-ref"))
        self.assertEqual(self.api.create_calls, 1)
        self.assertEqual(git(self.repos["web"], "status", "--porcelain"), "")

    def test_unrelated_staged_path_blocks_before_commit(self):
        self.change()
        self.change(filename="unrelated.txt")
        git(self.repos["web"], "add", "unrelated.txt")
        old = git(self.repos["web"], "rev-parse", "HEAD")
        result = self.run_plan(self.plan(paths=["feature.txt"]))
        self.assertState(result, "FAILED")
        self.assertIn("unrelated staged", result["error"])
        self.assertEqual(old, git(self.repos["web"], "rev-parse", "HEAD"))
        self.assertEqual(self.api.create_calls, 0)

    def test_literal_pathspec_and_rename_include_exact_files(self):
        self.change(filename=":(glob)*.txt")
        git(self.repos["web"], "mv", "base.txt", "renamed.txt")
        self.assertState(self.run_plan(), "SUBMITTED")
        self.assertEqual(set(git(self.repos["web"], "ls-files").splitlines()), {":(glob)*.txt", "renamed.txt"})

    def test_worktree_edit_after_review_blocks(self):
        self.change()
        plan = self.plan()
        self.change(text="changed after review\n")
        result = self.run_plan(plan)
        self.assertState(result, "FAILED")
        self.assertIn("content changed", result["error"])

    def test_validation_then_concurrent_clean_commit_is_never_published(self):
        self.change()
        original = submit.Runner.validate
        def advance(runner, name, repo, source, plan, expected_head):
            original(runner, name, repo, source, plan, expected_head)
            (repo / "unreviewed.txt").write_text("unreviewed\n")
            git(repo, "add", "unreviewed.txt")
            git(repo, "commit", "-m", "외부 커밋")
        with mock.patch.object(submit.Runner, "validate", new=advance):
            result = self.run_plan()
        self.assertState(result, "FAILED")
        self.assertIn("HEAD changed", result["error"])
        self.assertEqual(self.api.create_calls, 0)
        self.assertNotIn("task", submit.remote_heads(self.repos["web"], "origin", "task"))

    def test_file_edit_between_fingerprint_and_staging_is_not_committed(self):
        self.change()
        old = git(self.repos["web"], "rev-parse", "HEAD")
        original = submit.g
        def edit_before_add(repo, *args):
            if args[0] == "add":
                (repo / "feature.txt").write_text("unreviewed save after fingerprint\n")
            return original(repo, *args)
        with mock.patch.object(submit, "g", side_effect=edit_before_add):
            result = self.run_plan()
        self.assertState(result, "FAILED")
        self.assertIn("content changed while staging", result["error"])
        self.assertEqual(git(self.repos["web"], "rev-parse", "HEAD"), old)
        self.assertEqual(self.api.create_calls, 0)

    def test_remote_change_after_plan_blocks_publication(self):
        self.change()
        plan = self.plan()
        with mock.patch.object(submit, "remote_identity", return_value=("owner/web", "changed-remote-key")):
            result = self.run_plan(plan)
        self.assertState(result, "FAILED")
        self.assertEqual(self.api.create_calls, 0)

    def test_lost_pr_create_response_adopts_original_before_new_dirty_work(self):
        self.change()
        self.api.fail_after_create = True
        self.assertState(self.run_plan(), "FAILED")
        submitted = git(self.repos["web"], "rev-parse", "HEAD")
        self.change(filename="next.txt")
        result = self.run_plan()
        self.assertState(result, "SUBMITTED")
        self.assertEqual(self.api.create_calls, 1)
        self.assertEqual(self.runner().identity("web")["submission"]["continuation_boundary_sha"], submitted)
        self.assertIn("?? next.txt", git(self.repos["web"], "status", "--porcelain"))

    def test_push_response_loss_reuses_exact_remote_head(self):
        self.change()
        original = submit.g
        def lose_response(repo, *args):
            result = original(repo, *args)
            if args[0] == "push":
                raise submit.SubmitError("push succeeded but response lost")
            return result
        with mock.patch.object(submit, "g", side_effect=lose_response):
            self.assertState(self.run_plan(), "FAILED")
        self.assertEqual(self.api.create_calls, 0)
        self.assertState(self.run_plan(), "SUBMITTED")
        self.assertEqual(self.api.create_calls, 1)

    def test_record_failure_does_not_duplicate_pr_or_generation(self):
        self.change()
        with mock.patch.object(manifest, "record_batch", side_effect=manifest.ManifestError("disk error")):
            self.assertState(self.run_plan(), "FAILED")
        self.assertState(self.run_plan(), "SUBMITTED")
        self.assertEqual(self.api.create_calls, 1)
        self.assertEqual(self.runner().identity("web")["generation"], 1)

    def test_published_recovery_allows_source_advance_without_consuming_dirty_B(self):
        self.change()
        self.api.fail_after_create = True
        self.assertState(self.run_plan(), "FAILED")
        original_head = git(self.repos["web"], "rev-parse", "HEAD")
        source = self.sources["web"]
        (source / "source.txt").write_text("another source change\n")
        git(source, "add", "source.txt")
        git(source, "commit", "-m", "원본 추가")
        git(source, "push", "origin", "main")
        self.change(filename="next.txt")
        self.assertState(self.run_plan(), "SUBMITTED")
        self.assertEqual(original_head, self.runner().identity("web")["submission"]["observed_head_sha"])
        self.assertIn("?? next.txt", git(self.repos["web"], "status", "--porcelain"))
        self.assertEqual(self.api.create_calls, 1)

    def test_unpublished_pending_replans_saved_head_after_source_advance(self):
        self.change()
        original = submit.g
        def fail_before_push(repo, *args):
            if args[0] == "push":
                raise submit.SubmitError("network unavailable before push")
            return original(repo, *args)
        with mock.patch.object(submit, "g", side_effect=fail_before_push):
            self.assertState(self.run_plan(), "FAILED")
        source = self.sources["web"]
        (source / "source.txt").write_text("source advanced\n")
        git(source, "add", "source.txt")
        git(source, "commit", "-m", "원본 추가")
        git(source, "push", "origin", "main")
        result = self.run_plan()
        self.assertState(result, "SUBMITTED")
        self.assertEqual(self.runner().identity("web")["branch"], "task-mrw-g2")
        self.assertEqual(self.api.create_calls, 1)

    def test_empty_push_lease_does_not_overwrite_concurrently_created_ancestor(self):
        self.change()
        original = submit.g
        old = git(self.sources["web"], "rev-parse", "HEAD")
        def race(repo, *args):
            if args[0] == "push":
                git(self.remotes["web"], "update-ref", "refs/heads/task", old)
            return original(repo, *args)
        with mock.patch.object(submit, "g", side_effect=race):
            self.assertState(self.run_plan(), "FAILED")
        self.assertEqual(submit.remote_heads(self.repos["web"], "origin", "task")["task"], old)
        self.assertEqual(self.api.create_calls, 0)

    def test_record_committed_before_process_interruption_is_acknowledged_once(self):
        self.change()
        write_manifest = manifest.write_manifest
        def crash_after_write(path, data):
            write_manifest(path, data)
            if data["slots"]["slot-3"]["repositories"]["web"]["generation"] == 1:
                raise SystemExit("simulated process interruption after manifest record")
        with mock.patch.object(manifest, "write_manifest", side_effect=crash_after_write), self.assertRaises(SystemExit):
            self.run_plan()
        result = self.run_plan()
        self.assertState(result, "RECORDED")
        self.assertEqual(self.api.create_calls, 1)
        self.assertEqual(self.runner().identity("web")["generation"], 1)

    def test_source_advance_after_validation_replans_and_revalidates_once(self):
        self.change()
        original = submit.Runner.validate
        calls = []
        def advance_once(runner, name, repo, source, plan, expected_head):
            original(runner, name, repo, source, plan, expected_head)
            calls.append(expected_head)
            if len(calls) == 1:
                original_source = self.sources["web"]
                (original_source / "source.txt").write_text("concurrent source advance\n")
                git(original_source, "add", "source.txt")
                git(original_source, "commit", "-m", "원본 추가")
                git(original_source, "push", "origin", "main")
        with mock.patch.object(submit.Runner, "validate", new=advance_once):
            self.assertState(self.run_plan(), "SUBMITTED")
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0], calls[1])

    def test_invalid_patch_never_records_and_can_retry_verified_same_head(self):
        self.change()
        self.api.corrupt_patch = True
        self.assertState(self.run_plan(), "FAILED")
        self.assertEqual(self.runner().identity("web")["generation"], 0)
        self.api.corrupt_patch = False
        self.assertState(self.run_plan(), "SUBMITTED")
        self.assertEqual(self.api.create_calls, 1)

    def test_verifier_rejects_wrong_identity_draft_and_incomplete_files(self):
        self.change()
        self.assertState(self.run_plan(), "SUBMITTED")
        repo = self.repos["web"]
        head = git(repo, "rev-parse", "HEAD")
        source = git(self.sources["web"], "rev-parse", "HEAD")
        original = self.api.view
        mutations = [lambda pr: pr.update(draft=True),
            lambda pr: pr["head"].update(sha="0" * 40),
            lambda pr: pr["base"].update(sha="0" * 40),
            lambda pr: pr["head"].update(ref="other"),
            lambda pr: pr["base"]["repo"].update(full_name="other/repository"),
            lambda pr: pr.update(changed_files=3001)]
        for mutate in mutations:
            def view(repository, number):
                pr = original(repository, number)
                mutate(pr)
                return pr
            with self.subTest(mutation=mutate), mock.patch.object(self.api, "view", side_effect=view), \
                    self.assertRaises(verifier.VerificationError):
                verifier.verify(repo, "owner/web", 1, "main", "task", source, head, self.api)

    def test_wrong_remote_head_during_recovery_is_not_overwritten(self):
        self.change()
        self.api.fail_after_create = True
        self.assertState(self.run_plan(), "FAILED")
        repo = self.repos["web"]
        self.change(filename="next.txt")
        git(repo, "add", "next.txt")
        git(repo, "commit", "-m", "다음 변경")
        git(repo, "push", "origin", "task")
        before = git(self.remotes["web"], "show-ref")
        self.assertState(self.run_plan(), "FAILED")
        self.assertEqual(before, git(self.remotes["web"], "show-ref"))
        self.assertEqual(self.runner().identity("web")["generation"], 0)

    def test_mixed_bundle_records_success_before_other_failure(self):
        self.add_repo("api")
        self.change()
        self.change("api")
        self.api.corrupt_patch = False
        results = self.runner().execute({"web": self.plan(), "api": self.plan("api", validation_mode="always", validation=[[sys.executable, "-c", "raise SystemExit(1)"]])})
        self.assertEqual([r["state"] for r in results["repositories"]], ["SUBMITTED", "FAILED"])
        self.assertEqual(results["repositories"][1]["error_code"], "VALIDATION_FAILED")
        self.assertEqual(self.runner().identity("web")["generation"], 1)
        self.assertEqual(self.runner().identity("api")["generation"], 0)

    def test_none_source_advance_rotates_and_leaves_source_worktree_untouched(self):
        self.change()
        source = self.sources["web"]
        (source / "source.txt").write_text("source advance\n")
        git(source, "add", "source.txt")
        git(source, "commit", "-m", "원본 변경")
        git(source, "push", "origin", "main")
        source_head = git(source, "rev-parse", "HEAD")
        result = self.run_plan()
        self.assertState(result, "SUBMITTED")
        identity = self.runner().identity("web")
        self.assertEqual(identity["branch"], "task-mrw-g2")
        self.assertEqual(git(self.repos["web"], "show", "-s", "--format=%P", "HEAD"), source_head)
        self.assertEqual(git(source, "rev-parse", "HEAD"), source_head)
        self.assertEqual(git(source, "status", "--porcelain"), "")
        self.assertNotIn("refs/heads/task\n", git(source, "show-ref") + "\n")

    def test_rotation_switch_crash_resumes_without_duplicate_transfer(self):
        self.change()
        source = self.sources["web"]
        (source / "source.txt").write_text("source advance\n")
        git(source, "add", "source.txt")
        git(source, "commit", "-m", "원본 변경")
        git(source, "push", "origin", "main")
        advance = manifest.advance_rotation
        def crash(data, slot, name, expected, target, target_head_sha=None):
            if target == "switched":
                raise manifest.ManifestError("simulated crash after checkout")
            return advance(data, slot, name, expected, target, target_head_sha)
        with mock.patch.object(manifest, "advance_rotation", side_effect=crash):
            self.assertState(self.run_plan(), "FAILED")
        repo = self.repos["web"]
        target = git(repo, "rev-parse", "HEAD")
        self.assertEqual(git(repo, "branch", "--show-current"), "task-mrw-g2")
        self.assertState(self.run_plan(), "SUBMITTED")
        self.assertEqual(git(repo, "rev-parse", "HEAD"), target)

    def test_squash_merged_rotation_transfers_only_continuation_and_retains_remote(self):
        self.change()
        self.assertState(self.run_plan(), "SUBMITTED")
        source = self.sources["web"]
        git(source, "merge", "--squash", "task")
        git(source, "commit", "-m", "기능 스쿼시 병합")
        git(source, "push", "origin", "main")
        merged_sha = git(source, "rev-parse", "HEAD")
        old_pr = self.api.prs[("owner/web", 1)]
        old_pr.update(state="closed", merged_at="2026-09-10T00:00:00Z", merge_commit_sha=merged_sha)
        self.change(filename="next.txt")
        result = self.run_plan()
        self.assertState(result, "SUBMITTED")
        identity = self.runner().identity("web")
        self.assertEqual(identity["generation"], 2)
        self.assertEqual(identity["pending_remote_cleanups"][0]["pr"], old_pr["html_url"])
        self.assertEqual(git(self.repos["web"], "diff", "--name-only", f"{merged_sha}...HEAD"), "next.txt")
        self.assertIn("task", submit.remote_heads(self.repos["web"], "origin", "task"))

    def test_default_submit_does_not_rerun_development_validation(self):
        self.change()
        with mock.patch.object(verifier, "command", wraps=verifier.command) as command:
            result = self.run_plan(self.plan(validation=[[sys.executable, "-c", "raise SystemExit(99)"]]))
        self.assertState(result, "SUBMITTED")
        self.assertFalse(any(c.args[0][0] == sys.executable for c in command.call_args_list))
        phase = next(p for p in result["phases"] if p["phase"] == "validation")
        self.assertFalse(phase["executed"])

    def test_default_submit_needs_no_test_commands_or_cache_context(self):
        self.change()
        plan = self.plan()
        plan.pop("validation")
        self.assertState(self.run_plan(plan), "SUBMITTED")

    def test_explicit_test_request_executes_commands(self):
        self.change()
        with mock.patch.object(verifier, "command", wraps=verifier.command) as command:
            result = self.runner(test=True).execute({"web": self.plan()})["repositories"][0]
        self.assertState(result, "SUBMITTED")
        self.assertEqual(sum(c.args[0][0] == sys.executable for c in command.call_args_list), 1)

    def advance_source(self):
        source = self.sources["web"]
        (source / "source.txt").write_text("source change\n")
        git(source, "add", "source.txt")
        git(source, "commit", "-m", "원본 변경")
        git(source, "push", "origin", "main")

    def test_changed_tree_requires_revalidation_and_retry_cannot_silently_skip_it(self):
        self.change()
        self.advance_source()
        result = self.run_plan(self.plan(validation=[]))
        self.assertState(result, "FAILED")
        self.assertEqual(result["error_code"], "REVALIDATION_REQUIRED")
        self.assertEqual(self.api.create_calls, 0)
        self.assertState(self.run_plan(self.plan(validation=[])), "FAILED")
        with mock.patch.object(verifier, "command", wraps=verifier.command) as command:
            result = self.run_plan()
        self.assertState(result, "SUBMITTED")
        self.assertEqual(sum(c.args[0][0] == sys.executable for c in command.call_args_list), 1)

    def test_commit_id_only_change_does_not_trigger_validation(self):
        self.change()
        source = self.sources["web"]
        git(source, "commit", "--allow-empty", "-m", "메타데이터만 변경")
        git(source, "push", "origin", "main")
        result = self.run_plan(self.plan(validation=[]))
        self.assertState(result, "SUBMITTED")
        self.assertEqual(self.runner().identity("web")["branch"], "task-mrw-g2")
        self.assertFalse(next(p for p in result["phases"] if p["phase"] == "validation")["executed"])

    def test_prepare_generates_snapshot_and_skips_unselected_clean_remote_queries(self):
        self.add_repo("api")
        self.change()
        with mock.patch.object(self.api, "find", wraps=self.api.find) as find:
            result = self.runner().prepare()
        generated = json.loads(Path(result["plan_path"]).read_text())["repositories"]
        self.assertEqual(set(generated), {"web"})
        self.assertEqual(generated["web"]["expected_head"], self.plan()["expected_head"])
        self.assertEqual(generated["web"]["paths"], ["feature.txt"])
        self.assertFalse(any(c.args[0] == "owner/api" for c in find.call_args_list))
        again = self.runner().prepare()
        self.assertNotEqual(result["plan_path"], again["plan_path"])
        self.assertEqual(Path(result["plan_path"]).read_text(), Path(again["plan_path"]).read_text())

    def test_execute_preflight_is_local_and_does_not_query_sibling_remote(self):
        self.add_repo("api")
        self.change()
        runner = self.runner()
        with mock.patch.object(submit, "remote_heads", wraps=submit.remote_heads) as heads, \
                mock.patch.object(self.api, "find", wraps=self.api.find) as find:
            runner.preflight(local_only=True, repositories={"web"})
        heads.assert_not_called()
        find.assert_not_called()
        with mock.patch.object(submit, "remote_heads", wraps=submit.remote_heads) as heads:
            self.assertState(self.run_plan(), "SUBMITTED")
        self.assertFalse(any(c.args[0] == self.repos["api"] for c in heads.call_args_list))

    def test_failure_after_creation_returns_pr_url(self):
        self.change()
        self.api.corrupt_patch = True
        result = self.run_plan()
        self.assertState(result, "FAILED")
        self.assertEqual(result["pr"], "https://github.com/owner/web/pull/1")
        self.assertEqual(result["failed_phase"], "verify_pr")
        self.assertEqual(result["error_code"], "VERIFICATION_FAILED")

    def test_two_binary_additions_accept_github_summary_and_verify_full_tree(self):
        repo = self.repos["web"]
        (repo / "card.jpg").write_bytes(b"\xff\xd8\x00card\xff\xd9")
        (repo / "cuty.jpg").write_bytes(b"\xff\xd8\x00cuty\xff\xd9")
        with mock.patch.object(self.api, "api", wraps=self.api.api) as api:
            result = self.run_plan()
        self.assertState(result, "SUBMITTED")
        self.assertEqual(result["files"], 2)
        self.assertEqual(result["tree_sha"], git(repo, "rev-parse", "HEAD^{tree}"))
        self.assertEqual(sum('/git/commits/' in c.args[0] for c in api.call_args_list), 2)
        raw = self.api.api("repos/owner/web/pulls/1", diff=True)
        self.assertNotIn(b"GIT binary patch", raw)
        self.assertEqual(raw.count(b"Binary files "), 2)

    def test_binary_replace_delete_rename_and_mode_changes(self):
        repo = self.repos["web"]
        for name in ("replace.bin", "delete.bin", "old.bin", "mode.bin"):
            (repo / name).write_bytes(b"\0original binary " + name.encode())
        git(repo, "add", ".")
        git(repo, "commit", "-m", "바이너리 기준")
        # Publish this baseline to source, then exercise all operations in one PR.
        git(self.sources["web"], "merge", "--ff-only", "task")
        git(self.sources["web"], "push", "origin", "main")
        self.data["slots"]["slot-3"]["repositories"]["web"]["branch_base_sha"] = git(repo, "rev-parse", "HEAD")
        manifest.write_manifest(manifest.manifest_path(self.root), self.data)
        (repo / "replace.bin").write_bytes(b"\0replacement binary")
        git(repo, "rm", "delete.bin")
        git(repo, "mv", "old.bin", "new.bin")
        (repo / "mode.bin").chmod(0o755)
        result = self.run_plan()
        self.assertState(result, "SUBMITTED")
        self.assertEqual(result["files"], 4)

    def test_binary_proof_rejects_wrong_or_incomplete_tree_even_when_patch_matches(self):
        repo = self.repos["web"]
        (repo / "image.bin").write_bytes(b"\0binary")
        self.assertState(self.run_plan(), "SUBMITTED")
        head = git(repo, "rev-parse", "HEAD")
        source = git(self.sources["web"], "rev-parse", "HEAD")
        original = self.api.api
        for incomplete in (False, True):
            def bad_tree(endpoint, **kwargs):
                response = original(endpoint, **kwargs)
                if '/git/commits/' in endpoint:
                    response['tree'] = {} if incomplete else {'sha': '0' * 40}
                return response
            with self.subTest(incomplete=incomplete), mock.patch.object(self.api, "api", side_effect=bad_tree), \
                    self.assertRaisesRegex(verifier.VerificationError, "tree mismatch"):
                verifier.verify(repo, "owner/web", 1, "main", "task", source, head, self.api)

    def test_binary_summary_does_not_allow_truncated_patch(self):
        (self.repos["web"] / "image.bin").write_bytes(b"\0binary")
        self.api.corrupt_patch = True
        result = self.run_plan()
        self.assertState(result, "FAILED")
        self.assertEqual(result["error_code"], "VERIFICATION_FAILED")
        self.assertEqual(self.runner().identity("web")["generation"], 0)

    def test_failure_between_rotation_and_validation_does_not_lose_required_recheck(self):
        self.change()
        self.advance_source()
        first = self.run_plan(self.plan(validation=[], pr_title=""))
        self.assertState(first, "FAILED")
        self.assertEqual(self.runner().identity("web")["branch"], "task-mrw-g2")
        second = self.run_plan(self.plan(validation=[]))
        self.assertState(second, "FAILED")
        self.assertEqual(second["error_code"], "REVALIDATION_REQUIRED")
        self.assertEqual(self.api.create_calls, 0)

    def test_no_diff_does_not_force_tests_for_the_next_development_change(self):
        self.change()
        source = self.sources["web"]
        (source / "feature.txt").write_text("new feature\n")
        self.advance_source()  # Commits source.txt; include the same task work separately.
        git(source, "add", "feature.txt")
        git(source, "commit", "-m", "동일 기능 이미 반영")
        git(source, "push", "origin", "main")
        self.assertState(self.run_plan(self.plan(validation=[])), "NO_DIFF")
        self.change(filename="next.ts")
        self.assertState(self.run_plan(self.plan(validation=[])), "SUBMITTED")

    def test_prepare_reports_committed_work_even_when_dirty_paths_are_empty(self):
        self.change()
        repo = self.repos["web"]
        git(repo, "add", "feature.txt")
        git(repo, "commit", "-m", "개발 단계 커밋")
        row = self.runner().prepare()["repositories"][0]
        self.assertEqual(row["changed_paths"], [])
        self.assertEqual(row["committed_review"]["changed_paths"], ["feature.txt"])

    def test_cli_prepare_review_execute_roundtrip_without_tests(self):
        self.change()
        runner_type = submit.Runner
        def runner(root, bundle, test=False):
            return runner_type(root, bundle, api=self.api, test=test)
        args = ["submit_bundle.py", "--root", str(self.root), "--bundle", "worktrees/slot-3"]
        output = io.StringIO()
        with mock.patch.object(submit, "Runner", side_effect=runner), \
                mock.patch.object(sys, "argv", args + ["--prepare"]), \
                mock.patch.object(sys, "stdout", output):
            self.assertEqual(submit.main(), 0)
        path = Path(json.loads(output.getvalue())["plan_path"])
        document = json.loads(path.read_text())
        document["repositories"]["web"].update(commit_message="기능 추가", transfer_subject="기능 추가",
            pr_title="기능 추가", pr_body="개발 단계에서 검증한 기능입니다.")
        path.write_text(json.dumps(document))
        output = io.StringIO()
        with mock.patch.object(submit, "Runner", side_effect=runner), \
                mock.patch.object(sys, "argv", args + ["--execute", "--plan", str(path)]), \
                mock.patch.object(sys, "stdout", output):
            self.assertEqual(submit.main(), 0)
        result = json.loads(output.getvalue())["repositories"][0]
        self.assertState(result, "SUBMITTED")
        self.assertFalse(next(p for p in result["phases"] if p["phase"] == "validation")["executed"])

    def test_merged_pending_recovers_even_with_deleted_remote_branch_and_dirty_continuation(self):
        self.change()
        self.api.corrupt_patch = True
        self.assertState(self.run_plan(), "FAILED")
        saved = git(self.repos["web"], "rev-parse", "HEAD")
        source = self.sources["web"]
        git(source, "merge", "--squash", "task")
        git(source, "commit", "-m", "기능 병합")
        git(source, "push", "origin", "main")
        self.api.prs[("owner/web", 1)].update(state="closed", merged_at="2026-09-11T00:00:00Z",
            merge_commit_sha=git(source, "rev-parse", "HEAD"))
        git(self.remotes["web"], "update-ref", "-d", "refs/heads/task")
        self.change(filename="next.txt")
        self.api.corrupt_patch = False
        result = self.run_plan()
        self.assertState(result, "RECORDED")
        self.assertEqual(result["pr_state"], "MERGED")
        self.assertEqual(self.api.create_calls, 1)
        self.assertEqual(self.runner().identity("web")["submission"]["continuation_boundary_sha"], saved)
        self.assertIn("?? next.txt", git(self.repos["web"], "status", "--porcelain"))
        self.assertNotIn("task", submit.remote_heads(self.repos["web"], "origin", "task"))


class PatchTests(unittest.TestCase):
    def test_presentation_only_normalization(self):
        local = b"diff --git a/a b/a\nindex " + b"a"*40 + b".." + b"b"*40 + b" 100644\n@@ -1,2 +1,2 @@ function\n-old\n+new \n same\n"
        remote = local.replace(b"a"*40, b"a"*7).replace(b"b"*40, b"b"*12).replace(b"@@ function", b"@@ different label")
        verifier.compare_patches(local, remote)
        for changed in [remote.replace(b"+new ", b"+new"), remote.replace(b"100644", b"100755"),
                        remote.replace(b"a/a", b"a/b"), remote + b"\\ No newline at end of file\n",
                        remote.replace(b"aaaaaaa", b"ccccccc"), remote.replace(b"+1,2", b"+1,3")]:
            with self.subTest(patch=changed), self.assertRaises(verifier.VerificationError):
                verifier.compare_patches(local, changed)

    def test_rename_binary_and_eof_are_not_discarded(self):
        for original, modified in [(b"rename from old\n", b"rename from other\n"),
                (b"GIT binary patch\nliteral 1\nA\n", b"GIT binary patch\nliteral 1\nB\n"),
                (b"+line\n\\ No newline at end of file\n", b"+line\n")]:
            with self.subTest(original=original), self.assertRaises(verifier.VerificationError):
                verifier.compare_patches(original, modified)

    def test_exact_remote_repository_parsing(self):
        for url in ["git@github.com:owner/repo.git", "https://github.com/owner/repo.git", "ssh://git@github.com/owner/repo"]:
            self.assertEqual(verifier.github_repository(url), "owner/repo")
        with self.assertRaises(verifier.VerificationError):
            verifier.github_repository("https://github.com.evil.invalid/owner/repo")


if __name__ == "__main__":
    unittest.main()
