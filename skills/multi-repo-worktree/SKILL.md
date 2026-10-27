---
name: multi-repo-worktree
description: "Use only when the user explicitly invokes `$common-skills:multi-repo-worktree` (Codex) or `/common-skills:multi-repo-worktree` (Claude Code), links `[$common-skills:multi-repo-worktree](...)`, or explicitly asks to use multi-repo-worktree for a bundle of multiple independent Git repositories. Coordinate help, status, one-time create, generation-aware submit, pull, development-worktree sync, and source-to-prod branch promotion with permanent slots and repository-local recovery. Do not use for ordinary Git, single-repo, or single-worktree requests."
---

# Multi Repo Worktree

Coordinate one permanent development bundle whose folders are independent Git repositories. Keep the slot and worktrees, but rotate each repository's local task branch when its remote source must become the new base. Treat repository branches, source refs, dirty state, PR state, generation, and failures independently.

## Invocation

Use (Codex):

```text
$common-skills:multi-repo-worktree <keyword> [free-form intent]
```

Use (Claude Code):

```text
/common-skills:multi-repo-worktree <keyword> [free-form intent]
```

Support only:

- `help`: explain the workflow and safety gates.
- `status`: inspect repositories, slot bindings, branch generations, PRs, and recovery journals.
- `create`: initialize a permanent slot and its first task worktrees once.
- `submit`: commit task changes, create a ready PR, wait behind an open PR, or rotate after a merged PR and create the next ready PR. An explicit PR-correction intent is handled as an isolated submit variant.
- `promote`: advance each repository's remote source branch through `dev -> dev2 -> prod` with reviewed fast-forward pushes; skip stages preceding a source already on that path.
- `pull`: fast-forward confirmed original source worktrees.
- `sync`: fetch remote source refs in development worktrees; rotate eligible `NONE`/`MERGED` repositories, remove exactly proven merged PR branches after their continuation is preserved, and leave `OPEN` repositories waiting without changing their branch or files.

Without a keyword, show help and ask which keyword to use. Do not accept or advertise `recycle`, `integrate`, `merge`, or legacy aliases.

## Required Inspection

For ordinary `submit`, read `references/submit-runner.md` and use its Python `--prepare` / `--execute` path. It generates the snapshot plan and review material, reports each repository's action and required inputs, and checks the complete selected bundle locally. Read the generated material and provide one Korean task summary when requested; use separate prose only for different scopes or repository conventions. Remote candidate lookups and independent PR proofs run with bounded concurrency. Use an exact generated resume plan after failure; content changes require fresh review. Do not copy hashes, reconstruct routine diffs, or manually orchestrate Git/PR steps. Development tests are not repeated solely for PR creation; actual tree changes during integration or an explicit test request trigger focused revalidation. Read `references/worktree-operations.md` only for explicit PR correction, legacy recovery, or an unsupported exception.

For `sync`, read `references/sync-runner.md` and use its Python preflight/execution path. Reuse the scoped inspection and journaled rotation helpers; do not manually chain routine fetch, PR classification, rotation, and cleanup commands. Read `references/worktree-operations.md` only for an unsupported legacy or conflict-recovery exception.

When submit or sync reports `CONFLICT_RESOLUTION_REQUIRED`, read `references/conflict-recovery.md` and continue the same request: inspect the isolated base/source/local changes, reconcile the recorded conflict paths, record the resolution and resume with focused validation. A routine merge conflict is authorized recovery, not a reason to ask whether to continue or to end the task. Do not execute a quoted submit example when the user is asking to improve this skill.

For `promote`, read `references/promote-runner.md` and use `scripts/promote_bundle.py`. Its generated plan pins remote refs and lists each stage's commits and paths before publication. The source `x` is the manifest's repository-specific `source_branch`, not a hard-coded feature branch. Run promotion only when the user requests it; adding or updating this capability does not authorize running it against production branches.

For `status`, `create`, and `pull`:

1. Read `references/worktree-operations.md` from this skill directory completely.
2. Resolve `<skill-dir>` from the loaded `SKILL.md`; do not assume cwd is the plugin root.
3. Run the read-only inspector when a workspace path exists:

