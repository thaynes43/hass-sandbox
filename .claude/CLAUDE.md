# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A Home Assistant YAML sandbox + AppDaemon Python apps. HA YAML (automations, scripts, cards, helpers) is copy-pasted into/from the HA UI editor. AppDaemon apps run in Kubernetes; this repo is the dev environment.

## Agent file structure

- `.agents/playbooks/` — shared playbooks (generic, usable by any AI agent)
- `.cursor/rules/` — detailed architecture and coding rules (originally Cursor-formatted but content applies to all agents)
- `.cursor/playbooks/` — Cursor wrappers around `.agents/playbooks/` with Cursor-specific metadata
- `.claude/rules/` — Claude-specific rule files that index the above

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

To release: bump `VERSION`, merge to `main`, and Flux picks up the new tag.

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
│   ├── apps-prod.yaml   # All entries have disable: true; Docker build strips this and writes apps.yaml
│   ├── apps-dev.yaml    # Dev-only apps (keys must end in _dev); never deployed
│   ├── detection_summary_app/
│   ├── detection_summary_viewer/
│   ├── door_notify/
│   ├── immich_fetcher/
│   └── photo_frame_viewer/
└── providers/           # Shared libraries (not AppDaemon apps)
    ├── ai_providers/    # LLM/image provider adapters (OpenAI, Gemini, Ollama, ComfyUI)
    ├── ha_provisioner/  # HA entity provisioning via REST API
    ├── photo_providers/ # Photo source provider (Immich; extensible)
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

All Lovelace card → AppDaemon communication must go through a relay HA script provisioned by the app, called via `hass.callService("script", "<app>_relay", { command, payload })`. Never use `fire_event` from cards — it requires admin. The script fires an `<app>_command` event that AppDaemon listens for. Full template in `.cursor/rules/appdaemon-architecture.mdc` §3.

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
- `apps-prod.yaml`: keys without `_dev` suffix; always have `disable: true`

## Key rules and conventions

See `.claude/rules/` for domain-specific rules. Summary below.

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

### AppDaemon deploy communication (required)

After any `appdaemon/` change, state what was changed:
- **Repo Updated** — changes are in the repo; will deploy automatically when merged to `main` via Docker image build

### Button mapping doc sync (required)

Any change to switch button behavior in `home-assistant/automations/switch-buttons/**` or related blueprints **must** also update `agent-docs/button-mappings.md` in the same session.

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
| `.agents/playbooks/multi-agent-plan.md` | Structuring large tasks across multiple agent sessions |
| `.agents/playbooks/playbook-authoring-guide.md` | Writing a new playbook |
