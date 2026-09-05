# AGENTS.md

Guidance for AI coding agents (Codex and any other tool that reads `AGENTS.md`) working in this repository. Claude Code uses `.claude/CLAUDE.md`, which carries the same conventions.

## What this repo is

A Home Assistant YAML sandbox + AppDaemon Python apps.

- `home-assistant/` — repo mirror for HA YAML (automations, scripts, cards, helpers); changes are copy-pasted into/from the HA UI editor. No CI; reference/backup, not source of truth for HA.
- `appdaemon/` — Python AppDaemon apps. Dev here; production deploys via Docker image build on merge to `main` (Flux rolls the Kubernetes deployment).

## Agent file structure

- `.agents/rules/` — detailed architecture and coding rules (canonical source, agent-agnostic)
- `.agents/playbooks/` — shared playbooks (generic, usable by any AI agent)
- `.agents/plans/` — saved multi-session plans
- `.claude/` — Claude-specific index files
- `AGENTS.md` (this file) — entry point for Codex and other agents

## Read these first when working in the matching area

- Everywhere:
  - `.agents/rules/hass.md` — project overview, HA/AppDaemon container separation, MCP usage, YAML formatting, non-admin frontend rule (**always applies**)
- `appdaemon/`:
  - `.agents/rules/appdaemon-architecture.md` — system overview, self-provisioning, relay script pattern, new app checklist
  - `.agents/rules/appdaemon-coding-guidelines.md` — apps vs shared libs, AI offloading, dev/prod naming
  - `.agents/rules/appdaemon-dev-environment.md` — venvs, test commands, cross-platform
  - `.agents/rules/appdaemon-documentation.md` — README requirements, documentation map, dependency graph
  - `.agents/rules/ai-provider-architecture-guidelines.md` — capability bundles, model settings, prompt policy layering
  - `.agents/rules/logging-standards.md` — log levels, required logging points
  - `.agents/rules/security-policy.md` — **always applies** in `appdaemon/`
  - `.agents/rules/git-workflow.md` — branching, PRs, CI gates, commit conventions
- `home-assistant/`:
  - `.agents/rules/appdaemon-vs-ha-yaml.md` — when to use AppDaemon vs HA YAML
  - `.agents/rules/ha-change-scope-communication.md` — **required** response format after HA changes
  - `.agents/rules/button-mappings-doc-sync.md` — button mapping doc must stay in sync
  - `.agents/rules/night-lights-import.md` — night light automation import process
  - `.agents/rules/ha-entity-and-device-settings.md` — entity vs device registry, area assignment via MCP
- Custom Lovelace cards or frontend JS:
  - `.agents/rules/custom-card-guidelines.md` — touch/click dedup, relay scripts, focus guards, cache busting
- Docs site (`docs/`, `mkdocs.yml`):
  - `.agents/rules/docs-site.md` — page map, nav requirements, build check

If the task matches a playbook in `.agents/playbooks/`, read it before editing code.

## Commands

### Run tests (Linux, primary)

```bash
# From repo root
source .venv/bin/activate && cd appdaemon && python -m pytest tests/ -v --tb=short
```

Single test file:

```bash
source .venv/bin/activate && cd appdaemon && python -m pytest tests/test_door_notify.py -v --tb=short
```

### Local AppDaemon run

```bash
# From repo root with venv active
appdaemon -c appdaemon
```

### Deploy AppDaemon

Production deploys are automated. Merging to `main` triggers a Docker image build and push to GHCR; Flux rolls the Kubernetes deployment.

If an agent creates or updates an AppDaemon PR, it must bump `VERSION` on that branch before opening the PR unless the user explicitly says not to. Use semver: patch for fixes, minor for features, major for breaking changes. **Before bumping, compare against `main` (`git show main:VERSION`)** — if already bumped on this branch, do not bump again.

Pull requests are opened **ready for review** (`gh pr create`, never `--draft`) and squash-merged by the agent once every check, including the Claude Code Review and docs-audit workflows, is green. The owner does not mark PRs ready or merge them; a green, unmerged PR is unfinished work.

