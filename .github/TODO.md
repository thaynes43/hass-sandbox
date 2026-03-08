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

## Integration test workflow

**Priority**: Low
**Status**: Not started

Run integration tests on a schedule or manual trigger. These require:
- HA instance access (or mock)
- AI provider API keys
- Environment variable gating (`RUN_HA_INTEGRATION_TESTS=1`, etc.)