```bash
python3 "<skill-dir>/scripts/inspect_worktrees.py" --root "<workspace-root>" --json
```

Use `--bundle "worktrees/<bundle-name>"` when known. Never infer that a workspace or bundle folder is a Git repository.
The bundle selector resolves manifest repositories before Git inspection and returns only those worktrees. Other registered worktrees are checked internally for relevant branch occupancy. Use `--all-worktrees` only for explicitly needed workspace-wide diagnostics.

For these slot-backed operations, read the manifest without changing Git (submit/sync runners already include this):

```bash
python3 "<skill-dir>/scripts/slot_manifest.py" --root "<workspace-root>" status
```

## Workflow And Ownership

```text
create once
  -> develop A
  -> submit ready PR A
  -> continue B locally in the same development worktree
  -> user squash-merges PR A on GitHub
  -> sync or next submit fetches origin/source in the development worktree
  -> move only B onto a fresh generation branch based on that fetched SHA
  -> rotation retires the old local generation branch after switching
  -> sync deletes the exact merged remote PR ref when one remains
  -> submit ready PR B
  -> repeat
```

- The slot path and development worktrees persist. There is no frozen, active, released, owner-transfer, or submit-time cleanup lifecycle.
- The current local task branch is generation-scoped; it is not a permanent slot identity.
- `create` owns only first initialization and exact retry. It records each confirmed remote source SHA as the initial branch base.
- `submit` owns Korean task commits, Korean ready PR creation, open-PR waiting, explicit isolated PR correction, generation rotation with local old-generation retirement, and repository-local manifest recording.
- `promote` owns remote branch advancement from the manifest source through `dev -> dev2 -> prod`. It uses only remote committed content, preserves local branches/worktrees and PR evidence, and records per-stage progress separately from the slot manifest. It finishes when the planned commit is observed on remote `prod`; server deployment is outside this operation.
- The user merges PRs on GitHub. Never enable auto-merge or run a remote PR merge command.
- `pull` changes only confirmed original/source worktrees.
- `sync` runs only in the development bundle. Fetch updates shared remote-tracking refs, but it never switches or updates the original worktree's HEAD, index, files, or local source branch. Every rotation retires its exact old local generation; after a recorded PR is exactly `MERGED`, `sync` also removes its recorded remote ref and may finish legacy local cleanup when exact completed-rotation evidence exists.

The workspace-root `.multi-repo-worktree/slots.json` is authoritative for each repository's branch family, active branch and branch generation, active branch base SHA, source branch, remote, PR generation/current PR, submission boundary/head evidence, and optional rotation journal. The transient operation lock prevents concurrent writers; it is not a slot lifecycle.

## Decision Contract

- An explicit `create`, `submit`, `pull`, or `sync` request authorizes that keyword's routine writes. `status`, inspection, and dry-run are read-only.
- An explicit `promote` request authorizes its reviewed remote fast-forward updates. Describe the selected repositories, variable source, stage path and included branch changes before execution; routine approval is not repeated. Missing branches, divergence, stale plans or rejected protected-branch pushes stop that repository without creating branches, forcing updates or bypassing protection.
- Before routine Git/remote writes, inspect the complete bundle and send a concise non-blocking summary of targets, changed paths, candidate PR states, ref/rotation changes and partial-failure behavior. Submit's `--prepare` may first write its fresh local plan; non-candidates need no remote classification. Do not ask for routine command, generated Korean text, or manifest-proven flow approval.
- Ask one consolidated question only for conflicting permanent facts, ambiguous/secret/unrelated publish content, an inseparable PR correction, destructive work outside the bounded cleanup cases below, stale-lock recovery, or a real unresolved behavior decision in a conflict. Automatically recover ordinary integration conflicts while preserving both sides' intent; do not ask merely because Git reported a conflict.
- Revalidate local, remote, manifest, and GitHub identities immediately before publication. Compatible advancement of the stored source ref may be automatically refetched and replanned before local/ref/remote publication begins.
- Never reset, clean, force checkout, force push to rewrite a branch, or amend/rebase published history. Resolve generation conflicts in the runner-owned isolated worktree using the procedure in `references/conflict-recovery.md`; never blindly choose an entire side, discard tests, or weaken assertions to get a green result. Promotion updates require independently proven fast-forward ancestry plus an exact expected-head lease; the lease rejects moved/deleted refs and never authorizes history replacement. Automatic deletion is limited to: a clean internal correction worktree and its unpublished branch after exact correction verification; an exact detached generation worktree under the workspace's dedicated multi-repo-worktree path after its journal ownership, source/index/tree/HEAD, and cleanliness are proven; an exact clean conflict worktree after its hooked resolution commit is preserved by the verified generation target; the exact old local generation during every common rotation after the target is switched and verified; and an exact merged remote PR ref during explicit `sync`. Local retirement and legacy local cleanup use a compare-and-delete local ref; remote cleanup uses an exact remote deletion lease. Active/source/moved/foreign/checked-out refs are never deleted.

