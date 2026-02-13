# AppDaemon Setup

This repo uses `appdaemon/` as the **development environment** for AppDaemon apps. Production runs in **Kubernetes** on the cluster; configs are mounted from `X:\`.

## Production

- **AppDaemon UI**: https://appdaemon.haynesops.com/
- **Config location**: `X:\` (production configs mounted here)
- **Deploy**: Use `appdaemon/deploy.py` to sync dev changes to production when ready.

## Development environment (`appdaemon/`)

```
appdaemon/
├── appdaemon.yaml           # Local dev config (.gitignored; never deployed)
├── secrets.yaml             # Local dev secrets (.gitignored; never deployed)
├── requirements.txt         # Python deps (pip install -r)
├── deploy.py                # Deploy script (apps/ → X:\)
├── apps/
│   ├── apps.yaml            # App registration (environment_test)
│   └── environment_test.py  # Environment test app
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

### Verifying environment_test locally

1. Run `appdaemon -c appdaemon` (config dir is `appdaemon/`).
2. Check logs for: `Environment test: AppDaemon dev environment loaded`.
3. Edit `environment_test.py` and save; AppDaemon auto-reloads.

## Deploying to production

See `.cursor/rules/appdaemon-vs-ha-yaml.mdc` for the full deploy procedure. Summary:

```bash
python appdaemon/deploy.py
# or with explicit target:
python appdaemon/deploy.py --target X:\
# dry run first:
python appdaemon/deploy.py --dry-run
```

## References

- [AppDaemon docs](https://appdaemon.readthedocs.io/en/latest/)
- [Writing AppDaemon Apps](https://appdaemon.readthedocs.io/en/latest/APPGUIDE.html)
- `.cursor/rules/appdaemon-vs-ha-yaml.mdc` — deploy procedure and AppDaemon vs HA YAML
