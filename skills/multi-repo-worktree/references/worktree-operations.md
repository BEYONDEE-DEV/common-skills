# Worktree Operations

## Contents

- Keyword authorization and decision contract
- Roles and generation-branch flow
- Permanent slot manifest
- Read-only inspection and status
- Complete-bundle preflight
- Create
- Submit common phase
- Submit NONE
- Submit OPEN and explicit correction
- Submit MERGED
- Common generation rotation
- Retry, legacy recovery, and partial results
- Pull
- Sync
- Promote

## Keyword Authorization And Decision Contract

Expose only `help`, `status`, `create`, `submit`, `promote`, `pull`, and `sync`. Do not normalize or advertise removed `recycle`, `integrate`, `merge`, `merge-back`, or `sync-derived` commands.

An explicit write-keyword request authorizes that keyword's routine writes. `status`, inspection, or dry-run stays read-only. Before routine writes:

1. Inspect every participating repository and exact GitHub state without mutation.
2. Resolve stored source branch, remote, branch family, active branch/generation/base, submission evidence, PR, and rotation journal per repository.
3. Preflight the complete bundle and classify each repository independently.
4. Send one concise non-blocking summary of targets, changed paths, PR classifications, fetch/ref changes, rotation behavior, conflict behavior, and possible partial results.
5. Re-inspect relevant identities immediately before fetch, branch/ref, manifest, or remote publication.
6. Execute and record repository-local results. A repository-local wait/conflict/failure does not roll back or suppress a valid sibling.

Do not ask for routine commands, task-related path lists, generated Korean text, or manifest-proven transitions. Ask one consolidated question only for conflicting permanent facts, ambiguous/secret/unrelated content, an inseparable PR correction, stale-lock recovery, or conflict recovery.

Never reset, clean, force checkout, force push to rewrite a branch, rewrite published history, amend, stash, or auto-resolve conflicts. `promote` uses an exact expected-head update lease only after independently proving fast-forward ancestry; it never rewrites history or recreates a deleted branch. A clean internal correction worktree and its unpublished temporary local branch may be removed after exact remote and manifest verification. A journaled internal generation worktree may be populated from the exact result tree and removed without force only after its source/index/tree/HEAD are proven. Every common rotation retires only its journal-proven old local generation after the target is switched and verified. During explicit `sync`, an exact merged remote PR ref may also be removed; a legacy local ref may be removed only with exact completed-rotation proof. The cleanup remote operation is an expected-head deletion lease, not permission to update or rewrite a branch. These are bounded routine cleanups, not slot release.

## Roles And Generation-Branch Flow

```text
create once at fetched origin/source SHA
  -> develop A
  -> submit ready PR A and record its submitted HEAD boundary
  -> keep the same slot/worktree and develop B locally without pushing it
  -> user squash-merges PR A on GitHub
  -> sync or next submit fetches origin/source in the development worktree
  -> 3-way B's net tree change onto a fresh branch parented by the fetched SHA
  -> switch the same development worktree to that generation branch
  -> submit ready PR B
```

- **Development user**: creates one permanent bundle, develops, runs repeated `submit`, and may run development-only `sync`.
- **Original user**: runs `pull` only in original/source worktrees.
- **GitHub user**: reviews and manually merges ready PRs. The skill never merges them.
- **Promotion user**: requests `promote` after changes reach the remote source branch. This advances that repository's variable source through `dev -> dev2 -> prod` using normal fast-forward pushes and independent per-stage progress. Read `promote-runner.md` for the dedicated runner; PR handling remains with submit/GitHub.

There is no slot freeze, release, reassignment, or task-start `create`. The slot and worktree persist while active task branches rotate. Git cannot infer whether a local commit is a PR correction or future work, so ordinary OPEN submit never pushes local-ahead commits. Corrections require explicit intent and content separation.

## Permanent Slot Manifest

`<workspace-root>/.stageflow-worktrees/slots.json` uses schema 5:

```json
{
  "schema_version": 5,
  "slots": {
    "slot-1": {
      "path": "/absolute/worktrees/slot-1",
      "repositories": {
        "service-a": {
          "branch_family": "feature-etc-docs",
          "branch": "feature-etc-docs",
          "branch_generation": 1,
          "branch_base_sha": "1111111111111111111111111111111111111111",
          "source_branch": "feature-etc",
          "remote": "origin",
          "generation": 1,
          "pr": "https://github.com/example/service-a/pull/17",
          "submission": {
            "generation": 1,
            "head_branch": "feature-etc-docs",
            "continuation_boundary_sha": "2222222222222222222222222222222222222222",
            "observed_head_sha": "2222222222222222222222222222222222222222"
          }
        }
      }
    }
  }
}
```

