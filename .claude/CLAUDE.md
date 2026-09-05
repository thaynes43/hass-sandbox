# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A Home Assistant YAML sandbox + AppDaemon Python apps. HA YAML (automations, scripts, cards, helpers) is copy-pasted into/from the HA UI editor. AppDaemon apps run in Kubernetes; this repo is the dev environment.

## Agent file structure

- `.agents/rules/` — detailed architecture and coding rules (canonical source, agent-agnostic)
- `.agents/playbooks/` — shared playbooks (generic, usable by any AI agent)
- `.agents/plans/` — saved multi-session plans
- `.claude/rules/` — Claude-specific rule files that index the above
- `AGENTS.md` (repo root) — entry point for Codex and other agents that read AGENTS.md

## Commands

### Running tests

```bash
# Linux (primary) — from repo root
source .venv/bin/activate && cd appdaemon && python -m pytest tests/ -v --tb=short
```

Run a single test file:
```bash
source .venv/bin/activate && cd appdaemon && python -m pytest tests/test_door_notify.py -v --tb=short
```

Windows with WSL (from PowerShell):
```bash
wsl bash -c "cd /mnt/d/labspace/hass-sandbox && source .venv-wsl/bin/activate && cd appdaemon && python -m pytest tests/ -v --tb=short"
```

Windows fallback (from repo root, no WSL):
```bash
.\.venv\Scripts\python.exe -m pytest appdaemon/tests/ -v
```

### Local AppDaemon run

```bash
# From repo root with venv active
appdaemon -c appdaemon
```

### Deploy to production

Production deploys are automated via Docker image builds. Merging to `main` triggers a GitHub Actions workflow that builds and pushes `ghcr.io/thaynes43/appdaemon:<version>` to GHCR. Flux detects the new image and rolls the Kubernetes deployment.

If an agent creates or updates an AppDaemon PR, it must bump `VERSION` on that branch before opening the PR unless the user explicitly says not to. Use semver: patch for fixes, minor for features, major for breaking changes. The merge to `main` then automatically produces the semver tag.

**Before bumping VERSION**: Always compare the current `VERSION` file against `main` (`git show main:VERSION`). If it's already been bumped on this branch, do not bump again. Context wipes between sessions cause duplicate bumps — always check first.

### Install dependencies

```bash
pip install -r appdaemon/requirements.txt
```

## Architecture

### Two distinct areas

**`home-assistant/`** — HA YAML configs (automations, scripts, cards, helpers, blueprints). No CI; changes are copy-pasted into the HA UI. This is reference/backup, not source of truth for HA.

**`appdaemon/`** — Python AppDaemon apps. Dev here, production deploys via Docker image build on merge to `main`. Tests live in `appdaemon/tests/`.

### AppDaemon folder layout

```
appdaemon/
├── appdaemon.yaml       # Local dev config (committed; never deployed)
├── secrets.yaml         # Local dev secrets (.gitignored)
├── requirements.txt
├── apps/
│   ├── apps-prod.yaml   # Entries normally have disable: true; Docker build strips this and writes apps.yaml
│   ├── apps-dev.yaml    # Dev-only apps (keys must end in _dev); never deployed
│   └── <app packages>/  # calendar_from_schedule_app, countdown_app, dashboard_notify,
│                        # detection_summary_app, detection_summary_viewer, door_notify,
│                        # health_checks, immich_fetcher, media_dashboard_app,
│                        # photo_frame_viewer, school_lunch_app, vestaboard_apps
└── providers/           # Shared libraries (not AppDaemon apps)
    ├── ai_providers/    # LLM/image provider adapters (OpenAI, Gemini, Ollama, ComfyUI)
    ├── ha_provisioner/  # HA entity provisioning via REST API
    ├── media_providers/ # Media dashboard API clients (Tautulli, TMDB, mdblist, SerpAPI)
    ├── photo_providers/ # Photo source provider (Immich; extensible)
    ├── school_menu/     # School lunch menu API client (School Nutrition and Fitness)
    ├── vestaboard/      # Vestaboard local API client + character grid encoding
    └── secrets.py       # resolve_secret() — env-var name → value at runtime
```

### Import path pattern

