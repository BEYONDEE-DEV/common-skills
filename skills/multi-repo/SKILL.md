---
name: multi-repo
description: "Use only when the user explicitly invokes `$common-skills:multi-repo` (Codex) or `/common-skills:multi-repo` (Claude Code). Submits task branches across a folder of independent Git repositories (commit, merge the latest source branch, push, create or update the PR), shows their status, and promotes branches through dev, dev2 and prod with fast-forward pushes."
disable-model-invocation: true
---

# Multi Repo

Operate on every Git repository directly under a bundle folder (or on one repository). Each repository is handled independently: one failing or waiting never stops the others.

```text
$common-skills:multi-repo <status|submit|promote|help> [free-form intent]     # Codex
/common-skills:multi-repo <status|submit|promote|help> [free-form intent]     # Claude Code
```

Without a keyword, or with `help`, show the help block at the end and stop.

## Runner

All mechanics go through one script in this skill's directory. `<skill-dir>` is the directory containing this `SKILL.md` (Claude Code shows it as the skill's base directory); never assume the session cwd is the plugin root.

```bash
python3 "<skill-dir>/scripts/multi_repo.py" --root "<bundle>" [--repo <name> ...] <command> [options]
```

- On Windows use `py -3` (or `python`) instead of `python3`.
- `--root` and `--repo` come before the command. `<bundle>` defaults to the cwd; use the folder that contains the repositories, or a single repository.
- Output is JSON. Read `repositories[].status` per repository; a top-level `status` means the whole call failed (for example `NO_REPOSITORIES`).
- Requirements: Python 3.9+, `git`, and `gh` authenticated for the repository's GitHub host (`gh auth status`).

## submit

Goal: every selected repository ends with its task branch pushed, containing the latest source branch, and an open PR into that source. Use exactly two runner calls in the normal case; do not run per-repository `git status`/`git diff` yourself.

1. **Prepare** (changes nothing): `submit --prepare [--source <branch>]`. All repositories are fetched concurrently. Each `READY` repository reports `dirty` paths with `dirty_diffstat`, `dirty_diff` (cut at 12,000 characters when `dirty_diff_truncated`; read files directly only if you need more), `untracked`, the open `pr`, and, when `needs_pr_text`, the `commits` and `diffstat` a new PR would contain (plus the dirty changes once committed).
2. **Write one plan file** outside every repository (for example in the system temp directory; a file inside a repository would make it dirty), covering every repository to submit (omit repositories to leave alone):

```json
{"repositories": {
  "api": {"commit": {"message": "로그인 오류 메시지 개선", "paths": ["src/login.ts", "src/old.ts"]},
          "pr": {"title": "로그인 오류 메시지 개선", "body": "## 변경 사항\n- ..."}},
  "web": {}
}}
```

   - `commit`: only when the repository is dirty. List exactly the task-related paths (for renames, both sides); only those are committed, whatever else is staged. Write the message in Korean following the repository's existing commit conventions. If changes look unrelated, secret, or generated, leave them out and ask the user.
   - `pr`: only when `needs_pr_text`. Korean title and body from `commits`, `diffstat` and the committed changes; follow the repository's PR template if any.
   - An empty entry (`{}`) just merges the source, pushes, and updates the existing PR.
3. **Execute**: `submit --plan <file> [--source <branch>]`. For each planned repository, concurrently: commit the planned paths, merge `origin/<branch>` and then `origin/<source>` (normal merge commits; never rebase), push without force, and create or update the PR.
4. Act on any repository not at `CREATED`, `UPDATED` or `NO_CHANGES`, then rerun `--repo <name> submit --plan <file>` for it. Rerunning a plan is safe: already committed paths and existing PRs are not duplicated.

Without `--prepare`/`--plan`, `submit` runs step 3 with no commits for every repository; `--repo <name> submit --title "<title>" --body-file <file>` creates a single PR directly.

