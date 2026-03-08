# HaynesOps Home Automation

Welcome to the documentation for the HaynesOps Home Automation project — a Home Assistant + AppDaemon automation platform running on Kubernetes.

## What's in this project

- **Home Assistant YAML** — automations, scripts, cards, helpers, and blueprints
- **AppDaemon apps** — Python-based automation apps for complex workflows, AI integrations, and device management
- **Custom Lovelace cards** — purpose-built dashboard cards for wall displays and control panels

## Quick links

| Resource | Description |
|----------|-------------|
| [Architecture Overview](architecture/overview.md) | How HA, AppDaemon, and the frontend fit together |
| [AppDaemon Apps](apps/index.md) | Per-app documentation |
| [Getting Started](setup/getting-started.md) | Development environment setup |

## Project structure

```
hass-sandbox/
├── home-assistant/       # HA YAML (automations, scripts, cards, blueprints)
├── appdaemon/            # AppDaemon Python apps and providers
│   ├── apps/             # Application modules
│   └── providers/        # Shared libraries (AI, photos, HA provisioning)
├── docs/                 # This documentation site (mkdocs-material)
└── agent-docs/           # Internal agent/developer reference docs
```
