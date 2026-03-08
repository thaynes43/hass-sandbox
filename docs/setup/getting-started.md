# Getting Started

## Prerequisites

- Python 3.12+
- Git
- Access to the Home Assistant instance (for integration testing)

## Clone and set up

```bash
git clone git@github.com:thaynes43/hass-sandbox.git
cd hass-sandbox
python -m venv .venv
source .venv/bin/activate
pip install -r appdaemon/requirements.txt
```

## Create local secrets

Create `appdaemon/secrets.yaml` (gitignored) with your HA long-lived access token:

```yaml
ha_url: "http://your-ha-instance:8123"
token: "your_long_lived_access_token"
```

## Run AppDaemon locally

```bash
source .venv/bin/activate
appdaemon -c appdaemon
```

## Run tests

```bash
source .venv/bin/activate
cd appdaemon
python -m pytest tests/ -v --tb=short
```

## Build this documentation site

```bash
source .venv/bin/activate
mkdocs serve
```

Then open [http://127.0.0.1:8000](http://127.0.0.1:8000).

## Deploy documentation to GitHub Pages

```bash
mkdocs gh-deploy
```

## Project layout

```
appdaemon/
├── appdaemon.yaml       # Local dev config (committed; never deployed)
├── secrets.yaml         # Local dev secrets (.gitignored)
├── requirements.txt
├── deploy.py            # Syncs apps/ + providers/ → production
├── apps/                # AppDaemon app modules
└── providers/           # Shared libraries (AI, photos, provisioning)
```
