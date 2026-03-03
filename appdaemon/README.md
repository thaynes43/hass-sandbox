# AppDaemon

Python apps for Home Assistant. Dev in this repo; production runs in Kubernetes (configs at `X:\`).

## Deploy

- **`deploy.py`** syncs `apps/` and shared libs to the production path (default `X:\`). Reads `apps-prod.yaml`, strips `disable: true`, writes `apps.yaml`. Run from repo root: `python appdaemon/deploy.py`.
- **Production** `appdaemon.yaml` and `secrets.yaml` are deployed via **Flux**; deploy.py never overwrites them.

## Dev vs prod app config

- **Prod:** `apps-prod.yaml` has all apps with `disable: true`. Deploy strips disable and writes `X:\apps\apps.yaml`. Prod AppDaemon loads `apps.yaml`.
- **Dev:** `apps-dev.yaml` has dev-only apps (keys end in `_dev`). Never deployed. Local AppDaemon loads both; prod apps stay disabled, so only _dev apps run. Promote with `python appdaemon/deploy.py --merge-dev-apps` (repo-only), then review `apps-prod.yaml` before deploying.

## Global TODO

Add CI/CD pipeline that lets us run the test on feature branches and trigger auto deployments to the live AppDaemon on tagged releases.

This will require setting up a custom runer since the deploymnet process is highly specific to my environment.