- `branch_family` is permanent naming input. The initial branch may equal the family; every rotation target is exactly `<branch-family>-stageflow-g<branch-generation>`.
- `branch_generation` counts local branch rotations independently of PR `generation`.
- `branch_base_sha` is the remote source commit on which the active branch was last prepared. It is the explicit base for an unsubmitted or already-rotated branch.
- PR generation `0` requires `pr: null` and `submission: null`. Positive PR generations require both a PR and exact submission evidence.
- `continuation_boundary_sha` is fixed for that submitted PR even if an isolated correction advances `observed_head_sha`. It identifies the local tree before future work began.
- `rotation` is present only during a generation rotation and is a crash journal, not a lifecycle state. Its phase is `planned`, `branch-created`, `switched`, or `retired`. Before `switched`, the top-level branch is effective active; in `switched`/`retired`, the exact journal target is effective active while the top-level binding intentionally remains the old source until completion.

Example journal:

```json
{
  "phase": "planned",
  "from_branch": "feature-etc-docs",
  "from_branch_generation": 1,
  "from_head_sha": "3333333333333333333333333333333333333333",
  "boundary_sha": "2222222222222222222222222222222222222222",
  "source_sha": "4444444444444444444444444444444444444444",
  "source_tree_sha": "6666666666666666666666666666666666666666",
  "target_branch": "feature-etc-docs-stageflow-g2",
  "target_branch_generation": 2,
  "result_tree_sha": "5555555555555555555555555555555555555555",
  "transfer_subject": "feat(events): 이벤트 필터 상태 보존",
  "temporary_worktree": "/workspace/.stageflow-worktrees/generation-worktrees/<sha256-slot-repo-target>.worktree"
}
```

Use the helper relative to the loaded skill directory:

```bash
python3 "<skill-dir>/scripts/slot_manifest.py" --root "<workspace-root>" status

python3 "<skill-dir>/scripts/slot_manifest.py" --root "<workspace-root>" \
  initialize --slot "<slot>" --path "<slot-path>" \
  --repository "<repo>" "<branch-family>" "<source>" "<remote>" "<fetched-source-sha>"

python3 "<skill-dir>/scripts/slot_manifest.py" --root "<workspace-root>" \
  record-batch --slot "<slot>" --token "<token>" \
  --repository-update "<repo>" "<current-pr-generation>" "<new-pr>" \
  "<active-head-branch>" "<submitted-head-sha>" "<submitted-head-sha>"

python3 "<skill-dir>/scripts/slot_manifest.py" --root "<workspace-root>" \
  record-correction --slot "<slot>" --token "<token>" \
  --repository-correction "<repo>" "<generation>" "<pr>" \
  "<expected-observed-head>" "<new-observed-head>"
```

`record-batch`, migration, correction, and every rotation-journal mutation require the exact operation lock. Call them once per successful repository so a later sibling failure cannot erase completed evidence. Exact retries are idempotent; conflicting values fail without partial writes.

Schema 2/3/4 status is promoted in memory to a lossless schema-5 envelope with `legacy_schema`, without changing file bytes during status. New manifests start at schema 5. Initializing a new slot in an existing legacy file may atomically serialize the envelope, but it preserves every other repository's legacy marker and grants no generation-write eligibility. Before an existing repository's first semantic write, hold that slot's operation lock and prove its exact evidence. Use `migrate-batch` for branch/generation/submission evidence and schema-4 repositories without an active rotation. Never guess from a branch name or latest PR number. Generation 0 uses `-` for PR and submission fields; a positive generation requires exact values:

```bash
python3 "<skill-dir>/scripts/slot_manifest.py" --root "<workspace-root>" \
  migrate-batch --slot "<slot>" --token "<token>" \
  --repository-migration "<repo>" "<generation>" "<pr-or-dash>" \
  "<family>" "<branch-generation>" "<base-sha>" \
  "<head-or-dash>" "<boundary-or-dash>" "<observed-or-dash>"

python3 "<skill-dir>/scripts/slot_manifest.py" --root "<workspace-root>" \
  reconcile-batch --slot "<slot>" --token "<token>" \
  --repository-recovery "<repo>" "<current-generation>" "<current-pr>" \
  "<recovered-pr>" "<recovered-head-branch>" \
  "<recovered-boundary-sha>" "<recovered-observed-head-sha>"
```

## Read-Only Inspection And Status

Run the inspector and manifest status before a write. For GitHub state use exact repository identity:

