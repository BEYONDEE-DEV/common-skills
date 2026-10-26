from __future__ import annotations

import ast
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "skills" / "multi-repo-worktree"
SKILL = SKILL_DIR / "SKILL.md"
REFERENCE = SKILL_DIR / "references" / "worktree-operations.md"
INSPECTOR = SKILL_DIR / "scripts" / "inspect_worktrees.py"
SLOT_MANIFEST = SKILL_DIR / "scripts" / "slot_manifest.py"
GENERATION_PREPARER = SKILL_DIR / "scripts" / "prepare_generation_branch.py"
GENERATION_RETIRER = SKILL_DIR / "scripts" / "retire_generation_branch.py"
GENERATION_AUDITOR = SKILL_DIR / "scripts" / "audit_generation_branches.py"
MERGED_BRANCH_CLEANER = SKILL_DIR / "scripts" / "cleanup_merged_branch.py"
OPENAI_YAML = SKILL_DIR / "agents" / "openai.yaml"
PLUGIN_JSON = ROOT / ".codex-plugin" / "plugin.json"
OID_1 = "1" * 40
OID_2 = "2" * 40
OID_3 = "3" * 40


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def generation_temporary_worktree(
    repo: Path,
    family: str,
    generation: int,
    *,
    workspace_root: Path | None = None,
    slot: str = "slot-1",
    repository: str | None = None,
) -> Path:
    root = (workspace_root or repo.parent).resolve()
    repo_identity = repository or repo.name
    target = f"{family}-stageflow-g{generation}"
    digest = hashlib.sha256("\0".join((slot, repo_identity, target)).encode("utf-8")).hexdigest()
    return root / ".stageflow-worktrees" / "generation-worktrees" / f"{digest}.worktree"


