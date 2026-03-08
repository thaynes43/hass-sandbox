# AppDaemon rules for Codex

When working in `appdaemon/`, read these canonical rule files first:

- `.cursor/rules/appdaemon-architecture.mdc`
- `.cursor/rules/appdaemon-coding-guidelines.mdc`
- `.cursor/rules/appdaemon-dev-environment.mdc`
- `.cursor/rules/ai-provider-archetecture-guidelines.mdc`
- `.cursor/rules/security-policy.mdc`

Also read these shared playbooks when the task matches:

- `.agents/playbooks/appdaemon-deploy.md`
- `.agents/playbooks/ha-provisioner.md`
- `.agents/playbooks/appdaemon-ai-provider.md`
- `.agents/playbooks/detection-app.md`
- `.agents/playbooks/security-audit.md`

## Quick reminders

- Shared libraries belong in `appdaemon/providers/`, not `appdaemon/apps/`.
- Apps importing providers need the `sys.path.append(str(Path(__file__).resolve().parents[2]))` pattern.
- Apps should self-provision HA helpers and scripts where possible.
- Prompt policy belongs in app-level prompt builders, not provider adapters.
- Dev app keys end in `_dev`.
