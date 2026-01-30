# hass-sandbox

This repo is a **working subset of Home Assistant YAML** (automations, scripts, helpers, and Lovelace card snippets) that I keep in Git for safer edits, diffs, and reuse.

## What’s in here

- **`automations/`**: Automations copied from the Home Assistant UI YAML editor.
  - **`automations/occupancy-based-lighting/`**: Per-zone “motion detected → lights on” and “motion cleared → lights off” automations.
  - **`automations/switch-buttons/`**: Inovelli button/hold mappings that call scripts.
- **`scripts/`**: Script YAML used by automations and dashboards.
  - **`scripts/inovelli/`**: Shared + entity-defined scripts for Inovelli mmWave “hold” behavior, LED indicators, and group operations.
- **`helpers/`**: Helper entities (e.g. `input_select`) used as “normal mode” source-of-truth.
- **`cards/`**: Lovelace/Bubble Card YAML snippets (popups, history, advanced panels).

## Key conventions (important)

### Inovelli “hold” LED colors are state

- **Reserved colors**: Any LED bar color used to indicate “hold” is *reserved for that purpose only* (do not reuse for other notifications).
- **Manual vs automation holds**: Must use different fixed colors / identifiers:
  - Manual hold: `input_text.inovelli_manual_hold`
  - Auto/automation hold (e.g. cleaners-mode): `input_text.inovelli_auto_hold`
- **Clearing behavior**: Automations that clear holds must only clear holds matching the **automation hold** color so manual holds are preserved.

### Migration in progress: `normal_mode` → `normal_mode_input_select`

We are migrating away from hardcoded script parameters like `normal_mode: ...`.

- **Rule**: when editing a per-switch block, match `switch_name` first, then replace *only in that same block*:
  - Replace `normal_mode: ...`
  - With `normal_mode_input_select: input_select.<zone>_mmwave_normal_mode`
- If a zone uses `normal_mode_input_select`, ensure it’s included in:
  - `automations/inovelli_mmwave_normal_mode_sync_input_select.yaml`

## Notes

- These files are **snippets**, not a complete Home Assistant configuration.
- Sources of truth for “known good” Bubble Card patterns live under:
  - `cards/rumpus-room/`, `cards/concessions/`, and `cards/global/`