```bash
gh pr view "<recorded-pr>" --repo "<owner/repo>" \
  --json number,state,isDraft,headRefName,headRefOid,baseRefName,createdAt,mergedAt,mergeCommit,url
```

Primary status format:

```text
대상 묶음: worktrees/<bundle-name>
원본 브랜치: <source-branch (추정)|확인 필요>
기준 폴더: <absolute-bundle-folder>

<repo>: 폴더 <relative-folder> / 현재 브랜치 <branch> / 변경상태 <dirty|clean>
```

Add manifest PR/branch generation and a held operation lock or rotation journal as secondary lines. During `switched` or `retired`, report the journal target as effective active and the top-level branch binding as pending completion; never present the old top-level branch as current. An absent lock is normal. Do not use a Markdown table. Status never enriches legacy evidence.

## Complete-Bundle Preflight

Before fetch or another write, inspect all repositories. A bundle-global identity/path/lock ambiguity blocks the operation. Repository-local unsupported state blocks only that repository when siblings are independently proven.

Require:

- manifest path and repository mapping match the requested bundle;
- every active development branch equals its manifest branch and is not detached;
- no merge, rebase, cherry-pick, revert, or bisect is in progress;
- cleanliness is classified per repository: a dirty `sync` worktree is reported `failed/skipped unchanged`, while clean siblings continue; `submit` dirt is limited to clearly task-related literal paths;
- stored remote identifies the expected repository and source ref;
- no branch is checked out in an unexpected worktree;
- no different operation-lock token is present;
- current PR is exact base/head, ready, and classified `OPEN` or `MERGED`; `CLOSED`, Draft, missing, and unknown states are repository-local blockers;
- a present rotation journal matches actual refs, HEAD, target tree, and phase before any resume.

Fetch updates shared remote-tracking refs and is a Git write. Inspect first, then fetch every eligible repository, pin `refs/remotes/<remote>/<source-branch>`, and never use a moving symbolic name in later rotation commands.

## Create

Use `create` only for first initialization or exact retry.

Resolve each clean source worktree, checked-out source branch, repository-matching remote, complete repo set, target slot path, child path, initial branch family, and exact source SHA. Fetch and pin the remote source before initialization. Initialize the complete binding before the first worktree add:

```bash
python3 "<skill-dir>/scripts/slot_manifest.py" --root "<workspace-root>" \
  initialize --slot "<slot>" --path "<slot-path>" \
  --repository "<repo-a>" "<family-a>" "<source-a>" "<remote-a>" "<source-sha-a>" \
  --repository "<repo-b>" "<family-b>" "<source-b>" "<remote-b>" "<source-sha-b>"

git -C "<source-worktree>" worktree add -b "<branch-family>" \
  "<target-path>" "<pinned-source-sha>"
```

Exact retry leaves correct clean results and fills only missing worktrees. A changed path, repository set, family, remote, source, SHA, dirty partial worktree, or unexpected existing ref blocks. Later work never invokes `create`.

## Submit Common Phase

For ordinary submit, use `scripts/submit_bundle.py` following `submit-runner.md`; the sections below define its invariants and the exceptional/manual correction path, not a second sequence to execute after the runner. Its preflight replaces separate inspector/manifest/PR queries. Publication diff verification is centralized in `scripts/verify_pr.py`.

Acquire the slot operation lock only after read-only classification. Classifications belong to repositories, not the batch:

- `NONE`: generation 0, no PR/submission, no conflicting open PR for active branch.
- `OPEN`: recorded PR is OPEN, ready, exact base/head, and its head OID equals manifest `observed_head_sha`.
- `MERGED`: recorded PR is MERGED with merge commit/time and exact base/head/head OID.

Allow any mix. An OPEN repository waiting behind its PR is a successful wait, not a reason to block a NONE or MERGED sibling.

Inspect staged, unstaged, and non-ignored untracked content. In `submit`, stage only exact task paths and use Korean commit text:

```bash
git -C "<development-worktree>" add -A -- ":(literal)<task-path-1>" ":(literal)<task-path-2>"
git -C "<development-worktree>" diff --cached --check
git -C "<development-worktree>" commit -m "<generated-Korean-commit-message>"
```

Before staging, reject any unrelated already-staged paths: plain `git commit` includes the entire index. Include both sides of a rename in the reviewed path set, but do not re-add an old name already deleted from the index. The runner enforces these checks and binds the worktree/index fingerprint to the reviewed plan. Never use `git add .`, amend, stash, reset, force-add, or bypass hooks for user task commits. In an ordinary submit, clearly future-work changes in an OPEN repository may be committed locally with Korean text under the same task-path rules, but they are never pushed while that PR remains open. Explicit correction mode first separates correction content from future work and never stages the combined development tree as a correction.

