# Home Assistant YAML rules

When working in `home-assistant/`, read these for full detail:
- `.agents/rules/hass.md` — project overview, YAML formatting, non-admin frontend rule (always applies)
- `.agents/rules/appdaemon-vs-ha-yaml.md` — when to use AppDaemon vs HA YAML, deploy procedure
- `.agents/rules/ha-change-scope-communication.md` — required communication protocol (always applies)
- `.agents/rules/button-mappings-doc-sync.md` — button mapping doc must stay in sync
- `.agents/rules/night-lights-import.md` — how to import night light automations from HA
- `.agents/rules/ha-entity-and-device-settings.md` — entity vs device registry, area assignment via MCP

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
