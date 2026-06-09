# AppDaemon rules

When working in `appdaemon/`, read these for full detail:
- `.agents/rules/appdaemon-architecture.md` — system overview, folder structure, self-provisioning, relay script pattern, new app checklist
- `.agents/rules/appdaemon-coding-guidelines.md` — apps vs shared libs, AI offloading, dev/prod naming
- `.agents/rules/appdaemon-dev-environment.md` — venvs, test commands, cross-platform (Linux/WSL/Windows)
- `.agents/rules/appdaemon-documentation.md` — README requirements, documentation map, app dependency graph
- `.agents/rules/ai-provider-architecture-guidelines.md` — capability bundles, model settings, prompt policy layering
- `.agents/rules/logging-standards.md` — log levels, required logging points, formatting conventions
- `.agents/rules/security-policy.md` — always applies; see also `.claude/rules/security.md`
- `.agents/rules/git-workflow.md` — branching, PRs, CI gates, commit conventions

## Key decisions to know before coding

### Apps vs providers
- `appdaemon/apps/` — AppDaemon app modules only (referenced by `apps-prod.yaml` / `apps-dev.yaml`)
- `appdaemon/providers/` — shared libraries used by multiple apps; baked into Docker image at `apps/providers/`
- Never put shared libs inside `appdaemon/apps/`

### sys.path fix (required for all apps importing providers)
```python
import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parents[2]))  # adds appdaemon/ root
```
`.parents[2]` assumes `appdaemon/apps/<pkg>/module.py` layout.

### Self-provisioning
Apps call `ha_provisioner` on startup to create all needed HA entities — never tell users to create helpers manually. Helpers, relay scripts: provisioned. Shell commands, Lovelace resources, `local_file` cameras: manual (document in app README).

### Relay script (card → AppDaemon)
Cards call `hass.callService("script", "<app>_relay", { command, payload })`. Never use `fire_event` (requires admin). AppDaemon listens for `<app>_command` event. Full template in `.agents/rules/appdaemon-architecture.md` §3.

### Dev/prod app naming
- Dev keys end in `_dev`; prod keys do not; prod entries always have `disable: true`

### Relevant playbooks
- `.agents/playbooks/appdaemon-deploy.md` — Docker image build and deploy process
- `.agents/playbooks/ha-provisioner.md` — add self-provisioning to an app
- `.agents/playbooks/appdaemon-ai-provider.md` — add a new AI provider
- `.agents/playbooks/detection-app.md` — add a new detection summary entrance
- `.agents/playbooks/security-audit.md` — pre-deploy audit