AppDaemon only adds `appdaemon/apps/` to `sys.path`. App modules that import from `providers/` must add the AppDaemon root at module level:

```python
import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parents[2]))  # adds appdaemon/
```

Then import as `from providers.ai_providers...`, `from providers.ha_provisioner...`, etc.

### Apps are self-provisioning

Apps must **not** require manual HA entity setup. On startup, apps call `ha_provisioner` to create helpers, scripts, etc. (`ensure_script`, `ensure_helper`). These calls are idempotent. What still requires manual steps: shell commands (in `configuration.yaml`), Lovelace resources, `local_file` cameras.

### Relay script pattern (card → AppDaemon)

All Lovelace card → AppDaemon communication must go through a relay HA script provisioned by the app, called via `hass.callService("script", "<app>_relay", { command, payload })`. Never use `fire_event` from cards — it requires admin. The script fires an `<app>_command` event that AppDaemon listens for. Full template in `.agents/rules/appdaemon-architecture.md` §3.

### AI provider architecture

- **Contracts**: `simple_text_provider.py`, `multimodal_text_provider.py`, `image_generation_provider.py`
- **Registry**: `providers/ai_providers/registry.py` resolves capability bundle refs to provider configs
- **Model settings**: `providers/ai_providers/model_settings/*.yaml` — named bundles with defaults per provider (e.g. `gemini-default`, `openai-budget`)
- **Adapters** (transport only): `openai/`, `gemini/`, `ollama/` under `providers/ai_providers/` — never inject prompt policy
- **Prompt policy** belongs in app-level prompt builders (`detection_summary_app/prompting/`), not in provider adapters

Apps reference bundles via `ai_provider_conf`:
```yaml
ai_provider_conf:
  simple_text: openai-budget
  multimodal: gemini-default
  image: gemini-sota
```

### Secrets pattern

App YAML passes env var **names** (e.g. `api_key_env: OPENAI_API_KEY`, `ha_token_env: HA_TOKEN`). Providers call `providers.secrets.resolve_secret()` at runtime. Production secrets come from Kubernetes ExternalSecret. Dev uses `.env` (gitignored).

### Dev vs prod app naming

- `apps-dev.yaml`: keys end in `_dev` (e.g. `detection_summary_app_dev`)
- `apps-prod.yaml`: keys without `_dev` suffix; normally have `disable: true` (omit it only when an app should also run locally)

## Key rules and conventions

See `.claude/rules/` for domain-specific rules. Summary below.

### Verify end-to-end yourself before commit (required)

Agents work autonomously in this repo; the owner does not test, mark ready, or merge for you (Tom, 2026-09-04). Unit tests passing does not mean a feature works end-to-end, so when a change touches runtime behavior (AppDaemon apps, Lovelace card JS, HA MCP interactions, queue/TTL behavior) verify it yourself before committing: run the unit tests, then exercise the change live (HA MCP state/traces/logs, `kubectl logs`/`exec` on the AppDaemon pod, Playwright screenshots for cards) and state in the PR body exactly what was verified and how. Anything you could not verify goes under a **Not verified** line in the PR body. Declare it; do not block on the owner.

### Questions go to the owner one at a time (required)

The only thing that waits on the owner is a genuine requirements or design question. Push it with the `AskUserQuestion` tool, one question per prompt, at the moment it arises, with its premise verified first. Never batch questions and never leave them as an "open questions" list in a message: the owner does not act on prose, and the work stalls.

### When to use AppDaemon vs HA YAML

- **HA YAML**: simple trigger→condition→action, helpers as state, occupancy lighting, switch mappings
- **AppDaemon**: persistent/derived state, multi-step sequences with retries, Python logic, AI workloads

AI work (LLM calls, image processing) must run in AppDaemon, not HA. HA handles snapshots and device actions only.

### Security (mandatory — full detail in `.claude/rules/security.md`)

- No credentials in `appdaemon/apps/` code — only env var names via `_env` suffix keys
- All external HTTP calls in `appdaemon/providers/`, never in `appdaemon/apps/`
- No secrets in `fire_event` payloads, `set_state` attributes, or card JS
- `secrets.yaml` is gitignored; never commit it

### Integration tests

