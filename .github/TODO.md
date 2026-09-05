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
**Status**: Done — ruleset "Protect Main" is active

- Required status checks (exact context names, do not rename these jobs):
  `test` (unit-tests.yml), `docs-build` (docs-build.yml), `build-and-push`
  (build-appdaemon.yml). Each filters paths *inside* the job so a skipped run
  still reports success — a job-level `if:` would leave the check missing and
  block every PR.
- `strict_required_status_checks_policy` is on: a branch must be up to date
  with `main` before it can merge.
- Pull request required (0 approvals), linear history, no force pushes, no
  deletions. There are no bypass actors, so `GITHUB_TOKEN` cannot push to
  `main` from any workflow.

## Documentation audit workflow improvements

**Priority**: Medium
**Status**: Deployed as `agent-docs-audit.yml` + `docs-site-audit.yml`

Both use Claude to review PR diffs for documentation consistency and post a
single severity-tagged comment. Future improvements:
- Add structured output format for audit results (`claude_args: --json-schema`)
- Consider making the audits required checks (they are advisory today)
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

## Pin the AppDaemon base image

**Priority**: Medium
**Status**: Not started

`docker/Dockerfile` starts `FROM acockburn/appdaemon:latest`. A rebuild can
therefore silently pull a new AppDaemon major/minor into production with no
commit to point at. Pin the concrete tag currently running (4.5.13) and bump it
deliberately, so image builds are reproducible and an AppDaemon upgrade shows up
as a reviewable diff.
