# Git workflow for AppDaemon development

> **Applies to:** `appdaemon/**`

## Branch strategy

All work must go through a feature branch and pull request. Never commit directly to `main`.

### Branch naming

| Type | Pattern | Example |
|------|---------|---------|
| Feature | `feature/<short-description>` | `feature/add-doorbell-detection` |
| Bug fix | `fix/<short-description>` | `fix/door-notify-consolidation` |
| Documentation | `docs/<short-description>` | `docs/add-provider-readmes` |
| Refactor | `refactor/<short-description>` | `refactor/door-notify-package` |

### Workflow

1. **Create branch** from `main`:
   ```bash
   git checkout main && git pull
   git checkout -b feature/my-feature
   ```

2. **Commit often** with clear messages:
   ```
   appdaemon: <imperative summary>

   <optional body explaining why>
   ```
   Prefix with `appdaemon:` for AppDaemon changes, `home-assistant:` for HA YAML, `ci:` for workflow changes.

3. **Push and create the PR ready for review** against `main`:
   ```bash
   git push -u origin feature/my-feature
   gh pr create --title "appdaemon: short description" --body "..."
   ```
   Never `--draft`. Opening ready triggers the Code Review and docs-audit workflows; that spend is intended for agent PRs (Tom, 2026-09-04).