## Submit NONE

Fetch and pin the stored remote source. If it differs from `branch_base_sha`, run the common generation rotation exactly as NONE `sync` before publication; this keeps first submission based on the currently inspected remote source without merging or rewriting the existing local branch. Then require the active branch base SHA to be the pinned source, prove the local diff contains only task work, push without force, and create a Korean ready PR:

```bash
git -C "<development-worktree>" push "<remote>" \
  "HEAD:refs/heads/<active-branch>"
gh pr create --repo "<owner/repo>" --base "<source-branch>" \
  --head "<active-branch>" --title "<generated-Korean-title>" \
  --body "<generated-Korean-body>"
```

Do not pass `--draft`. Verify OPEN, ready, exact base/head and head SHA, plus exact changed-files patch. Then immediately call `record-batch` for that repository with the submitted HEAD as both continuation boundary and observed head. A created-but-unrecorded retry adopts only one exact OPEN PR matching base/head/SHA.

## Submit OPEN And Explicit Correction

Ordinary OPEN submit never pushes the local active branch. After the common phase has optionally committed clearly task-related future work locally, it fetches/queries exact PR and remote refs, reports `PR 병합 대기`, and performs no further branch/index/file/commit mutation. Remote PR head, PR generation, and continuation boundary remain unchanged.

Only an explicit correction intent enters correction mode. Before writing, require the user-provided/inspected correction paths or patch to be semantically separable from future work. If a path contains both, ask one decision and do not guess.

1. Re-fetch and require remote PR head equals manifest `observed_head_sha`.
2. Create a unique internal local branch/worktree from that observed head, outside the development bundle but under `.stageflow-worktrees/corrections/`.
3. Apply only the proven correction patch, inspect it, commit with Korean text, and verify the development worktree HEAD/tree remains byte-for-byte unchanged.
4. Push the correction commit as a normal fast-forward to the existing remote PR head ref. Never force push.
5. Verify the same PR remains OPEN/ready and points to the new SHA, then call `record-correction`. This changes only `observed_head_sha`; it never changes `continuation_boundary_sha`.
6. After remote and manifest verification, remove only the clean internal correction worktree and its unpublished local temporary branch. If cleanup fails, report the exact leftover; do not alter the development branch.

If the remote head advances concurrently, do not push or force. Re-fetch, discard no work, and report a retry. A retry may adopt an already-pushed exact correction only when the PR head, commit parent, patch, development tree, and manifest boundary prove it uniquely.

## Submit MERGED

Commit clearly task-related future work first. Fetch and pin the stored remote source. Determine whether rotation is required:

- If active `branch == submission.head_branch`, this is the unrotated submitted branch. Use `submission.continuation_boundary_sha` as the explicit merge base.
- If active branch differs, it has already rotated. Use `branch_base_sha`; rotate again only when the pinned source advanced.

Run the common rotation before the first remote publication. After rotation, validate `pinned-source...active-HEAD` contains only future work. If empty, do not push or create a PR; the fresh branch at pinned source remains a successful synchronized no-op. Otherwise follow Submit NONE's push/create/verify flow and immediately record the new PR generation with the new submitted HEAD boundary.

Old submitted branch names and remote PR refs are not reused. Rotation during `submit` retires its exact old local generation, but `submit` never deletes the remote PR ref. Before recording the new PR, `record-batch` durably moves the replaced PR/submission/rotation identity into `pending_remote_cleanups`. An explicit later `sync` selects that exact pending record, performs merged remote cleanup after proving the continuation is preserved, and removes only the completed record. Multiple pending records remain independently addressable instead of being overwritten by the newest submission.

## Common Generation Rotation

Rotation is shared by eligible `NONE`, `MERGED`, and already-rotated unsubmitted branches. It never updates an original worktree.

Preconditions:

- development worktree is clean and its HEAD/branch equal inspected manifest values;
- boundary, local HEAD, and pinned source objects exist;
- target generation is `branch_generation + 1` and target name is exactly `<branch-family>-stageflow-g<target-generation>`;
- no in-progress journal conflicts with the requested transition.

First compute the result tree without changing refs, index, worktree, or current branch:

```bash
python3 "<skill-dir>/scripts/prepare_generation_branch.py" analyze \
  --repo "<development-worktree>" --from-head "<local-head-sha>" \
  --boundary "<boundary-sha>" --source "<pinned-source-sha>" \
  --branch-family "<branch-family>" --target-generation "<next-generation>"
```