class MultiRepoWorktreeSkillTests(unittest.TestCase):
    def test_skill_declares_explicit_only_multi_repo_scope(self) -> None:
        text = read(SKILL)

        self.assertIn("name: multi-repo-worktree", text)
        self.assertIn("Use only when the user explicitly invokes", text)
        self.assertIn("Do not use for ordinary Git, single-repo, or single-worktree requests", text)
        self.assertIn("multiple independent Git repositories", text)
        for keyword in ["`help`", "`status`", "`create`", "`submit`", "`promote`", "`pull`", "`sync`"]:
            self.assertIn(keyword, text)
        self.assertIn("$stageflow:multi-repo-worktree <keyword> [free-form intent]", text)
        self.assertIn("[$stageflow:multi-repo-worktree](...)", text)
        self.assertIn("references/worktree-operations.md", text)
        self.assertIn("scripts/inspect_worktrees.py", text)
        self.assertIn('python3 "<skill-dir>/scripts/inspect_worktrees.py"', text)
        self.assertIn("do not assume cwd is the plugin root", text)
        self.assertNotIn("**Setup**", text)
        self.assertNotIn("TODO", text)

    def test_skill_uses_intent_authorization_and_narrow_decision_gates(self) -> None:
        text = read(SKILL)

        self.assertIn("## Decision Contract", text)
        self.assertIn("generation-scoped", text)
        self.assertIn("explicit `create`, `submit`, `pull`, or `sync` request", text)
        self.assertIn("`status`, inspection, and dry-run are read-only", text)
        self.assertIn("concise non-blocking summary", text)
        self.assertIn("Do not ask for routine command", text)
        self.assertIn("Ask one consolidated question only", text)
        self.assertIn("records each confirmed remote source SHA", text)
        self.assertIn("Never reset, clean, force checkout, force push to rewrite a branch", text)
        self.assertIn("exact remote deletion lease", text)
        self.assertIn("compare-and-delete local ref", text)
        self.assertIn("`sync` runs only in the development bundle", text)
        self.assertIn("inseparable PR correction", text)
        for obsolete in [
            "Accept approval only after presenting that exact plan",
            "Approval applies only to the displayed batch",
            "Receive explicit approval after the exact plan",
            "approved-path Korean commits",
        ]:
            self.assertNotIn(obsolete, text)

    def test_reference_covers_required_operations(self) -> None:
        text = read(REFERENCE)

        for phrase in [
            "## Keyword Authorization And Decision Contract",
            "## Roles And Generation-Branch Flow",
            "## Permanent Slot Manifest",
            "## Read-Only Inspection And Status",
            "## Create",
            "## Submit Common Phase",
            "## Submit NONE",
            "## Submit OPEN And Explicit Correction",
            "## Submit MERGED",
            "## Common Generation Rotation",
            "## Retry, Legacy Recovery, And Partial Results",
            "## Pull",
            "## Sync",
            "### Cleanup Of Submitted Branches",
            "`help`",
            "`status`",
            "`create`",
            "`pull`",
            "`sync`",
            "Preflight the complete bundle and classify each repository independently",
            "Do not ask for routine commands",
            "Ask one consolidated question only",
            "inspection, or dry-run stays read-only",
            "source branch",
            "development worktree",
            "explicit merge base",
            "merge --ff-only",
        ]:
            self.assertIn(phrase, text)

        for removed_heading in ["## Recycle", "## Integrate", "## Merge"]:
            self.assertNotIn(removed_heading, text)
        for obsolete in [
            "Receive explicit approval after the exact plan",
            "approved-Korean-commit-message",
            "approved-Korean-title",
            "Any drift requires a new plan and approval",
        ]:
            self.assertNotIn(obsolete, text)

    def test_permanent_pr_slot_workflow_has_explicit_roles_and_safety_gates(self) -> None:
        skill = read(SKILL)
        reference = read(REFERENCE)

        for phrase in [
            ".stageflow-worktrees/slots.json",
            "There is no frozen, active, released",
            "user squash-merges PR A on GitHub",
            "Never enable auto-merge or run a remote PR merge command",
            "`pull` changes only confirmed original/source worktrees",
            "`sync` runs only in the development bundle",
            "successful siblings remain recorded",
        ]:
            self.assertIn(phrase, skill)

        for phrase in [
            "user squash-merges PR A on GitHub",
            "Development user",
            "Original user",
            "GitHub user",
            "PR generation `0` requires `pr: null`",
            "Exact retries are idempotent",
            "not a lifecycle state",
            "same slot/worktree and develop B locally",
            '"schema_version": 5',
            '"source_branch": "feature-etc"',
            '"remote": "origin"',
            "without changing file bytes",
        ]:
            self.assertIn(phrase, reference)

        for removed in [
            "stageflow-submitted-sha",
            "inspect_pr_submission.py",
            "gh pr merge",
            "--match-head-commit",
        ]:
            self.assertNotIn(removed, skill)
            self.assertNotIn(removed, reference)

        self.assertNotIn("scripts/inspect_pr_submission.py", skill)

    def test_submit_commits_in_korean_and_supports_chained_ready_prs(self) -> None:
        skill = read(SKILL)
        reference = read(REFERENCE)

        for phrase in [
            "Inspect staged, unstaged, and non-ignored untracked content",
            "Korean commit text",
            'add -A --',
            'commit -m "<generated-Korean-commit-message>"',
            "Do not pass `--draft`",
            "Ordinary OPEN submit never pushes the local active branch",
            "continuation_boundary_sha",
            "record-correction",
            "exactly one transfer commit",
            "immediately call `record-batch`",
        ]:
            self.assertIn(phrase, reference)

        self.assertIn("Korean task commits", skill)
        self.assertIn("Ordinary `OPEN` never pushes local commits", skill)
        self.assertIn("Korean ready PR", skill)
        self.assertIn("explicit correction intent", skill)
        self.assertIn("`MERGED` rotates", skill)
        self.assertIn("Never enable auto-merge or run a remote PR merge command", skill)
        self.assertIn("preserving the continuation boundary", skill)
        self.assertIn("B-only net user outcome", skill)
        self.assertIn("normalized subject", skill)
        self.assertIn("source_tree_sha", reference)
        self.assertIn("feat(events): 이벤트 필터 상태 보존", reference)
        self.assertIn("migrate-rotation-subject", reference)
        self.assertIn("discard-legacy-temporary", reference)
        self.assertNotIn("후속 작업을 최신 <source-branch> 기준으로 이전", reference)
        self.assertNotIn("gh pr edit", reference)
        self.assertNotIn('git -C "<development-worktree>" add .', reference)

    def test_submit_supports_mixed_repository_states_and_generations(self) -> None:
        skill = read(SKILL)
        reference = read(REFERENCE)

        for phrase in [
            "Classify each repository independently",
            "Record each successful repository immediately",
            "valid `NONE`, `OPEN`, or `MERGED` repository in the same bundle may still proceed",
        ]:
            self.assertIn(phrase, skill)

        for phrase in [
            "Allow any mix",
            "successful PR, correction, migration, or rotation phase immediately",
            "waiting or dirty OPEN repository does not stop an eligible clean NONE/MERGED sibling",
            "failed/skipped unchanged",
            "repository-local 3-way conflict",
            '--repository-update "<repo>" "<current-pr-generation>" "<new-pr>"',
        ]:
            self.assertIn(phrase, reference)

        self.assertNotIn(
            "changed repositories with different generation or `NONE`/`OPEN`/`MERGED` classifications",
            reference,
        )
        self.assertNotIn("mixed multi-repo states are unsupported", skill)

    def test_submit_recovers_one_exactly_proven_omitted_merged_pr(self) -> None:
        skill = read(SKILL)
        reference = read(REFERENCE)

        for phrase in [
            "incompletely proven legacy/lost history",
            "repository-local unsupported states",
            "A valid `NONE`, `OPEN`, or `MERGED` repository",
        ]:
            self.assertIn(phrase, skill)
        for phrase in [
            "Legacy schema 2/3 and omitted PR history",
            "exact GitHub PR base/head/head SHA",
            "local/remote refs",
            "merge parents",
            "tree comparisons yield one result",
            "With two candidates, missing objects",
            "never choose merely the highest PR number",
            "full recovered submission evidence",
            "Do not ask the user to edit JSON",
        ]:
            self.assertIn(phrase, reference)
        self.assertNotIn("choose merely the highest PR number", reference.split("never choose", 1)[0])

    def test_create_is_one_time_idempotent_initialization(self) -> None:
        skill = read(SKILL)
        reference = read(REFERENCE)

        self.assertIn("first initialization and exact retry", skill)
        self.assertIn("`create` owns only first initialization", skill)
        self.assertIn("Initialize schema-5 manifest evidence", skill)
        self.assertIn("Use `create` only for first initialization or exact retry", reference)
        self.assertIn("initialize --slot", reference)
        self.assertIn("worktree add -b", reference)
        self.assertIn("Exact retry leaves correct clean results", reference)
        self.assertIn("Later work never invokes `create`", reference)
        self.assertNotIn("--preserve-repositories", reference)

    def test_pull_and_sync_have_separate_locations_and_update_rules(self) -> None:
        skill = read(SKILL)
        reference = read(REFERENCE)

        self.assertIn("`pull` changes only confirmed original/source worktrees", skill)
        self.assertIn("fast-forward", skill)
        self.assertIn("`sync` runs only in the development bundle", skill)
        self.assertIn("never switches or updates the original worktree", skill)
        self.assertNotIn("`origin/<source>`", skill)
        self.assertIn("merge --ff-only \"<pinned-source-sha>\"", reference)
        self.assertIn("prepare_generation_branch.py", reference)
        self.assertIn("Do not use plain `git pull`", reference)
        self.assertIn("never changes the original worktree's checked-out state", reference)
        self.assertIn("It never invokes `pull`", reference)
        self.assertIn("cleanup_merged_branch.py", skill)
        self.assertIn("cleanup_merged_branch.py", reference)
        self.assertIn("retire_generation_branch.py", skill)
        self.assertIn("retire_generation_branch.py", reference)
        self.assertIn("audit_generation_branches.py", skill)
        self.assertIn("audit_generation_branches.py", reference)
        self.assertIn("Do not require the PR head commit itself to be an ancestor of source", reference)
        self.assertIn("git update-ref -d", reference)
        self.assertIn("--force-with-lease=refs/heads/<head>:<observed-head>", reference)

    def test_merged_branch_cleaner_uses_exact_ref_deletion_primitives(self) -> None:
        text = read(MERGED_BRANCH_CLEANER)
        tree = ast.parse(text)

        self.assertIn('"merge-base",\n        "--is-ancestor",\n        merge_commit,', text)
        self.assertIn('"merge-tree",\n        "--write-tree"', text)
        self.assertIn('"push", lease, remote, f":refs/heads/{old_branch}"', text)
        self.assertIn('"update-ref", "-d", local_ref, local_head', text)
        self.assertIn('require_operation_lock(args.root, args.slot, args.token)', text)
        self.assertIn('if not args.execute:', text)
        self.assertNotIn('"branch", "-D"', text)
        self.assertIsInstance(tree, ast.Module)

    def test_generation_retirement_and_audit_have_bounded_git_primitives(self) -> None:
        retirement = read(GENERATION_RETIRER)
        audit = read(GENERATION_AUDITOR)

        self.assertIn('["git", "-C", str(repo), "update-ref", "--stdin"]', retirement)
        self.assertIn('f"verify refs/heads/{target_branch} {target_head}', retirement)
        self.assertIn('f"delete {old_ref} {expected_old_head}', retirement)
        self.assertNotIn('"branch", "-D"', retirement)
        self.assertNotIn('"push"', retirement)
        for mutating_primitive in (
            '"update-ref"',
            '"branch", "-D"',
            '"push"',
            '"switch"',
            '"checkout"',
        ):
            self.assertNotIn(mutating_primitive, audit)

    def test_status_output_policy_uses_single_line_repo_summary(self) -> None:
        skill = read(SKILL)
        reference = read(REFERENCE)

        for text in [skill, reference]:
            self.assertIn("기준 폴더", text)
            self.assertIn("현재 브랜치", text)
            self.assertIn("변경상태", text)
            self.assertIn("dirty", text)
            self.assertIn("clean", text)

        self.assertIn("Do not use a Markdown table", skill)
        self.assertIn("원본 브랜치: feature-etc", skill)
        self.assertIn("majoong_events_api: 폴더 majoong_events_api / 현재 브랜치 feature-etc-docs-stageflow-g2 / 변경상태 dirty", skill)
        self.assertIn("Do not use a Markdown table", reference)
        self.assertIn("<source-branch (추정)|확인 필요>", reference)
        self.assertIn("<repo>: 폴더 <relative-folder> / 현재 브랜치 <branch> / 변경상태 <dirty|clean>", reference)
        self.assertIn("Add manifest PR/branch generation", reference)
        self.assertIn("held operation lock or rotation journal", reference)

    def test_inspector_is_read_only_and_avoids_mutating_git_commands(self) -> None:
        text = read(INSPECTOR)
        tree = ast.parse(text)
        git_commands: list[tuple[str, ...]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id != "run_git" or len(node.args) < 2 or not isinstance(node.args[1], ast.List):
                continue
            values = tuple(
                item.value for item in node.args[1].elts if isinstance(item, ast.Constant) and isinstance(item.value, str)
            )
            if values:
                git_commands.append(values)

        forbidden = {"add", "merge", "rebase", "push", "pull", "fetch", "reset", "switch", "checkout"}
        self.assertFalse({command[0] for command in git_commands} & forbidden)
        self.assertNotIn(("branch", "-D"), git_commands)

        self.assertIn('"worktree", "list", "--porcelain"', text)
        self.assertIn('"status", "--porcelain"', text)
        self.assertIn('"rev-parse", "--show-toplevel"', text)
        self.assertIn('"rev-parse", "--git-path", name', text)
        self.assertIn('"operation_state": operation_state(path)', text)
        self.assertIn('key in {"locked", "prunable"}', text)
        self.assertIn('parser.add_argument("--bundle"', text)
        self.assertIn('"GIT_OPTIONAL_LOCKS"] = "0"', text)
        self.assertIn('"git", "--no-optional-locks"', text)
        self.assertNotIn("repo별 다름", text)

    @unittest.skipIf(shutil.which("git") is None, "git is required for inspector smoke test")
    def test_inspector_reports_git_repo_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "service-a"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            result = subprocess.run(
                [sys.executable, str(INSPECTOR), "--root", str(root), "--max-depth", "2", "--json"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            data = json.loads(result.stdout)

            self.assertEqual(data["repository_group_count"], 1)
            self.assertGreaterEqual(data["worktree_count"], 1)
            self.assertEqual(data["groups"][0]["worktrees"][0]["dirty"], "clean")

    @unittest.skipIf(shutil.which("git") is None, "git is required for inspector smoke test")
    def test_inspector_filtered_output_uses_bundle_and_single_line_repos(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "service-a"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
            subprocess.run(["git", "checkout", "-b", "feature-etc"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            (repo / "README.md").write_text("hello\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            bundle_repo = root / "worktrees" / "feature-etc-docs" / "service-a"
            subprocess.run(
                ["git", "worktree", "add", "-b", "feature-etc-docs", str(bundle_repo), "feature-etc"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(INSPECTOR),
                    "--root",
                    str(root),
                    "--max-depth",
                    "4",
                    "--bundle",
                    "worktrees/feature-etc-docs",
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            output = result.stdout

            self.assertIn("대상 묶음: worktrees/feature-etc-docs", output)
            self.assertIn("원본 브랜치: feature-etc (추정)", output)
            self.assertIn(f"기준 폴더: {bundle_repo.parent}", output)
            self.assertIn(
                "service-a: 폴더 service-a / 현재 브랜치 feature-etc-docs / 변경상태 clean",
                output,
            )
            self.assertNotIn("| Worktree Path |", output)
            self.assertNotIn("| --- |", output)
            self.assertNotIn("Upstream", output)
            self.assertNotIn("Workspace:", output)
            self.assertNotIn("Repository groups:", output)
            self.assertNotIn("Worktrees:", output)
            self.assertNotIn("대상 묶음: workspace", output)

            json_result = subprocess.run(
                [
                    sys.executable,
                    str(INSPECTOR),
                    "--root",
                    str(root),
                    "--max-depth",
                    "4",
                    "--bundle",
                    str(bundle_repo.parent),
                    "--json",
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            bundle_data = json.loads(json_result.stdout)

            self.assertEqual([bundle["name"] for bundle in bundle_data["bundles"]], ["worktrees/feature-etc-docs"])
            self.assertEqual(bundle_data["bundles"][0]["items"][0]["operation_state"], "-")

            bundle_result = subprocess.run(
                [sys.executable, str(INSPECTOR), "--root", str(bundle_repo.parent), "--max-depth", "2"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            bundle_output = bundle_result.stdout

            self.assertIn("대상 묶음: worktrees/feature-etc-docs", bundle_output)
            self.assertIn("원본 브랜치: 확인 필요", bundle_output)
            self.assertIn(f"기준 폴더: {bundle_repo.parent}", bundle_output)
            self.assertIn(
                "service-a: 폴더 service-a / 현재 브랜치 feature-etc-docs / 변경상태 clean",
                bundle_output,
            )
            self.assertNotIn("outside workspace", bundle_output)
            self.assertNotIn("폴더 /", bundle_output)
            self.assertNotIn("Workspace:", bundle_output)

    @unittest.skipIf(shutil.which("git") is None, "git is required for inspector smoke test")
    def test_inspector_reports_in_progress_merge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "service-a"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
            subprocess.run(["git", "checkout", "-b", "main"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            tracked = repo / "tracked.txt"
            tracked.write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, stdout=subprocess.PIPE)
            subprocess.run(["git", "checkout", "-b", "topic"], cwd=repo, check=True, stdout=subprocess.PIPE)
            tracked.write_text("topic\n", encoding="utf-8")
            subprocess.run(["git", "commit", "-am", "topic"], cwd=repo, check=True, stdout=subprocess.PIPE)
            subprocess.run(["git", "checkout", "main"], cwd=repo, check=True, stdout=subprocess.PIPE)
            tracked.write_text("main\n", encoding="utf-8")
            subprocess.run(["git", "commit", "-am", "main"], cwd=repo, check=True, stdout=subprocess.PIPE)
            merge = subprocess.run(
                ["git", "merge", "topic"],
                cwd=repo,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertNotEqual(merge.returncode, 0)

            result = subprocess.run(
                [sys.executable, str(INSPECTOR), "--root", str(root), "--max-depth", "2", "--json"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            data = json.loads(result.stdout)

            self.assertEqual(data["groups"][0]["worktrees"][0]["operation_state"], "merge")

    @unittest.skipIf(shutil.which("git") is None, "git is required for inspector smoke test")
    def test_inspector_requires_confirmation_when_any_bundle_source_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = parent / "workspace"
            root.mkdir()

            repo_a = root / "service-a"
            repo_a.mkdir()
            subprocess.run(["git", "init"], cwd=repo_a, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_a, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_a, check=True)
            subprocess.run(["git", "checkout", "-b", "feature-base"], cwd=repo_a, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            (repo_a / "README.md").write_text("hello\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo_a, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=repo_a, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(
                [
                    "git",
                    "worktree",
                    "add",
                    "-b",
                    "feature-docs",
                    str(root / "worktrees" / "feature-docs" / "service-a"),
                    "feature-base",
                ],
                cwd=repo_a,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            repo_b = parent / "external-service-b"
            repo_b.mkdir()
            subprocess.run(["git", "init"], cwd=repo_b, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_b, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_b, check=True)
            subprocess.run(["git", "checkout", "-b", "other-base"], cwd=repo_b, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            (repo_b / "README.md").write_text("hello\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo_b, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=repo_b, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(
                [
                    "git",
                    "worktree",
                    "add",
                    "-b",
                    "feature-docs-b",
                    str(root / "worktrees" / "feature-docs" / "service-b"),
                    "other-base",
                ],
                cwd=repo_b,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            result = subprocess.run(
                [sys.executable, str(INSPECTOR), "--root", str(root), "--max-depth", "4"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            output = result.stdout
            bundle_section = output.split("대상 묶음: worktrees/feature-docs", maxsplit=1)[1]
            bundle_section = bundle_section.split("\n\n대상 묶음:", maxsplit=1)[0]

            self.assertIn("원본 브랜치: 확인 필요", bundle_section)
            self.assertIn("service-a: 폴더 service-a / 현재 브랜치 feature-docs / 변경상태 clean", bundle_section)
            self.assertIn("service-b: 폴더 service-b / 현재 브랜치 feature-docs-b / 변경상태 clean", bundle_section)
            self.assertNotIn("원본 브랜치: feature-base (추정)", bundle_section)

    @unittest.skipIf(shutil.which("git") is None, "git is required for inspector smoke test")
    def test_inspector_requires_confirmation_for_ambiguous_workspace_siblings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "service-a"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
            subprocess.run(["git", "checkout", "-b", "feature-base"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            (repo / "README.md").write_text("hello\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(
                ["git", "worktree", "add", "-b", "other-base", str(root / "service-a-alt"), "feature-base"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            subprocess.run(
                [
                    "git",
                    "worktree",
                    "add",
                    "-b",
                    "feature-docs",
                    str(root / "worktrees" / "feature-docs" / "service-a"),
                    "feature-base",
                ],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            result = subprocess.run(
                [sys.executable, str(INSPECTOR), "--root", str(root), "--max-depth", "4"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            output = result.stdout
            bundle_section = output.split("대상 묶음: worktrees/feature-docs", maxsplit=1)[1]
            bundle_section = bundle_section.split("\n\n대상 묶음:", maxsplit=1)[0]

            self.assertIn("원본 브랜치: 확인 필요", bundle_section)
            self.assertNotIn("원본 브랜치: feature-base (추정)", bundle_section)
            self.assertNotIn("원본 브랜치: other-base (추정)", bundle_section)

    def test_openai_metadata_is_explicit_only(self) -> None:
        text = read(OPENAI_YAML)

        self.assertIn('display_name: "Multi Repo Worktree"', text)
        # Presentation copy may describe newly added operations; invocation policy
        # and the explicit skill prompt are the behavioral contract here.
        self.assertIn("Use $stageflow:multi-repo-worktree submit to publish ready PRs", text)
        self.assertIn("allow_implicit_invocation: false", text)

    def test_plugin_manifest_exposes_multi_repo_worktree_prompt(self) -> None:
        manifest = json.loads(read(PLUGIN_JSON))
        interface = manifest["interface"]

        self.assertIn("multi-repo-worktree", interface["longDescription"])
        self.assertIn(
            "permanent development worktrees with one-time create, squash-merge-safe generation branches",
            interface["longDescription"],
        )
        self.assertIn(
            "Use $stageflow:multi-repo-worktree submit to publish ready PRs while preserving local follow-up work.",
            interface["defaultPrompt"],
        )


class SlotManifestTests(unittest.TestCase):
    def run_manifest(self, root: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SLOT_MANIFEST), "--root", str(root), *arguments],
            check=check,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def run_retirer(self, root: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(GENERATION_RETIRER), "--root", str(root), *arguments],
            check=check,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def test_generation_writes_reject_abbreviated_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "worktrees" / "slot-1"
            abbreviated_initialize = self.run_manifest(
                root,
                "initialize",
                "--slot", "slot-1",
                "--path", str(path),
                "--repository", "service-a", "task-a", "main", "origin",
                check=False,
            )
            self.assertNotEqual(abbreviated_initialize.returncode, 0)
            self.assertFalse((root / ".stageflow-worktrees" / "slots.json").exists())

            self.run_manifest(
                root,
                "initialize",
                "--slot", "slot-1",
                "--path", str(path),
                "--repository", "service-a", "task-a", "main", "origin", OID_1,
            )
            self.run_manifest(root, "lock", "--slot", "slot-1", "--token", "submit-a")
            before = (root / ".stageflow-worktrees" / "slots.json").read_bytes()
            abbreviated_record = self.run_manifest(
                root,
                "record-batch",
                "--slot", "slot-1",
                "--token", "submit-a",
                "--repository-update", "service-a", "0", "17",
                check=False,
            )
            self.assertNotEqual(abbreviated_record.returncode, 0)
            self.assertEqual((root / ".stageflow-worktrees" / "slots.json").read_bytes(), before)

    def test_initialize_is_exactly_idempotent_and_rejects_binding_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "worktrees" / "slot-1"
            first = self.run_manifest(
                root,
                "initialize",
                "--slot", "slot-1",
                "--path", str(path),
                "--repository", "service-a", "task-a", "main", "origin", OID_1,
                "--repository", "service-b", "task-b", "main", "origin", OID_1,
            )
            second = self.run_manifest(
                root,
                "initialize",
                "--slot", "slot-1",
                "--path", str(path),
                "--repository", "service-b", "task-b", "main", "origin", OID_1,
                "--repository", "service-a", "task-a", "main", "origin", OID_1,
            )
            self.run_manifest(root, "lock", "--slot", "slot-1", "--token", "submit-a")
            self.run_manifest(
                root,
                "record-batch",
                "--slot", "slot-1",
                "--token", "submit-a",
                "--repository-update", "service-a", "0", "17", "task-a", OID_2, OID_2,
            )
            after_pr = self.run_manifest(
                root,
                "initialize",
                "--slot", "slot-1",
                "--path", str(path),
                "--repository", "service-a", "task-a", "main", "origin", OID_1,
                "--repository", "service-b", "task-b", "main", "origin", OID_1,
            )
            conflict = self.run_manifest(
                root,
                "initialize",
                "--slot", "slot-1",
                "--path", str(path),
                "--repository", "service-a", "other-branch", "main", "origin", OID_1,
                "--repository", "service-b", "task-b", "main", "origin", OID_1,
                check=False,
            )

            self.assertEqual(json.loads(first.stdout)["result"], json.loads(second.stdout)["result"])
            self.assertEqual(
                json.loads(after_pr.stdout)["result"]["repositories"]["service-a"]["pr"],
                "17",
            )
            self.assertEqual(conflict.returncode, 2)
            self.assertIn("permanent slot binding mismatch", conflict.stderr)
            result = json.loads(first.stdout)["result"]
            self.assertNotIn("owner", result)
            self.assertNotIn("state", result)
            identity = result["repositories"]["service-a"]
            self.assertEqual(identity["branch"], "task-a")
            self.assertEqual(identity["branch_family"], "task-a")
            self.assertEqual(identity["branch_generation"], 1)
            self.assertEqual(identity["generation"], 0)

    def test_concurrent_conflicting_initializations_leave_one_valid_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "worktrees" / "slot-1"
            commands = [
                [
                    sys.executable,
                    str(SLOT_MANIFEST),
                    "--root",
                    str(root),
                    "initialize",
                    "--slot", "slot-1",
                    "--path", str(path),
                "--repository", "service-a", branch, "main", "origin", OID_1,                ]
                for branch in ["task-a", "task-b"]
            ]
            processes = [
                subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for command in commands
            ]
            results = [process.communicate(timeout=10) + (process.returncode,) for process in processes]

            self.assertEqual(sorted(result[2] for result in results), [0, 2])
            status = json.loads(self.run_manifest(root, "status", "--slot", "slot-1").stdout)["result"]
            self.assertIn(status["repositories"]["service-a"]["branch"], {"task-a", "task-b"})
            self.assertEqual(status["repositories"]["service-a"]["generation"], 0)

    def test_schema_v2_status_is_read_only_and_selected_slot_context_binding_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / ".stageflow-worktrees"
            state.mkdir()
            manifest_path = state / "slots.json"
            manifest = {
                "schema_version": 2,
                "slots": {
                    "slot-1": {
                        "path": str((root / "worktrees" / "slot-1").resolve()),
                        "repositories": {
                            "service-a": {"branch": "task-a", "generation": 1, "pr": "17"},
                            "service-b": {"branch": "task-b", "generation": 1, "pr": "23"},
                        },
                    },
                    "slot-2": {
                        "path": str((root / "worktrees" / "slot-2").resolve()),
                        "repositories": {
                            "service-c": {"branch": "task-c", "generation": 0, "pr": None}
                        },
                    },
                },
            }
            manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
            before = manifest_path.read_bytes()

            status = json.loads(self.run_manifest(root, "status").stdout)["result"]
            self.assertEqual(status["schema_version"], 5)
            self.assertIsNone(status["slots"]["slot-1"]["repositories"]["service-a"]["source_branch"])
            self.assertIsNone(status["slots"]["slot-2"]["repositories"]["service-c"]["remote"])
            self.assertEqual(manifest_path.read_bytes(), before)

            locked_before_context = self.run_manifest(
                root, "lock", "--slot", "slot-1", "--token", "submit-a", check=False
            )
            self.assertEqual(locked_before_context.returncode, 2)
            self.assertIn("requires source context", locked_before_context.stderr)
            self.assertEqual(manifest_path.read_bytes(), before)

            incomplete = self.run_manifest(
                root,
                "bind-context",
                "--slot", "slot-1",
                "--repository-context", "service-a", "main", "origin",
                check=False,
            )
            self.assertEqual(incomplete.returncode, 2)
            self.assertIn("context must cover every repository", incomplete.stderr)
            self.assertEqual(manifest_path.read_bytes(), before)

            bound = self.run_manifest(
                root,
                "bind-context",
                "--slot", "slot-1",
                "--repository-context", "service-a", "main", "origin",
                "--repository-context", "service-b", "main", "upstream",
            )
            repeated = self.run_manifest(
                root,
                "bind-context",
                "--slot", "slot-1",
                "--repository-context", "service-b", "main", "upstream",
                "--repository-context", "service-a", "main", "origin",
            )
            self.assertEqual(json.loads(bound.stdout)["result"], json.loads(repeated.stdout)["result"])

            written = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(written["schema_version"], 5)
            self.assertEqual(
                written["slots"]["slot-1"]["repositories"]["service-a"]["source_branch"],
                "main",
            )
            self.assertEqual(
                written["slots"]["slot-1"]["repositories"]["service-b"]["remote"],
                "upstream",
            )
            self.assertIsNone(
                written["slots"]["slot-2"]["repositories"]["service-c"]["source_branch"]
            )
            self.assertIsNone(written["slots"]["slot-2"]["repositories"]["service-c"]["remote"])

            conflict_before = manifest_path.read_bytes()
            conflict = self.run_manifest(
                root,
                "bind-context",
                "--slot", "slot-1",
                "--repository-context", "service-a", "develop", "origin",
                "--repository-context", "service-b", "main", "upstream",
                check=False,
            )
            self.assertEqual(conflict.returncode, 2)
            self.assertIn("source context mismatch", conflict.stderr)
            self.assertEqual(manifest_path.read_bytes(), conflict_before)

    def test_legacy_v2_requires_exact_migration_and_preserves_current_pr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / ".stageflow-worktrees"
            state.mkdir()
            slot_path = (root / "worktrees" / "slot-1").resolve()
            manifest = {
                "schema_version": 2,
                "slots": {
                    "slot-1": {
                        "path": str(slot_path),
                        "repositories": {
                            "service-a": {"branch": "task-a", "generation": 1, "pr": "17"}
                        },
                    }
                },
            }
            (state / "slots.json").write_text(json.dumps(manifest), encoding="utf-8")

            inferred = self.run_manifest(
                root,
                "initialize",
                "--slot", "slot-1",
                "--path", str(slot_path),
                "--repository", "service-a", "task-a", "main", "origin", OID_1,
                check=False,
            )
            self.assertEqual(inferred.returncode, 2)
            self.assertIn("permanent slot binding mismatch", inferred.stderr)

            self.run_manifest(
                root,
                "bind-context",
                "--slot", "slot-1",
                "--repository-context", "service-a", "main", "origin",
            )
            self.run_manifest(root, "lock", "--slot", "slot-1", "--token", "migrate-a")
            migrated = self.run_manifest(
                root,
                "migrate-batch",
                "--slot", "slot-1",
                "--token", "migrate-a",
                "--repository-migration",
                "service-a", "1", "17", "task-a", "1", OID_1, "task-a", OID_2, OID_2,
            )
            identity = json.loads(migrated.stdout)["result"]["repositories"]["service-a"]
            self.assertEqual(identity["generation"], 1)
            self.assertEqual(identity["pr"], "17")
            self.assertEqual(identity["source_branch"], "main")
            self.assertEqual(identity["remote"], "origin")
            self.assertEqual(identity["branch_base_sha"], OID_1)
            self.assertNotIn("legacy_schema", identity)

            before = (state / "slots.json").read_bytes()
            mismatch = self.run_manifest(
                root,
                "initialize",
                "--slot", "slot-1",
                "--path", str(slot_path),
                "--repository", "service-a", "task-a", "develop", "origin", OID_1,
                check=False,
            )
            self.assertEqual(mismatch.returncode, 2)
            self.assertIn("permanent slot binding mismatch", mismatch.stderr)
            self.assertEqual((state / "slots.json").read_bytes(), before)

    def test_manifest_lock_is_recoverable_after_holder_process_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            crash_while_locked = """
import os
import pathlib
import sys

module = {"__name__": "slot_manifest_crash_test"}
source = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
exec(compile(source, sys.argv[1], "exec"), module)
path = module["manifest_path"](pathlib.Path(sys.argv[2]))
with module["manifest_lock"](path, timeout_seconds=1.0):
    os._exit(0)
"""
            subprocess.run(
                [sys.executable, "-c", crash_while_locked, str(SLOT_MANIFEST), str(root)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            lock_path = root / ".stageflow-worktrees" / "slots.json.lock"
            self.assertTrue(lock_path.exists())
            initialized = self.run_manifest(
                root,
                "initialize",
                "--slot", "slot-1",
                "--path", str(root / "worktrees" / "slot-1"),
                "--repository", "service-a", "task-a", "main", "origin", OID_1,
            )
            self.assertEqual(json.loads(initialized.stdout)["result"]["path"], str(root / "worktrees" / "slot-1"))

    def test_operation_lock_is_exclusive_idempotent_and_exact_token_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "worktrees" / "slot-1"
            self.run_manifest(
                root,
                "initialize",
                "--slot", "slot-1",
                "--path", str(path),
                "--repository", "service-a", "task-a", "main", "origin", OID_1,
            )
            first = self.run_manifest(root, "lock", "--slot", "slot-1", "--token", "submit-a")
            repeated = self.run_manifest(root, "lock", "--slot", "slot-1", "--token", "submit-a")
            competing = self.run_manifest(
                root, "lock", "--slot", "slot-1", "--token", "submit-b", check=False
            )
            wrong_unlock = self.run_manifest(
                root, "unlock", "--slot", "slot-1", "--token", "submit-b", check=False
            )
            status = self.run_manifest(root, "lock-status", "--slot", "slot-1")
            unlocked = self.run_manifest(
                root, "unlock", "--slot", "slot-1", "--token", "submit-a"
            )

            self.assertEqual(json.loads(first.stdout)["result"]["token"], "submit-a")
            self.assertEqual(json.loads(repeated.stdout)["result"]["token"], "submit-a")
            self.assertEqual(json.loads(status.stdout)["result"]["token"], "submit-a")
            self.assertEqual(json.loads(unlocked.stdout)["result"]["token"], "submit-a")
            self.assertEqual(competing.returncode, 2)
            self.assertIn("slot operation is locked", competing.stderr)
            self.assertEqual(wrong_unlock.returncode, 2)
            self.assertIn("token mismatch", wrong_unlock.stderr)

    def test_record_batch_requires_lock_and_advances_all_repositories_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "worktrees" / "slot-1"
            self.run_manifest(
                root,
                "initialize",
                "--slot", "slot-1",
                "--path", str(path),
                "--repository", "service-a", "task-a", "main", "origin", OID_1,
                "--repository", "service-b", "task-b", "main", "origin", OID_1,
            )
            no_lock = self.run_manifest(
                root,
                "record-batch",
                "--slot", "slot-1",
                "--token", "submit-a",
                "--repository-update", "service-a", "0", "17", "task-a", OID_2, OID_2,
                "--repository-update", "service-b", "0", "23", "task-b", OID_2, OID_2,
                check=False,
            )
            self.run_manifest(root, "lock", "--slot", "slot-1", "--token", "submit-a")
            recorded = self.run_manifest(
                root,
                "record-batch",
                "--slot", "slot-1",
                "--token", "submit-a",
                "--repository-update", "service-a", "0", "17", "task-a", OID_2, OID_2,
                "--repository-update", "service-b", "0", "23", "task-b", OID_2, OID_2,
            )

            self.assertEqual(no_lock.returncode, 2)
            self.assertIn("not locked", no_lock.stderr)
            result = json.loads(recorded.stdout)["result"]
            self.assertEqual(result["repositories"]["service-a"]["generation"], 1)
            self.assertEqual(result["repositories"]["service-a"]["pr"], "17")
            self.assertEqual(result["repositories"]["service-b"]["generation"], 1)
            self.assertEqual(result["repositories"]["service-b"]["pr"], "23")
            self.assertIn("subprocess", read(SLOT_MANIFEST))

    def test_record_batch_is_retry_idempotent_and_advances_next_pr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "worktrees" / "slot-1"
            self.run_manifest(
                root,
                "initialize",
                "--slot", "slot-1",
                "--path", str(path),
                "--repository", "service-a", "task-a", "main", "origin", OID_1,
            )
            self.run_manifest(root, "lock", "--slot", "slot-1", "--token", "submit-a")
            first = self.run_manifest(
                root,
                "record-batch",
                "--slot", "slot-1",
                "--token", "submit-a",
                "--repository-update", "service-a", "0", "17", "task-a", OID_2, OID_2,
            )
            repeated = self.run_manifest(
                root,
                "record-batch",
                "--slot", "slot-1",
                "--token", "submit-a",
                "--repository-update", "service-a", "0", "17", "task-a", OID_2, OID_2,
            )
            second = self.run_manifest(
                root,
                "record-batch",
                "--slot", "slot-1",
                "--token", "submit-a",
                "--repository-update", "service-a", "1", "18", "task-a", OID_2, OID_2,
            )

            self.assertEqual(json.loads(first.stdout)["result"], json.loads(repeated.stdout)["result"])
            identity = json.loads(second.stdout)["result"]["repositories"]["service-a"]
            self.assertEqual(identity["branch"], "task-a")
            self.assertEqual(identity["generation"], 2)
            self.assertEqual(identity["pr"], "18")
            self.assertNotIn("legacy_schema", identity)

    def test_record_batch_atomically_advances_mixed_repository_generations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "worktrees" / "slot-1"
            self.run_manifest(
                root,
                "initialize",
                "--slot", "slot-1",
                "--path", str(path),
                "--repository", "service-none", "task-none", "main", "origin", OID_1,
                "--repository", "service-merged", "task-merged", "main", "origin", OID_1,
            )
            self.run_manifest(root, "lock", "--slot", "slot-1", "--token", "submit-a")
            self.run_manifest(
                root,
                "record-batch",
                "--slot", "slot-1",
                "--token", "submit-a",
                "--repository-update", "service-merged", "0", "23", "task-merged", OID_2, OID_2,
            )

            before_invalid = (
                root / ".stageflow-worktrees" / "slots.json"
            ).read_bytes()
            invalid = self.run_manifest(
                root,
                "record-batch",
                "--slot", "slot-1",
                "--token", "submit-a",
                "--repository-update", "service-none", "0", "17", "task-none", OID_2, OID_2,
                "--repository-update", "service-merged", "0", "24", "task-merged", OID_2, OID_2,
                check=False,
            )
            self.assertEqual(invalid.returncode, 2)
            self.assertIn("generation mismatch", invalid.stderr)
            self.assertEqual(
                (root / ".stageflow-worktrees" / "slots.json").read_bytes(),
                before_invalid,
            )

            mixed = self.run_manifest(
                root,
                "record-batch",
                "--slot", "slot-1",
                "--token", "submit-a",
                "--repository-update", "service-none", "0", "17", "task-none", OID_2, OID_2,
                "--repository-update", "service-merged", "1", "24", "task-merged", OID_2, OID_2,
            )
            repeated = self.run_manifest(
                root,
                "record-batch",
                "--slot", "slot-1",
                "--token", "submit-a",
                "--repository-update", "service-none", "0", "17", "task-none", OID_2, OID_2,
                "--repository-update", "service-merged", "1", "24", "task-merged", OID_2, OID_2,
            )

            self.assertEqual(json.loads(mixed.stdout)["result"], json.loads(repeated.stdout)["result"])
            repositories = json.loads(mixed.stdout)["result"]["repositories"]
            self.assertEqual(repositories["service-none"]["generation"], 1)
            self.assertEqual(repositories["service-none"]["pr"], "17")
            self.assertEqual(repositories["service-merged"]["generation"], 2)
            self.assertEqual(repositories["service-merged"]["pr"], "24")

    def test_record_batch_rejects_generation_or_repository_mismatch_without_partial_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "worktrees" / "slot-1"
            self.run_manifest(
                root,
                "initialize",
                "--slot", "slot-1",
                "--path", str(path),
                "--repository", "service-a", "task-a", "main", "origin", OID_1,
                "--repository", "service-b", "task-b", "main", "origin", OID_1,
            )
            self.run_manifest(root, "lock", "--slot", "slot-1", "--token", "submit-a")
            unknown = self.run_manifest(
                root,
                "record-batch",
                "--slot", "slot-1",
                "--token", "submit-a",
                "--repository-update", "service-a", "0", "17", "task-a", OID_2, OID_2,
                "--repository-update", "missing", "0", "23", "missing-branch", OID_2, OID_2,
                check=False,
            )
            wrong_generation = self.run_manifest(
                root,
                "record-batch",
                "--slot", "slot-1",
                "--token", "submit-a",
                "--repository-update", "service-a", "1", "17", "task-a", OID_2, OID_2,
                check=False,
            )
            duplicate = self.run_manifest(
                root,
                "record-batch",
                "--slot", "slot-1",
                "--token", "submit-a",
                "--repository-update", "service-a", "0", "17", "task-a", OID_2, OID_2,
                "--repository-update", "service-a", "0", "18", "task-a", OID_2, OID_2,
                check=False,
            )
            status = json.loads(self.run_manifest(root, "status", "--slot", "slot-1").stdout)["result"]

            self.assertEqual(unknown.returncode, 2)
            self.assertIn("no repository binding", unknown.stderr)
            self.assertEqual(wrong_generation.returncode, 2)
            self.assertIn("generation mismatch", wrong_generation.stderr)
            self.assertEqual(duplicate.returncode, 2)
            self.assertIn("duplicate repository PR update", duplicate.stderr)
            self.assertEqual(status["repositories"]["service-a"]["generation"], 0)
            self.assertIsNone(status["repositories"]["service-a"]["pr"])

    def test_reconcile_batch_requires_exact_current_pr_and_is_retry_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "worktrees" / "slot-1"
            self.run_manifest(
                root,
                "initialize",
                "--slot", "slot-1",
                "--path", str(path),
                "--repository", "service-a", "task-a", "main", "origin", OID_1,
            )
            self.run_manifest(root, "lock", "--slot", "slot-1", "--token", "submit-a")
            self.run_manifest(
                root,
                "record-batch",
                "--slot", "slot-1",
                "--token", "submit-a",
                "--repository-update", "service-a", "0", "17", "task-a", OID_2, OID_2,
            )
            wrong_pr = self.run_manifest(
                root,
                "reconcile-batch",
                "--slot", "slot-1",
                "--token", "submit-a",
                "--repository-recovery", "service-a", "1", "16", "19", "task-a", OID_3, OID_3,
                check=False,
            )
            first = self.run_manifest(
                root,
                "reconcile-batch",
                "--slot", "slot-1",
                "--token", "submit-a",
                "--repository-recovery", "service-a", "1", "17", "19", "task-a", OID_3, OID_3,
            )
            repeated = self.run_manifest(
                root,
                "reconcile-batch",
                "--slot", "slot-1",
                "--token", "submit-a",
                "--repository-recovery", "service-a", "1", "17", "19", "task-a", OID_3, OID_3,
            )

            self.assertEqual(wrong_pr.returncode, 2)
            self.assertIn("current PR mismatch", wrong_pr.stderr)
            self.assertEqual(json.loads(first.stdout)["result"], json.loads(repeated.stdout)["result"])
            identity = json.loads(first.stdout)["result"]["repositories"]["service-a"]
            self.assertEqual(identity["generation"], 2)
            self.assertEqual(identity["pr"], "19")
            self.assertEqual(
                identity["last_reconciliation"],
                {
                    "from_generation": 1,
                    "from_pr": "17",
                    "to_generation": 2,
                    "to_pr": "19",
                    "head_branch": "task-a",
                    "continuation_boundary_sha": OID_3,
                    "observed_head_sha": OID_3,
                },
            )

            before_wrong_retry = (root / ".stageflow-worktrees" / "slots.json").read_bytes()
            wrong_retry = self.run_manifest(
                root,
                "reconcile-batch",
                "--slot", "slot-1",
                "--token", "submit-a",
                "--repository-recovery", "service-a", "1", "16", "19", "task-a", OID_3, OID_3,
                check=False,
            )
            self.assertEqual(wrong_retry.returncode, 2)
            self.assertIn("recovery retry does not match its receipt", wrong_retry.stderr)
            self.assertEqual(
                (root / ".stageflow-worktrees" / "slots.json").read_bytes(),
                before_wrong_retry,
            )

    def test_reconcile_batch_requires_lock_and_rejects_generation_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "worktrees" / "slot-1"
            self.run_manifest(
                root,
                "initialize",
                "--slot", "slot-1",
                "--path", str(path),
                "--repository", "service-a", "task-a", "main", "origin", OID_1,
            )
            self.run_manifest(root, "lock", "--slot", "slot-1", "--token", "submit-a")
            self.run_manifest(
                root,
                "record-batch",
                "--slot", "slot-1",
                "--token", "submit-a",
                "--repository-update", "service-a", "0", "17", "task-a", OID_2, OID_2,
            )
            self.run_manifest(root, "unlock", "--slot", "slot-1", "--token", "submit-a")
            no_lock = self.run_manifest(
                root,
                "reconcile-batch",
                "--slot", "slot-1",
                "--token", "submit-a",
                "--repository-recovery", "service-a", "1", "17", "19", "task-a", OID_3, OID_3,
                check=False,
            )
            self.run_manifest(root, "lock", "--slot", "slot-1", "--token", "submit-a")
            wrong_generation = self.run_manifest(
                root,
                "reconcile-batch",
                "--slot", "slot-1",
                "--token", "submit-a",
                "--repository-recovery", "service-a", "2", "17", "19", "task-a", OID_3, OID_3,
                check=False,
            )

            self.assertEqual(no_lock.returncode, 2)
            self.assertIn("not locked", no_lock.stderr)
            self.assertEqual(wrong_generation.returncode, 2)
            self.assertIn("generation mismatch", wrong_generation.stderr)

    def test_reconcile_batch_updates_multiple_repositories_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "worktrees" / "slot-2"
            self.run_manifest(
                root,
                "initialize",
                "--slot", "slot-2",
                "--path", str(path),
                "--repository", "service-newer", "task-newer", "main", "origin", OID_1,
                "--repository", "service-older", "task-older", "main", "origin", OID_1,
            )
            self.run_manifest(root, "lock", "--slot", "slot-2", "--token", "submit-b")
            self.run_manifest(
                root,
                "record-batch",
                "--slot", "slot-2",
                "--token", "submit-b",
                "--repository-update", "service-newer", "0", "20", "task-newer", OID_2, OID_2,
                "--repository-update", "service-older", "0", "10", "task-older", OID_2, OID_2,
            )
            self.run_manifest(
                root,
                "record-batch",
                "--slot", "slot-2",
                "--token", "submit-b",
                "--repository-update", "service-newer", "1", "22", "task-newer", OID_2, OID_2,
            )

            before_invalid = (root / ".stageflow-worktrees" / "slots.json").read_bytes()
            invalid = self.run_manifest(
                root,
                "reconcile-batch",
                "--slot", "slot-2",
                "--token", "submit-b",
                "--repository-recovery", "service-newer", "2", "22", "24", "task-newer", OID_3, OID_3,
                "--repository-recovery", "service-older", "2", "10", "12", "task-older", OID_3, OID_3,
                check=False,
            )
            self.assertEqual(invalid.returncode, 2)
            self.assertIn("generation mismatch", invalid.stderr)
            self.assertEqual(
                (root / ".stageflow-worktrees" / "slots.json").read_bytes(),
                before_invalid,
            )

            recovered = self.run_manifest(
                root,
                "reconcile-batch",
                "--slot", "slot-2",
                "--token", "submit-b",
                "--repository-recovery", "service-newer", "2", "22", "24", "task-newer", OID_3, OID_3,
                "--repository-recovery", "service-older", "1", "10", "12", "task-older", OID_3, OID_3,
            )
            repositories = json.loads(recovered.stdout)["result"]["repositories"]
            self.assertEqual(repositories["service-newer"]["generation"], 3)
            self.assertEqual(repositories["service-newer"]["pr"], "24")
            self.assertEqual(repositories["service-older"]["generation"], 2)
            self.assertEqual(repositories["service-older"]["pr"], "12")

            before_mixed_retry = (root / ".stageflow-worktrees" / "slots.json").read_bytes()
            mixed_wrong_retry = self.run_manifest(
                root,
                "reconcile-batch",
                "--slot", "slot-2",
                "--token", "submit-b",
                "--repository-recovery", "service-newer", "2", "WRONG", "24", "task-newer", OID_3, OID_3,
                "--repository-recovery", "service-older", "2", "12", "14", "task-older", OID_3, OID_3,
                check=False,
            )
            self.assertEqual(mixed_wrong_retry.returncode, 2)
            self.assertIn("recovery retry does not match its receipt", mixed_wrong_retry.stderr)
            self.assertEqual(
                (root / ".stageflow-worktrees" / "slots.json").read_bytes(),
                before_mixed_retry,
            )

            advanced = self.run_manifest(
                root,
                "record-batch",
                "--slot", "slot-2",
                "--token", "submit-b",
                "--repository-update", "service-newer", "3", "26", "task-newer", OID_2, OID_2,
            )
            self.assertNotIn(
                "last_reconciliation",
                json.loads(advanced.stdout)["result"]["repositories"]["service-newer"],
            )

    def test_legacy_lifecycle_manifest_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / ".stageflow-worktrees"
            state.mkdir()
            manifest = {
                "schema_version": 1,
                "slots": {
                    "slot-1": {
                        "owner": "task-a",
                        "path": str((root / "worktrees" / "slot-1").resolve()),
                        "repositories": {
                            "service-a": {
                                "branch": "task-a",
                                "pr": "17",
                                "head_sha": "a" * 40,
                                "pushed_sha": "a" * 40,
                            }
                        },
                        "state": "active",
                    }
                },
            }
            (state / "slots.json").write_text(json.dumps(manifest), encoding="utf-8")

            status = self.run_manifest(root, "status", "--slot", "slot-1", check=False)

            self.assertEqual(status.returncode, 2)
            self.assertIn("unsupported manifest schema: 1", status.stderr)
            self.assertIn("legacy active/released manifests require manual correction", status.stderr)

    def test_permanent_slot_rejects_a_different_path_or_repository_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "worktrees" / "slot-1"
            replacement = root / "worktrees" / "other-slot"
            self.run_manifest(
                root,
                "initialize",
                "--slot", "slot-1",
                "--path", str(original),
                "--repository", "service-a", "task-a", "main", "origin", OID_1,
            )
            path_result = self.run_manifest(
                root,
                "initialize",
                "--slot", "slot-1",
                "--path", str(replacement),
                "--repository", "service-a", "task-a", "main", "origin", OID_1,
                check=False,
            )
            repositories_result = self.run_manifest(
                root,
                "initialize",
                "--slot", "slot-1",
                "--path", str(original),
                "--repository", "service-a", "task-a", "main", "origin", OID_1,
                "--repository", "service-b", "task-b", "main", "origin", OID_1,
                check=False,
            )

            self.assertEqual(path_result.returncode, 2)
            self.assertIn("permanent slot binding mismatch", path_result.stderr)
            self.assertEqual(repositories_result.returncode, 2)
            self.assertIn("permanent slot binding mismatch", repositories_result.stderr)
            status = json.loads(self.run_manifest(root, "status", "--slot", "slot-1").stdout)["result"]
            self.assertEqual(status["path"], str(original))

    def test_concurrent_operation_locks_allow_only_one_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.run_manifest(
                root,
                "initialize",
                "--slot", "slot-1",
                "--path", str(root / "worktrees" / "slot-1"),
                "--repository", "service-a", "task-a", "main", "origin", OID_1,
            )
            commands = [
                [
                    sys.executable,
                    str(SLOT_MANIFEST),
                    "--root", str(root),
                    "lock",
                    "--slot", "slot-1",
                    "--token", token,
                ]
                for token in ["submit-a", "submit-b"]
            ]
            processes = [
                subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for command in commands
            ]
            results = [process.communicate(timeout=10) + (process.returncode,) for process in processes]

            self.assertEqual(sorted(result[2] for result in results), [0, 2])
            lock = json.loads(self.run_manifest(root, "lock-status", "--slot", "slot-1").stdout)["result"]
            self.assertIn(lock["token"], {"submit-a", "submit-b"})

    def test_unpublished_lock_candidate_does_not_create_a_malformed_stale_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.run_manifest(
                root,
                "initialize",
                "--slot", "slot-1",
                "--path", str(root / "worktrees" / "slot-1"),
                "--repository", "service-a", "task-a", "main", "origin", OID_1,
            )
            locks = root / ".stageflow-worktrees" / "operation-locks"
            locks.mkdir()
            (locks / "interrupted-candidate.tmp").write_text("{partial", encoding="utf-8")

            acquired = self.run_manifest(
                root, "lock", "--slot", "slot-1", "--token", "submit-a"
            )
            status = self.run_manifest(root, "lock-status", "--slot", "slot-1")

            self.assertEqual(json.loads(acquired.stdout)["result"]["token"], "submit-a")
            self.assertEqual(json.loads(status.stdout)["result"]["token"], "submit-a")
            source = read(SLOT_MANIFEST)
            self.assertIn("os.link(candidate, path)", source)
            self.assertNotIn("os.open(path, os.O_CREAT | os.O_EXCL", source)

    def test_lock_publish_failure_leaves_no_final_or_candidate_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module: dict[str, object] = {"__name__": "slot_manifest_test"}
            exec(compile(read(SLOT_MANIFEST), str(SLOT_MANIFEST), "exec"), module)
            lock_path = module["operation_lock_path"](root, "slot-1")

            with mock.patch.object(module["os"], "link", side_effect=OSError("simulated crash")):
                with self.assertRaises(module["ManifestError"]):
                    module["acquire_operation_lock"](root, "slot-1", "submit-a")

            self.assertFalse(lock_path.exists())
            self.assertEqual(list(lock_path.parent.glob("*.tmp")), [])

    def test_schema_rejects_relative_paths_and_unexpected_lifecycle_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / ".stageflow-worktrees"
            state.mkdir()
            manifest = {
                "schema_version": 2,
                "slots": {
                    "slot-1": {
                        "path": "worktrees/slot-1",
                        "repositories": {
                            "service-a": {"branch": "task-a", "generation": 0, "pr": None}
                        },
                    }
                },
            }
            (state / "slots.json").write_text(json.dumps(manifest), encoding="utf-8")
            relative = self.run_manifest(root, "status", check=False)

            manifest["slots"]["slot-1"]["path"] = str((root / "worktrees" / "slot-1").resolve())
            manifest["slots"]["slot-1"]["state"] = "active"
            (state / "slots.json").write_text(json.dumps(manifest), encoding="utf-8")
            lifecycle = self.run_manifest(root, "status", check=False)

            self.assertEqual(relative.returncode, 2)
            self.assertIn("invalid path", relative.stderr)
            self.assertEqual(lifecycle.returncode, 2)
            self.assertIn("unexpected fields", lifecycle.stderr)

    def test_schema_rejects_boolean_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / ".stageflow-worktrees"
            state.mkdir()
            for generation, pr in [(True, "17"), (False, None)]:
                with self.subTest(generation=generation):
                    manifest = {
                        "schema_version": 2,
                        "slots": {
                            "slot-1": {
                                "path": str((root / "worktrees" / "slot-1").resolve()),
                                "repositories": {
                                    "service-a": {
                                        "branch": "task-a",
                                        "generation": generation,
                                        "pr": pr,
                                    }
                                },
                            }
                        },
                    }
                    (state / "slots.json").write_text(json.dumps(manifest), encoding="utf-8")
                    result = self.run_manifest(root, "status", check=False)

                    self.assertEqual(result.returncode, 2)
                    self.assertIn("invalid generation", result.stderr)

    def test_schema_v3_rejects_partial_or_empty_source_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / ".stageflow-worktrees"
            state.mkdir()
            manifest_path = state / "slots.json"
            base_identity = {
                "branch": "task-a",
                "source_branch": "main",
                "remote": "origin",
                "generation": 0,
                "pr": None,
            }
            for source_branch, remote, error in [
                (None, "origin", "bind source branch and remote together"),
                ("main", None, "bind source branch and remote together"),
                ("", "origin", "invalid source context"),
                ("main", "", "invalid source context"),
            ]:
                with self.subTest(source_branch=source_branch, remote=remote):
                    identity = dict(base_identity, source_branch=source_branch, remote=remote)
                    manifest = {
                        "schema_version": 3,
                        "slots": {
                            "slot-1": {
                                "path": str((root / "worktrees" / "slot-1").resolve()),
                                "repositories": {"service-a": identity},
                            }
                        },
                    }
                    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                    result = self.run_manifest(root, "status", check=False)
                    self.assertEqual(result.returncode, 2)
                    self.assertIn(error, result.stderr)

    def test_schema_v3_rejects_null_or_malformed_reconciliation_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / ".stageflow-worktrees"
            state.mkdir()
            manifest_path = state / "slots.json"
            base_identity = {
                "branch": "task-a",
                "source_branch": "main",
                "remote": "origin",
                "generation": 2,
                "pr": "19",
            }
            for receipt in [
                None,
                {},
                {
                    "from_generation": 1,
                    "from_pr": "17",
                    "to_generation": 3,
                    "to_pr": "19",
                },
            ]:
                with self.subTest(receipt=receipt):
                    identity = dict(base_identity, last_reconciliation=receipt)
                    manifest = {
                        "schema_version": 3,
                        "slots": {
                            "slot-1": {
                                "path": str((root / "worktrees" / "slot-1").resolve()),
                                "repositories": {"service-a": identity},
                            }
                        },
                    }
                    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                    result = self.run_manifest(root, "status", check=False)
                    self.assertEqual(result.returncode, 2)
                    self.assertIn("invalid reconciliation receipt", result.stderr)

    def test_schema_version_must_be_an_exact_integer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / ".stageflow-worktrees"
            state.mkdir()
            manifest_path = state / "slots.json"
            for version in [True, 2.0, 3.0, "3"]:
                with self.subTest(version=version):
                    manifest_path.write_text(
                        json.dumps({"schema_version": version, "slots": {}}),
                        encoding="utf-8",
            )
                    result = self.run_manifest(root, "status", check=False)
                    self.assertEqual(result.returncode, 2)
                    self.assertIn("unsupported manifest schema", result.stderr)

    def test_malformed_manifest_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / ".stageflow-worktrees"
            state.mkdir()
            (state / "slots.json").write_text("{broken", encoding="utf-8")
            result = self.run_manifest(root, "status", check=False)

            self.assertEqual(result.returncode, 2)
            self.assertIn("cannot read manifest", result.stderr)

    def test_generation_submission_correction_and_rotation_are_exactly_journaled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = "1" * 40
            submitted = "2" * 40
            corrected = "3" * 40
            local_head = "4" * 40
            next_source = "5" * 40
            result_tree = "6" * 40
            source_tree = "8" * 40
            transfer_subject = "feat: 이벤트 필터 상태 보존"
            self.run_manifest(
                root,
                "initialize",
                "--slot", "slot-1",
                "--path", str(root / "worktrees" / "slot-1"),
                "--repository", "service-a", "task-a", "main", "origin", source,
            )
            self.run_manifest(root, "lock", "--slot", "slot-1", "--token", "submit-a")
            recorded = self.run_manifest(
                root,
                "record-batch",
                "--slot", "slot-1",
                "--token", "submit-a",
                "--repository-update", "service-a", "0", "17", "task-a", submitted, submitted,
            )
            identity = json.loads(recorded.stdout)["result"]["repositories"]["service-a"]
            self.assertEqual(identity["branch_base_sha"], source)
            self.assertEqual(identity["submission"]["continuation_boundary_sha"], submitted)

            corrected_result = self.run_manifest(
                root,
                "record-correction",
                "--slot", "slot-1",
                "--token", "submit-a",
                "--repository-correction", "service-a", "1", "17", submitted, corrected,
            )
            identity = json.loads(corrected_result.stdout)["result"]["repositories"]["service-a"]
            self.assertEqual(identity["submission"]["continuation_boundary_sha"], submitted)
            self.assertEqual(identity["submission"]["observed_head_sha"], corrected)

            target = "task-a-stageflow-g2"
            temporary_worktree = str(generation_temporary_worktree(root / "service-a", "task-a", 2))
            invalid_temporary = self.run_manifest(
                root,
                "begin-rotation",
                "--slot", "slot-1",
                "--token", "submit-a",
                "--repository-rotation",
                "service-a", "task-a", "1", local_head, submitted, next_source, source_tree,
                target, "2", result_tree, transfer_subject,
                str(root / "original-source-worktree"),
                check=False,
            )
            self.assertEqual(invalid_temporary.returncode, 2)
            self.assertIn("temporary worktree is not deterministic", invalid_temporary.stderr)
            status_before_rotation = json.loads(
                self.run_manifest(root, "status", "--slot", "slot-1").stdout
            )["result"]
            self.assertNotIn("rotation", status_before_rotation["repositories"]["service-a"])
            begun = self.run_manifest(
                root,
                "begin-rotation",
                "--slot", "slot-1",
                "--token", "submit-a",
                "--repository-rotation",
                "service-a", "task-a", "1", local_head, submitted, next_source, source_tree,
                target, "2", result_tree, transfer_subject,
                temporary_worktree,
            )
            self.assertEqual(
                json.loads(begun.stdout)["result"]["repositories"]["service-a"]["rotation"]["phase"],
                "planned",
            )
            self.assertEqual(
                json.loads(begun.stdout)["result"]["repositories"]["service-a"]["rotation"]["temporary_worktree"],
                temporary_worktree,
            )
            self.assertEqual(
                json.loads(begun.stdout)["result"]["repositories"]["service-a"]["rotation"]["transfer_subject"],
                transfer_subject,
            )
            self.run_manifest(
                root,
                "advance-rotation",
                "--slot", "slot-1",
                "--token", "submit-a",
                "--repository", "service-a",
                "--expected-phase", "planned",
                "--target-phase", "branch-created",
                "--target-head-sha", result_tree,
            )
            self.run_manifest(
                root,
                "advance-rotation",
                "--slot", "slot-1",
                "--token", "submit-a",
                "--repository", "service-a",
                "--expected-phase", "branch-created",
                "--target-phase", "switched",
            )
            bypass = self.run_manifest(
                root,
                "advance-rotation",
                "--slot", "slot-1",
                "--token", "submit-a",
                "--repository", "service-a",
                "--expected-phase", "switched",
                "--target-phase", "retired",
                check=False,
            )
            self.assertEqual(bypass.returncode, 2)
            self.assertIn("invalid rotation phase transition", bypass.stderr)
            completed = self.run_manifest(
                root,
                "complete-rotation",
                "--slot", "slot-1",
                "--token", "submit-a",
                "--repository", "service-a",
                "--target-branch", target,
                "--target-generation", "2",
                "--source-sha", next_source,
                "--target-head-sha", result_tree,
                "--result-tree-sha", result_tree,
                check=False,
            )
            self.assertEqual(completed.returncode, 2)
            self.assertIn("has not retired", completed.stderr)
            identity = json.loads(
                self.run_manifest(root, "status", "--slot", "slot-1").stdout
            )["result"]["repositories"]["service-a"]
            self.assertEqual(identity["rotation"]["phase"], "switched")

    def test_schema_v3_requires_exact_migration_before_generation_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / ".stageflow-worktrees"
            state.mkdir()
            manifest = {
                "schema_version": 3,
                "slots": {
                    "slot-1": {
                        "path": str((root / "worktrees" / "slot-1").resolve()),
                        "repositories": {
                            "service-a": {
                                "branch": "task-a",
                                "source_branch": "main",
                                "remote": "origin",
                                "generation": 1,
                                "pr": "17",
                            }
                        },
                    }
                },
            }
            (state / "slots.json").write_text(json.dumps(manifest), encoding="utf-8")
            status = json.loads(self.run_manifest(root, "status", "--slot", "slot-1").stdout)["result"]
            self.assertEqual(status["repositories"]["service-a"]["legacy_schema"], 3)
            self.run_manifest(root, "lock", "--slot", "slot-1", "--token", "submit-a")
            migrated = self.run_manifest(
                root,
                "migrate-batch",
                "--slot", "slot-1",
                "--token", "submit-a",
                "--repository-migration",
                "service-a", "1", "17", "task-a", "1", "1" * 40,
                "task-a", "2" * 40, "2" * 40,
            )
            identity = json.loads(migrated.stdout)["result"]["repositories"]["service-a"]
            self.assertNotIn("legacy_schema", identity)
            self.assertEqual(identity["branch_base_sha"], "1" * 40)
            self.assertEqual(identity["submission"]["observed_head_sha"], "2" * 40)

    def test_schema_v4_active_rotation_requires_lock_and_git_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / ".stageflow-worktrees"
            state.mkdir()
            temporary = generation_temporary_worktree(
                root / "service-a",
                "task-a",
                2,
                workspace_root=root,
                repository="service-a",
            )
            legacy_rotation = {
                "phase": "planned",
                "from_branch": "task-a",
                "from_branch_generation": 1,
                "from_head_sha": "3" * 40,
                "boundary_sha": "2" * 40,
                "source_sha": "4" * 40,
                "target_branch": "task-a-stageflow-g2",
                "target_branch_generation": 2,
                "result_tree_sha": "6" * 40,
                "temporary_worktree": str(temporary),
            }
            manifest = {
                "schema_version": 4,
                "slots": {
                    "slot-1": {
                        "path": str((root / "worktrees" / "slot-1").resolve()),
                        "repositories": {
                            "service-a": {
                                "branch_family": "task-a",
                                "branch": "task-a",
                                "branch_generation": 1,
                                "branch_base_sha": "1" * 40,
                                "source_branch": "main",
                                "remote": "origin",
                                "generation": 1,
                                "pr": "17",
                                "submission": {
                                    "generation": 1,
                                    "head_branch": "task-a",
                                    "continuation_boundary_sha": "2" * 40,
                                    "observed_head_sha": "2" * 40,
                                },
                                "rotation": legacy_rotation,
                            }
                        },
                    }
                },
            }
            manifest_path = state / "slots.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            status = json.loads(self.run_manifest(root, "status").stdout)["result"]
            promoted = status["slots"]["slot-1"]["repositories"]["service-a"]
            self.assertEqual(status["schema_version"], 5)
            self.assertEqual(promoted["legacy_schema"], 4)
            self.assertNotIn("source_tree_sha", promoted["rotation"])
            self.assertEqual(json.loads(manifest_path.read_text())["schema_version"], 4)

            arguments = (
                "migrate-rotation-subject",
                "--slot", "slot-1",
                "--token", "rotate-a",
                "--repository-subject",
                "service-a", "task-a", "1", "3" * 40, "2" * 40, "4" * 40,
                "5" * 40, "task-a-stageflow-g2", "2", "6" * 40,
                "  feat(events): 이벤트 필터 상태 보존  ", str(temporary),
            )
            unlocked = self.run_manifest(root, *arguments, check=False)
            self.assertEqual(unlocked.returncode, 2)
            self.assertIn("not locked", unlocked.stderr)
            self.assertEqual(json.loads(manifest_path.read_text())["schema_version"], 4)

            self.run_manifest(root, "lock", "--slot", "slot-1", "--token", "rotate-a")
            missing_git = self.run_manifest(root, *arguments, check=False)
            self.assertEqual(missing_git.returncode, 2)
            self.assertIn("rotation Git evidence is invalid", missing_git.stderr)
            self.assertEqual(json.loads(manifest_path.read_text())["schema_version"], 4)

    def test_rotation_subject_and_empty_tree_invariants_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.run_manifest(
                root,
                "initialize",
                "--slot", "slot-1",
                "--path", str(root / "worktrees" / "slot-1"),
                "--repository", "service-a", "task-a", "main", "origin", "1" * 40,
            )
            self.run_manifest(root, "lock", "--slot", "slot-1", "--token", "rotate-a")
            temporary = generation_temporary_worktree(
                root / "service-a",
                "task-a",
                2,
                workspace_root=root,
                repository="service-a",
            )
            common = [
                "begin-rotation",
                "--slot", "slot-1",
                "--token", "rotate-a",
                "--repository-rotation",
                "service-a", "task-a", "1", "3" * 40, "2" * 40, "4" * 40,
            ]
            non_empty_without_subject = self.run_manifest(
                root,
                *common,
                "5" * 40, "task-a-stageflow-g2", "2", "6" * 40, "-", str(temporary),
                check=False,
            )
            self.assertEqual(non_empty_without_subject.returncode, 2)
            self.assertIn("non-empty rotation requires a subject", non_empty_without_subject.stderr)

            empty_with_subject = self.run_manifest(
                root,
                *common,
                "6" * 40, "task-a-stageflow-g2", "2", "6" * 40,
                "feat: 없어야 하는 메시지", str(temporary),
                check=False,
            )
            self.assertEqual(empty_with_subject.returncode, 2)
            self.assertIn("empty rotation must not have a subject", empty_with_subject.stderr)

            for token in ("<source-branch>", "<branch-family>", "<target>", "<repo>"):
                with self.subTest(token=token):
                    placeholder = self.run_manifest(
                        root,
                        *common,
                        "5" * 40, "task-a-stageflow-g2", "2", "6" * 40,
                        f"후속 {token} 이동", str(temporary),
                        check=False,
                    )
                    self.assertEqual(placeholder.returncode, 2)
                    self.assertIn("unresolved workflow placeholder", placeholder.stderr)
            accepted_english = json.loads(self.run_manifest(
                root,
                *common,
                "5" * 40, "task-a-stageflow-g2", "2", "6" * 40,
                "feat(events): preserve filter state", str(temporary),
            ).stdout)["result"]
            self.assertEqual(
                accepted_english["repositories"]["service-a"]["rotation"]["transfer_subject"],
                "feat(events): preserve filter state",
            )

    def test_schema_v4_new_slot_initialize_preserves_existing_legacy_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / ".stageflow-worktrees"
            state.mkdir()
            existing_identity = {
                "branch_family": "task-a",
                "branch": "task-a",
                "branch_generation": 1,
                "branch_base_sha": "1" * 40,
                "source_branch": "main",
                "remote": "origin",
                "generation": 0,
                "pr": None,
                "submission": None,
            }
            (state / "slots.json").write_text(
                json.dumps(
                    {
                        "schema_version": 4,
                        "slots": {
                            "slot-1": {
                                "path": str((root / "worktrees" / "slot-1").resolve()),
                                "repositories": {"service-a": existing_identity},
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            initialized = json.loads(self.run_manifest(
                root,
                "initialize",
                "--slot", "slot-2",
                "--path", str(root / "worktrees" / "slot-2"),
                "--repository", "service-b", "task-b", "main", "origin", "2" * 40,
            ).stdout)["result"]
            self.assertNotIn("legacy_schema", initialized["repositories"]["service-b"])
            written = json.loads((state / "slots.json").read_text(encoding="utf-8"))
            self.assertEqual(written["schema_version"], 5)
            self.assertEqual(
                written["slots"]["slot-1"]["repositories"]["service-a"]["legacy_schema"],
                4,
            )
            self.assertEqual(
                {
                    key: value
                    for key, value in written["slots"]["slot-1"]["repositories"]["service-a"].items()
                    if key != "legacy_schema"
                },
                existing_identity,
            )

    def test_schema_v3_migration_upgrades_legacy_reconciliation_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / ".stageflow-worktrees"
            state.mkdir()
            manifest = {
                "schema_version": 3,
                "slots": {
                    "slot-1": {
                        "path": str((root / "worktrees" / "slot-1").resolve()),
                        "repositories": {
                            "service-a": {
                                "branch": "task-a",
                                "source_branch": "main",
                                "remote": "origin",
                                "generation": 2,
                                "pr": "19",
                                "last_reconciliation": {
                                    "from_generation": 1,
                                    "from_pr": "17",
                                    "to_generation": 2,
                                    "to_pr": "19",
                                },
                            }
                        },
                    }
                },
            }
            (state / "slots.json").write_text(json.dumps(manifest), encoding="utf-8")
            self.run_manifest(root, "lock", "--slot", "slot-1", "--token", "migrate-a")
            migrated = self.run_manifest(
                root,
                "migrate-batch",
                "--slot", "slot-1",
                "--token", "migrate-a",
                "--repository-migration",
                "service-a", "2", "19", "task-a", "1", OID_1,
                "task-a", OID_2, OID_3,
            )
            identity = json.loads(migrated.stdout)["result"]["repositories"]["service-a"]
            self.assertNotIn("legacy_schema", identity)
            self.assertEqual(
                identity["last_reconciliation"],
                {
                    "from_generation": 1,
                    "from_pr": "17",
                    "to_generation": 2,
                    "to_pr": "19",
                    "head_branch": "task-a",
                    "continuation_boundary_sha": OID_2,
                    "observed_head_sha": OID_3,
                },
            )

    def test_reconciliation_records_full_recovered_submission_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.run_manifest(
                root,
                "initialize",
                "--slot", "slot-1",
                "--path", str(root / "worktrees" / "slot-1"),
                "--repository", "service-a", "task-a", "main", "origin", "1" * 40,
            )
            self.run_manifest(root, "lock", "--slot", "slot-1", "--token", "submit-a")
            self.run_manifest(
                root,
                "record-batch",
                "--slot", "slot-1",
                "--token", "submit-a",
                "--repository-update", "service-a", "0", "17", "task-a", "2" * 40, "2" * 40,
            )
            recovered = self.run_manifest(
                root,
                "reconcile-batch",
                "--slot", "slot-1",
                "--token", "submit-a",
                "--repository-recovery", "service-a", "1", "17", "19",
                "task-a-stageflow-g2", "3" * 40, "4" * 40,
            )
            identity = json.loads(recovered.stdout)["result"]["repositories"]["service-a"]
            self.assertEqual(identity["generation"], 2)
            self.assertEqual(identity["pr"], "19")
            self.assertEqual(
                identity["submission"],
                {
                    "generation": 2,
                    "head_branch": "task-a-stageflow-g2",
                    "continuation_boundary_sha": "3" * 40,
                    "observed_head_sha": "4" * 40,
                },
            )
            self.assertEqual(identity["last_reconciliation"]["observed_head_sha"], "4" * 40)
            retried = self.run_manifest(
                root,
                "reconcile-batch",
                "--slot", "slot-1",
                "--token", "submit-a",
                "--repository-recovery", "service-a", "1", "17", "19",
                "task-a-stageflow-g2", "3" * 40, "4" * 40,
            )
            self.assertEqual(json.loads(retried.stdout)["result"]["repositories"]["service-a"], identity)

@unittest.skipIf(shutil.which("git") is None, "git is required for generation branch tests")
class GenerationBranchPreparationTests(unittest.TestCase):
    def git(self, path: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(path), *arguments],
            check=check,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def prepare_repo(self, root: Path) -> Path:
        repo = root / "repo"
        subprocess.run(
            ["git", "init", "-b", "main", str(repo)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.git(repo, "config", "user.name", "Stageflow Test")
        self.git(repo, "config", "user.email", "stageflow@example.invalid")
        (repo / "base.txt").write_text("base\n", encoding="utf-8")
        self.git(repo, "add", "base.txt")
        self.git(repo, "commit", "-m", "base")
        return repo

    def run_preparer(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        prepared_arguments = list(arguments)
        if (
            prepared_arguments[0] in {"create", "verify"}
            and "--source-tree" not in prepared_arguments
        ):
            repo = Path(prepared_arguments[prepared_arguments.index("--repo") + 1])
            source = prepared_arguments[prepared_arguments.index("--source") + 1]
            source_tree = self.git(repo, "rev-parse", f"{source}^{{tree}}").stdout.strip()
            prepared_arguments.extend(["--source-tree", source_tree])
        if prepared_arguments[0] == "create" and "--workspace-root" not in prepared_arguments:
            repo = Path(prepared_arguments[prepared_arguments.index("--repo") + 1])
            prepared_arguments.extend(
                ["--workspace-root", str(repo.parent), "--slot", "slot-1", "--repository", repo.name]
            )
        return subprocess.run(
            [sys.executable, str(GENERATION_PREPARER), *prepared_arguments],
            check=check,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def run_manifest(self, root: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SLOT_MANIFEST), "--root", str(root), *arguments],
            check=check,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def run_retirer(self, root: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(GENERATION_RETIRER), "--root", str(root), *arguments],
            check=check,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def rotate(
        self,
        repo: Path,
        *,
        from_head: str,
        boundary: str,
        source: str,
        family: str = "task",
        generation: int = 2,
    ) -> tuple[dict[str, object], str]:
        analyzed = self.run_preparer(
            "analyze",
            "--repo", str(repo),
            "--from-head", from_head,
            "--boundary", boundary,
            "--source", source,
            "--branch-family", family,
            "--target-generation", str(generation),
        )
        plan = json.loads(analyzed.stdout)["result"]
        create_arguments = [
            "create",
            "--repo", str(repo),
            "--source", source,
            "--result-tree", str(plan["result_tree_sha"]),
            "--branch-family", family,
            "--target-generation", str(generation),
            "--temporary-worktree", str(generation_temporary_worktree(repo, family, generation)),
        ]
        if plan["result_tree_sha"] != plan["source_tree_sha"]:
            create_arguments.extend(["--message", "feat: 후속 작업 결과 반영"])
        created = self.run_preparer(*create_arguments)
        return plan, json.loads(created.stdout)["result"]["target_sha"]

    def test_squash_merge_moves_only_next_work_to_source_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.prepare_repo(Path(tmp))
            self.git(repo, "switch", "-c", "task")
            (repo / "a.txt").write_text("work A\n", encoding="utf-8")
            self.git(repo, "add", "a.txt")
            self.git(repo, "commit", "-m", "작업 A")
            boundary = self.git(repo, "rev-parse", "HEAD").stdout.strip()

            self.git(repo, "switch", "main")
            (repo / "a.txt").write_text("work A\n", encoding="utf-8")
            (repo / "source.txt").write_text("source advance\n", encoding="utf-8")
            self.git(repo, "add", "a.txt", "source.txt")
            self.git(repo, "commit", "-m", "작업 A 스쿼시 및 기준 변경")
            source = self.git(repo, "rev-parse", "HEAD").stdout.strip()

            self.git(repo, "switch", "task")
            (repo / "b.txt").write_text("work B\n", encoding="utf-8")
            self.git(repo, "add", "b.txt")
            self.git(repo, "commit", "-m", "작업 B")
            local_head = self.git(repo, "rev-parse", "HEAD").stdout.strip()

            analyzed = self.run_preparer(
                "analyze",
                "--repo", str(repo),
                "--from-head", local_head,
                "--boundary", boundary,
                "--source", source,
                "--branch-family", "task",
                "--target-generation", "2",
            )
            plan = json.loads(analyzed.stdout)["result"]
            created = self.run_preparer(
                "create",
                "--repo", str(repo),
                "--source", source,
                "--result-tree", plan["result_tree_sha"],
                "--branch-family", "task",
                "--target-generation", "2",
                "--message", "작업 B를 새 기준으로 이전",
                "--temporary-worktree", str(generation_temporary_worktree(repo, "task", 2)),
            )
            target_sha = json.loads(created.stdout)["result"]["target_sha"]
            repeated = self.run_preparer(
                "create",
                "--repo", str(repo),
                "--source", source,
                "--result-tree", plan["result_tree_sha"],
                "--branch-family", "task",
                "--target-generation", "2",
                "--message", "작업 B를 새 기준으로 이전",
                "--temporary-worktree", str(generation_temporary_worktree(repo, "task", 2)),
            )
            self.assertFalse(json.loads(repeated.stdout)["result"]["created"])
            self.assertEqual(self.git(repo, "show", "-s", "--format=%P", target_sha).stdout.strip(), source)
            self.assertEqual(
                self.git(repo, "diff", "--name-only", f"{source}...{target_sha}").stdout.splitlines(),
                ["b.txt"],
            )
            self.assertEqual(self.git(repo, "show", f"{target_sha}:source.txt").stdout, "source advance\n")
            self.assertEqual(
                self.git(repo, "show", "-s", "--format=%s", target_sha).stdout.strip(),
                "작업 B를 새 기준으로 이전",
            )

    def test_conflict_does_not_create_target_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.prepare_repo(Path(tmp))
            self.git(repo, "switch", "-c", "task")
            boundary = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            (repo / "base.txt").write_text("local work\n", encoding="utf-8")
            self.git(repo, "add", "base.txt")
            self.git(repo, "commit", "-m", "local")
            local_head = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            self.git(repo, "switch", "main")
            (repo / "base.txt").write_text("source work\n", encoding="utf-8")
            self.git(repo, "add", "base.txt")
            self.git(repo, "commit", "-m", "source")
            source = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            self.git(repo, "switch", "task")

            result = self.run_preparer(
                "analyze",
                "--repo", str(repo),
                "--from-head", local_head,
                "--boundary", boundary,
                "--source", source,
                "--branch-family", "task",
                "--target-generation", "2",
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("no branch was changed", result.stderr)
            self.assertNotEqual(
                self.git(repo, "rev-parse", "--verify", "refs/heads/task-stageflow-g2", check=False).returncode,
                0,
            )

    def test_non_ancestor_boundary_fails_before_target_creation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.prepare_repo(Path(tmp))
            base = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            self.git(repo, "switch", "-c", "abandoned")
            (repo / "abandoned.txt").write_text("not in task\n", encoding="utf-8")
            self.git(repo, "add", "abandoned.txt")
            self.git(repo, "commit", "-m", "abandoned")
            wrong_boundary = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            self.git(repo, "switch", "-c", "task", base)
            (repo / "b.txt").write_text("work B\n", encoding="utf-8")
            self.git(repo, "add", "b.txt")
            self.git(repo, "commit", "-m", "작업 B")
            local_head = self.git(repo, "rev-parse", "HEAD").stdout.strip()

            result = self.run_preparer(
                "analyze",
                "--repo", str(repo),
                "--from-head", local_head,
                "--boundary", wrong_boundary,
                "--source", base,
                "--branch-family", "task",
                "--target-generation", "2",
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("not an ancestor", result.stderr)
            self.assertNotEqual(
                self.git(repo, "rev-parse", "--verify", "refs/heads/task-stageflow-g2", check=False).returncode,
                0,
            )

    def test_empty_follow_up_points_generation_branch_directly_at_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.prepare_repo(Path(tmp))
            self.git(repo, "switch", "-c", "task")
            (repo / "a.txt").write_text("work A\n", encoding="utf-8")
            self.git(repo, "add", "a.txt")
            self.git(repo, "commit", "-m", "작업 A")
            boundary = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            self.git(repo, "switch", "main")
            (repo / "a.txt").write_text("work A\n", encoding="utf-8")
            self.git(repo, "add", "a.txt")
            self.git(repo, "commit", "-m", "작업 A 스쿼시")
            source = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            self.git(repo, "switch", "task")

            plan, target = self.rotate(
                repo,
                from_head=boundary,
                boundary=boundary,
                source=source,
            )
            self.assertEqual(plan["result_tree_sha"], self.git(repo, "rev-parse", f"{source}^{{tree}}").stdout.strip())
            self.assertEqual(target, source)
            self.assertEqual(self.git(repo, "rev-parse", "task-stageflow-g2").stdout.strip(), source)

    def test_transfer_subject_is_normalized_and_exact_retry_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.prepare_repo(Path(tmp))
            source = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            self.git(repo, "switch", "-c", "task")
            (repo / "events.txt").write_text("filter state\n", encoding="utf-8")
            self.git(repo, "add", "events.txt")
            self.git(repo, "commit", "-m", "작업 B")
            local_head = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            plan = json.loads(self.run_preparer(
                "analyze",
                "--repo", str(repo),
                "--from-head", local_head,
                "--boundary", source,
                "--source", source,
                "--branch-family", "task",
                "--target-generation", "2",
            ).stdout)["result"]
            self.assertEqual(
                plan["source_tree_sha"],
                self.git(repo, "rev-parse", f"{source}^{{tree}}").stdout.strip(),
            )
            wrong_source_tree = self.run_preparer(
                "create",
                "--repo", str(repo),
                "--source", source,
                "--source-tree", source,
                "--result-tree", str(plan["result_tree_sha"]),
                "--branch-family", "task",
                "--target-generation", "2",
                "--message", "feat(events): 이벤트 필터 상태 보존",
                "--temporary-worktree", str(generation_temporary_worktree(repo, "task", 2)),
                check=False,
            )
            self.assertEqual(wrong_source_tree.returncode, 2)
            self.assertIn("source tree does not match", wrong_source_tree.stderr)
            self.assertNotEqual(
                self.git(
                    repo,
                    "rev-parse",
                    "--verify",
                    "refs/heads/task-stageflow-g2",
                    check=False,
                ).returncode,
                0,
            )
            created = json.loads(self.run_preparer(
                "create",
                "--repo", str(repo),
                "--source", source,
                "--result-tree", str(plan["result_tree_sha"]),
                "--branch-family", "task",
                "--target-generation", "2",
                "--message", "  feat(events): 이벤트 필터 상태 보존  ",
                "--temporary-worktree", str(generation_temporary_worktree(repo, "task", 2)),
            ).stdout)["result"]
            self.assertEqual(created["transfer_subject"], "feat(events): 이벤트 필터 상태 보존")
            self.assertEqual(
                self.git(repo, "show", "-s", "--format=%s", created["target_sha"]).stdout.strip(),
                created["transfer_subject"],
            )

            wrong_subject = self.run_preparer(
                "verify",
                "--repo", str(repo),
                "--source", source,
                "--result-tree", str(plan["result_tree_sha"]),
                "--branch-family", "task",
                "--target-generation", "2",
                "--message", "fix(events): 다른 결과",
                check=False,
            )
            self.assertEqual(wrong_subject.returncode, 2)
            self.assertIn("does not match the journal", wrong_subject.stderr)
            verified = json.loads(self.run_preparer(
                "verify",
                "--repo", str(repo),
                "--source", source,
                "--result-tree", str(plan["result_tree_sha"]),
                "--branch-family", "task",
                "--target-generation", "2",
                "--message", "feat(events): 이벤트 필터 상태 보존",
            ).stdout)["result"]
            self.assertEqual(verified["target_sha"], created["target_sha"])

    def test_invalid_transfer_subjects_publish_no_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.prepare_repo(Path(tmp))
            source = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            self.git(repo, "switch", "-c", "task")
            (repo / "b.txt").write_text("work B\n", encoding="utf-8")
            self.git(repo, "add", "b.txt")
            self.git(repo, "commit", "-m", "작업 B")
            local_head = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            plan = json.loads(self.run_preparer(
                "analyze",
                "--repo", str(repo),
                "--from-head", local_head,
                "--boundary", source,
                "--source", source,
                "--branch-family", "task",
                "--target-generation", "2",
            ).stdout)["result"]
            for subject in (
                "   ",
                "feat: 첫 줄\n둘째 줄",
                "feat: 제어\x7f문자",
                "후속 작업을 최신 <source-branch> 기준으로 이전",
                "후속 <branch-family> 이동",
                "후속 <target> 이동",
                "후속 <repo> 이동",
            ):
                with self.subTest(subject=subject):
                    result = self.run_preparer(
                        "create",
                        "--repo", str(repo),
                        "--source", source,
                        "--result-tree", str(plan["result_tree_sha"]),
                        "--branch-family", "task",
                        "--target-generation", "2",
                        "--message", subject,
                        "--temporary-worktree", str(generation_temporary_worktree(repo, "task", 2)),
                        check=False,
                    )
                    self.assertEqual(result.returncode, 2)
                    self.assertNotEqual(
                        self.git(
                            repo,
                            "rev-parse",
                            "--verify",
                            "refs/heads/task-stageflow-g2",
                            check=False,
                        ).returncode,
                        0,
                    )

    def test_subject_mutating_hook_blocks_before_target_and_preserves_retry_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.prepare_repo(root)
            source = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            self.git(repo, "switch", "-c", "task")
            (repo / "b.txt").write_text("work B\n", encoding="utf-8")
            self.git(repo, "add", "b.txt")
            self.git(repo, "commit", "-m", "작업 B")
            local_head = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            plan = json.loads(self.run_preparer(
                "analyze",
                "--repo", str(repo),
                "--from-head", local_head,
                "--boundary", source,
                "--source", source,
                "--branch-family", "task",
                "--target-generation", "2",
            ).stdout)["result"]
            hooks = root / "hooks"
            hooks.mkdir()
            hook = hooks / "commit-msg"
            hook.write_text("#!/bin/sh\nprintf 'chore: hook changed subject\\n' > \"$1\"\n", encoding="utf-8")
            hook.chmod(0o755)
            self.git(repo, "config", "core.hooksPath", str(hooks))
            temporary = generation_temporary_worktree(repo, "task", 2)
            arguments = (
                "create",
                "--repo", str(repo),
                "--source", source,
                "--result-tree", str(plan["result_tree_sha"]),
                "--branch-family", "task",
                "--target-generation", "2",
                "--message", "feat: 작업 B 반영",
                "--temporary-worktree", str(temporary),
            )
            first = self.run_preparer(*arguments, check=False)
            self.assertEqual(first.returncode, 2)
            self.assertIn("expected 'feat: 작업 B 반영'", first.stderr)
            self.assertTrue(temporary.is_dir())
            self.assertEqual(
                self.git(temporary, "show", "-s", "--format=%s", "HEAD").stdout.strip(),
                "chore: hook changed subject",
            )
            self.assertNotEqual(
                self.git(
                    repo,
                    "rev-parse",
                    "--verify",
                    "refs/heads/task-stageflow-g2",
                    check=False,
                ).returncode,
                0,
            )
            retry = self.run_preparer(*arguments, check=False)
            self.assertEqual(retry.returncode, 2)
            self.assertTrue(temporary.is_dir())
            self.assertNotEqual(
                self.git(
                    repo,
                    "rev-parse",
                    "--verify",
                    "refs/heads/task-stageflow-g2",
                    check=False,
                ).returncode,
                0,
            )
            self.git(repo, "worktree", "remove", "--force", str(temporary))

    def test_delete_and_rename_are_transferred_to_source_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.prepare_repo(Path(tmp))
            (repo / "delete-me.txt").write_text("delete\n", encoding="utf-8")
            (repo / "rename-me.txt").write_text("rename\n", encoding="utf-8")
            self.git(repo, "add", "delete-me.txt", "rename-me.txt")
            self.git(repo, "commit", "-m", "transfer fixtures")
            boundary = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            self.git(repo, "switch", "-c", "task")
            (repo / "delete-me.txt").unlink()
            self.git(repo, "mv", "rename-me.txt", "renamed.txt")
            self.git(repo, "add", "delete-me.txt")
            self.git(repo, "commit", "-m", "삭제 및 이름 변경")
            local_head = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            self.git(repo, "switch", "main")
            (repo / "source.txt").write_text("source advance\n", encoding="utf-8")
            self.git(repo, "add", "source.txt")
            self.git(repo, "commit", "-m", "source advance")
            source = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            self.git(repo, "switch", "task")

            _, target = self.rotate(
                repo,
                from_head=local_head,
                boundary=boundary,
                source=source,
            )
            self.assertNotEqual(self.git(repo, "cat-file", "-e", f"{target}:delete-me.txt", check=False).returncode, 0)
            self.assertNotEqual(self.git(repo, "cat-file", "-e", f"{target}:rename-me.txt", check=False).returncode, 0)
            self.assertEqual(self.git(repo, "show", f"{target}:renamed.txt").stdout, "rename\n")
            self.assertEqual(self.git(repo, "show", f"{target}:source.txt").stdout, "source advance\n")

    def test_already_rotated_unsubmitted_work_survives_repeated_source_advance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.prepare_repo(Path(tmp))
            self.git(repo, "switch", "-c", "task")
            (repo / "a.txt").write_text("work A\n", encoding="utf-8")
            self.git(repo, "add", "a.txt")
            self.git(repo, "commit", "-m", "작업 A")
            submitted = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            (repo / "b.txt").write_text("work B\n", encoding="utf-8")
            self.git(repo, "add", "b.txt")
            self.git(repo, "commit", "-m", "작업 B")
            local_head = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            self.git(repo, "switch", "main")
            (repo / "a.txt").write_text("work A\n", encoding="utf-8")
            self.git(repo, "add", "a.txt")
            self.git(repo, "commit", "-m", "작업 A 스쿼시")
            source_one = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            self.git(repo, "switch", "task")
            _, generation_two = self.rotate(
                repo,
                from_head=local_head,
                boundary=submitted,
                source=source_one,
            )
            self.git(repo, "switch", "task-stageflow-g2")
            (repo / "c.txt").write_text("work C\n", encoding="utf-8")
            self.git(repo, "add", "c.txt")
            self.git(repo, "commit", "-m", "작업 C")
            generation_two_head = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            self.git(repo, "switch", "main")
            (repo / "source.txt").write_text("source two\n", encoding="utf-8")
            self.git(repo, "add", "source.txt")
            self.git(repo, "commit", "-m", "기준 추가 변경")
            source_two = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            self.git(repo, "switch", "task-stageflow-g2")
            _, generation_three = self.rotate(
                repo,
                from_head=generation_two_head,
                boundary=source_one,
                source=source_two,
                generation=3,
            )

            self.assertEqual(
                self.git(repo, "diff", "--name-only", f"{source_two}...{generation_three}").stdout.splitlines(),
                ["b.txt", "c.txt"],
            )
            self.assertEqual(self.git(repo, "show", "-s", "--format=%P", generation_two).stdout.strip(), source_one)
            self.assertEqual(self.git(repo, "show", "-s", "--format=%P", generation_three).stdout.strip(), source_two)

    def test_transfer_commit_runs_repository_commit_hook_before_ref_publication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.prepare_repo(root)
            boundary = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            self.git(repo, "switch", "-c", "task")
            (repo / "b.txt").write_text("work B\n", encoding="utf-8")
            self.git(repo, "add", "b.txt")
            self.git(repo, "commit", "-m", "작업 B")
            local_head = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            plan = json.loads(self.run_preparer(
                "analyze",
                "--repo", str(repo),
                "--from-head", local_head,
                "--boundary", boundary,
                "--source", boundary,
                "--branch-family", "task",
                "--target-generation", "2",
            ).stdout)["result"]
            hooks = root / "hooks"
            hooks.mkdir()
            marker = root / "commit-msg-ran"
            hook = hooks / "commit-msg"
            hook.write_text(f"#!/bin/sh\nprintf ran > '{marker}'\nexit 1\n", encoding="utf-8")
            hook.chmod(0o755)
            self.git(repo, "config", "core.hooksPath", str(hooks))

            result = self.run_preparer(
                "create",
                "--repo", str(repo),
                "--source", boundary,
                "--result-tree", str(plan["result_tree_sha"]),
                "--branch-family", "task",
                "--target-generation", "2",
                "--message", "후속 작업을 최신 기준으로 이전",
                "--temporary-worktree", str(generation_temporary_worktree(repo, "task", 2)),
                check=False,
            )
            error = json.loads(result.stderr)["error"]
            preserved = generation_temporary_worktree(repo, "task", 2)
            try:
                self.assertEqual(result.returncode, 2)
                self.assertTrue(marker.exists())
                self.assertIn("journaled temporary worktree", error)
                self.assertTrue(preserved.exists())
                self.assertNotEqual(
                    self.git(repo, "rev-parse", "--verify", "refs/heads/task-stageflow-g2", check=False).returncode,
                    0,
                )
            finally:
                self.git(repo, "worktree", "remove", "--force", str(preserved))
                preserved.parent.rmdir()

    def test_post_commit_ignored_file_is_preserved_before_target_publication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.prepare_repo(root)
            (repo / ".gitignore").write_text("ignored-secret.txt\n", encoding="utf-8")
            self.git(repo, "add", ".gitignore")
            self.git(repo, "commit", "-m", "ignore post-commit secret")
            source = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            self.git(repo, "switch", "-c", "task")
            (repo / "b.txt").write_text("work B\n", encoding="utf-8")
            self.git(repo, "add", "b.txt")
            self.git(repo, "commit", "-m", "작업 B")
            local_head = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            plan = json.loads(self.run_preparer(
                "analyze",
                "--repo", str(repo),
                "--from-head", local_head,
                "--boundary", source,
                "--source", source,
                "--branch-family", "task",
                "--target-generation", "2",
            ).stdout)["result"]
            hooks = root / "hooks"
            hooks.mkdir()
            hook = hooks / "post-commit"
            hook.write_text("#!/bin/sh\nprintf secret > ignored-secret.txt\n", encoding="utf-8")
            hook.chmod(0o755)
            self.git(repo, "config", "core.hooksPath", str(hooks))
            temporary = generation_temporary_worktree(repo, "task", 2)
            try:
                result = self.run_preparer(
                    "create",
                    "--repo", str(repo),
                    "--source", source,
                    "--result-tree", str(plan["result_tree_sha"]),
                    "--branch-family", "task",
                    "--target-generation", "2",
                    "--message", "후속 작업을 최신 기준으로 이전",
                    "--temporary-worktree", str(temporary),
                    check=False,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn("not clean after commit", result.stderr)
                self.assertEqual(
                    (temporary / "ignored-secret.txt").read_text(encoding="utf-8"),
                    "secret",
                )
                self.assertNotEqual(
                    self.git(repo, "rev-parse", "--verify", "refs/heads/task-stageflow-g2", check=False).returncode,
                    0,
                )
            finally:
                if temporary.exists():
                    self.git(repo, "worktree", "remove", "--force", str(temporary))

    def test_create_resumes_exact_journaled_temporary_worktree_crash_states(self) -> None:
        for crash_state in ("source", "result-staged", "result-committed"):
            with self.subTest(crash_state=crash_state), tempfile.TemporaryDirectory() as tmp:
                repo = self.prepare_repo(Path(tmp))
                source = self.git(repo, "rev-parse", "HEAD").stdout.strip()
                self.git(repo, "switch", "-c", "task")
                (repo / "b.txt").write_text("work B\n", encoding="utf-8")
                self.git(repo, "add", "b.txt")
                self.git(repo, "commit", "-m", "작업 B")
                local_head = self.git(repo, "rev-parse", "HEAD").stdout.strip()
                plan = json.loads(self.run_preparer(
                    "analyze",
                    "--repo", str(repo),
                    "--from-head", local_head,
                    "--boundary", source,
                    "--source", source,
                    "--branch-family", "task",
                    "--target-generation", "2",
                ).stdout)["result"]
                result_tree = str(plan["result_tree_sha"])
                temporary = generation_temporary_worktree(repo, "task", 2)
                temporary.parent.mkdir(parents=True)
                self.git(repo, "worktree", "add", "--detach", str(temporary), source)
                if crash_state != "source":
                    self.git(
                        temporary,
                        "restore",
                        "--source", result_tree,
                        "--staged",
                        "--worktree",
                        "--",
                        ":/",
                    )
                if crash_state == "result-committed":
                    self.git(temporary, "commit", "-m", "feat: 후속 작업 결과 반영")

                created = json.loads(self.run_preparer(
                    "create",
                    "--repo", str(repo),
                    "--source", source,
                    "--result-tree", result_tree,
                    "--branch-family", "task",
                    "--target-generation", "2",
                    "--message", "feat: 후속 작업 결과 반영",
                    "--temporary-worktree", str(temporary),
                ).stdout)["result"]
                target = str(created["target_sha"])

                self.assertFalse(temporary.exists())
                self.assertNotIn(
                    str(temporary),
                    self.git(repo, "worktree", "list", "--porcelain").stdout,
                )
                self.assertEqual(self.git(repo, "show", "-s", "--format=%P", target).stdout.strip(), source)
                self.assertEqual(self.git(repo, "rev-parse", f"{target}^{{tree}}").stdout.strip(), result_tree)

    def test_legacy_temporary_inspection_and_pre_target_discard_are_exact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.prepare_repo(root)
            source = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            self.git(repo, "switch", "-c", "task")
            (repo / "b.txt").write_text("work B\n", encoding="utf-8")
            self.git(repo, "add", "b.txt")
            self.git(repo, "commit", "-m", "작업 B")
            local_head = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            plan = json.loads(self.run_preparer(
                "analyze",
                "--repo", str(repo),
                "--from-head", local_head,
                "--boundary", source,
                "--source", source,
                "--branch-family", "task",
                "--target-generation", "2",
            ).stdout)["result"]
            result_tree = str(plan["result_tree_sha"])
            temporary = generation_temporary_worktree(repo, "task", 2)
            state_dir = root / ".stageflow-worktrees"
            state_dir.mkdir(exist_ok=True)
            manifest_path = state_dir / "slots.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": 4,
                        "slots": {
                            "slot-1": {
                                "path": str(root.resolve()),
                                "repositories": {
                                    "repo": {
                                        "branch_family": "task",
                                        "branch": "task",
                                        "branch_generation": 1,
                                        "branch_base_sha": source,
                                        "source_branch": "main",
                                        "remote": "origin",
                                        "generation": 1,
                                        "pr": "17",
                                        "submission": {
                                            "generation": 1,
                                            "head_branch": "task",
                                            "continuation_boundary_sha": source,
                                            "observed_head_sha": source,
                                        },
                                        "rotation": {
                                            "phase": "planned",
                                            "from_branch": "task",
                                            "from_branch_generation": 1,
                                            "from_head_sha": local_head,
                                            "boundary_sha": source,
                                            "source_sha": source,
                                            "target_branch": "task-stageflow-g2",
                                            "target_branch_generation": 2,
                                            "result_tree_sha": result_tree,
                                            "temporary_worktree": str(temporary),
                                        },
                                    }
                                },
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            def create_legacy_temporary() -> str:
                temporary.parent.mkdir(parents=True, exist_ok=True)
                self.git(repo, "worktree", "add", "--detach", str(temporary), source)
                self.git(
                    temporary,
                    "restore",
                    "--source", result_tree,
                    "--staged",
                    "--worktree",
                    "--",
                    ":/",
                )
                self.git(
                    temporary,
                    "commit",
                    "-m",
                    "후속 작업을 최신 <source-branch> 기준으로 이전",
                )
                return self.git(temporary, "rev-parse", "HEAD").stdout.strip()

            legacy_head = create_legacy_temporary()
            common = (
                "--repo", str(repo),
                "--source", source,
                "--result-tree", result_tree,
                "--branch-family", "task",
                "--target-generation", "2",
                "--temporary-worktree", str(temporary),
                "--workspace-root", str(root),
                "--slot", "slot-1",
                "--repository", "repo",
            )
            inspected = json.loads(
                self.run_preparer("inspect-legacy", *common).stdout
            )["result"]
            self.assertEqual(inspected["target_status"], "absent")
            self.assertEqual(inspected["temporary_status"], "completed")
            self.assertEqual(inspected["temporary_head_sha"], legacy_head)
            self.assertEqual(
                inspected["temporary_subject"],
                "후속 작업을 최신 <source-branch> 기준으로 이전",
            )

            no_lock = self.run_preparer(
                "discard-legacy-temporary",
                *common,
                "--expected-phase", "planned",
                "--expected-subject", "후속 작업을 최신 <source-branch> 기준으로 이전",
                "--token", "rotate-a",
                check=False,
            )
            self.assertEqual(no_lock.returncode, 2)
            self.assertIn("not locked", no_lock.stderr)
            self.assertTrue(temporary.is_dir())
            self.run_manifest(root, "lock", "--slot", "slot-1", "--token", "rotate-a")
            wrong_token = self.run_preparer(
                "discard-legacy-temporary",
                *common,
                "--expected-phase", "planned",
                "--expected-subject", "후속 작업을 최신 <source-branch> 기준으로 이전",
                "--token", "wrong",
                check=False,
            )
            self.assertEqual(wrong_token.returncode, 2)
            self.assertIn("token mismatch", wrong_token.stderr)
            self.assertTrue(temporary.is_dir())
            discarded = json.loads(self.run_preparer(
                "discard-legacy-temporary",
                *common,
                "--expected-phase", "planned",
                "--expected-subject", "후속 작업을 최신 <source-branch> 기준으로 이전",
                "--token", "rotate-a",
            ).stdout)["result"]
            self.assertTrue(discarded["discarded"])
            self.assertFalse(temporary.exists())

            legacy_head = create_legacy_temporary()
            persisted = json.loads(manifest_path.read_text(encoding="utf-8"))
            persisted["slots"]["slot-1"]["repositories"]["repo"]["rotation"]["phase"] = "branch-created"
            manifest_path.write_text(json.dumps(persisted), encoding="utf-8")
            non_planned = self.run_preparer(
                "discard-legacy-temporary",
                *common,
                "--expected-phase", "planned",
                "--expected-subject", "후속 작업을 최신 <source-branch> 기준으로 이전",
                "--token", "rotate-a",
                check=False,
            )
            self.assertEqual(non_planned.returncode, 2)
            self.assertIn("persisted rotation phase", non_planned.stderr)
            self.assertTrue(temporary.is_dir())
            persisted["slots"]["slot-1"]["repositories"]["repo"]["rotation"]["phase"] = "planned"
            manifest_path.write_text(json.dumps(persisted), encoding="utf-8")
            self.git(repo, "branch", "task-stageflow-g2", legacy_head)
            after_publication = self.run_preparer(
                "discard-legacy-temporary",
                *common,
                "--expected-phase", "planned",
                "--expected-subject", "후속 작업을 최신 <source-branch> 기준으로 이전",
                "--token", "rotate-a",
                check=False,
            )
            self.assertEqual(after_publication.returncode, 2)
            self.assertIn("after target publication", after_publication.stderr)
            self.assertTrue(temporary.is_dir())
            self.git(repo, "branch", "-D", "task-stageflow-g2")
            (temporary / "untracked.txt").write_text("preserve\n", encoding="utf-8")
            dirty = self.run_preparer(
                "discard-legacy-temporary",
                *common,
                "--expected-phase", "planned",
                "--expected-subject", "후속 작업을 최신 <source-branch> 기준으로 이전",
                "--token", "rotate-a",
                check=False,
            )
            self.assertEqual(dirty.returncode, 2)
            self.assertIn("additional changes", dirty.stderr)
            self.assertTrue((temporary / "untracked.txt").exists())
            self.git(repo, "worktree", "remove", "--force", str(temporary))

    def test_schema_v4_recovery_binds_manifest_lock_git_state_and_subject(self) -> None:
        states = (
            "absent",
            "source",
            "suitable-temp",
            "mechanical-temp",
            "suitable-target",
            "unsuitable-target",
            "switched-unsuitable",
        )
        for legacy_state in states:
            with self.subTest(legacy_state=legacy_state), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                repo = self.prepare_repo(root)
                source = self.git(repo, "rev-parse", "HEAD").stdout.strip()
                source_tree = self.git(repo, "rev-parse", f"{source}^{{tree}}").stdout.strip()
                self.git(repo, "switch", "-c", "task")
                (repo / "b.txt").write_text("work B\n", encoding="utf-8")
                self.git(repo, "add", "b.txt")
                self.git(repo, "commit", "-m", "작업 B")
                local_head = self.git(repo, "rev-parse", "HEAD").stdout.strip()
                plan = json.loads(self.run_preparer(
                    "analyze",
                    "--repo", str(repo),
                    "--from-head", local_head,
                    "--boundary", source,
                    "--source", source,
                    "--branch-family", "task",
                    "--target-generation", "2",
                ).stdout)["result"]
                result_tree = str(plan["result_tree_sha"])
                target = "task-stageflow-g2"
                temporary = generation_temporary_worktree(repo, "task", 2)
                desired_subject = "feat: 작업 B 반영"
                old_subject = "후속 작업을 최신 <source-branch> 기준으로 이전"

                def make_temporary(subject: str | None) -> str:
                    temporary.parent.mkdir(parents=True, exist_ok=True)
                    self.git(repo, "worktree", "add", "--detach", str(temporary), source)
                    if subject is None:
                        return source
                    self.git(
                        temporary,
                        "restore",
                        "--source", result_tree,
                        "--staged",
                        "--worktree",
                        "--",
                        ":/",
                    )
                    self.git(temporary, "commit", "-m", subject)
                    return self.git(temporary, "rev-parse", "HEAD").stdout.strip()

                phase = "planned"
                if legacy_state == "source":
                    make_temporary(None)
                elif legacy_state in {"suitable-temp", "mechanical-temp"}:
                    make_temporary(
                        desired_subject if legacy_state == "suitable-temp" else old_subject
                    )
                elif legacy_state in {
                    "suitable-target",
                    "unsuitable-target",
                    "switched-unsuitable",
                }:
                    transfer_head = make_temporary(
                        desired_subject if legacy_state == "suitable-target" else old_subject
                    )
                    self.git(repo, "worktree", "remove", str(temporary))
                    self.git(repo, "branch", target, transfer_head)
                    phase = (
                        "switched"
                        if legacy_state == "switched-unsuitable"
                        else "branch-created"
                    )
                    if phase == "switched":
                        self.git(repo, "switch", target)

                state_dir = root / ".stageflow-worktrees"
                state_dir.mkdir(exist_ok=True)
                manifest_path = state_dir / "slots.json"
                manifest_path.write_text(
                    json.dumps(
                        {
                            "schema_version": 4,
                            "slots": {
                                "slot-1": {
                                    "path": str(root.resolve()),
                                    "repositories": {
                                        "repo": {
                                            "branch_family": "task",
                                            "branch": "task",
                                            "branch_generation": 1,
                                            "branch_base_sha": source,
                                            "source_branch": "main",
                                            "remote": "origin",
                                            "generation": 1,
                                            "pr": "17",
                                            "submission": {
                                                "generation": 1,
                                                "head_branch": "task",
                                                "continuation_boundary_sha": source,
                                                "observed_head_sha": source,
                                            },
                                            "rotation": {
                                                "phase": phase,
                                                "from_branch": "task",
                                                "from_branch_generation": 1,
                                                "from_head_sha": local_head,
                                                "boundary_sha": source,
                                                "source_sha": source,
                                                "target_branch": target,
                                                "target_branch_generation": 2,
                                                "result_tree_sha": result_tree,
                                                "temporary_worktree": str(temporary),
                                            },
                                        }
                                    },
                                }
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                self.run_manifest(root, "lock", "--slot", "slot-1", "--token", "rotate-a")
                migration = (
                    "migrate-rotation-subject",
                    "--slot", "slot-1",
                    "--token", "rotate-a",
                    "--repository-subject",
                    "repo", "task", "1", local_head, source, source, source_tree,
                    target, "2", result_tree, desired_subject, str(temporary),
                )

                if legacy_state in {"unsuitable-target", "switched-unsuitable"}:
                    before = manifest_path.read_bytes()
                    rejected = self.run_manifest(root, *migration, check=False)
                    self.assertEqual(rejected.returncode, 2)
                    self.assertIn("published target subject is not suitable", rejected.stderr)
                    self.assertEqual(manifest_path.read_bytes(), before)
                    discard = self.run_preparer(
                        "discard-legacy-temporary",
                        "--repo", str(repo),
                        "--source", source,
                        "--result-tree", result_tree,
                        "--branch-family", "task",
                        "--target-generation", "2",
                        "--temporary-worktree", str(temporary),
                        "--workspace-root", str(root),
                        "--slot", "slot-1",
                        "--repository", "repo",
                        "--expected-phase", "planned",
                        "--expected-subject", old_subject,
                        "--token", "rotate-a",
                        check=False,
                    )
                    self.assertEqual(discard.returncode, 2)
                    self.assertEqual(
                        self.git(repo, "show", "-s", "--format=%s", target).stdout.strip(),
                        old_subject,
                    )
                    continue

                if legacy_state == "mechanical-temp":
                    rejected = self.run_manifest(root, *migration, check=False)
                    self.assertEqual(rejected.returncode, 2)
                    self.assertIn("requires safe recreation", rejected.stderr)
                    discarded = self.run_preparer(
                        "discard-legacy-temporary",
                        "--repo", str(repo),
                        "--source", source,
                        "--result-tree", result_tree,
                        "--branch-family", "task",
                        "--target-generation", "2",
                        "--temporary-worktree", str(temporary),
                        "--workspace-root", str(root),
                        "--slot", "slot-1",
                        "--repository", "repo",
                        "--expected-phase", "planned",
                        "--expected-subject", old_subject,
                        "--token", "rotate-a",
                    )
                    self.assertEqual(
                        json.loads(discarded.stdout)["result"]["temporary_status"],
                        "discarded",
                    )
                    self.assertFalse(temporary.exists())
                    self.assertEqual(
                        json.loads(manifest_path.read_text())["schema_version"],
                        4,
                    )

                migrated = json.loads(self.run_manifest(root, *migration).stdout)["result"]
                identity = migrated["repositories"]["repo"]
                self.assertNotIn("legacy_schema", identity)
                self.assertEqual(identity["rotation"]["source_tree_sha"], source_tree)
                self.assertEqual(identity["rotation"]["transfer_subject"], desired_subject)

                if legacy_state == "suitable-target":
                    verified = self.run_preparer(
                        "verify",
                        "--repo", str(repo),
                        "--source", source,
                        "--source-tree", source_tree,
                        "--result-tree", result_tree,
                        "--branch-family", "task",
                        "--target-generation", "2",
                        "--message", desired_subject,
                    )
                    target_head = json.loads(verified.stdout)["result"]["target_sha"]
                else:
                    created = self.run_preparer(
                        "create",
                        "--repo", str(repo),
                        "--source", source,
                        "--source-tree", source_tree,
                        "--result-tree", result_tree,
                        "--branch-family", "task",
                        "--target-generation", "2",
                        "--message", desired_subject,
                        "--temporary-worktree", str(temporary),
                    )
                    target_head = json.loads(created.stdout)["result"]["target_sha"]
                self.assertEqual(
                    self.git(repo, "show", "-s", "--format=%s", target_head).stdout.strip(),
                    desired_subject,
                )
                retried = json.loads(self.run_manifest(root, *migration).stdout)["result"]
                self.assertEqual(retried, migrated)

    def test_create_preserves_ignored_untracked_collision_in_journaled_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.prepare_repo(Path(tmp))
            (repo / ".gitignore").write_text("b.txt\n", encoding="utf-8")
            self.git(repo, "add", ".gitignore")
            self.git(repo, "commit", "-m", "ignore transfer path")
            source = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            self.git(repo, "switch", "-c", "task")
            (repo / "b.txt").write_text("work B\n", encoding="utf-8")
            self.git(repo, "add", "-f", "b.txt")
            self.git(repo, "commit", "-m", "작업 B")
            local_head = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            plan = json.loads(self.run_preparer(
                "analyze",
                "--repo", str(repo),
                "--from-head", local_head,
                "--boundary", source,
                "--source", source,
                "--branch-family", "task",
                "--target-generation", "2",
            ).stdout)["result"]
            temporary = generation_temporary_worktree(repo, "task", 2)
            temporary.parent.mkdir(parents=True)
            self.git(repo, "worktree", "add", "--detach", str(temporary), source)
            (temporary / "b.txt").write_text("ignored secret must survive\n", encoding="utf-8")
            try:
                result = self.run_preparer(
                    "create",
                    "--repo", str(repo),
                    "--source", source,
                    "--result-tree", str(plan["result_tree_sha"]),
                    "--branch-family", "task",
                    "--target-generation", "2",
                    "--message", "후속 작업을 최신 기준으로 이전",
                    "--temporary-worktree", str(temporary),
                    check=False,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn("untracked content", result.stderr)
                self.assertEqual(
                    (temporary / "b.txt").read_text(encoding="utf-8"),
                    "ignored secret must survive\n",
                )
                self.assertNotEqual(
                    self.git(repo, "rev-parse", "--verify", "refs/heads/task-stageflow-g2", check=False).returncode,
                    0,
                )
            finally:
                self.git(repo, "worktree", "remove", "--force", str(temporary))

    def test_create_rejects_original_worktree_as_temporary_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = self.prepare_repo(root)
            source = self.git(original, "rev-parse", "HEAD").stdout.strip()
            original_tree = self.git(original, "rev-parse", "HEAD^{tree}").stdout.strip()
            derived = root / "derived"
            self.git(original, "worktree", "add", "-b", "task", str(derived), source)
            (derived / "b.txt").write_text("work B\n", encoding="utf-8")
            self.git(derived, "add", "b.txt")
            self.git(derived, "commit", "-m", "작업 B")
            local_head = self.git(derived, "rev-parse", "HEAD").stdout.strip()
            plan = json.loads(self.run_preparer(
                "analyze",
                "--repo", str(derived),
                "--from-head", local_head,
                "--boundary", source,
                "--source", source,
                "--branch-family", "task",
                "--target-generation", "2",
            ).stdout)["result"]

            result = self.run_preparer(
                "create",
                "--repo", str(derived),
                "--source", source,
                "--result-tree", str(plan["result_tree_sha"]),
                "--branch-family", "task",
                "--target-generation", "2",
                "--message", "후속 작업을 최신 기준으로 이전",
                "--temporary-worktree", str(original),
                check=False,
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("does not match its Stageflow journal identity", result.stderr)
            self.assertEqual(self.git(original, "branch", "--show-current").stdout.strip(), "main")
            self.assertEqual(self.git(original, "rev-parse", "HEAD").stdout.strip(), source)
            self.assertEqual(self.git(original, "rev-parse", "HEAD^{tree}").stdout.strip(), original_tree)
            self.assertNotEqual(
                self.git(derived, "rev-parse", "--verify", "refs/heads/task-stageflow-g2", check=False).returncode,
                0,
            )

    def test_create_rejects_attached_branch_at_exact_internal_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.prepare_repo(Path(tmp))
            source = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            self.git(repo, "switch", "-c", "task")
            (repo / "b.txt").write_text("work B\n", encoding="utf-8")
            self.git(repo, "add", "b.txt")
            self.git(repo, "commit", "-m", "작업 B")
            local_head = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            plan = json.loads(self.run_preparer(
                "analyze",
                "--repo", str(repo),
                "--from-head", local_head,
                "--boundary", source,
                "--source", source,
                "--branch-family", "task",
                "--target-generation", "2",
            ).stdout)["result"]
            temporary = generation_temporary_worktree(repo, "task", 2)
            temporary.parent.mkdir(parents=True)
            self.git(repo, "worktree", "add", "-b", "unrelated-attached", str(temporary), source)
            try:
                result = self.run_preparer(
                    "create",
                    "--repo", str(repo),
                    "--source", source,
                    "--result-tree", str(plan["result_tree_sha"]),
                    "--branch-family", "task",
                    "--target-generation", "2",
                    "--message", "후속 작업을 최신 기준으로 이전",
                    "--temporary-worktree", str(temporary),
                    check=False,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn("not detached", result.stderr)
                self.assertEqual(self.git(temporary, "branch", "--show-current").stdout.strip(), "unrelated-attached")
                self.assertEqual(self.git(temporary, "rev-parse", "HEAD").stdout.strip(), source)
                self.assertNotEqual(
                    self.git(repo, "rev-parse", "--verify", "refs/heads/task-stageflow-g2", check=False).returncode,
                    0,
                )
            finally:
                self.git(repo, "worktree", "remove", str(temporary))
                self.git(repo, "branch", "-D", "unrelated-attached")

    def test_existing_target_with_different_tree_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.prepare_repo(Path(tmp))
            self.git(repo, "switch", "-c", "task")
            boundary = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            (repo / "b.txt").write_text("work B\n", encoding="utf-8")
            self.git(repo, "add", "b.txt")
            self.git(repo, "commit", "-m", "작업 B")
            local_head = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            self.git(repo, "branch", "task-stageflow-g2", boundary)
            existing = self.git(repo, "rev-parse", "task-stageflow-g2").stdout.strip()

            result = self.run_preparer(
                "analyze",
                "--repo", str(repo),
                "--from-head", local_head,
                "--boundary", boundary,
                "--source", boundary,
                "--branch-family", "task",
                "--target-generation", "2",
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("different tree", result.stderr)
            self.assertEqual(self.git(repo, "rev-parse", "task-stageflow-g2").stdout.strip(), existing)

    def test_verify_rejects_target_ref_moved_after_creation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.prepare_repo(Path(tmp))
            boundary = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            self.git(repo, "switch", "-c", "task")
            (repo / "b.txt").write_text("work B\n", encoding="utf-8")
            self.git(repo, "add", "b.txt")
            self.git(repo, "commit", "-m", "작업 B")
            local_head = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            plan, _ = self.rotate(
                repo,
                from_head=local_head,
                boundary=boundary,
                source=boundary,
            )
            self.git(repo, "branch", "-f", "task-stageflow-g2", boundary)

            verified = self.run_preparer(
                "verify",
                "--repo", str(repo),
                "--source", boundary,
                "--result-tree", str(plan["result_tree_sha"]),
                "--branch-family", "task",
                "--target-generation", "2",
                "--message", "feat: 후속 작업 결과 반영",
                check=False,
            )
            self.assertEqual(verified.returncode, 2)
            self.assertIn("does not match the journal", verified.stderr)
            self.assertEqual(self.git(repo, "rev-parse", "task-stageflow-g2").stdout.strip(), boundary)

    def test_legacy_source_merge_is_normalized_to_one_source_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.prepare_repo(Path(tmp))
            self.git(repo, "switch", "-c", "task")
            (repo / "a.txt").write_text("work A\n", encoding="utf-8")
            self.git(repo, "add", "a.txt")
            self.git(repo, "commit", "-m", "작업 A")
            boundary = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            self.git(repo, "switch", "main")
            (repo / "a.txt").write_text("work A\n", encoding="utf-8")
            (repo / "source.txt").write_text("source 1\n", encoding="utf-8")
            self.git(repo, "add", "a.txt", "source.txt")
            self.git(repo, "commit", "-m", "작업 A 스쿼시")
            source_one = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            self.git(repo, "switch", "task")
            (repo / "b.txt").write_text("work B\n", encoding="utf-8")
            self.git(repo, "add", "b.txt")
            self.git(repo, "commit", "-m", "작업 B")
            self.git(repo, "merge", source_one, "-m", "기존 기준 동기화 merge")
            self.git(repo, "switch", "main")
            (repo / "source.txt").write_text("source 2\n", encoding="utf-8")
            self.git(repo, "add", "source.txt")
            self.git(repo, "commit", "-m", "기준 추가 변경")
            source_two = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            self.git(repo, "switch", "task")
            local_head = self.git(repo, "rev-parse", "HEAD").stdout.strip()

            analyzed = self.run_preparer(
                "analyze",
                "--repo", str(repo),
                "--from-head", local_head,
                "--boundary", source_one,
                "--source", source_two,
                "--branch-family", "task",
                "--target-generation", "2",
            )
            result_tree = json.loads(analyzed.stdout)["result"]["result_tree_sha"]
            created = self.run_preparer(
                "create",
                "--repo", str(repo),
                "--source", source_two,
                "--result-tree", result_tree,
                "--branch-family", "task",
                "--target-generation", "2",
                "--message", "작업 B를 최신 기준으로 이전",
                "--temporary-worktree", str(generation_temporary_worktree(repo, "task", 2)),
            )
            target = json.loads(created.stdout)["result"]["target_sha"]
            self.assertEqual(self.git(repo, "show", "-s", "--format=%P", target).stdout.strip(), source_two)
            self.assertEqual(
                self.git(repo, "diff", "--name-only", f"{source_two}...{target}").stdout.splitlines(),
                ["b.txt"],
            )
            self.assertEqual(self.git(repo, "show", f"{target}:source.txt").stdout, "source 2\n")

    def test_isolated_correction_keeps_local_future_work_and_fast_forwards_remote_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            subprocess.run(
                ["git", "init", "--bare", str(remote)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            repo = self.prepare_repo(root)
            self.git(repo, "remote", "add", "origin", str(remote))
            self.git(repo, "push", "-u", "origin", "main")
            self.git(repo, "switch", "-c", "task")
            (repo / "a.txt").write_text("work A\n", encoding="utf-8")
            self.git(repo, "add", "a.txt")
            self.git(repo, "commit", "-m", "작업 A")
            submitted = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            self.git(repo, "push", "origin", "HEAD:refs/heads/task")
            (repo / "b.txt").write_text("work B\n", encoding="utf-8")
            self.git(repo, "add", "b.txt")
            self.git(repo, "commit", "-m", "작업 B")
            development_head = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            development_tree = self.git(repo, "rev-parse", "HEAD^{tree}").stdout.strip()

            correction = root / "correction"
            self.git(repo, "worktree", "add", "-b", "stageflow-correction", str(correction), submitted)
            (correction / "a.txt").write_text("work A fixed\n", encoding="utf-8")
            self.git(correction, "add", "a.txt")
            self.git(correction, "commit", "-m", "PR A 문제 수정")
            corrected = self.git(correction, "rev-parse", "HEAD").stdout.strip()
            self.git(correction, "push", "origin", "HEAD:refs/heads/task")

            remote_head = self.git(repo, "ls-remote", "--heads", "origin", "refs/heads/task").stdout.split()[0]
            self.assertEqual(remote_head, corrected)
            self.assertEqual(self.git(repo, "show", "-s", "--format=%P", corrected).stdout.strip(), submitted)
            self.assertEqual(self.git(repo, "rev-parse", "HEAD").stdout.strip(), development_head)
            self.assertEqual(self.git(repo, "rev-parse", "HEAD^{tree}").stdout.strip(), development_tree)
            self.assertEqual((repo / "b.txt").read_text(encoding="utf-8"), "work B\n")

            publisher = root / "publisher"
            subprocess.run(
                ["git", "clone", "--branch", "main", str(remote), str(publisher)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.git(publisher, "config", "user.name", "Stageflow Test")
            self.git(publisher, "config", "user.email", "stageflow@example.invalid")
            (publisher / "a.txt").write_text("work A fixed\n", encoding="utf-8")
            (publisher / "source.txt").write_text("squash source\n", encoding="utf-8")
            self.git(publisher, "add", "a.txt", "source.txt")
            self.git(publisher, "commit", "-m", "수정된 작업 A 스쿼시")
            self.git(publisher, "push", "origin", "main")
            self.git(repo, "fetch", "--prune", "origin")
            source = self.git(repo, "rev-parse", "refs/remotes/origin/main").stdout.strip()
            _, target = self.rotate(
                repo,
                from_head=development_head,
                boundary=submitted,
                source=source,
            )
            self.assertEqual(
                self.git(repo, "diff", "--name-only", f"{source}...{target}").stdout.splitlines(),
                ["b.txt"],
            )
            self.assertEqual(self.git(repo, "show", f"{target}:a.txt").stdout, "work A fixed\n")
            self.assertEqual(self.git(repo, "show", f"{target}:b.txt").stdout, "work B\n")

            self.git(repo, "worktree", "remove", str(correction))
            self.git(repo, "branch", "-D", "stageflow-correction")
            self.assertFalse(correction.exists())

    def test_generation_switch_in_linked_worktree_leaves_original_checkout_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            subprocess.run(
                ["git", "init", "--bare", str(remote)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            original = self.prepare_repo(root)
            self.git(original, "remote", "add", "origin", str(remote))
            self.git(original, "push", "-u", "origin", "main")
            original_head = self.git(original, "rev-parse", "HEAD").stdout.strip()
            derived = root / "worktrees" / "slot-1" / "service-a"
            derived.parent.mkdir(parents=True)
            self.git(original, "worktree", "add", "-b", "task", str(derived), "main")
            (derived / "a.txt").write_text("work A\n", encoding="utf-8")
            self.git(derived, "add", "a.txt")
            self.git(derived, "commit", "-m", "작업 A")
            boundary = self.git(derived, "rev-parse", "HEAD").stdout.strip()
            self.git(derived, "push", "origin", "HEAD:refs/heads/task")
            (derived / "b.txt").write_text("work B\n", encoding="utf-8")
            self.git(derived, "add", "b.txt")
            self.git(derived, "commit", "-m", "작업 B")
            local_head = self.git(derived, "rev-parse", "HEAD").stdout.strip()

            publisher = root / "publisher"
            subprocess.run(
                ["git", "clone", "--branch", "main", str(remote), str(publisher)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.git(publisher, "config", "user.name", "Stageflow Test")
            self.git(publisher, "config", "user.email", "stageflow@example.invalid")
            (publisher / "a.txt").write_text("work A\n", encoding="utf-8")
            (publisher / "source.txt").write_text("remote source\n", encoding="utf-8")
            self.git(publisher, "add", "a.txt", "source.txt")
            self.git(publisher, "commit", "-m", "작업 A 스쿼시")
            self.git(publisher, "push", "origin", "main")

            self.git(derived, "fetch", "--prune", "origin")
            source = self.git(derived, "rev-parse", "refs/remotes/origin/main").stdout.strip()
            analyzed = self.run_preparer(
                "analyze",
                "--repo", str(derived),
                "--from-head", local_head,
                "--boundary", boundary,
                "--source", source,
                "--branch-family", "task",
                "--target-generation", "2",
            )
            result_tree = json.loads(analyzed.stdout)["result"]["result_tree_sha"]
            self.run_preparer(
                "create",
                "--repo", str(derived),
                "--source", source,
                "--result-tree", result_tree,
                "--branch-family", "task",
                "--target-generation", "2",
                "--message", "작업 B를 최신 기준으로 이전",
                "--temporary-worktree", str(generation_temporary_worktree(derived, "task", 2)),
            )
            self.git(derived, "switch", "task-stageflow-g2")

            self.assertEqual(self.git(original, "branch", "--show-current").stdout.strip(), "main")
            self.assertEqual(self.git(original, "rev-parse", "HEAD").stdout.strip(), original_head)
            self.assertFalse((original / "a.txt").exists())
            self.assertFalse((original / "source.txt").exists())
            self.assertEqual(self.git(derived, "branch", "--show-current").stdout.strip(), "task-stageflow-g2")
            self.assertEqual(
                self.git(derived, "diff", "--name-only", f"{source}...HEAD").stdout.splitlines(),
                ["b.txt"],
            )

    def test_manifest_and_git_resume_after_switch_before_phase_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepared_repo = self.prepare_repo(root)
            slot_path = root / "worktrees" / "slot-1"
            slot_path.mkdir(parents=True)
            repo = slot_path / "service-a"
            prepared_repo.rename(repo)
            initial_source = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            self.git(repo, "switch", "-c", "task")
            (repo / "a.txt").write_text("work A\n", encoding="utf-8")
            self.git(repo, "add", "a.txt")
            self.git(repo, "commit", "-m", "작업 A")
            boundary = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            (repo / "b.txt").write_text("work B\n", encoding="utf-8")
            self.git(repo, "add", "b.txt")
            self.git(repo, "commit", "-m", "작업 B")
            local_head = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            self.git(repo, "switch", "main")
            (repo / "a.txt").write_text("work A\n", encoding="utf-8")
            (repo / "source.txt").write_text("source advance\n", encoding="utf-8")
            self.git(repo, "add", "a.txt", "source.txt")
            self.git(repo, "commit", "-m", "작업 A 스쿼시")
            source = self.git(repo, "rev-parse", "HEAD").stdout.strip()
            self.git(repo, "switch", "task")

            self.run_manifest(
                root,
                "initialize",
                "--slot", "slot-1",
                "--path", str(slot_path),
                "--repository", "service-a", "task", "main", "origin", initial_source,
            )
            self.run_manifest(root, "lock", "--slot", "slot-1", "--token", "sync-a")
            self.run_manifest(
                root,
                "record-batch",
                "--slot", "slot-1",
                "--token", "sync-a",
                "--repository-update", "service-a", "0", "17", "task", boundary, boundary,
            )
            plan = json.loads(self.run_preparer(
                "analyze",
                "--repo", str(repo),
                "--from-head", local_head,
                "--boundary", boundary,
                "--source", source,
                "--branch-family", "task",
                "--target-generation", "2",
            ).stdout)["result"]
            target = str(plan["target_branch"])
            result_tree = str(plan["result_tree_sha"])
            temporary_worktree = generation_temporary_worktree(
                repo,
                "task",
                2,
                workspace_root=root,
                repository="service-a",
            )
            self.run_manifest(
                root,
                "begin-rotation",
                "--slot", "slot-1",
                "--token", "sync-a",
                "--repository-rotation",
                "service-a", "task", "1", local_head, boundary, source,
                str(plan["source_tree_sha"]), target, "2", result_tree,
                "feat: 작업 B 반영",
                str(temporary_worktree),
            )
            created = json.loads(self.run_preparer(
                "create",
                "--repo", str(repo),
                "--source", source,
                "--result-tree", result_tree,
                "--branch-family", "task",
                "--target-generation", "2",
                "--message", "feat: 작업 B 반영",
                "--temporary-worktree", str(temporary_worktree),
                "--workspace-root", str(root),
                "--slot", "slot-1",
                "--repository", "service-a",
            ).stdout)["result"]
            target_head = str(created["target_sha"])
            self.run_manifest(
                root,
                "advance-rotation",
                "--slot", "slot-1",
                "--token", "sync-a",
                "--repository", "service-a",
                "--expected-phase", "planned",
                "--target-phase", "branch-created",
                "--target-head-sha", target_head,
            )

            self.git(repo, "switch", target)
            interrupted = json.loads(self.run_manifest(root, "status", "--slot", "slot-1").stdout)["result"]
            self.assertEqual(interrupted["repositories"]["service-a"]["rotation"]["phase"], "branch-created")
            self.assertEqual(self.git(repo, "branch", "--show-current").stdout.strip(), target)

            verified = json.loads(self.run_preparer(
                "verify",
                "--repo", str(repo),
                "--source", source,
                "--result-tree", result_tree,
                "--branch-family", "task",
                "--target-generation", "2",
                "--message", "feat: 작업 B 반영",
            ).stdout)["result"]
            self.assertEqual(verified["target_sha"], target_head)
            self.assertEqual(self.git(repo, "status", "--porcelain").stdout, "")
            self.run_manifest(
                root,
                "advance-rotation",
                "--slot", "slot-1",
                "--token", "sync-a",
                "--repository", "service-a",
                "--expected-phase", "branch-created",
                "--target-phase", "switched",
            )
            retired = json.loads(self.run_retirer(
                root,
                "--slot", "slot-1",
                "--token", "sync-a",
                "--repository", "service-a",
                "--execute",
            ).stdout)["result"]
            self.assertEqual(retired["status"], "deleted")
            completed = self.run_manifest(
                root,
                "complete-rotation",
                "--slot", "slot-1",
                "--token", "sync-a",
                "--repository", "service-a",
                "--target-branch", target,
                "--target-generation", "2",
                "--source-sha", source,
                "--target-head-sha", target_head,
                "--result-tree-sha", result_tree,
            )
            identity = json.loads(completed.stdout)["result"]["repositories"]["service-a"]
            self.assertNotIn("rotation", identity)
            self.assertEqual(identity["last_rotation"]["target_head_sha"], target_head)
            self.assertEqual(identity["last_rotation"]["result_tree_sha"], result_tree)
            retried = self.run_manifest(
                root,
                "complete-rotation",
                "--slot", "slot-1",
                "--token", "sync-a",
                "--repository", "service-a",
                "--target-branch", target,
                "--target-generation", "2",
                "--source-sha", source,
                "--target-head-sha", target_head,
                "--result-tree-sha", result_tree,
            )
            self.assertEqual(json.loads(retried.stdout)["result"]["repositories"]["service-a"], identity)


@unittest.skipIf(shutil.which("git") is None, "git is required for pull/sync flow tests")
class PullAndSyncFlowTests(unittest.TestCase):
    def git(self, path: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(path), *arguments],
            check=check,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def configure(self, path: Path) -> None:
        self.git(path, "config", "user.email", "test@example.com")
        self.git(path, "config", "user.name", "Test User")

    def run_preparer(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        prepared_arguments = list(arguments)
        if (
            prepared_arguments[0] in {"create", "verify"}
            and "--source-tree" not in prepared_arguments
        ):
            repo = Path(prepared_arguments[prepared_arguments.index("--repo") + 1])
            source = prepared_arguments[prepared_arguments.index("--source") + 1]
            source_tree = self.git(repo, "rev-parse", f"{source}^{{tree}}").stdout.strip()
            prepared_arguments.extend(["--source-tree", source_tree])
        if prepared_arguments[0] == "create" and "--workspace-root" not in prepared_arguments:
            repo = Path(prepared_arguments[prepared_arguments.index("--repo") + 1])
            prepared_arguments.extend(
                ["--workspace-root", str(repo.parent), "--slot", "slot-1", "--repository", repo.name]
            )
        return subprocess.run(
            [sys.executable, str(GENERATION_PREPARER), *prepared_arguments],
            check=check,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def run_manifest(self, root: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SLOT_MANIFEST), "--root", str(root), *arguments],
            check=check,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def run_cleaner(self, root: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(MERGED_BRANCH_CLEANER), "--root", str(root), *arguments],
            check=check,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def run_retirer(self, root: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(GENERATION_RETIRER), "--root", str(root), *arguments],
            check=check,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def make_remote_fixture(self, root: Path) -> tuple[Path, Path, Path]:
        remote = root / "remote.git"
        seed = root / "seed"
        original = root / "original"
        subprocess.run(
            ["git", "init", "--bare", str(remote)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        subprocess.run(["git", "clone", str(remote), str(seed)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.configure(seed)
        self.git(seed, "switch", "-c", "main")
        (seed / "tracked.txt").write_text("base\n", encoding="utf-8")
        self.git(seed, "add", "tracked.txt")
        self.git(seed, "commit", "-m", "base")
        self.git(seed, "push", "-u", "origin", "main")
        subprocess.run(["git", "clone", "--branch", "main", str(remote), str(original)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.configure(original)
        return remote, seed, original

    def advance_remote(self, seed: Path, content: str) -> str:
        (seed / "tracked.txt").write_text(content, encoding="utf-8")
        self.git(seed, "commit", "-am", content.strip())
        self.git(seed, "push", "origin", "main")
        return self.git(seed, "rev-parse", "HEAD").stdout.strip()

    def prepare_squash_merged_cleanup_fixture(
        self,
        root: Path,
        *,
        retire_and_complete: bool = True,
    ) -> dict[str, Any]:
        remote, seed, original = self.make_remote_fixture(root)
        slot_path = root / "worktrees" / "slot-1"
        slot_path.mkdir(parents=True)
        derived = slot_path / "service-a"
        self.git(original, "worktree", "add", "-b", "task", str(derived), "main")
        original_head = self.git(original, "rev-parse", "HEAD").stdout.strip()

        (derived / "a.txt").write_text("submitted A\n", encoding="utf-8")
        self.git(derived, "add", "a.txt")
        self.git(derived, "commit", "-m", "작업 A")
        submitted_head = self.git(derived, "rev-parse", "HEAD").stdout.strip()
        self.git(derived, "push", "origin", "task")

        (derived / "b.txt").write_text("continued B\n", encoding="utf-8")
        self.git(derived, "add", "b.txt")
        self.git(derived, "commit", "-m", "작업 B")
        local_head = self.git(derived, "rev-parse", "HEAD").stdout.strip()

        (seed / "a.txt").write_text("submitted A\n", encoding="utf-8")
        (seed / "source.txt").write_text("source after squash\n", encoding="utf-8")
        self.git(seed, "add", "a.txt", "source.txt")
        self.git(seed, "commit", "-m", "작업 A 스쿼시 병합")
        merge_commit = self.git(seed, "rev-parse", "HEAD").stdout.strip()
        self.git(seed, "push", "origin", "main")

        self.git(derived, "fetch", "--prune", "origin")
        source = self.git(derived, "rev-parse", "refs/remotes/origin/main").stdout.strip()
        self.assertEqual(source, merge_commit)
        self.assertNotEqual(
            self.git(
                derived,
                "merge-base",
                "--is-ancestor",
                submitted_head,
                source,
                check=False,
            ).returncode,
            0,
        )

        self.run_manifest(
            root,
            "initialize",
            "--slot", "slot-1",
            "--path", str(slot_path),
            "--repository", "service-a", "task", "main", "origin", original_head,
        )
        self.run_manifest(root, "lock", "--slot", "slot-1", "--token", "sync-a")
        self.run_manifest(
            root,
            "record-batch",
            "--slot", "slot-1",
            "--token", "sync-a",
            "--repository-update", "service-a", "0", "17", "task",
            submitted_head, submitted_head,
        )

        plan = json.loads(self.run_preparer(
            "analyze",
            "--repo", str(derived),
            "--from-head", local_head,
            "--boundary", submitted_head,
            "--source", source,
            "--branch-family", "task",
            "--target-generation", "2",
        ).stdout)["result"]
        temporary = generation_temporary_worktree(
            derived,
            "task",
            2,
            workspace_root=root,
            repository="service-a",
        )
        self.run_manifest(
            root,
            "begin-rotation",
            "--slot", "slot-1",
            "--token", "sync-a",
            "--repository-rotation",
            "service-a", "task", "1", local_head, submitted_head, source,
            str(plan["source_tree_sha"]), str(plan["target_branch"]), "2",
            str(plan["result_tree_sha"]), "feat: 작업 B 보존", str(temporary),
        )
        created = json.loads(self.run_preparer(
            "create",
            "--repo", str(derived),
            "--source", source,
            "--result-tree", str(plan["result_tree_sha"]),
            "--branch-family", "task",
            "--target-generation", "2",
            "--message", "feat: 작업 B 보존",
            "--temporary-worktree", str(temporary),
            "--workspace-root", str(root),
            "--slot", "slot-1",
            "--repository", "service-a",
        ).stdout)["result"]
        target = str(plan["target_branch"])
        target_head = str(created["target_sha"])
        self.run_manifest(
            root,
            "advance-rotation",
            "--slot", "slot-1",
            "--token", "sync-a",
            "--repository", "service-a",
            "--expected-phase", "planned",
            "--target-phase", "branch-created",
            "--target-head-sha", target_head,
        )
        self.git(derived, "switch", target)
        self.run_manifest(
            root,
            "advance-rotation",
            "--slot", "slot-1",
            "--token", "sync-a",
            "--repository", "service-a",
            "--expected-phase", "branch-created",
            "--target-phase", "switched",
        )
        fixture = {
            "remote": remote,
            "seed": seed,
            "original": original,
            "derived": derived,
            "original_head": original_head,
            "submitted_head": submitted_head,
            "local_head": local_head,
            "merge_commit": merge_commit,
            "source": source,
            "target": target,
            "target_head": target_head,
        }
        if not retire_and_complete:
            return fixture
        retired = json.loads(self.run_retirer(
            root,
            "--slot", "slot-1",
            "--repository", "service-a",
            "--token", "sync-a",
            "--execute",
        ).stdout)["result"]
        self.assertEqual(retired["status"], "deleted")
        retry_after_delete = json.loads(self.run_retirer(
            root,
            "--slot", "slot-1",
            "--repository", "service-a",
            "--token", "sync-a",
            "--execute",
        ).stdout)["result"]
        self.assertEqual(retry_after_delete["status"], "already-retired")
        self.assertNotEqual(
            self.git(derived, "show-ref", "--verify", "--quiet", "refs/heads/task", check=False).returncode,
            0,
        )
        retry_after_phase = json.loads(self.run_retirer(
            root,
            "--slot", "slot-1",
            "--repository", "service-a",
            "--token", "sync-a",
            "--execute",
        ).stdout)["result"]
        self.assertEqual(retry_after_phase["status"], "already-retired")
        self.run_manifest(
            root,
            "complete-rotation",
            "--slot", "slot-1",
            "--token", "sync-a",
            "--repository", "service-a",
            "--target-branch", target,
            "--target-generation", "2",
            "--source-sha", source,
            "--target-head-sha", target_head,
            "--result-tree-sha", str(plan["result_tree_sha"]),
        )
        return fixture

    def cleanup_arguments(self, fixture: dict[str, Any]) -> list[str]:
        return [
            "--slot", "slot-1",
            "--repository", "service-a",
            "--token", "sync-a",
            "--github-number", "17",
            "--github-url", "https://github.com/example/service-a/pull/17",
            "--github-state", "MERGED",
            "--github-is-draft", "false",
            "--github-base", "main",
            "--github-head", "task",
            "--github-head-sha", str(fixture["submitted_head"]),
            "--github-merged-at", "2026-08-07T00:00:00Z",
            "--github-merge-commit", str(fixture["merge_commit"]),
            "--execute",
        ]

    def test_sync_cleanup_deletes_squash_merged_local_and_remote_branches_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = self.prepare_squash_merged_cleanup_fixture(root)
            derived = fixture["derived"]
            original = fixture["original"]

            cleaned = self.run_cleaner(root, *self.cleanup_arguments(fixture))
            payload = json.loads(cleaned.stdout)["result"]
            self.assertEqual(payload["status"], "deleted")
            self.assertTrue(payload["local_deleted"])
            self.assertTrue(payload["remote_deleted"])
            self.assertNotEqual(
                self.git(derived, "rev-parse", "--verify", "refs/heads/task", check=False).returncode,
                0,
            )
            self.assertEqual(
                self.git(
                    derived,
                    "ls-remote",
                    "--heads",
                    "origin",
                    "refs/heads/task",
                ).stdout,
                "",
            )
            self.assertEqual(self.git(derived, "branch", "--show-current").stdout.strip(), fixture["target"])
            self.assertEqual(self.git(derived, "show", "HEAD:b.txt").stdout, "continued B\n")
            self.assertEqual(self.git(original, "rev-parse", "HEAD").stdout.strip(), fixture["original_head"])
            self.assertEqual(self.git(original, "branch", "--show-current").stdout.strip(), "main")

            retried = self.run_cleaner(root, *self.cleanup_arguments(fixture))
            self.assertEqual(json.loads(retried.stdout)["result"]["status"], "already-absent")

    def test_new_submission_preserves_old_remote_cleanup_evidence_until_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = self.prepare_squash_merged_cleanup_fixture(root)
            recorded = self.run_manifest(
                root,
                "record-batch",
                "--slot", "slot-1",
                "--token", "sync-a",
                "--repository-update",
                "service-a", "1", "18", fixture["target"],
                fixture["target_head"], fixture["target_head"],
            )
            identity = json.loads(recorded.stdout)["result"]["repositories"]["service-a"]
            self.assertEqual(identity["pr"], "18")
            self.assertEqual(identity["pending_remote_cleanups"][0]["pr"], "17")
            self.assertEqual(
                identity["pending_remote_cleanups"][0]["submission"]["head_branch"],
                "task",
            )

            cleaned = self.run_cleaner(root, *self.cleanup_arguments(fixture))
            self.assertTrue(json.loads(cleaned.stdout)["result"]["remote_deleted"])
            after = json.loads(
                self.run_manifest(root, "status", "--slot", "slot-1").stdout
            )["result"]["repositories"]["service-a"]
            self.assertEqual(after["pr"], "18")
            self.assertNotIn("pending_remote_cleanups", after)

    def test_completed_rotation_retry_rejects_moved_or_missing_target(self) -> None:
        for scenario in ("moved", "missing"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                fixture = self.prepare_squash_merged_cleanup_fixture(root)
                derived = fixture["derived"]
                if scenario == "moved":
                    tree = self.git(
                        derived,
                        "rev-parse",
                        f"{fixture['target_head']}^{{tree}}",
                    ).stdout.strip()
                    moved = self.git(
                        derived,
                        "commit-tree",
                        tree,
                        "-p",
                        fixture["source"],
                        "-m",
                        "moved completed target",
                    ).stdout.strip()
                    self.git(
                        derived,
                        "update-ref",
                        f"refs/heads/{fixture['target']}",
                        moved,
                        fixture["target_head"],
                    )
                else:
                    self.git(derived, "switch", "--detach", fixture["target_head"])
                    self.git(
                        derived,
                        "update-ref",
                        "-d",
                        f"refs/heads/{fixture['target']}",
                        fixture["target_head"],
                    )
                retried = self.run_manifest(
                    root,
                    "complete-rotation",
                    "--slot", "slot-1",
                    "--token", "sync-a",
                    "--repository", "service-a",
                    "--target-branch", fixture["target"],
                    "--target-generation", "2",
                    "--source-sha", fixture["source"],
                    "--target-head-sha", fixture["target_head"],
                    "--result-tree-sha", self.git(
                        derived,
                        "rev-parse",
                        f"{fixture['target_head']}^{{tree}}",
                    ).stdout.strip(),
                    check=False,
                )
                self.assertEqual(retried.returncode, 2)

    def test_generation_audit_is_read_only_and_reports_legacy_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = self.prepare_squash_merged_cleanup_fixture(root)
            derived = fixture["derived"]
            manifest = root / ".stageflow-worktrees" / "slots.json"
            self.git(
                derived,
                "update-ref",
                "refs/heads/task",
                fixture["local_head"],
                "0" * 40,
            )
            self.git(
                derived,
                "update-ref",
                "refs/heads/task-stageflow-g8",
                fixture["source"],
                "0" * 40,
            )
            ambiguous = self.git(
                derived,
                "commit-tree",
                self.git(
                    derived,
                    "rev-parse",
                    f"{fixture['local_head']}^{{tree}}",
                ).stdout.strip(),
                "-p",
                fixture["local_head"],
                "-m",
                "ambiguous continuation",
            ).stdout.strip()
            self.git(
                derived,
                "update-ref",
                "refs/heads/task-stageflow-g9",
                ambiguous,
                "0" * 40,
            )
            checked_worktree = root / "audit-checked-worktree"
            self.git(
                derived,
                "worktree",
                "add",
                "-b",
                "task-stageflow-g7",
                str(checked_worktree),
                fixture["source"],
            )
            manifest_before = manifest.read_bytes()
            refs_before = self.git(
                derived,
                "for-each-ref",
                "--format=%(refname) %(objectname)",
                "refs/heads/",
            ).stdout
            head_before = self.git(derived, "rev-parse", "HEAD").stdout
            status_before = self.git(derived, "status", "--porcelain", "--untracked-files=all").stdout
            worktrees_before = self.git(derived, "worktree", "list", "--porcelain").stdout

            audited = subprocess.run(
                [
                    sys.executable,
                    str(GENERATION_AUDITOR),
                    "--root",
                    str(root),
                    "--slot",
                    "slot-1",
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            payload = json.loads(audited.stdout)["result"]
            report = payload["repositories"][0]
            receipt = next(row for row in report["branches"] if row["branch"] == "task")
            checked = next(row for row in report["branches"] if row["branch"] == "task-stageflow-g7")
            reachable = next(row for row in report["branches"] if row["branch"] == "task-stageflow-g8")
            legacy = next(row for row in report["branches"] if row["branch"] == "task-stageflow-g9")
            self.assertTrue(payload["read_only"])
            self.assertEqual(receipt["classification"], "exact-journal-evidence")
            self.assertEqual(checked["classification"], "protected")
            self.assertIn("checked-out", checked["reasons"])
            self.assertEqual(reachable["classification"], "reachable-from-protected")
            self.assertFalse(legacy["active"])
            self.assertEqual(legacy["classification"], "unresolved")
            with_merged_evidence = subprocess.run(
                [
                    sys.executable,
                    str(GENERATION_AUDITOR),
                    "--root",
                    str(root),
                    "--slot",
                    "slot-1",
                    "--merged-pr-evidence",
                    "service-a",
                    "96",
                    "task-stageflow-g9",
                    ambiguous,
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            evidence_report = json.loads(with_merged_evidence.stdout)["result"]["repositories"][0]
            evidence_branch = next(
                row for row in evidence_report["branches"] if row["branch"] == "task-stageflow-g9"
            )
            self.assertEqual(evidence_branch["classification"], "exact-merged-pr-evidence")
            self.assertEqual(manifest.read_bytes(), manifest_before)
            self.assertEqual(
                self.git(
                    derived,
                    "for-each-ref",
                    "--format=%(refname) %(objectname)",
                    "refs/heads/",
                ).stdout,
                refs_before,
            )
            self.assertEqual(self.git(derived, "rev-parse", "HEAD").stdout, head_before)
            self.assertEqual(
                self.git(derived, "status", "--porcelain", "--untracked-files=all").stdout,
                status_before,
            )
            self.assertEqual(
                self.git(derived, "worktree", "list", "--porcelain").stdout,
                worktrees_before,
            )

    def test_generation_audit_uses_phase_aware_active_during_switched_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = self.prepare_squash_merged_cleanup_fixture(
                root,
                retire_and_complete=False,
            )
            audited = subprocess.run(
                [
                    sys.executable,
                    str(GENERATION_AUDITOR),
                    "--root",
                    str(root),
                    "--slot",
                    "slot-1",
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            report = json.loads(audited.stdout)["result"]["repositories"][0]
            self.assertEqual(report["effective_active_branch"], fixture["target"])
            self.assertEqual(report["effective_active_basis"], "rotation-target")
            old = next(row for row in report["branches"] if row["branch"] == "task")
            target = next(row for row in report["branches"] if row["branch"] == fixture["target"])
            self.assertEqual(old["classification"], "exact-journal-evidence")
            self.assertEqual(target["classification"], "protected")

    def test_generation_retirement_blocks_moved_old_ref_and_manifest_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = self.prepare_squash_merged_cleanup_fixture(
                root,
                retire_and_complete=False,
            )
            derived = fixture["derived"]
            old_head = str(fixture["local_head"])
            old_tree = self.git(derived, "rev-parse", f"{old_head}^{{tree}}").stdout.strip()
            advanced = self.git(
                derived,
                "commit-tree",
                old_tree,
                "-p",
                old_head,
                "-m",
                "foreign advancement",
            ).stdout.strip()
            self.git(derived, "update-ref", "refs/heads/task", advanced, old_head)

            blocked = self.run_retirer(
                root,
                "--slot", "slot-1",
                "--repository", "service-a",
                "--token", "sync-a",
                "--execute",
                check=False,
            )
            self.assertEqual(blocked.returncode, 2)
            self.assertIn("old branch moved", blocked.stderr)
            self.assertEqual(self.git(derived, "rev-parse", "refs/heads/task").stdout.strip(), advanced)
            self.assertEqual(
                self.git(derived, "rev-parse", f"refs/heads/{fixture['target']}").stdout.strip(),
                fixture["target_head"],
            )

            completion = self.run_manifest(
                root,
                "complete-rotation",
                "--slot", "slot-1",
                "--token", "sync-a",
                "--repository", "service-a",
                "--target-branch", fixture["target"],
                "--target-generation", "2",
                "--source-sha", fixture["source"],
                "--target-head-sha", fixture["target_head"],
                "--result-tree-sha", self.git(
                    derived,
                    "rev-parse",
                    f"{fixture['target_head']}^{{tree}}",
                ).stdout.strip(),
                check=False,
            )
            self.assertEqual(completion.returncode, 2)
            self.assertIn("has not retired", completion.stderr)
            status = json.loads(self.run_manifest(root, "status", "--slot", "slot-1").stdout)["result"]
            self.assertEqual(status["repositories"]["service-a"]["rotation"]["phase"], "switched")

    def test_generation_retirement_preserves_old_ref_on_target_or_checkout_blockers(self) -> None:
        for scenario in ("dirty-target", "moved-target", "checked-out-old", "symbolic-old"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                fixture = self.prepare_squash_merged_cleanup_fixture(
                    root,
                    retire_and_complete=False,
                )
                derived = fixture["derived"]
                protected_main_before = self.git(
                    derived,
                    "rev-parse",
                    "refs/heads/main",
                ).stdout.strip()
                if scenario == "dirty-target":
                    (derived / "unexpected.txt").write_text("dirty\n", encoding="utf-8")
                elif scenario == "moved-target":
                    target_tree = self.git(
                        derived,
                        "rev-parse",
                        f"{fixture['target_head']}^{{tree}}",
                    ).stdout.strip()
                    moved_target = self.git(
                        derived,
                        "commit-tree",
                        target_tree,
                        "-p",
                        fixture["source"],
                        "-m",
                        "feat: 작업 B 보존\n\nforeign replacement",
                    ).stdout.strip()
                    self.git(
                        derived,
                        "update-ref",
                        f"refs/heads/{fixture['target']}",
                        moved_target,
                        fixture["target_head"],
                    )
                elif scenario == "checked-out-old":
                    self.git(
                        derived,
                        "worktree",
                        "add",
                        str(root / "old-checkout"),
                        "task",
                    )
                else:
                    self.git(derived, "update-ref", "-d", "refs/heads/task", fixture["local_head"])
                    self.git(derived, "symbolic-ref", "refs/heads/task", "refs/heads/main")

                blocked = self.run_retirer(
                    root,
                    "--slot", "slot-1",
                    "--repository", "service-a",
                    "--token", "sync-a",
                    "--execute",
                    check=False,
                )
                self.assertEqual(blocked.returncode, 2)
                if scenario == "symbolic-old":
                    self.assertEqual(
                        self.git(derived, "symbolic-ref", "refs/heads/task").stdout.strip(),
                        "refs/heads/main",
                    )
                    self.assertEqual(
                        self.git(derived, "rev-parse", "refs/heads/main").stdout.strip(),
                        protected_main_before,
                    )
                else:
                    self.assertEqual(
                        self.git(derived, "rev-parse", "refs/heads/task").stdout.strip(),
                        fixture["local_head"],
                    )
                status = json.loads(
                    self.run_manifest(root, "status", "--slot", "slot-1").stdout
                )["result"]["repositories"]["service-a"]
                self.assertEqual(status["rotation"]["phase"], "switched")

    def test_merged_remote_cleanup_preserves_unproven_or_advanced_refs(self) -> None:
        for scenario in ("advanced", "not-merged", "missing-merge-commit"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                fixture = self.prepare_squash_merged_cleanup_fixture(root)
                derived = fixture["derived"]
                arguments = self.cleanup_arguments(fixture)
                expected_remote = fixture["submitted_head"]
                if scenario == "advanced":
                    submitted_tree = self.git(
                        derived,
                        "rev-parse",
                        f"{fixture['submitted_head']}^{{tree}}",
                    ).stdout.strip()
                    expected_remote = self.git(
                        derived,
                        "commit-tree",
                        submitted_tree,
                        "-p",
                        fixture["submitted_head"],
                        "-m",
                        "remote advanced",
                    ).stdout.strip()
                    self.git(derived, "push", "origin", f"{expected_remote}:refs/heads/task")
                elif scenario == "not-merged":
                    arguments[arguments.index("--github-state") + 1] = "OPEN"
                else:
                    arguments[arguments.index("--github-merge-commit") + 1] = OID_1

                blocked = self.run_cleaner(root, *arguments, check=False)
                self.assertEqual(blocked.returncode, 2)
                remote = self.git(
                    derived,
                    "ls-remote",
                    "--heads",
                    "origin",
                    "refs/heads/task",
                ).stdout.split()[0]
                self.assertEqual(remote, expected_remote)

    def test_sync_cleanup_preserves_refs_when_old_local_branch_has_new_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = self.prepare_squash_merged_cleanup_fixture(root)
            derived = fixture["derived"]
            old_head = str(fixture["local_head"])
            old_tree = self.git(derived, "rev-parse", f"{old_head}^{{tree}}").stdout.strip()
            advanced = self.git(
                derived,
                "commit-tree",
                old_tree,
                "-p",
                old_head,
                "-m",
                "old branch advanced",
            ).stdout.strip()
            self.git(derived, "update-ref", "refs/heads/task", old_head, "0" * 40)
            self.git(derived, "update-ref", "refs/heads/task", advanced, old_head)

            blocked = self.run_cleaner(
                root,
                *self.cleanup_arguments(fixture),
                check=False,
            )
            self.assertEqual(blocked.returncode, 2)
            self.assertIn("old local branch moved", blocked.stderr)
            self.assertEqual(
                self.git(derived, "rev-parse", "refs/heads/task").stdout.strip(),
                advanced,
            )
            self.assertEqual(
                self.git(
                    derived,
                    "ls-remote",
                    "--heads",
                    "origin",
                    "refs/heads/task",
                ).stdout.split()[0],
                fixture["submitted_head"],
            )

    def test_pull_fetch_then_fast_forwards_only_the_original(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, seed, original = self.make_remote_fixture(Path(tmp))
            remote_head = self.advance_remote(seed, "remote update\n")

            self.git(original, "fetch", "--prune", "origin")
            pinned = self.git(original, "rev-parse", "refs/remotes/origin/main").stdout.strip()
            self.assertEqual(pinned, remote_head)
            self.git(original, "merge", "--ff-only", pinned)

            self.assertEqual(self.git(original, "rev-parse", "HEAD").stdout.strip(), remote_head)
            self.assertEqual(self.git(original, "branch", "--show-current").stdout.strip(), "main")
            self.assertEqual(self.git(original, "status", "--porcelain").stdout, "")

    def test_pull_uses_the_manifest_remote_name_instead_of_literal_origin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, seed, original = self.make_remote_fixture(Path(tmp))
            self.git(original, "remote", "rename", "origin", "upstream")
            remote_head = self.advance_remote(seed, "remote update\n")

            self.git(original, "fetch", "--prune", "upstream")
            pinned = self.git(
                original, "rev-parse", "refs/remotes/upstream/main"
            ).stdout.strip()
            self.assertEqual(pinned, remote_head)
            self.git(original, "merge", "--ff-only", pinned)

            self.assertEqual(self.git(original, "rev-parse", "HEAD").stdout.strip(), remote_head)
            self.assertEqual(self.git(original, "status", "--porcelain").stdout, "")

    def test_sync_fetches_in_derived_worktree_without_updating_original_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, seed, original = self.make_remote_fixture(root)
            derived = root / "derived"
            self.git(original, "worktree", "add", "-b", "task", str(derived), "main")
            boundary = self.git(derived, "rev-parse", "HEAD").stdout.strip()
            (derived / "local.txt").write_text("unsubmitted work\n", encoding="utf-8")
            self.git(derived, "add", "local.txt")
            self.git(derived, "commit", "-m", "제출 전 작업")
            local_head = self.git(derived, "rev-parse", "HEAD").stdout.strip()
            original_head = self.git(original, "rev-parse", "HEAD").stdout.strip()
            remote_head = self.advance_remote(seed, "remote update\n")

            self.git(derived, "fetch", "--prune", "origin")
            pinned = self.git(derived, "rev-parse", "refs/remotes/origin/main").stdout.strip()
            self.assertEqual(pinned, remote_head)
            plan = json.loads(self.run_preparer(
                "analyze",
                "--repo", str(derived),
                "--from-head", local_head,
                "--boundary", boundary,
                "--source", pinned,
                "--branch-family", "task",
                "--target-generation", "2",
            ).stdout)["result"]
            self.run_preparer(
                "create",
                "--repo", str(derived),
                "--source", pinned,
                "--result-tree", str(plan["result_tree_sha"]),
                "--branch-family", "task",
                "--target-generation", "2",
                "--message", "제출 전 작업을 최신 기준으로 이전",
                "--temporary-worktree", str(generation_temporary_worktree(derived, "task", 2)),
            )
            self.git(derived, "switch", "task-stageflow-g2")

            self.assertEqual(self.git(derived, "show", "HEAD:local.txt").stdout, "unsubmitted work\n")
            self.assertEqual(self.git(derived, "show", "HEAD:tracked.txt").stdout, "remote update\n")
            self.assertEqual(self.git(derived, "show", "-s", "--format=%P", "HEAD").stdout.strip(), pinned)
            self.assertEqual(self.git(original, "rev-parse", "HEAD").stdout.strip(), original_head)
            self.assertEqual(self.git(original, "branch", "--show-current").stdout.strip(), "main")
            self.assertEqual(self.git(original, "status", "--porcelain").stdout, "")

    def test_sync_uses_the_manifest_remote_name_without_updating_original_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, seed, original = self.make_remote_fixture(root)
            self.git(original, "remote", "rename", "origin", "upstream")
            derived = root / "derived"
            self.git(original, "worktree", "add", "-b", "task", str(derived), "main")
            boundary = self.git(derived, "rev-parse", "HEAD").stdout.strip()
            original_head = self.git(original, "rev-parse", "HEAD").stdout.strip()
            remote_head = self.advance_remote(seed, "remote update\n")

            self.git(derived, "fetch", "--prune", "upstream")
            pinned = self.git(
                derived, "rev-parse", "refs/remotes/upstream/main"
            ).stdout.strip()
            self.assertEqual(pinned, remote_head)
            plan = json.loads(self.run_preparer(
                "analyze",
                "--repo", str(derived),
                "--from-head", boundary,
                "--boundary", boundary,
                "--source", pinned,
                "--branch-family", "task",
                "--target-generation", "2",
            ).stdout)["result"]
            created = json.loads(self.run_preparer(
                "create",
                "--repo", str(derived),
                "--source", pinned,
                "--result-tree", str(plan["result_tree_sha"]),
                "--branch-family", "task",
                "--target-generation", "2",
                "--temporary-worktree", str(generation_temporary_worktree(derived, "task", 2)),
            ).stdout)["result"]
            self.git(derived, "switch", "task-stageflow-g2")

            self.assertEqual(created["target_sha"], remote_head)
            self.assertEqual(self.git(derived, "rev-parse", "HEAD").stdout.strip(), remote_head)
            self.assertEqual(self.git(original, "rev-parse", "HEAD").stdout.strip(), original_head)
            self.assertEqual(self.git(original, "status", "--porcelain").stdout, "")

    def test_pull_fast_forward_check_rejects_diverged_original(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, seed, original = self.make_remote_fixture(Path(tmp))
            (original / "local.txt").write_text("local\n", encoding="utf-8")
            self.git(original, "add", "local.txt")
            self.git(original, "commit", "-m", "local")
            self.advance_remote(seed, "remote update\n")
            self.git(original, "fetch", "--prune", "origin")
            pinned = self.git(original, "rev-parse", "refs/remotes/origin/main").stdout.strip()

            ancestor = self.git(original, "merge-base", "--is-ancestor", "HEAD", pinned, check=False)
            merge = self.git(original, "merge", "--ff-only", pinned, check=False)

            self.assertNotEqual(ancestor.returncode, 0)
            self.assertNotEqual(merge.returncode, 0)

    def test_preflight_can_detect_dirty_and_wrong_location_before_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, _, original = self.make_remote_fixture(root)
            derived = root / "derived"
            self.git(original, "worktree", "add", "-b", "task", str(derived), "main")
            (derived / "tracked.txt").write_text("dirty task change\n", encoding="utf-8")

            self.assertNotEqual(self.git(derived, "status", "--porcelain").stdout, "")
            self.assertEqual(self.git(derived, "branch", "--show-current").stdout.strip(), "task")
            self.assertEqual(self.git(original, "branch", "--show-current").stdout.strip(), "main")
            self.assertNotEqual(
                self.git(original, "branch", "--show-current").stdout.strip(),
                "task",
            )

    def test_open_sync_fetches_only_and_preserves_local_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, seed, original = self.make_remote_fixture(root)
            derived = root / "derived"
            self.git(original, "worktree", "add", "-b", "task", str(derived), "main")
            (derived / "a.txt").write_text("submitted A\n", encoding="utf-8")
            self.git(derived, "add", "a.txt")
            self.git(derived, "commit", "-m", "작업 A")
            (derived / "b.txt").write_text("local B\n", encoding="utf-8")
            self.git(derived, "add", "b.txt")
            self.git(derived, "commit", "-m", "작업 B")
            before_head = self.git(derived, "rev-parse", "HEAD").stdout.strip()
            before_tree = self.git(derived, "rev-parse", "HEAD^{tree}").stdout.strip()
            before_branch = self.git(derived, "branch", "--show-current").stdout.strip()
            remote_head = self.advance_remote(seed, "remote while PR open\n")

            self.git(derived, "fetch", "--prune", "origin")

            self.assertEqual(
                self.git(derived, "rev-parse", "refs/remotes/origin/main").stdout.strip(),
                remote_head,
            )
            self.assertEqual(self.git(derived, "rev-parse", "HEAD").stdout.strip(), before_head)
            self.assertEqual(self.git(derived, "rev-parse", "HEAD^{tree}").stdout.strip(), before_tree)
            self.assertEqual(self.git(derived, "branch", "--show-current").stdout.strip(), before_branch)
            self.assertEqual((derived / "b.txt").read_text(encoding="utf-8"), "local B\n")

    def test_dirty_repository_stays_unchanged_while_clean_sibling_rotates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dirty_root = root / "dirty-repository"
            clean_root = root / "clean-repository"
            dirty_root.mkdir()
            clean_root.mkdir()
            _, _, dirty_original = self.make_remote_fixture(dirty_root)
            _, clean_seed, clean_original = self.make_remote_fixture(clean_root)
            dirty_derived = dirty_root / "derived"
            clean_derived = clean_root / "derived"
            self.git(dirty_original, "worktree", "add", "-b", "dirty-task", str(dirty_derived), "main")
            self.git(clean_original, "worktree", "add", "-b", "clean-task", str(clean_derived), "main")
            (dirty_derived / "tracked.txt").write_text("dirty and skipped\n", encoding="utf-8")
            dirty_head = self.git(dirty_derived, "rev-parse", "HEAD").stdout.strip()
            clean_boundary = self.git(clean_derived, "rev-parse", "HEAD").stdout.strip()
            (clean_derived / "local.txt").write_text("clean sibling work\n", encoding="utf-8")
            self.git(clean_derived, "add", "local.txt")
            self.git(clean_derived, "commit", "-m", "정상 형제 작업")
            clean_head = self.git(clean_derived, "rev-parse", "HEAD").stdout.strip()
            clean_source = self.advance_remote(clean_seed, "clean source advance\n")

            self.git(clean_derived, "fetch", "--prune", "origin")
            plan = json.loads(self.run_preparer(
                "analyze",
                "--repo", str(clean_derived),
                "--from-head", clean_head,
                "--boundary", clean_boundary,
                "--source", clean_source,
                "--branch-family", "clean-task",
                "--target-generation", "2",
            ).stdout)["result"]
            self.run_preparer(
                "create",
                "--repo", str(clean_derived),
                "--source", clean_source,
                "--result-tree", str(plan["result_tree_sha"]),
                "--branch-family", "clean-task",
                "--target-generation", "2",
                "--message", "정상 형제 작업을 최신 기준으로 이전",
                "--temporary-worktree", str(generation_temporary_worktree(clean_derived, "clean-task", 2)),
            )
            self.git(clean_derived, "switch", "clean-task-stageflow-g2")

            self.assertEqual(self.git(dirty_derived, "rev-parse", "HEAD").stdout.strip(), dirty_head)
            self.assertEqual(self.git(dirty_derived, "branch", "--show-current").stdout.strip(), "dirty-task")
            self.assertIn("tracked.txt", self.git(dirty_derived, "status", "--porcelain").stdout)
            self.assertEqual(
                self.git(clean_derived, "branch", "--show-current").stdout.strip(),
                "clean-task-stageflow-g2",
            )
            self.assertEqual(self.git(clean_derived, "show", "HEAD:local.txt").stdout, "clean sibling work\n")

    def test_sync_conflict_stops_in_derived_and_leaves_original_checkout_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, seed, original = self.make_remote_fixture(root)
            derived = root / "derived"
            self.git(original, "worktree", "add", "-b", "task", str(derived), "main")
            boundary = self.git(derived, "rev-parse", "HEAD").stdout.strip()
            (derived / "tracked.txt").write_text("task change\n", encoding="utf-8")
            self.git(derived, "commit", "-am", "task change")
            local_head = self.git(derived, "rev-parse", "HEAD").stdout.strip()
            original_head = self.git(original, "rev-parse", "HEAD").stdout.strip()
            original_content = (original / "tracked.txt").read_text(encoding="utf-8")
            remote_head = self.advance_remote(seed, "remote change\n")

            self.git(derived, "fetch", "--prune", "origin")
            pinned = self.git(derived, "rev-parse", "refs/remotes/origin/main").stdout.strip()
            self.assertEqual(pinned, remote_head)
            analyzed = self.run_preparer(
                "analyze",
                "--repo", str(derived),
                "--from-head", local_head,
                "--boundary", boundary,
                "--source", pinned,
                "--branch-family", "task",
                "--target-generation", "2",
                check=False,
            )

            self.assertEqual(analyzed.returncode, 2)
            self.assertIn("no branch was changed", analyzed.stderr)
            self.assertNotEqual(
                self.git(derived, "rev-parse", "--verify", "refs/heads/task-stageflow-g2", check=False).returncode,
                0,
            )
            self.assertNotEqual(
                self.git(derived, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode,
                0,
            )
            self.assertEqual(self.git(original, "rev-parse", "HEAD").stdout.strip(), original_head)
            self.assertEqual(self.git(original, "branch", "--show-current").stdout.strip(), "main")
            self.assertEqual(self.git(original, "status", "--porcelain").stdout, "")
            self.assertEqual((original / "tracked.txt").read_text(encoding="utf-8"), original_content)


if __name__ == "__main__":
    unittest.main()
