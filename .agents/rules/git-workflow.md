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

4. **PR must pass** before merge:
   - Unit tests (`.github/workflows/unit-tests.yml`)
   - Docs build (`.github/workflows/docs-build.yml`) — if docs/** changed
   - Agent docs audit (`.github/workflows/agent-docs-audit.yml`)
   - Docs site audit (`.github/workflows/docs-site-audit.yml`)
   - Code review (`.github/workflows/claude-code-review.yml`)

5. **Merge it yourself** once every check above is green: address any Code Review findings with follow-up commits, then
   ```bash
   gh pr merge <number> --squash --delete-branch
   gh pr view <number> --json state   # expect MERGED
   ```
   Do not leave a green PR waiting for the owner; he does not mark PRs ready or merge them.

6. **After merge to main**, deployment is automatic — GitHub Actions builds the Docker image, Flux rolls the Kubernetes deployment.

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
| Agent Docs Audit | PR marked ready / reopened | `appdaemon/` | Required to pass |
| Docs Site Audit | PR marked ready / reopened | `appdaemon/`, `home-assistant/`, `docs/` | Required to pass |
| Code Review | PR marked ready / reopened | — | Required to pass |
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
7. Wait for every check (unit tests, docs build, both docs audits, Code Review), address findings, then squash-merge the PR yourself and delete the branch

PRs created by agents via GitHub (e.g. `@claude` in issues) open as ready immediately — Code Review will run on those automatically.