The helper runs a 3-way tree merge with explicit base=`boundary`, ours=`pinned source`, theirs=`local HEAD`. A conflict returns failure before a target ref or worktree change. Do not resolve automatically.

The analyze result includes both `source_tree_sha` and `result_tree_sha`. If they match, record `transfer_subject: null`, omit `--message`, and create no transfer commit. Otherwise inspect the B-only net diff and task context and choose one concise subject for the user-visible outcome. Summarize multiple B commits as one result; describe rename/delete by the resulting behavior; exclude correction A, branch/base movement, and source provenance. Follow the repository's required commit syntax first and include Korean meaning when compatible. If repository policy and the intended meaning cannot coexist, block only that repository and ask the user; never bypass hooks. Do not add source/boundary trailers because the manifest owns that provenance.

Before journaling, reject CR/LF, ASCII controls, blank values, and known unresolved workflow placeholders, then trim only outer ASCII spaces. The helper does not use regex to judge the natural-language meaning, Korean content, or repository convention. The normalized subject is part of rotation retry identity, so a different subject is not an exact retry even when the result tree is unchanged.

Derive the one allowed temporary path as `<workspace-root>/.stageflow-worktrees/generation-worktrees/<sha256(slot NUL repo NUL target)>.worktree`. Use lowercase SHA-256 hex and the `.worktree` suffix. This path is outside user repositories and is owned only by this exact slot/repository/target rotation. Journal the exact plan before ref mutation; `begin-rotation` rejects every other or non-normalized path:

```bash
python3 "<skill-dir>/scripts/slot_manifest.py" --root "<workspace-root>" \
  begin-rotation --slot "<slot>" --token "<token>" \
  --repository-rotation "<repo>" "<from-branch>" "<from-generation>" \
  "<from-head>" "<boundary>" "<source>" "<source-tree>" \
  "<target>" "<target-generation>" "<result-tree>" \
  "<task-specific-transfer-subject-or-dash>" \
  "<deterministic-absolute-temporary-worktree>"
```

Then create without overwriting an existing ref:

```bash
python3 "<skill-dir>/scripts/prepare_generation_branch.py" create \
  --repo "<development-worktree>" --source "<pinned-source-sha>" \
  --source-tree "<source-tree-sha>" \
  --result-tree "<result-tree-sha>" --branch-family "<branch-family>" \
  --target-generation "<next-generation>" \
  --message "feat(events): 이벤트 필터 상태 보존" \
  --temporary-worktree "<deterministic-absolute-temporary-worktree>" \
  --workspace-root "<workspace-root>" --slot "<slot>" --repository "<repo>"
```

For an empty result, omit `--message`; target points directly at source. `create` and `verify` require the journaled `--source-tree` and compare it with the actual `source^{tree}` before adopting or publishing a target. Otherwise the helper creates exactly one transfer commit whose sole parent is source, tree is the computed result, and `%s` equals the journaled subject. It independently recomputes the deterministic workspace/slot/repository/target path, rejects traversal or any original/development/other worktree path, requires a registered retry to have detached HEAD, restores the exact result tree there, and uses normal `git commit`, so configured commit hooks and signing policy run. A retry recognizes only these exact states at that path: clean source HEAD with source/result index, or a clean one-parent completed result commit with the exact subject. Any unregistered existing path, missing registered worktree, attached branch, unexpected index, unstaged content, ignored or ordinary untracked content, HEAD, tree, parent, source-tree evidence, or subject blocks without overwrite. If a commit hook changes the subject, preserve the temporary state, publish no target ref, report the mismatch, and do not loop by automatically recreating it. The helper removes only a proven clean temporary worktree without force before publishing the target ref; a rejected hook or cleanup failure publishes no target ref and reports the journaled path. Run the repository's required validation on the switched target before any push. Advance journal `planned -> branch-created` with `--target-head-sha <created-or-verified-target-sha>` so the exact target becomes durable before any old-ref deletion. A legacy schema-5 `branch-created` retry without that field must repeat this same transition to bind it before switching. Run helper `verify` with the same `--source-tree` and, for a non-empty result, the same `--message`, switch the same development worktree with `git switch <target>`, verify clean HEAD/tree and original-worktree invariants, then advance `branch-created -> switched`.

While the exact operation lock remains held, retire only the journal's `from_branch`:

```bash
python3 "<skill-dir>/scripts/retire_generation_branch.py" \
  --root "<workspace-root>" --slot "<slot>" --repository "<repo>" \
  --token "<held-operation-lock-token>" --execute
```

