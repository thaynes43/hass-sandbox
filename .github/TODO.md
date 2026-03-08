# GitHub CI/CD TODO

Deferred items for CI/CD pipeline improvements.

## Auto-deploy to production on merge to main

**Priority**: High
**Status**: Needs design

Currently production deploys follow `.agents/playbooks/appdaemon-deploy.md` which requires manual `python appdaemon/deploy.py` execution. This should be automated when code merges to `main`.

### Design options

1. **Custom GitHub runner with AppDaemon PVC mounted**
   - Self-hosted runner in the Kubernetes cluster
   - Mount the AppDaemon PVC (`/apps/`) directly
   - Runner executes `deploy.py --target /apps/`
   - Pros: Minimal change to deploy process
   - Cons: Requires runner infrastructure, PVC access from runner pod

2. **Tagged custom Docker images**
   - Build a custom AppDaemon Docker image with Python deps baked in
   - Tag images on merge (e.g. `ghcr.io/thaynes43/appdaemon:latest`)
   - Kubernetes deployment references the image tag
   - Pros: Immutable deploys, rollback via image tags, no PVC sync
   - Cons: Requires revamping deployment model, Dockerfile, registry setup

3. **Hybrid: Runner builds + pushes image, ArgoCD/Flux deploys**
   - GitHub Actions builds Docker image on merge
   - GitOps controller detects new image and rolls the deployment
   - Pros: Full GitOps, clean separation
   - Cons: Most infrastructure to set up

### Next steps
- Decide on approach
- Prototype the chosen option
- Add workflow file

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
