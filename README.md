# Stageflow Plugin

Stageflow is a Codex plugin for request definition, implementation planning, implementation review, and atomic project documentation workflows.

It also bundles Simple Workflow as a lightweight `plan.md`-centered workflow alongside the full Stageflow flow.

## Installation

The plugin name is `stageflow`, and the marketplace name is `stage-flow`.

### Method 1: Use a Local Clone

Use this when you want to inspect or edit the plugin source locally.

```bash
git clone git@github.com:BEYONDEE-DEV/stage-flow-plugin.git
cd stage-flow-plugin

codex plugin marketplace add "$(pwd)"
codex plugin add stageflow@stage-flow
```

If you prefer HTTPS:

```bash
git clone https://github.com/BEYONDEE-DEV/stage-flow-plugin.git
cd stage-flow-plugin

codex plugin marketplace add "$(pwd)"
codex plugin add stageflow@stage-flow
```

### Method 2: Use the Git Marketplace Directly

Use this when you want to install the plugin without keeping a local clone.

```bash
codex plugin marketplace add git@github.com:BEYONDEE-DEV/stage-flow-plugin.git --ref main
codex plugin add stageflow@stage-flow
```

If you prefer HTTPS:

```bash
codex plugin marketplace add https://github.com/BEYONDEE-DEV/stage-flow-plugin.git --ref main
codex plugin add stageflow@stage-flow
```

## Verify Installation

```bash
codex plugin list
```

You should see:

```text
stageflow@stage-flow  installed, enabled
```

## Included Workflows

- Stageflow: use `Stageflow`, `stageflow`, `stage-flow`, or `.stageflow`.
- Simple Workflow: use `Simple Workflow`, `simple-workflow`, or `.simple`.
- Atomic Docs: use `atomic-docs`.
- Multi Repo Worktree: use `$stageflow:multi-repo-worktree` for permanent development bundles, PR submission, synchronization, and branch promotion.

For an existing slot, `$stageflow:multi-repo-worktree promote` advances each selected repository's remote source branch through `dev → dev2 → prod`. The source is taken from that repository's slot configuration; a source already on this path skips prior stages. The command prepares a reviewable snapshot, uses fast-forward pushes, and records partial progress for retry while local development continues. It finishes at remote `prod` branch reflection. Divergent histories require separate integration; existing branch protection still applies.

### Simple Workflow Roles

Simple Workflow defaults to `gpt-6-astra` as the main planner/coordinator at its existing effort and dispatches approved write work to a separate `gpt-6-astra` implementation subagent with explicit `reasoning_effort: "low"` (Astra Light). Independent reviewers inherit the main agent's model and effort; they do not use the implementation worker's Light setting. The main agent owns the plan, one independent plan challenge, user approval, Goal and workflow metadata, and final review; the implementation worker owns bounded code changes and affected tests, retaining its model/effort through fix and revalidation rounds.

The skill cannot switch the model of an already-running main task. Select Astra when starting the task if that exact main role is required. Explicit user role-specific model/effort choices take precedence, and unavailable or unverifiable settings are reported rather than silently substituted. Plan-only and read-only requests do not dispatch the implementation worker. After delivering a reviewed plan-only result, the workflow preserves that plan but deactivates its session pointer so unrelated follow-ups are not captured; executing it later requires explicit reactivation and execution approval.

## Update

For a local clone:

```bash
git pull
codex plugin add stageflow@stage-flow
```

For a Git marketplace install:

```bash
codex plugin marketplace upgrade
codex plugin add stageflow@stage-flow
```

After installing or updating, start a new Codex thread so newly loaded skills and tools are available.

## Notes

- Private repository installs require GitHub SSH or HTTPS authentication on the user's machine.
- Git marketplace installs only use content that has been pushed to GitHub.