## Core repository facts

### AppDaemon layout

```text
appdaemon/
├── appdaemon.yaml                # local dev config (committed; never deployed)
├── secrets.yaml                  # local only, gitignored
├── apps/
│   ├── apps-dev.yaml             # dev-only apps; keys end in _dev
│   ├── apps-prod.yaml            # prod apps; entries normally disable: true (build strips it)
│   └── <app packages>/           # detection_summary_app, door_notify, vestaboard_apps, ...
└── providers/                    # shared libraries (not AppDaemon apps)
    ├── ai_providers/
    ├── ha_provisioner/
    ├── media_providers/
    ├── photo_providers/
    ├── school_menu/
    ├── vestaboard/
    └── secrets.py
```

### Important conventions

- Shared code goes in `appdaemon/providers/`, not `appdaemon/apps/`.
- Apps importing providers need the `sys.path.append(str(Path(__file__).resolve().parents[2]))` pattern described in `.agents/rules/appdaemon-architecture.md`.
- Apps self-provision HA helpers and scripts on startup via `ha_provisioner` — never tell users to create helpers manually.
- Cards talk to AppDaemon via a relay HA script (`hass.callService("script", "<app>_relay", ...)`), never `fire_event`.
- AI prompt policy belongs in the app layer, not in provider transport adapters.
- Dev apps live in `apps-dev.yaml` and use `_dev` suffixes; prod apps live in `apps-prod.yaml` with `disable: true`.

### Security (mandatory — `.agents/rules/security-policy.md`)

- No credentials in `appdaemon/apps/` code — app configs pass env var **names** via `_env` suffix keys (e.g. `api_key_env: OPENAI_API_KEY`); providers resolve via `providers.secrets.resolve_secret()`.
- All external HTTP calls live in `appdaemon/providers/`, never in `appdaemon/apps/`.
- Never expose secrets to frontend code, events, or state.
- `secrets.yaml` and `.env` stay uncommitted.

## Home Assistant MCP guidance

This repo often uses the Home Assistant MCP server for live HA work, but only when needed.

- Prefer repo edits when the user is asking for source changes only.
- Use MCP when the task requires live HA state, dashboard edits, helper creation, automation creation, or entity verification that cannot be inferred locally.
- Avoid broad exploratory MCP queries when the user can provide exact entity IDs directly.

## Communication requirements

- After `home-assistant/` changes, follow `.agents/rules/ha-change-scope-communication.md` — the response must start with exactly one of **Repo YAML Only Updated - You copy paste** or **Repo YAML & Live HA Updated**.
- After `appdaemon/` changes, clearly state whether changes were only made in the repo or also deployed. Do not claim live HA changes unless they were actually performed.
- When changes cannot be fully validated by unit tests alone (runtime behavior, card JS, MCP interactions), verify them yourself live (HA MCP, `kubectl`, Playwright) before committing and state what was verified in the PR body; list anything unverifiable under **Not verified** instead of waiting on the owner.
- Only genuine requirements or design questions wait on the owner: ask them with `AskUserQuestion`, one at a time, when they arise. Never batch them or leave them as prose.

## Shared playbooks

Use these when the task clearly matches:

- `.agents/playbooks/appdaemon-deploy.md`
- `.agents/playbooks/security-audit.md`
- `.agents/playbooks/ha-provisioner.md`
- `.agents/playbooks/appdaemon-ai-provider.md`
- `.agents/playbooks/detection-app.md`
- `.agents/playbooks/ha-automations-scripts.md`
- `.agents/playbooks/ha-helpers.md`
- `.agents/playbooks/ha-dashboard.md`
- `.agents/playbooks/occupancy-based-lighting.md`
- `.agents/playbooks/multi-agent-plan.md`
- `.agents/playbooks/cache-busting-playbook.md`
- `.agents/playbooks/playbook-authoring-guide.md`
