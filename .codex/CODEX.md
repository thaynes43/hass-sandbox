# CODEX.md

This file provides guidance to Codex agents working in this repository.

## What this repo is

A Home Assistant YAML sandbox plus AppDaemon Python apps.

- `home-assistant/` is the repo mirror for HA YAML that is typically copied into the HA UI.
- `appdaemon/` is the development workspace for AppDaemon apps and shared provider code.
- Production AppDaemon config is deployed separately; this repo is the development source.

## Agent file structure

- `.agents/playbooks/` — shared playbooks for any agent
- `.cursor/rules/` — detailed architecture and coding rules
- `.claude/` — Claude-specific index files
- `.codex/` — Codex-specific index files

The `.cursor` files are the canonical rule source in this repo. The `.codex` files should point to them instead of duplicating long rule bodies.

## Get started fast

Read these first when working in the matching area:

- `appdaemon/`:
  - `.codex/rules/appdaemon.md`
  - `.codex/rules/security.md`
- `home-assistant/`:
  - `.codex/rules/ha-yaml.md`
- custom Lovelace cards or frontend JS:
  - `.codex/rules/custom-cards.md`

If the task matches a playbook, read the shared playbook in `.agents/playbooks/` before editing code.

## Commands

### Run tests in WSL

```bash
cd /mnt/d/labspace/hass-sandbox
source .venv-wsl/bin/activate
cd appdaemon
python -m pytest tests/ -v --tb=short
```

Single test file:

```bash
cd /mnt/d/labspace/hass-sandbox
source .venv-wsl/bin/activate
cd appdaemon
python -m pytest tests/test_door_notify.py -v --tb=short
```

### Local AppDaemon run

```bash
cd /mnt/d/labspace/hass-sandbox
source .venv-wsl/bin/activate
appdaemon -c appdaemon
```

### Deploy AppDaemon

Production deploys are automated. Merging to `main` triggers a Docker image build and push to GHCR. Flux rolls the Kubernetes deployment automatically.

## Core repository facts

### AppDaemon layout

```text
appdaemon/
├── appdaemon.yaml
├── secrets.yaml                  # local only, gitignored
├── apps/
│   ├── apps-dev.yaml
│   ├── apps-prod.yaml
│   ├── detection_summary_app/
│   ├── detection_summary_viewer/
│   └── door_notify.py
└── providers/
    ├── ai_providers/
    ├── ha_provisioner/
    ├── photo_providers/
    └── secrets.py
```

### Important conventions

- Shared code goes in `appdaemon/providers/`, not `appdaemon/apps/`.
- AppDaemon apps importing providers need the `sys.path.append(...parents[2])` pattern described in `.cursor/rules/appdaemon-architecture.mdc`.
- Apps should self-provision HA helpers and scripts where possible.
- AI prompt policy belongs in the app layer, not in provider transport adapters.
- Dev apps live in `apps-dev.yaml` and use `_dev` suffixes.
- Prod apps live in `apps-prod.yaml` and keep `disable: true` until deployment strips it.

## Home Assistant MCP guidance

This repo often uses the Home Assistant MCP server for live HA work, but only when needed.

- Prefer repo edits when the user is asking for source changes only.
- Use MCP when the task requires live HA state, dashboard edits, helper creation, automation creation, or entity verification that cannot be inferred locally.
- Avoid broad exploratory MCP queries when the user can provide exact entity IDs directly.

## Current AI provider shape

The AI provider layer is capability-based:

- `simple_text`
- `multimodal`
- `image`

Current provider coverage:

- OpenAI: all three
- Gemini: all three
- Ollama: `simple_text` and `multimodal`
- ComfyUI: `image`

See:

- `appdaemon/providers/ai_providers/README.md`
- `.codex/rules/appdaemon.md`

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

## Communication requirements

- After `home-assistant/` changes, follow `.cursor/rules/ha-change-scope-communication.mdc`.
- After `appdaemon/` changes, clearly state whether changes were only made in the repo or also deployed.
- Do not claim live HA changes unless they were actually performed.
