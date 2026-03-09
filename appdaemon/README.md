# AppDaemon

Python apps for Home Assistant. Dev in this repo; production runs in Kubernetes as a custom Docker image.

## Deploy

- **Production** deploys are automated: merging to `main` builds a Docker image (`ghcr.io/thaynes43/appdaemon`) via GitHub Actions and pushes to GHCR. Flux rolls the Kubernetes deployment.
- **Versioning**: bump `VERSION` at repo root. Main builds get semver tags (e.g., `0.1.0`, `0.1.0-abc1234`). Feature branches get `branchname.sha` tags.
- **`appdaemon.yaml`** and **`secrets.yaml`** are deployed via Kubernetes Secret mounts; never baked into the image.

## Dev vs prod app config

- **Prod:** `apps-prod.yaml` has all apps with `disable: true`. The Docker build strips disable and bakes the result as `apps.yaml`. Prod AppDaemon loads `apps.yaml`.
- **Dev:** `apps-dev.yaml` has dev-only apps (keys end in `_dev`). Never included in the image. Local AppDaemon loads both; prod apps stay disabled, so only _dev apps run.

## Adding new Python dependencies

- **Dev-only** (pytest, linters): add to `appdaemon/requirements.txt`
- **Runtime** (needed in production): add to `docker/requirements-prod.txt`