The retirement helper holds the manifest lock, re-reads the authoritative manifest and operation lock, derives the bound repository, treats the target as effective active, and requires its exact clean checkout, ref, commit parent/subject, source tree, and result tree. Before deletion it durably binds the exact target SHA in the rotation journal. It protects the source and target names, scans all worktree checkouts, and requires the old ref to equal `from_head_sha`. One `git update-ref --stdin` transaction verifies the persisted target ref and compare-deletes the old ref; no standalone phase command can mark retirement. Only after deletion or an exact absent retry does the helper durably record `retired`. An absent old ref in `switched` is the exact retry state for a crash after deletion; a moved, foreign, checked-out, dirty, or mismatched ref is preserved. Do not start another rotation for that repository until phase reaches completion. Clean siblings continue independently.

Only after phase `retired`, complete with the exact verified target commit and result-tree evidence:

```bash
python3 "<skill-dir>/scripts/slot_manifest.py" --root "<workspace-root>" \
  complete-rotation --slot "<slot>" --token "<token>" \
  --repository "<repo>" --target-branch "<target>" \
  --target-generation "<target-generation>" --source-sha "<pinned-source-sha>" \
  --target-head-sha "<verified-target-sha>" --result-tree-sha "<result-tree-sha>"
```

Completion sets active branch/generation/base to target/next/source, stores an exact `last_rotation` receipt containing the source branch/head plus target commit and result tree, and removes only the journal. Older schema-5 receipts without source fields remain readable but cannot authorize local submitted-branch deletion. A retry without a journal succeeds only when every supplied target value exactly matches that receipt; it never treats an unrelated no-journal state or a moved target ref as completed.

Every common rotation retires its exact old local generation, regardless of `NONE`, `MERGED`, submit, sync, or an already-rotated unsubmitted state. The submitted remote ref is separate: for `MERGED` sync, keep submission and `last_rotation` evidence intact until remote cleanup confirms it absent. The cleanup helper remains backward-compatible with a legacy local ref that predates mandatory retirement.

Crash retry rules:

- `planned` + absent target and absent journaled temporary path: run create.
- `planned` + exact registered temporary source/result state: resume restore/commit/clean removal, then create the target ref.
- `planned` + exact clean completed transfer commit in the journaled temporary worktree: adopt it, remove that worktree without force, then create the target ref.
- `planned` + exact parent/tree/subject target: adopt it and advance.
- `branch-created` + old branch checked out: verify and switch.
- `branch-created` + exact target already checked out: treat this as a crash after `git switch`; verify target commit/tree and clean worktree, then adopt `branch-created -> switched`.
- `switched` + exact target checked out + exact old ref: compare-delete old ref, then advance to `retired`.
- `switched` + exact target checked out + absent old ref: treat this as a crash after compare-delete and advance to `retired`.
- `retired` + exact target checked out + absent old ref: complete manifest only.
- a target with a different tree/parent, unexpected checked-out branch, changed source/head/boundary, or impossible phase blocks without overwrite or deletion.

At steady state, a newly managed repository has one local branch-family ref: the effective active generation. The only accepted transient extra is the exact journaled `from_branch` while retirement is pending. Existing historical refs are not retroactively treated as managed transient refs and are never bulk-deleted.

For read-only legacy analysis, run:

```bash
python3 "<skill-dir>/scripts/audit_generation_branches.py" \
  --root "<workspace-root>" --slot "<slot>"
```

Optionally pass repeated `--merged-pr-evidence "<repo>" "<pr>" "<branch>" "<head-sha>"` values only after an exact external MERGED query. The audit enumerates exact family names, classifies active/source/checked-out protection and actual ancestry from protected refs, recognizes exact current rotation or last-rotation source evidence, and otherwise reports unresolved history. Names, same-tree results, and reflogs are context only and never safety proof. The audit changes no ref, worktree, manifest, lock, remote, or GitHub state.

## Retry, Legacy Recovery, And Partial Results

Git and GitHub writes are not one transaction. Report `completed`, `waiting`, `failed`, and `untouched` repositories plus exact retry state. Never claim automatic rollback.

Record each successful PR, correction, migration, or rotation phase immediately under the operation lock. A retry re-reads manifest/journal and actual refs rather than repeating earlier assumptions. Release only the operation lock after no mutation is active; never release a slot. A stale lock requires read-only reconciliation and one explicit unlock decision.