Live/costly tests go in `appdaemon/tests/integration-tests/` and require explicit env-gate opt-in (e.g. `RUN_EXTERNAL_IMAGE_TESTS=1`, `RUN_HA_INTEGRATION_TESTS=1`). Do not add them to the default unit-test path.

### HA YAML change communication (required)

After any `home-assistant/` change, the response must start with exactly one of:
- **Repo YAML Only Updated - You copy paste**
- **Repo YAML & Live HA Updated**

Followed by: what needs copy-pasting into HA, and what was updated in the repo.

### App README required (required)

Every new AppDaemon app **must** include a `README.md` in its package directory. See `.agents/rules/appdaemon-documentation.md` for the full template. Also add the app to the documentation map and dependency graph in that file.

### AppDaemon deploy communication (required)

After any `appdaemon/` change, state what was changed:
- **Repo Updated** — changes are in the repo; will deploy automatically when merged to `main` via Docker image build

### Pull requests: open ready for review and merge them yourself (required)

Open PRs **ready for review** (`gh pr create`, never `--draft`) once the branch is complete and verified. Marking ready triggers Claude Code Review, Agent Docs Audit, and Docs Site Audit on top of the required checks (`test`, `docs-build`, `build-and-push`); that review spend is intended. Wait for **all** of them, address findings with follow-up commits, then squash-merge your own PR (`gh pr merge <n> --squash --delete-branch`) and confirm it shows `MERGED`. Never push to `main` directly. A green, unmerged PR is unfinished work, not a hand-off.

### Button mapping doc sync (required)

Any change to switch button behavior in `home-assistant/automations/switch-buttons/**` or related blueprints **must** also update `agent-docs/button-mappings.md` in the same session.

### Helpers are never mirrored into repo YAML (required)

Helpers (`input_*`, `timer`, `counter`, template sensors, …) are UI/config-entry managed and
cannot be created or edited from YAML, so a repo copy is not copy/paste-able and provides no
value. Create and edit them live via `ha_config_set_helper` on the HA MCP server. Never add
files under `home-assistant/helpers/**` — it is legacy generic-pattern reference only. Helper
documentation (entry_ids, gotchas) belongs in `agent-docs/`, not as YAML under `home-assistant/`.

### Night light imports

When importing night light automations from HA: one file per automation, filename = entity_id minus `automation.` prefix + `.yaml`, strip the `id` field (HA assigns new id on paste).

### Custom Lovelace cards (JS)

- Use delegated touch+click events with deduplication to support desktop, iOS, and Android/UniFi wall displays
- Never `preventDefault()` on `<input>`, `<select>`, `<textarea>` touchend — breaks Android keyboard/dropdowns
- Skip re-render when an input has focus (prevents lost focus on keystrokes)
- Bump `?v=N` query param on Lovelace resource URL after updating card JS
- Cards extend `HTMLElement`, use `attachShadow({ mode: "open" })`, implement `setConfig()` and `set hass()`

## Available playbooks

When starting a task that matches one of these, read the playbook first:

| Playbook | When to use |
|----------|-------------|
| `.agents/playbooks/appdaemon-deploy.md` | AppDaemon Docker image build and deploy process |
| `.agents/playbooks/security-audit.md` | Pre-deploy security audit |
| `.agents/playbooks/ha-provisioner.md` | Adding self-provisioning to an AppDaemon app |
| `.agents/playbooks/appdaemon-ai-provider.md` | Adding a new AI provider |
| `.agents/playbooks/detection-app.md` | Adding a new detection summary entrance |
| `.agents/playbooks/ha-automations-scripts.md` | Creating/updating HA automations or scripts via MCP |
| `.agents/playbooks/ha-helpers.md` | Creating/updating HA helpers via MCP |
| `.agents/playbooks/ha-dashboard.md` | Editing HA dashboard views/cards via MCP |
| `.agents/playbooks/occupancy-based-lighting.md` | Adding/updating occupancy-based lighting zones |
| `.agents/playbooks/cache-busting-playbook.md` | Bumping `?v=N` on a Lovelace JS resource after card updates (MCP workflow) |
| `.agents/playbooks/multi-agent-plan.md` | Structuring large tasks across multiple agent sessions |
| `.agents/playbooks/playbook-authoring-guide.md` | Writing a new playbook |