4. **Required validation must pass** before merge:
   - Unit tests (`.github/workflows/unit-tests.yml`)
   - Docs build (`.github/workflows/docs-build.yml`) — if docs/** changed
   - Image build (`.github/workflows/build-appdaemon.yml`) — if its inputs changed
   - Applicable lint and security checks, including the pre-deploy security audit
   - GitHub-required statuses: `test`, `docs-build`, `build-and-push` (irrelevant changes report successful skips)

   Agent Docs Audit, Docs Site Audit, Claude Code Review, and other automated AI reviews or audits are optional and advisory; follow the availability policy below.

5. **Merge it yourself** once required validation passes and substantive review findings are addressed, then
   ```bash
   gh pr merge <number> --squash --delete-branch
   gh pr view <number> --json state   # expect MERGED
   ```
   Do not leave a validated PR waiting for the owner; he does not mark PRs ready or merge them. Fix each substantive review finding or state a concrete reason on the PR why it does not apply. AI service availability never adds an owner approval step.

6. **After merge to main**, GitHub Actions builds and pushes `ghcr.io/thaynes43/appdaemon:<VERSION>` — that part is automatic. **The rollout is not.** The haynes-ops HelmRelease pins `tag:`, so nothing runs the new image until you bump it: branch + PR in haynes-ops (`kubernetes/main/apps/home-automation/appdaemon/app/helmrelease.yaml`, that file only), self-merge, `flux reconcile kustomization appdaemon -n home-automation --with-source`, `kubectl rollout status deploy/appdaemon -n home-automation`, then confirm the pod runs `:<VERSION>` and the changed behaviour is visible in its logs. (1.16.5 was released and never rolled out because this step was assumed automatic.) **After any AppDaemon restart (a rollout included), `sensor.health_check_status` reads `unknown` for about 30–35 minutes. That is by design, not a regression.** The `*_lights` checkers (`MqttDeviceChecker`: basement, downstairs, upstairs, exterior) report each device `unknown` ("no MQTT data yet") until they have heard an MQTT message from it since startup; retained messages in the first 5 s are skipped. The controller's overall status is `unknown` whenever nothing is bad but not every checker is `ok`, and `unknown` never pages. On 2026-09-26, 09-27 and 09-28 it took about 35 minutes to reach `ok`. So to verify a deploy, read the checker you changed (`checkers.<id>.status` on `sensor.health_check_status`) and the AppDaemon log; if it still reads `unknown`, re-read it after one of its `check_interval_s`. Don't wait for the overall state, and don't chase the lights checkers' `unknown` (Tom, 2026-09-28). The one thing in that window that does page is a light whose HA state read fails: it reads `critical` instead of `warning` while its MQTT row is still `unknown`. How to tell that from a real outage, and when a slow warm-up is worth a look, is in `agent-docs/shepherd-runbooks/README.md`, universal precondition *Fresh AppDaemon restart → re-check, don't remediate*. The full chain, including the GHCR manifest check, is in haynes-ops `CLAUDE.md` → *AppDaemon Deploys*.

## AI review availability

Claude Code Review, Agent Docs Audit, Docs Site Audit, and all other automated AI reviews or audits are **optional and advisory**. Do not add them to branch protection or a required-checks list. Keep the workflows available, but their statuses are not merge or deployment gates.

An AI job that is unavailable, failed, skipped, or still pending does not block delivery and needs no owner approval. This includes quota exhaustion, authentication failure, provider outage, workflow setup failure, timeout, and turn-budget exhaustion. Do not keep retrying or waiting for an AI service once the change is otherwise ready.

Read findings already posted by current and earlier runs, even when the job failed. Fix each substantive finding or respond on the PR with a concrete reason it is wrong or does not apply. Review the diff and relevant documentation yourself, and use an independent native agent review when available. Record the review, validation and AI availability in the PR before merging. Native agent availability does not add a merge or deployment gate.

Required tests, lint, documentation and image builds, security checks, runtime verification, and deploy verification remain mandatory. AI unavailability does not excuse their failure or unresolved substantive findings.

## PR requirements

### Title format
Keep under 70 characters. Use the commit prefix convention: `appdaemon: <description>`.

### Body format
```markdown
## Summary
- Bullet points describing changes

## Test plan
- [ ] Unit tests pass
- [ ] Manual testing done (if applicable)
```

### What triggers CI

All workflows trigger on every PR but skip expensive work when irrelevant files aren't changed. This ensures required checks always report a status to GitHub.

| Workflow | Runs on | Skips if no changes to | Gate |
|----------|---------|----------------------|------|
| Unit Tests | Every PR push + push to `main` | `appdaemon/` | Required to pass |
| Docs Build | Every PR push | `docs/`, `mkdocs.yml` | Required to pass |
| Agent Docs Audit | Every PR push (`opened`, `synchronize`, `ready_for_review`, `reopened`) | `appdaemon/` | Optional, advisory |
| Docs Site Audit | Every PR push (same four types) | `appdaemon/`, `home-assistant/`, `docs/` | Optional, advisory |
| Code Review | Every PR push (same four types) | — | Optional, advisory |
| Build AppDaemon | PR push (build+push dev tags) + push to `main` (build+push semver tags) | `appdaemon/`, `docker/`, `VERSION` | Required to pass |
| CI Auto-Fix | Unit Tests workflow fails on PR | — | Auto-creates fix commit |
| Deploy Docs | Push to `main` | `docs/`, `mkdocs.yml` | Deploys to GitHub Pages |

## Versioning

The `VERSION` file at the repo root controls Docker image tags.

- Agents must bump `VERSION` on the feature branch before creating or updating a PR, unless the user explicitly says not to.
- A PR is not ready to open if the branch changes AppDaemon code, Docker build inputs, or deployment behavior and `VERSION` has not been updated.
- The merge to `main` then automatically produces the new semver tag.

| Change type | Version bump | Example |
|---|---|---|
| New feature / app | Minor | `0.1.0` → `0.2.0` |
| Bug fix / cosmetic | Patch | `0.1.0` → `0.1.1` |
| Breaking / architectural | Major | `0.1.0` → `1.0.0` |

On PRs, dev tags include the version + branch name (e.g. `0.2.0-my-feature.abc1234`). On merge to `main`, semver tags are pushed (`0.2.0`, `0.2.0-abc1234`, `latest`).

## Commit conventions

- One logical change per commit
- Run tests before committing: `cd appdaemon && python -m pytest tests/ -v --tb=short`
- Never commit secrets, `.env`, `secrets.yaml`, or credentials
- Never force-push to `main`
- Use `Co-Authored-By:` trailer when AI-assisted

## When agents create PRs

Agents (Claude Code, Cursor, etc.) creating PRs from local sessions must:
1. Create a feature branch (never commit to `main`)
2. Run tests locally before pushing
3. Bump `VERSION` using semver before opening the PR, unless the user explicitly opts out
4. Create the PR **ready for review** (`gh pr create`, never `--draft`)
5. Include a test plan in the PR body: what was verified live and how, plus a **Not verified** line for anything that could not be
6. Reference the issue/story if one exists
7. Wait for required tests and builds, complete applicable lint and security checks, and read available AI findings. Review the diff and relevant documentation yourself, and use an independent native agent review when available. Record the review, validation and AI availability in the PR. Address substantive findings, then squash-merge the PR yourself and delete the branch; no owner approval is needed for AI unavailability
8. Finish the deploy chain (Workflow step 6; for card JS, the copy into the HA pod plus the `?v=N` bump) in the same session — merged is not done. `.agents/rules/finish-in-flight-work.md`.

PRs created by agents via GitHub (e.g. `@claude` in issues) open as ready immediately — Code Review will run on those automatically.