Legacy schema 2/3 and omitted PR history may be recovered only when exact GitHub PR base/head/head SHA, local/remote refs, source inclusion, merge parents, and tree comparisons yield one result. For an existing source-sync merge commit, prove its exact source parent is contained by the current remote source and that the source-parent-to-local-HEAD tree delta is only next work; use that proven source parent as the one-time effective rotation boundary. Keep the original submitted boundary in submission evidence. This avoids treating changes introduced by the legacy source merge as future work. With two candidates, missing objects, a rewritten PR head outside an explicitly recorded isolated correction, or an unexplained tree, block that repository. After a recorded correction, the local continuation head containing B and the corrected remote PR head are intentionally divergent; this is supported because transfer always uses the unchanged continuation boundary and exact observed PR head evidence. Do not ask the user to edit JSON or bypass the skill.

For a schema-4 active rotation, first choose the B-only subject, acquire the slot operation lock, re-read manifest phase, and run `inspect-legacy`. It reports the actual target and deterministic temporary worktree parent/tree/cleanliness/subject without mutation. If its actual subject is suitable, atomically backfill the exact old identity plus `source_tree_sha` and the normalized subject:

```bash
python3 "<skill-dir>/scripts/prepare_generation_branch.py" inspect-legacy \
  --repo "<development-worktree>" --source "<pinned-source-sha>" \
  --result-tree "<result-tree-sha>" --branch-family "<branch-family>" \
  --target-generation "<next-generation>" \
  --temporary-worktree "<deterministic-absolute-temporary-worktree>" \
  --workspace-root "<workspace-root>" --slot "<slot>" --repository "<repo>"

python3 "<skill-dir>/scripts/slot_manifest.py" --root "<workspace-root>" \
  migrate-rotation-subject --slot "<slot>" --token "<token>" \
  --repository-subject "<repo>" "<from-branch>" "<from-generation>" \
  "<from-head>" "<boundary>" "<source>" "<source-tree>" \
  "<target>" "<target-generation>" "<result-tree>" \
  "<task-specific-transfer-subject-or-dash>" \
  "<deterministic-absolute-temporary-worktree>"
```

If the old temporary commit has an unsuitable mechanical subject, discard it only when the re-read phase is `planned`, target ref is absent, and `inspect-legacy` proves the deterministic Stageflow-owned worktree is detached, exact, and clean:

```bash
python3 "<skill-dir>/scripts/prepare_generation_branch.py" discard-legacy-temporary \
  --repo "<development-worktree>" --source "<pinned-source-sha>" \
  --result-tree "<result-tree-sha>" --branch-family "<branch-family>" \
  --target-generation "<next-generation>" \
  --temporary-worktree "<deterministic-absolute-temporary-worktree>" \
  --workspace-root "<workspace-root>" --slot "<slot>" --repository "<repo>" \
  --expected-phase planned --expected-subject "<actual-old-subject>" \
  --token "<held-operation-lock-token>"
```

The discard helper reads the authoritative manifest itself, verifies the exact held operation-lock token, persisted schema-4 journal and `planned` phase, manifest-bound repository path, development HEAD/branch/cleanliness, absent target, and exact temporary commit before removal. Then run `migrate-rotation-subject` with the confirmed new subject and resume `create`. The migration command independently reads the manifest-bound Git repository under the same operation lock and compares actual `source^{tree}`, phase, development HEAD/branch, target ref, temporary state, and actual subject before removing the legacy marker. A crash after discard leaves the old journal plus an absent temp and safely repeats inspection; a crash after backfill makes the schema-5 subject authoritative. A dirty/untracked/attached/foreign temp is never removed. If the target exists or phase is `branch-created`/`switched`, an unsuitable subject blocks that repository without migration, amend, ref replacement, force push, or history rewrite.

For omitted PR discovery, query all pages for the exact repository/base and deterministic branch family/generation candidates; never choose merely the highest PR number. Reconciliation must record full recovered submission evidence, not only a PR number.

## Pull

`pull` runs only from the manifest-bound original repository set with each source branch already checked out. Require every original worktree clean and free of in-progress operations. Fetch and pin every stored remote source, reject divergence, then:

```bash
git -C "<original-worktree>" merge --ff-only "<pinned-source-sha>"
```

Do not use plain `git pull`, merge commits, rebase, reset, branch switching, or development-worktree changes. A no-op at the pinned SHA succeeds.

## Sync

For ordinary sync, use `scripts/sync_bundle.py` following `sync-runner.md`. It owns scoped preflight, operation locking, per-repository fetch/classification, common rotation, exact current/pending merged-ref cleanup, and compact timing/retry results. The rules below are its invariants and exceptional recovery reference, not a second manual command sequence. A pending unrecorded submit publication must be recovered by submit before sync may rotate or clean that repository.