## Operation Rules

- **help**: Explain the seven keywords, the permanent-worktree/generation-branch flow, and `promote` for source-to-prod branch advancement. Do not inspect without an operation request.
- **status**: Stay read-only. Show `대상 묶음`, `원본 브랜치`, and `기준 폴더`, then one repository per line as `<repo>: 폴더 <relative-folder> / 현재 브랜치 <branch> / 변경상태 <dirty|clean>`. Add branch generation, manifest PR generation/current PR, legacy-proof requirement, and rotation journal separately. Do not use a Markdown table.
- **create**: Resolve every source worktree, source branch, remote, task branch family, target path, repository set, and exact fetched source SHA. Initialize schema-5 manifest evidence before `git worktree add`. Exact retry may complete a partial creation; changed permanent facts block.
- **submit**: Classify each repository independently. Commit only clearly task-related paths with Korean messages. `NONE` first rotates from its active branch base when the fetched source advanced, then pushes its active branch and creates a Korean ready PR, recording the submitted HEAD as both continuation boundary and observed PR head. Ordinary `OPEN` never pushes local commits and reports merge waiting; an explicit correction intent uses an isolated temporary worktree and fast-forward push, preserving the continuation boundary. `MERGED` rotates when needed from the pinned remote source, then creates the next PR only if the rotated branch has a diff. Record each successful repository immediately; do not roll it back because another repository waits or fails.
- **promote**: Prepare and inspect the selected remote paths, then execute the generated plan to advance `x -> dev -> dev2 -> prod`. When `x` is `dev`, `dev2` or `prod`, begin there. An ahead target is retained and its reviewed tip becomes the next stage's input. Divergence blocks before this repository's first push. Pin all steps, use normal fast-forward pushes, verify each remote result, journal progress and resume the same plan after partial failure. Independent repository successes remain recorded.
- **pull**: In clean confirmed original worktrees, fetch the stored remote, pin its source SHA, reject divergence, and fast-forward with `git merge --ff-only`. Do not use configuration-dependent `git pull`.
- **sync**: Classify cleanliness per repository. Leave a dirty development worktree `failed/skipped unchanged`, but continue clean siblings. Fetch and pin each eligible stored remote source. `NONE` rotates unsubmitted work from the active branch base when the source advanced. `OPEN` validates and reports merge waiting without branch/file/ref rotation or deletion. An unrotated `MERGED` repository rotates from its submission continuation boundary, retiring its old local generation before manifest completion, then cleans its exact remote PR branch. An already-rotated `MERGED` repository retries remaining merged-ref cleanup before another source-advance rotation. Remote cleanup accepts squash merge because it trusts exact GitHub `MERGED` evidence and the fetched merge commit, not PR-head ancestry. Apply these rules independently in a mixed bundle.

