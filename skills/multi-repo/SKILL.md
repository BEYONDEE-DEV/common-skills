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

Goal: every selected repository ends with its task branch pushed, containing the latest source branch, and an open PR into that source.

1. Commit first. For each repository with changes (`git status`), commit only task-related paths, with a Korean message that follows the repository's existing commit conventions. Leave a repository uncommitted and ask the user only if its changes look unrelated, secret, or generated.
2. Run `submit`. It fetches, merges `origin/<branch>` and then `origin/<source>` into the task branch (normal merge commits; never rebase), pushes without force, and finds the PR for the branch.
3. Act on each repository's status, then rerun `submit` for that repository until it reaches `CREATED`, `UPDATED` or `NO_CHANGES`:

| status | meaning | next step |
|---|---|---|
| `SOURCE_REQUIRED` | No `--source`, remembered source, or PR base. | Ask the user which branch to merge from (`remote_branches` lists candidates); rerun with `--source <branch>`. It is remembered afterwards in `git config branch.<branch>.multiRepoSource`. |
| `DIRTY` | Uncommitted or untracked files remain (`dirty`). | Commit them as in step 1, or ask the user. Ignored files never count. |
| `CONFLICT` | A merge stopped (`merging`, `conflicts`). | Resolve every listed file preserving both sides' intent (never take a whole side blindly, never drop tests), `git add` them, rerun `submit`; it completes the merge commit. Ask the user only when the intents genuinely contradict. |
| `PR_TEXT_REQUIRED` | Pushed; no open PR yet. | Write a Korean PR title and body from `commits` and `diffstat` (follow the repository's PR template if any), save the body to a file, rerun with `--repo <name> --title "<title>" --body-file <file>`. |
| `CREATED` / `UPDATED` | PR opened / existing open PR updated by the push. | Done. Report `pr.url`; mention any `warning`. |
| `NO_CHANGES` | Branch has no difference from the source (e.g. its PR was already squash-merged). | Done; nothing pushed. |
| `PROTECTED_BRANCH` | Current branch is `main`, `master`, `dev`, `dev2`, `prod`, or equals the source. | Stop for this repository; tell the user to switch to a task branch. |
| `PUSH_REJECTED` / `MERGE_FAILED` / `COMMAND_FAILED` / `DETACHED` / `GH_MISSING` / `SOURCE_NOT_FOUND` | Environment or remote problem (`error`). | Report the error; rerun once if the remote simply moved. If a commit hook rejected the merge commit (merge still in progress, no conflicts), commit it with `git commit -m "<message the hook accepts>"` and rerun. Never force-push. |

After a PR is squash-merged on GitHub, keep working on the same branch and run `submit` again: the merge brings in the squashed source, so the next PR shows only new work.

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
