# AppDaemon Setup

This repo uses `appdaemon/` as the **development environment** for AppDaemon apps. Production runs in **Kubernetes** as a custom Docker image.

## Production

- **AppDaemon UI**: https://appdaemon.haynesops.com/
- **Docker image**: `ghcr.io/thaynes43/appdaemon`
- **Deploy**: Automatic on merge to `main` — GitHub Actions builds the image, Flux rolls the deployment.
- **Versioning**: `VERSION` file at repo root. Main builds get semver tags (`0.1.0`, `0.1.0-abc1234`, `latest`). Feature branches get `branchname.sha` tags.

## Development environment (`appdaemon/`)

```
appdaemon/
├── appdaemon.yaml           # Local dev config (committed; never deployed)
├── secrets.yaml             # Local dev secrets (.gitignored; never deployed)
├── requirements.txt         # Python deps (pip install -r)
├── apps/
│   ├── apps-prod.yaml       # Production app list (all disable: true); Docker build strips disable
│   ├── apps-dev.yaml        # Dev-only app list (keys end in _dev); never in image
│   └── ...
└── providers/               # Shared libraries (AI, photos, provisioning)
```

### Local setup (pip)

1. `appdaemon.yaml` is in the repo. Create `appdaemon/secrets.yaml` (`.gitignored`) with `token: "your_long_lived_access_token"` for local HA auth.
2. From repo root:

```bash
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -r appdaemon/requirements.txt
appdaemon -c appdaemon
```

## Docker image build

The Docker build (`docker/Dockerfile`) does the following:

1. Installs runtime deps from `docker/requirements-prod.txt`
2. Processes `apps-prod.yaml` → `apps.yaml` (strips `disable` and `debug_preserve_run_dirs`)
3. Stages code at `/opt/appdaemon-code/`
4. At runtime, entrypoint copies to `/conf/apps/` (writable emptyDir)

`appdaemon.yaml` and `secrets.yaml` come from Kubernetes Secret mounts — never in the image.

## Running tests

```bash
python -m pytest appdaemon/tests -v
```

See `agent-docs/appdaemon-testing.md` for mocking HA calls and testing patterns.

## Adding new Python dependencies

- **Dev-only** (pytest, linters): add to `appdaemon/requirements.txt`
- **Runtime** (needed in prod): add to `docker/requirements-prod.txt`

## References

- [AppDaemon docs](https://appdaemon.readthedocs.io/en/latest/)
- [Writing AppDaemon Apps](https://appdaemon.readthedocs.io/en/latest/APPGUIDE.html)
- `.cursor/rules/appdaemon-vs-ha-yaml.mdc` — AppDaemon vs HA YAML
