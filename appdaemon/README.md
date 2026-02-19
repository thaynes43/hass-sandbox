# AppDaemon

Python apps for Home Assistant. Dev in this repo; production runs in Kubernetes (configs at `X:\`).

## Deploy

- **`deploy.py`** syncs `apps/` and `ai_providers/` to the production path (default `X:\`). Run from repo root: `python appdaemon/deploy.py`.
- **Production** `appdaemon.yaml` and `secrets.yaml` are deployed via **Flux** (ConfigMaps); deploy.py never overwrites them. Production uses `apps/apps.yaml`.

## Dev vs prod app config

- **Prod:** Uses `apps/apps.yaml` (same file deploy copies). Prod appdaemon.yaml (from Flux) uses `app_dir: /conf/apps` and loads `apps.yaml`.
- **Dev:** This repo’s `appdaemon.yaml` is set to `apps: apps/apps-dev.yaml` so local runs load only `apps-dev.yaml`. `apps-dev.yaml` is a copy of `apps.yaml` to start; adjust it for local dev (e.g. fewer notify targets) without touching prod.

## Global TODO

Add CI/CD pipeline that lets us run the test on feature branches and trigger auto deployments to the live AppDaemon on tagged releases.

This will require setting up a custom runer since the deploymnet process is highly specific to my environment. 