# BEYONDEE Skills

BEYONDEE Skills is a shared agent-skill plugin that works in both **Codex** and **Claude Code** from one repository.

| Item | Value |
|---|---|
| Plugin name | `common-skills` |
| Repository | `BEYONDEE-DEV/common-skills` |
| Marketplace name | `beyondee` |
| Codex manifest | `.codex-plugin/plugin.json`, `.agents/plugins/marketplace.json` |
| Claude Code manifest | `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json` |
| Skills | `skills/<skill-name>/SKILL.md` (shared by both hosts) |

## Included Skills

| Skill | Codex | Claude Code |
|---|---|---|
| Multi Repo | `$common-skills:multi-repo <keyword>` | `/common-skills:multi-repo <keyword>` |

Multi Repo handles every Git repository directly under a folder. Keywords: `status`, `submit` (commit, merge the latest source branch, push, create or update the PR), `promote` (fast-forward the task branch or a stage through `dev → dev2 → prod`), `help`. See [USAGE.ko.md](./USAGE.ko.md).

The skill scripts require Python 3.9+, `git`, and an authenticated `gh` CLI.

## Install in Codex

From a local clone:

```bash
git clone git@github.com:BEYONDEE-DEV/common-skills.git
cd common-skills
codex plugin marketplace add "$(pwd)"
codex plugin add common-skills@beyondee
```

Directly from Git:

```bash
codex plugin marketplace add git@github.com:BEYONDEE-DEV/common-skills.git --ref main
codex plugin add common-skills@beyondee
```

Verify with `codex plugin list`; you should see `common-skills@beyondee  installed, enabled`. Start a new Codex thread after installing or updating.

## Install in Claude Code

From a local clone:

```bash
claude plugin marketplace add "$(pwd)"
claude plugin install common-skills@beyondee
```

Directly from GitHub:

```bash
claude plugin marketplace add BEYONDEE-DEV/common-skills
claude plugin install common-skills@beyondee
```

Inside an interactive Claude Code session the same steps are `/plugin marketplace add <source>` and `/plugin install common-skills@beyondee`. Restart Claude Code after installing or updating.

## Update

| Host | Local clone | Git marketplace |
|---|---|---|
| Codex | `git pull`, then `codex plugin add common-skills@beyondee` | `codex plugin marketplace upgrade`, then `codex plugin add common-skills@beyondee` |
| Claude Code | `git pull`, then `claude plugin marketplace update beyondee` | `claude plugin marketplace update beyondee` |

## Adding a Skill

1. Create `skills/<skill-name>/SKILL.md` with `name` and `description` frontmatter. Both hosts read this file.
2. Optionally add `skills/<skill-name>/agents/openai.yaml` for Codex display metadata and implicit-invocation policy. Claude Code ignores it; use `disable-model-invocation: true` in the `SKILL.md` frontmatter for explicit-only skills.
3. Resolve bundled files from the skill directory (`<skill-dir>`), never from the session cwd.
4. Avoid host-only tools in the skill body, or describe the equivalent for each host.
5. Bump `version` in both `.codex-plugin/plugin.json` and `.claude-plugin/plugin.json` (kept identical by `tests/test_plugin_marketplace.py`).

## Development

```bash
python3 -m unittest tests/test_*.py
```

## Notes

- Private repository installs require GitHub SSH or HTTPS authentication on the user's machine.
- Git marketplace installs only use content that has been pushed to GitHub.