The common rotation first proves the continuation boundary is an ancestor of the local task HEAD, then computes a 3-way result tree with that explicit merge base, the pinned source as the new base, and the local task HEAD as the work side. For a non-empty result, the agent summarizes the B-only net user outcome and task context as one subject, follows repository commit conventions, and includes Korean meaning when compatible; branch movement, correction A, source provenance, and workflow placeholders are not the work outcome. The helper checks structural validity rather than guessing natural-language quality. It journals exact source/result trees, normalized subject, SHAs, and the deterministic internal path `.multi-repo-worktree/generation-worktrees/<sha256(slot,repo,target)>.worktree` before creating `<branch-family>-mrw-g<branch-generation>`. Both manifest and Git helper reject any other path, and an existing retry worktree must be detached. It never overwrites an existing different ref and creates at most one transfer commit with the journaled exact subject directly parented by the pinned source through normal `git commit` in that isolated worktree so hooks/signing policy apply. An empty result has neither transfer commit nor subject. A retry adopts only an exact source/index or completed-commit state whose parent/tree/subject matches the journaled identity; ignored and ordinary untracked content both block. It removes only the proven clean internal worktree without force, verifies and switches to the target, then `retire_generation_branch.py` compare-deletes only the journaled old local ref before manifest completion. Normal steady state therefore has exactly one managed family ref; a failed retirement preserves at most the exact old ref plus the active target and blocks only that repository's next rotation. It creates no source-sync merge commit and performs no force push or history rewrite.

Merged remote cleanup is separate from rotation. Before a new submission replaces current PR identity, the manifest moves the prior PR/submission/rotation evidence into `pending_remote_cleanups`; therefore a later `sync` does not lose the exact old remote identity. `scripts/cleanup_merged_branch.py` re-reads the held operation and manifest locks, selects exactly one current or pending PR record, requires the active clean generation to differ from that PR head, and proves the GitHub merge commit is contained by the fetched source. The local ref is normally already absent; that is an idempotent success. For pre-retirement/legacy completed rotations where it remains, exact receipt/tree/worktree proof is still required before compare-and-delete. Remote deletion uses the exact expected-head lease, and a completed pending record is removed atomically from the manifest. A mismatch preserves every remaining ref and blocks only that repository.

`scripts/audit_generation_branches.py` is read-only. It enumerates only exact branch-family refs, uses phase-aware active identity (`rotation.target_branch` in `switched`/`retired`), protects active/source/checked-out refs, tests actual commit reachability from protected refs, and distinguishes exact journal/receipt or externally supplied MERGED-PR evidence from unresolved history. Branch name, same tree, or reflog alone never authorizes deletion. Audit output is a report only; legacy branches are never deleted by this command.

`CLOSED`, Draft, missing/inaccessible or mismatched PRs, unexpected divergent/force-pushed heads, unknown API state, ambiguous correction content, an incompatible repository commit-message policy, and incompletely proven legacy/lost history are repository-local unsupported states. The intentional difference between a local B continuation head and an explicitly recorded corrected PR head is supported, because the unchanged continuation boundary isolates B during rotation. A schema-4 in-flight subject may be backfilled only from exact Git/manifest evidence; an unsuitable subject may be recreated only before target publication from a clean detached runner-owned temporary commit. After target creation or development-worktree switch, do not rewrite it automatically. External exceptions are not added lifecycle states. A valid `NONE`, `OPEN`, or `MERGED` repository in the same bundle may still proceed. A dirty worktree or ambiguous result stops only that repository; a routine conflict prepares isolated recovery and the agent continues after reconciling and validating it. Clean successful siblings remain recorded.

## Status Output

```text
대상 묶음: worktrees/feature-etc-docs
원본 브랜치: feature-etc
기준 폴더: /home/kgh-wsl/projects/admin-product-3/worktrees/feature-etc-docs

majoong_events_api: 폴더 majoong_events_api / 현재 브랜치 feature-etc-docs-mrw-g2 / 변경상태 dirty
majoong_events_web: 폴더 majoong_events_web / 현재 브랜치 feature-etc-docs / 변경상태 clean
```

Keep `dirty` and `clean` unchanged. Prefer manifest source branches; mark inspector-only inference with `(추정)`. If mappings differ or remain uncertain, show `원본 브랜치: 확인 필요`. Read-only status never migrates manifest evidence.
