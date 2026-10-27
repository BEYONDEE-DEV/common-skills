# Automatic Generation Conflict Recovery

This is part of an explicit `submit` or clean `sync` request. Carry routine integration conflicts through resolution and validation without asking the user for permission again. The Python runner supplies an isolated worktree; the agent supplies the semantic reconciliation. Do not use a generic ours/theirs strategy as a substitute for understanding the changes.

## Continue From The Runner Result

`error_code: CONFLICT_RESOLUTION_REQUIRED` includes `conflict_recovery` with an absolute `worktree`, `receipt_path`, exact `conflict_paths`, the pinned `boundary_sha` / `source_sha` / `from_head_sha`, and the initial conflict tree. This is an intermediate machine handoff, not a user-facing blocker. Preserve a submit's returned `resume_plan_path`; sync can reuse its plan while the development input is unchanged. Let successful sibling repositories remain complete and continue recovery for the failed ones.

1. Briefly report that integration conflicts are being reconciled. Read the base, upstream and local versions plus the task context and relevant repository guidance. Compare `boundary..source` and `boundary..from_head` to understand both intents; stage 2 is the source side and stage 3 is the local continuation side. Read non-conflicting merged files when needed for context.
2. Edit only the returned conflict paths in the returned worktree. Preserve both intended behaviors and test coverage. For a test conflict such as a new DOM test alongside an upstream assertion change, combine the DOM scenario with the upstream assertion's purpose; keep meaningful assertions and correct obsolete mechanics. Handle straightforward rename, modification/deletion and binary choices only when the recorded intent establishes the right result.
3. Inspect the resulting diff and stage each resolved path literally, including deliberate deletions. Do not modify or stage the original/development worktree, unrelated files, or the automatically merged non-conflicting result.

```bash
git -C "<conflict-worktree>" add -A -- ":(literal)<conflict-path>"
python3 "<skill-dir>/scripts/generation_conflicts.py" --root "<workspace-root>" \
  --slot "<slot>" --repository "<repository>" --receipt "<receipt_path>" \
  --subject "<outcome-focused-transfer-subject>" \
  --summary "<how-upstream-and-local-intents-were-preserved>"
```

Use the task's transfer subject, or a suitable outcome summary if none was available. Recording runs normal Git commit hooks/signing and rejects unresolved stages/markers, unstaged changes, unrelated path changes, extra files, changed source/development inputs, or a hook-altered tree/parent/subject. It keeps a detached clean commit anchored until the ordinary runner preserves it in the next generation. A record-response failure can adopt only the exact already-created commit on retry; never amend it or bypass hooks.

4. Resume the same submit using the generated resume plan (or the same sync using its unchanged plan). The runner consumes the recorded tree, finishes the normal generation journal and preserves the correct single-source parent. It removes only its verified clean conflict worktree after the target protects the commit. No PR/slot binding is manually rewritten.
5. Supply focused `validation` argv arrays in the resume plan based on the changed code and repository commands, for example `[["npm", "run", "check"]]` when that is the project's relevant check. Resolution always requires actual passing validation, even if its tree equals a previously reviewed tree or has no remaining PR diff. An integration check is not a reason to ask a routine approval question. Both submit and sync persist a required-check flag so re-preparing or retrying cannot erase the gate; submit cannot push/create a PR until it passes.

If checks fail, inspect the failure and fix issues within the authorized task, then review and validate the changed content through the ordinary runner path. Use fresh preparation when the development tree changes; never amend an already recorded resolution or force a stale resume plan. Keep successful remote work and report its actual state. Finish the user's original command rather than only announcing that conflicts were fixed.

## Changing Inputs And Decision Boundaries

- A compatible source advancement creates a new exact recovery context; preserve the previous isolated work. Re-inspect the new three-way inputs and carry forward still-applicable resolution edits deliberately. Never reuse a tree solely because the file names match. If the development input changed, prepare and review the new task snapshot first.
- Resume only a runner-owned detached worktree whose path, source/head, Git common directory and receipt match. Preserve foreign/attached/dirty unexpected state and all extra files. Do not force-remove a recovery worktree or clear an unproven stale lock.
- Ask only when satisfying both sides requires a new product decision, one side intentionally removes behavior the other requires, or the desired meaning cannot be inferred from the task and source evidence. Explain the concrete contradictory behaviors and the decision needed. A conflict marker, a changed test or the need to run a check alone is not such a decision.
- After two unsuccessful semantic resolution/validation attempts for the same unchanged inputs, report the remaining concrete issue and preserve all work. Do not loop by dropping code/tests, reducing assertions or retrying an unchanged failing command.
- This recovery applies to generation integration in submit/sync. Original-only `pull` and fast-forward-only `promote` keep their existing divergence boundaries; a rejected production push is not authorization to synthesize a merge or bypass protection.
