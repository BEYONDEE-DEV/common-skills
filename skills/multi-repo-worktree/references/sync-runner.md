# Sync Runner

## Ordinary Flow

Use only for an explicit `sync` request on the manifest-bound development bundle. Resolve `<skill-dir>` from the loaded skill; the workspace is not necessarily a Git repository.

First run one read-only preflight:

```bash
python3 "<skill-dir>/scripts/sync_bundle.py" --root "<workspace-root>" \
  --bundle "worktrees/<slot>"
```

The manifest resolves the target repositories before discovery. Only that bundle is returned; other registered worktrees provide relevant branch-occupancy evidence, not full status scans. Dirty repositories are classified without reading their file contents or querying GitHub. The runner reads the manifest, exact local/remote refs, PR metadata, and pending submit receipts. It does not fetch, write plans/reports, acquire a lock, or migrate legacy evidence during preflight.

Each eligible repository includes its classification, intended action, cleanup candidates, and a `plan` object containing the exact reviewed snapshot. `transfer_review` supplies the continuation-only boundary/head SHAs and changed file names, so choosing the transfer subject does not require another manifest dump. In-flight journals supply their recorded subject. Copy the plan objects into a JSON file outside the target repositories, such as `.multi-repo-worktree/sync-plans/<slot>.json`:

```json
{
  "repositories": {
    "events-web": {
      "expected_head": "<head_sha>",
      "expected_branch": "<branch>",
      "expected_generation": 1,
      "expected_identity": "<identity_fingerprint>",
      "expected_remote": "<remote_key>",
      "expected_source": "<source_sha>",
      "transfer_subject": "행사 검색 조건 유지"
    }
  }
}
```

Do not invent hashes or rewrite manifest data. `expected_generation` is the PR generation, not the branch generation. Include the requested repository set; an unresolved/dirty repository may have an empty plan object so it remains visible in the result. A repository that becomes clean later needs a fresh complete plan before it can mutate.

For a possible nonempty rotation, inspect the **committed continuation-only** net changes and relevant task context, then supply one outcome-focused `transfer_subject` following repository conventions. It must describe future work, not branch movement or previously merged work. Ordinary conflict-free sync needs no test commands, task path list, task commit message, or PR body; conflict recovery requires focused `validation` commands before completion. Empty transfers have a null subject and create no transfer commit. A journaled retry uses the original exact subject, never a newly guessed one. If source advancement reveals a nonempty transfer without a subject, report the missing subject and refresh the plan; do not use a workflow placeholder.

Send one concise non-blocking summary of targets, dirty skips, OPEN waits, planned rotations/cleanups, and partial-failure behavior. An explicit sync request authorizes these bounded routine writes, without another routine approval. Then call:

```bash
python3 "<skill-dir>/scripts/sync_bundle.py" --root "<workspace-root>" \
  --bundle "worktrees/<slot>" --plan "<absolute-plan.json>" --execute
```

Do not separately dump manifest/status/PR payloads or manually orchestrate the same steps. The runner repeats the relevant machine checks internally. On `CONFLICT_RESOLUTION_REQUIRED`, follow `conflict-recovery.md` and finish ordinary isolated recovery without another approval question. Ambiguous permanent facts, stale locks, unknown legacy history or unexpected remote heads retain the existing narrow decision boundary.

## Repository Behavior

- **Dirty:** `SKIPPED`, unchanged. No fetch, stage, task commit, stash, switch, rotation, or cleanup in that repository. Clean siblings may continue.
- **NONE:** fetch and pin the source. At the same branch base, return `NOOP`; otherwise rotate the committed unsubmitted net work. No branch push or PR creation.
- **OPEN:** verify the exact ready PR and remote head, fetch source, and return `WAITING`. Branch, files, local commits, submission evidence, and all current/pending cleanup refs remain unchanged. Never delete older pending merged refs through this OPEN path.
- **MERGED, unrotated:** transfer only continuation B from the immutable submission boundary onto the pinned source, finish local old-generation retirement and manifest recording, then clean the exact merged refs. Squash merges are supported through GitHub merge-commit inclusion, not PR-head ancestry.
- **MERGED, already rotated:** complete current/pending merged-ref cleanup **before** another source-advance rotation replaces its receipt. Failed cleanup prevents that repository's next rotation. Multiple pending records are checked/deleted/recorded individually; successful earlier cleanup remains complete if a later one fails.
- **In-flight rotation:** verify and resume the existing journal, then continue eligible cleanup/new-source work. A compatible `branch-created` journal missing `target_head_sha` is backfilled only from the verified target. Its pinned source must still be an ancestor of the fetched source.
- **Integration conflict:** use the returned isolated worktree and exact receipt to reconcile both intents and record a hooked commit. Resume with focused `validation` commands. Validation after resolution is mandatory and persisted; missing/failing checks stop cleanup/completion without publishing anything. A later clean retry still requires the checks even if no further rotation is needed.
- **Pending submit publication or unrecorded active remote branch:** stop that repository. Sync cannot adopt, delete, rotate, or rewrite a pushed-but-unrecorded submission; use submit's exact recovery path first. Draft/CLOSED/mismatched/unknown PRs likewise fail locally.

## Preservation And Recovery

The same slot operation lock protects submit and sync. The runner binds the reviewed path, manifest identity, remote mapping, branch, and HEAD. Compatible source advancement since preflight can be fetched and pinned after ancestry proof; source/ref drift during mutation stops the repository and preserves recorded progress for a fresh preflight.

Rotation uses the existing common journal, isolated transfer worktree, normal commit hooks/signing, and compare-and-delete old-local-ref helper. It creates no source-sync merge commit. Branch switching refuses to overwrite ignored files; a collision keeps the local data and the retry journal instead of discarding it. No original/source worktree HEAD, index, files, or local branch is updated. Fetch does update shared remote-tracking refs.

Cleanup re-queries the exact GitHub repository/PR and requires ready MERGED state, base/head/observed SHA, merge time/commit, source inclusion, and completed continuation preservation. It delegates to `cleanup_merged_branch.py`, with fresh binding/local/source guards immediately before each deletion. Remote deletion uses the exact expected-head lease; legacy local deletion requires the existing exact completed-rotation proof. Active, source, moved, foreign, or checked-out branches are preserved. A missing already-deleted ref is an idempotent success. No bulk pruning or deletion based only on names/age/ancestry is introduced.

The authoritative slot/branch data remains `slots.json` and its existing rotation/pending-cleanup records. Conflict receipts bind only an isolated resolution to exact Git inputs; shared submission receipts persist the required-validation flags without changing pending publication. No new slot lifecycle or source-of-truth manifest is introduced. A small `.multi-repo-worktree/sync-runs/` report stores durations, attempts/retries, partial completed work, and errors. The report never authorizes a deletion or changes submission generation/boundaries.

Results are compact per repository: `SYNCED`, `NOOP`, `WAITING`, `SKIPPED`, or `FAILED`, with completed rotations/cleanups, failed phase, remaining rotation phase/target, duration, and retry count. Dirty or unresolved results produce a nonzero exit status but never roll back siblings. Preflight uses `DIRTY`, `BLOCKED`, or `RECOVER` for unresolved states. Keep successful summaries short; investigate only the reported exception. Runtime improvements come from scoped inspection and fewer agent/tool round trips, not skipping required preservation checks.
