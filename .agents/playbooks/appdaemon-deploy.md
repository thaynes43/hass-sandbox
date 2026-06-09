# AppDaemon deploy playbook

Production AppDaemon runs as a custom Docker image (`ghcr.io/thaynes43/appdaemon`) in Kubernetes. App code is baked into the image at build time. Deploys happen automatically when code merges to `main`.

Before deploying to production, run the pre-deploy security audit: `.agents/playbooks/security-audit.md`.

## How deployment works

1. Developer merges PR to `main` (or pushes directly for hotfixes)
2. GitHub Actions workflow (`.github/workflows/build-appdaemon.yml`) builds a Docker image
3. Image is pushed to GHCR with semver tags from `VERSION` file (e.g., `0.1.0`, `0.1.0-abc1234`, `latest`)
4. Flux detects the new image tag and rolls the Kubernetes deployment
5. The container's entrypoint copies baked-in app code to `/conf/apps/` and starts AppDaemon

## Versioning

- **`VERSION` file** at repo root contains the semver version (e.g., `0.1.0`)
- **Main branch** tags: `<version>`, `<version>-<sha>`, `latest`
- **Feature branches** tags: `<version>-<branch>.<sha>`, `<version>-<branch>` (for testing pre-merge)
- Agents must bump `VERSION` on the feature branch before creating or updating a PR, unless the user explicitly says not to
- PR prep is incomplete until the version bump is committed on the branch
- Use semver: patch for fixes/internal changes, minor for features, major for breaking changes
- The merge to `main` then produces the correct semver tag

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

## App lifecycle: dev → prod → dev

Apps move between `apps-dev.yaml` (local development) and `apps-prod.yaml` (Kubernetes production). This is a manually driven process.

### Promote dev app to prod

When a new app is ready for production testing:

1. **Copy the app config** from `apps-dev.yaml` to `apps-prod.yaml`
2. **Strip the `_dev` suffix** from the app key (e.g., `my_app_dev` → `my_app`)
3. **Add `disable: true`** (Docker build strips this; prevents local AppDaemon from running prod apps)
4. **Remove `debug_preserve_run_dirs: true`** if present
5. **Convert `ai_provider_conf`** from dev providers (ollama/comfyui with `!secret` URLs) to prod providers (e.g., `openai-default`):
   ```yaml
   # Dev format (inline bundle + URL):
   ai_provider_conf:
     simple_text:
       bundle: ollama-qwen9b
       base_url: !secret ollama_url
     multimodal:
       bundle: ollama-qwen9b
       base_url: !secret ollama_url
     image:
       bundle: comfyui-qwen-edit
       base_url: !secret comfyui_url

   # Prod format (simple bundle reference):
   ai_provider_conf:
     simple_text: openai-default
     multimodal: openai-default
     image: openai-default
   ```
6. **Remove the app from `apps-dev.yaml`** — dev file should be empty when all apps are in prod
7. **Commit on a feature branch**, push, and test with the dev-tagged Docker image before merging to `main`

### Pull prod app back to dev

When a running prod app needs enhancements, bug fixes, or new features:

1. **Copy the app config** from `apps-prod.yaml` to `apps-dev.yaml`
2. **Add the `_dev` suffix** to the app key (e.g., `my_app` → `my_app_dev`)
3. **Remove `disable: true`** (dev apps run locally)
4. **Convert `ai_provider_conf`** from prod providers to dev providers (ollama/comfyui with `!secret` URLs)
5. **Optionally add `debug_preserve_run_dirs: true`** for troubleshooting
6. **Remove the app from `apps-prod.yaml`** so the next Docker build excludes it from production
7. **Push a new image tag** to Kubernetes that doesn't include this app (production will stop running it)
8. **Do dev work locally**, then follow the "Promote" flow when ready

### Key rules

- An app should only exist in ONE file at a time — never both `apps-dev.yaml` and `apps-prod.yaml`
- `apps-dev.yaml` should be empty when all development is complete and all apps are in prod
- Dev uses local AI providers (ollama, comfyui); prod uses cloud providers (openai)
- The `disable: true` flag is only in `apps-prod.yaml` — it prevents local AppDaemon from running prod apps; Docker build strips it

### Future: runtime app disable

Currently, removing an app from production requires building and deploying a new image. A future improvement could allow pausing/disabling individual apps at runtime without redeploying (e.g., via an HA helper toggle or AppDaemon admin API). This would make the dev↔prod cycle faster and less disruptive.

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
| App running in dev AND prod | Config exists in both YAML files | Remove from one — app should only be in one file at a time |
