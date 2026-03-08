# Security rules for Codex

Canonical policy:

- `.cursor/rules/security-policy.mdc`

Shared audit playbook:

- `.agents/playbooks/security-audit.md`

## Quick reminders

- No credentials in `appdaemon/apps/` code.
- External HTTP integrations belong in `appdaemon/providers/`.
- Never expose secrets to frontend code, events, or state.
- `secrets.yaml` and `.env` stay uncommitted.
- Use `_env` keys for credential-like config in app YAML.
