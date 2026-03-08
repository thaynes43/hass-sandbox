# AppDaemon deploy playbook

Production AppDaemon runs as a custom Docker image (`ghcr.io/thaynes43/appdaemon`) in Kubernetes. App code is baked into the image at build time. Deploys happen automatically when code merges to `main`.

## How deployment works

1. Developer merges PR to `main` (or pushes directly for hotfixes)
2. GitHub Actions workflow (`.github/workflows/build-appdaemon.yml`) builds a Docker image
3. Image is pushed to GHCR with semver tags from `VERSION` file (e.g., `0.1.0`, `0.1.0-abc1234`, `latest`)
4. Flux detects the new image tag and rolls the Kubernetes deployment
5. The container's entrypoint copies baked-in app code to `/conf/apps/` and starts AppDaemon

## Versioning

- **`VERSION` file** at repo root contains the semver version (e.g., `0.1.0`)
- **Main branch** tags: `<version>`, `<version>-<sha>`, `latest`
- **Feature branches** tags: `<branch>.<sha>`, `<branch>` (for testing pre-merge)
- Bump `VERSION` when releasing meaningful changes

## What the Docker build does

- Installs runtime deps from `docker/requirements-prod.txt`
- Processes `apps-prod.yaml` → `apps.yaml` (strips `disable: true` and `debug_preserve_run_dirs: true`)
- Stages app code at `/opt/appdaemon-code/apps/` and `/opt/appdaemon-code/providers/`
- Removes `apps-dev.yaml` and `apps-prod.yaml` from the image
- At runtime, entrypoint copies code to `/conf/apps/` (writable emptyDir) including `providers/` → `apps/providers/`

## What is NOT in the image

- `appdaemon.yaml` — comes from Kubernetes Secret mount at `/conf/appdaemon.yaml`
- `secrets.yaml` — comes from Kubernetes Secret mount at `/conf/secrets.yaml`
- Test files, dev configs, `.venv`, `__pycache__` — excluded by `.dockerignore`

## Promote dev app to prod

The `--merge-dev-apps` workflow is a manual pre-commit step. It strips `_dev` suffixes, converts media paths, and adds `disable: true` to `apps-prod.yaml`. This must be done before merging to `main`:

1. Edit `apps-prod.yaml` manually (or use a script) to promote dev app configs
2. Commit the updated `apps-prod.yaml`
3. The Docker build will strip `disable: true` automatically

## Adding new Python dependencies

1. Add the dependency to `docker/requirements-prod.txt`
2. The next Docker build will install it in the image
3. Dev dependencies (pytest, ruamel.yaml, etc.) stay in `appdaemon/requirements.txt` only

## Verification

After merge to `main`:
- Check GitHub Actions for successful build
- Verify image appears in GHCR: `ghcr.io/thaynes43/appdaemon`
- Check Flux reconciliation and pod status in Kubernetes
- Verify at https://appdaemon.haynesops.com/

## Common pitfalls

| Symptom | Cause | Fix |
|--------|-------|-----|
| Image build fails | Bad YAML in apps-prod.yaml | Fix YAML syntax, re-push |
| Pod CrashLoopBackOff | Missing secret mounts or bad config | Check K8s secret mounts, pod logs |
| `ModuleNotFoundError` in prod | Missing dep in `docker/requirements-prod.txt` | Add dep, rebuild image |
| Old code running after merge | Flux hasn't reconciled yet | Check Flux status, force reconcile |
| `!secret` tags lost in apps.yaml | process-apps-yaml.py bug | Check `docker/process-apps-yaml.py` SecretTag handling |
