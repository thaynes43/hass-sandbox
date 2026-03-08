# Home Assistant YAML rules

When working in `home-assistant/`, read these for full detail:
- `.cursor/rules/appdaemon-vs-ha-yaml.mdc` — when to use AppDaemon vs HA YAML, deploy procedure
- `.cursor/rules/ha-change-scope-communication.mdc` — required communication protocol (always applies)
- `.cursor/rules/button-mappings-doc-sync.mdc` — button mapping doc must stay in sync
- `.cursor/rules/night-lights-import.mdc` — how to import night light automations from HA
- `.cursor/rules/ha-entity-and-device-settings.mdc` — entity vs device registry, area assignment via MCP

## Required: scope communication

After every `home-assistant/` change, start the response with exactly one of:
- **Repo YAML Only Updated - You copy paste**
- **Repo YAML & Live HA Updated**

Then list: what needs copy-pasting, what was updated in repo, and (if live updated) which entity_ids were created/updated.

Dependent changes (helpers + automations + scripts + button mappings) must be applied as a set or not at all.

## Required: button mapping doc sync

Any change to `home-assistant/automations/switch-buttons/**` or related blueprints must update `agent-docs/button-mappings.md` in the same session.

## Relevant playbooks

- `.agents/playbooks/ha-automations-scripts.md` — create/update automations and scripts via MCP
- `.agents/playbooks/ha-helpers.md` — create/update/delete helpers via MCP
- `.agents/playbooks/ha-dashboard.md` — edit dashboard views/cards via MCP (config_hash pitfall!)
- `.agents/playbooks/occupancy-based-lighting.md` — add/update occupancy-based lighting zones