| status | meaning | next step |
|---|---|---|
| `SOURCE_REQUIRED` | No `--source`, remembered source, or PR base. | Ask the user which branch to merge from (`remote_branches` lists candidates); rerun with `--source <branch>`. It is remembered afterwards in `git config branch.<branch>.multiRepoSource`. |
| `READY` | (`--prepare` only) Ready for a plan entry. | Write its entry as in step 2. |
| `DIRTY` | Files outside the plan's `commit.paths` remain (`dirty`); nothing was merged or pushed. | Add them to the plan if they belong to the task, otherwise ask the user. Ignored files never count. |
| `CONFLICT` | A merge stopped (`merging`, `conflicts`). | Resolve every listed file preserving both sides' intent (never take a whole side blindly, never drop tests), `git add` them, rerun step 4; it completes the merge commit. Ask the user only when the intents genuinely contradict. |
| `PR_TEXT_REQUIRED` | Pushed; no open PR and the plan had no `pr` text. | Add `pr` to its plan entry from `commits` and `diffstat`, rerun step 4. |
| `CREATED` / `UPDATED` | PR opened / existing open PR updated by the push. | Done. Report `pr.url`; mention any `warning`. |
| `NO_CHANGES` | Branch has no difference from the source (e.g. its PR was already squash-merged). | Done; nothing pushed. |
| `PROTECTED_BRANCH` | Current branch is `main`, `master`, `dev`, `dev2`, `prod`, or equals the source. | Stop for this repository; tell the user to switch to a task branch. |
| `PUSH_REJECTED` / `MERGE_FAILED` / `COMMAND_FAILED` / `DETACHED` / `GH_MISSING` / `SOURCE_NOT_FOUND` / `PLAN_INVALID` / `UNKNOWN_REPOSITORY` | Environment or remote problem (`error`). | Report the error; rerun once if the remote simply moved. If a commit hook rejected the merge commit (merge still in progress, no conflicts), commit it with `git commit -m "<message the hook accepts>"` and rerun. Never force-push. |

After a PR is squash-merged on GitHub, keep working on the same branch and submit again: the merge brings in the squashed source, so the next PR shows only new work.

The user merges PRs on GitHub. Never merge a PR, enable auto-merge, force-push, reset, rebase, or delete branches.

## status

Run `status` (add `--offline` to skip fetching). Report one line per repository in Korean, e.g.:

```text
api: feature-login (← dev) / 변경 2개 / 앞섬 3 · 뒤처짐 1 / 미푸시 0 / PR #12 OPEN
```

## promote

Advances remote branches `<from> -> dev -> dev2 -> prod` with fast-forward pushes only. `--from` defaults to the current branch's source; when it is already `dev` or `dev2`, the chain starts there. `--chain a,b,c` overrides the stages.

1. Run `promote --from <branch>` to get the plan. Show the user, per repository, each step's `action` (`PUSH` with its `commits`, `UP_TO_DATE`, or `AHEAD` meaning that stage already contains more and its tip is carried forward) and any failed repository.
2. The user's explicit `promote` request authorizes execution of the plan you showed: run the same command with `--execute <plan_id>`.
3. Results: `PROMOTED` lists `promoted` stages; `UP_TO_DATE` needs nothing. `PLAN_CHANGED` means a remote moved after the plan was shown: nothing was pushed; show the new plan again. `DIVERGED` means a stage has commits the previous stage lacks: stop that repository and tell the user it needs a manual merge. `BRANCH_NOT_FOUND` / `PUSH_REJECTED` (for example branch protection) stop that repository; already pushed stages are reported in `promoted`, and rerunning resumes from the remaining stages.

Run `promote` only when the user asks for it.

## help

```text
multi-repo: 폴더 안의 Git 저장소들을 한 번에 다룹니다.

  status   저장소별 브랜치, source 대비 앞섬/뒤처짐, 미푸시 커밋, PR 상태
  submit   커밋 → 최신 source merge(충돌은 해결) → push → PR 생성/갱신
  promote  원격 브랜치를 <source> → dev → dev2 → prod 순서로 fast-forward 승격

PR merge는 GitHub에서 직접 하세요. merge 후에도 같은 브랜치에서 계속 작업하고 submit하면 됩니다.
```
