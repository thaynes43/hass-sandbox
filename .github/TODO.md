# GitHub CI/CD TODO

Deferred items for CI/CD pipeline improvements.

## Auto-deploy to production on merge to main

**Priority**: High
**Status**: Implemented

Custom Docker image approach (option 2) is implemented:
- `.github/workflows/build-appdaemon.yml` builds and pushes `ghcr.io/thaynes43/appdaemon` on merge to main
- Semver tags from `VERSION` file, feature branch dev tags
- Flux detects new image and rolls the Kubernetes deployment
- `deploy.py` has been removed

## Branch protection rules

**Priority**: High
**Status**: In progress (user configuring on GitHub)

- Protect `main` branch
- Require PR reviews
- Require status checks to pass (Unit Tests workflow)
- Prevent force pushes to `main`

## Documentation audit workflow improvements

**Priority**: Medium
**Status**: Initial version deployed

The current `doc-audit.yml` uses Claude to review PR diffs for documentation consistency.
Future improvements:
- Add structured output format for audit results
- Consider making doc audit a required check (not just advisory)
- Add automated README scaffolding for new apps/providers

## Runtime app disable (no-redeploy pause)

**Priority**: Medium
**Status**: Not started

Currently, removing an app from production requires building and deploying a new Docker image. A runtime disable mechanism would allow pausing individual apps without redeploying:
- Option A: HA helper toggle per app (e.g., `input_boolean.appdaemon_<app>_enabled`) — app checks on startup and listens for state changes
- Option B: AppDaemon admin API endpoint to disable/enable apps
- Option C: Config reload from a mounted ConfigMap (Flux-managed, no image rebuild needed)

This would make the dev↔prod cycle faster — pull an app back to dev without waiting for a new image build to stop it in prod.

## Integration test workflow

**Priority**: Low
**Status**: Not started

Run integration tests on a schedule or manual trigger. These require:
- HA instance access (or mock)
- AI provider API keys
- Environment variable gating (`RUN_HA_INTEGRATION_TESTS=1`, etc.)
