# Promote Runner

Use `promote` when the user asks to carry committed branch contents through the environment branches to `prod`.

## Source And Scope

Read each selected slot repository's `source_branch` and `remote` from the manifest. The source is variable (`x`); never hard-code `dev-feature-food`, infer it from a slot branch name, or substitute the working tree's `HEAD`.

```text
x other than an environment branch: x -> dev -> dev2 -> prod
x = dev:                           dev -> dev2 -> prod
x = dev2:                          dev2 -> prod
x = prod:                          prod (already at final stage)
```

Only remote commits participate. Local uncommitted content and local-only follow-up commits remain in their worktrees. To include task changes, first use the existing submit/PR process to put them on remote `x`, then request `promote`. This command does not create or merge PRs. The endpoint is remote branch reflection; it does not run deployment tools or claim server rollout completion. A branch push can still trigger the repository's existing CI/deployment automation.

Promotion carries the whole reviewed upstream branch tip. If an intermediate target already contains its input and has additional commits, keep its existing tip and include those additional commits in the next stage's review. Show that scope before execution; do not describe it as selecting only one PR's files. No extra test run is required for an unchanged fast-forwarded tree unless the user or repository policy requires one. The runner does not synthesize merge/squash commits or resolve divergence.

## Prepare And Inspect

An optional read-only inspection neither fetches nor writes local state:

```bash
python3 "<skill-dir>/scripts/promote_bundle.py" --root "<workspace-root>" \
  --bundle "worktrees/<slot>"
```

If a remote commit is missing locally, prepare is needed before ancestry can be checked. For an explicit promotion request, prepare the complete selected bundle:

```bash
python3 "<skill-dir>/scripts/promote_bundle.py" --root "<workspace-root>" \
  --bundle "worktrees/<slot>" --prepare
```

Use repeated `--repository "<manifest-repository-name>"` to select a subset. Without a subset, all manifest repositories in the bundle are considered independently. Resolve scope from the user's request; ask only when the repository selection or permanent source context is genuinely ambiguous.

Preparation fetches the exact source/environment refs without checking out branches, writing task files, or changing local source branches. It writes a fingerprinted plan under `.multi-repo-worktree/promotions/`. The result includes `plan_path` and per-repository `route`, original `refs`, and `steps` containing input/target/result SHA, action, commit subjects and changed paths.

Read the generated plan and send a concise summary of the repositories, source-to-prod paths, included changes, any blocked repositories and partial-success behavior. A `promote` request authorizes these normal updates; do not turn the summary into another routine approval gate. Missing branches or divergent histories block only that repository, before its first remote update. Never auto-create a production/environment branch or silently switch to a merge commit, force push or admin bypass.

## Execute And Resume

```bash
python3 "<skill-dir>/scripts/promote_bundle.py" --root "<workspace-root>" \
  --bundle "worktrees/<slot>" --execute "<generated-plan-path>"
```

The runner revalidates the complete selected bundle, locks the slot and each remote repository across slots, and uses one fast-forward push per changed stage. Each push independently proves ancestry and sets `--force-with-lease=refs/heads/<target>:<expected-before-sha>` solely as an atomic comparison condition. This rejects a target moved or deleted after the last query; it never permits a non-fast-forward update or branch recreation. Existing branch protection and server hooks apply normally. It never checks out or resets original/development branches, creates commits, modifies PR generations, or calls GitHub merge APIs.

Before every push it saves the intended stage in `<run-id>.progress.json`; after each push it queries the remote to verify the exact resulting SHA. Repeating `--execute` with the same generated plan accepts an exact already-completed stage and continues, including a lost push response. A different remote source/target SHA or changed binding stops that repository and requires a fresh prepared plan. A completed stage is never rolled back when a later push fails.

Report each repository as `PROMOTED`, `NOOP` or `FAILED`, its stage progress, and final observed `prod_sha` when successful. Independent siblings continue. Preserve the report and resume path after failure. Missing or stale operation locks require the existing explicit lock-recovery procedure; never unlock another operation automatically.

## Limits

Multi-repository promotion is not a single transaction. A failure can leave some repositories or intermediate branches promoted while others remain pending. Report that state as it is. Conflicting histories require a separate integration decision; this command never guesses how to combine them.