`sync` runs only in the requested development bundle. It never invokes `pull` and never changes the original worktree's checked-out state. Classify cleanliness per repository: leave each dirty repository unchanged and continue with every independently clean sibling. Fetch and pin each eligible repository's stored remote source, then apply repository-local state rules:

- **NONE**: use `branch_base_sha` as boundary. If pinned source equals branch base, no-op. If advanced, rotate net unsubmitted work to the next local branch generation; do not push or create a PR.
- **OPEN**: verify exact PR/base/head and fetch source, then report `PR 병합 대기`. Do not rotate, switch, stage, commit, push, or mutate submission evidence even when source advanced.
- **MERGED, not yet rotated** (`branch == submission.head_branch`): use `submission.continuation_boundary_sha`, rotate future work onto pinned source, retire the old local generation, complete the rotation receipt, then clean the exact remote submitted ref.
- **MERGED, already rotated** (`branch != submission.head_branch`): clean or retry cleanup of the submitted branch first. Only after both old refs are absent, use `branch_base_sha` and rotate again if pinned source advanced.
- **mixed bundle**: run each rule independently. A waiting or dirty OPEN repository does not stop an eligible clean NONE/MERGED sibling. A dirty repository or repository-local 3-way conflict is reported `failed/skipped unchanged` and does not undo recorded sibling success.

Dirty `sync` does not stash or make a WIP commit. Report the dirty paths unchanged. After the relevant PR is merged, a later `submit` may commit clearly task-related paths with Korean text and call the same common rotation.

### Cleanup Of Submitted Branches

This cleanup is part of explicit `sync`, never `status`, `pull`, ordinary `OPEN`, or a read-only inspection. Immediately before it, query the recorded PR again with the exact repository and obtain `number`, `url`, `state`, `isDraft`, `baseRefName`, `headRefName`, `headRefOid`, `mergedAt`, and `mergeCommit.oid`. Require non-draft `MERGED`, exact manifest base/head/head SHA, a non-null merge time/commit, and the fetched source ref to contain that merge commit. Do not require the PR head commit itself to be an ancestor of source: a squash merge intentionally does not have that ancestry.

After the rotation receipt is complete and while the exact slot operation lock remains held, run:

```bash
python3 "<skill-dir>/scripts/cleanup_merged_branch.py" \
  --root "<workspace-root>" --slot "<slot>" --repository "<repo>" \
  --token "<held-operation-lock-token>" \
  --github-number "<number>" --github-url "<url>" \
  --github-state MERGED --github-is-draft false \
  --github-base "<baseRefName>" --github-head "<headRefName>" \
  --github-head-sha "<headRefOid>" --github-merged-at "<mergedAt>" \
  --github-merge-commit "<mergeCommit.oid>" --execute
```

The helper derives the repository path, remote, source, active branch, and submitted branch from exactly one current or `pending_remote_cleanups` record in the slot manifest. It requires a clean exact active development branch and no active rotation journal. The old local ref is normally absent because rotation already retired it. When a pre-retirement/legacy local ref exists, it must still equal that cleanup record's exact `last_rotation.from_head_sha`, be absent from all worktree checkouts, and reproduce the completed rotation tree; otherwise it is preserved. It separately requires the remote ref, when present, to equal the cleanup record's observed PR head. A corrected remote PR head and the local B-continuation head may differ and are checked against their separate manifest values. After both refs are absent, a pending record is removed while the manifest lock remains held; current submission identity is never cleared by cleanup.

Deletion order is remote then local. The remote deletion uses only `--force-with-lease=refs/heads/<head>:<observed-head>` with a delete refspec, and the local deletion uses `git update-ref -d refs/heads/<head> <observed-local-head>`; never use blind `git push --delete`, `git branch -D`, or an unleased deletion. Re-read actual refs after each command. If GitHub already deleted the remote branch, continue with the locally proven branch. If one ref was deleted before a crash, the next `sync` treats it as an exact idempotent partial state. If a ref advanced, became checked out, the active tree moved, or proof is missing, preserve every remaining ref, report the repository-local blocker, and do not perform another rotation.

## Promote

Read `promote-runner.md` and use `scripts/promote_bundle.py` for an explicit `promote` request. It advances the manifest-bound remote source `x` through `dev -> dev2 -> prod`, skips preceding stages when `x` is already one of those branches, and preserves local worktrees and PR state. Preparation pins all branch inputs and reviews every fast-forward; execution journals and verifies each normal push. An ahead stage is retained with its additional commits included in the next stage's review. Missing branches, divergence, protection rejection or unexpected ref movement stop only that repository. Completion means the planned commits are observed on remote `prod`, not that any server deployment has finished.
