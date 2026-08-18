# Tom Mobile Dashboard (`tom-mobile`)

Seed doc for the **Tom Mobile** storage-mode dashboard (sidebar title "Tom Mobile", icon
`mdi:face-man-shimmer`, `require_admin: false`). Created 2026-08-17. This is the Tom-facing
counterpart to **Kellie Mobile** (`kellie-mobile`) and will grow over time — read this before
iterating on it.

- **Live source of truth**: the storage dashboard in HA (edit via MCP, see
  `.agents/playbooks/ha-dashboard.md`).
- **Repo backup**: `home-assistant/dashboards/tom-mobile.yaml` — keep it in sync after any live edit
  (same convention as `wall-display.yaml`).

## Layout (single sections view, `max_columns: 1`, flow = outdoors → in)

| Section (bubble separator) | Cards |
|---|---|
| Outdoors | "Outdoor Lights" bubble button: Calla / Lily / Floodlight toggles + Flood Hold |
| Pool | "Pool" bubble button: Lights toggle, Color → `#tom-pool-lights`, Water temp + Set point chips → `#tom-pool-heat` |
| Bike Chargers | 2-col grid: E-Bike / Mom Bike switch cards with dynamic charging icon + live W draw |
| First Floor | "First Floor Lights" toggle (`light.first_floor_chaos_lights`) |
| Bedroom | Primary Bedroom scene card (ported from Kellie Mobile) → `#tom-primary-bedroom` |
| Climate | Climate Control card w/ 68°/72° presets (ported) → `#tom-climate-control` |
| Doors & Locks | Locks card → `#tom-locks`, Garage Doors card → `#tom-garage-doors` |

Pop-up hashes are all `#tom-*`. Ported Kellie cards are verbatim copies except the hash renames —
if Kellie Mobile's versions get improved, consider porting the improvements here (and vice versa).

## Key entities

### Outdoors
- Front yard: `light.front_yard_hue_calla_lights`, `light.front_yard_hue_lily_lights` (Hue groups).
- Backyard floodlight ("spotlight" elsewhere): `light.downstairs_kitchen_back_yard_spotlight`
  (Inovelli Blue dimmer).
- **Flood Hold**: there is NO hold input_boolean. Manual hold = the three backyard occupancy
  automations disabled + switch LED set to the manual-hold color. The toggle calls
  `script.inovelli_toggle_mmwave_hold_led_indicator` with:
  ```yaml
  switch_name: downstairs_kitchen_back_yard_spotlight
  hold_color: input_text.inovelli_manual_hold
  prev_led_color_helper: input_number.outdoor_light_inovelli_led_color
  automation_entity_ids:
    - automation.switch_back_yard_slider_opens_turn_on_spotlight
    - automation.switch_back_yard_camera_detects_motion_turn_on_lights
    - automation.switch_back_yard_camera_stops_detecting_motion_turn_off_spotlight
  ```
  This is the exact payload used by the switch's Config-2x mapping and by the Automations
  dashboard (`dashboard-debug` backyard popup, repo copy:
  `home-assistant/cards/outdoors/backyard/outdoors-backyard-history.yaml`). **Always go through the
  script** — toggling the automations directly desyncs the switch LED indicator. Hold state is read
  from `automation.switch_back_yard_slider_opens_turn_on_spotlight` (`off` = hold active; card shows
  amber + `mdi:lock`).

### Pool (Pentair IntelliCenter, "Haynes Res.")
- Lights: `light.haynes_res_pool_lights` — on/off only; colors/shows are the light's `effect` list:
  SAm, Party Mode, Caribbean, Sunset, Romance, American, Royal, White, Red, Blue, Green, Magenta.
  Color buttons call `light.turn_on` with `effect:`. Active effect is highlighted (the `effect`
  attribute only persists while on).
- Heat: `climate.haynes_res_pool` is `heat_cool`; the **heating set point is `target_temp_low`**
  (integration quirk — `target_temp_high` is the pool max temp, mirrored by
  `number.haynes_res_pool_max_temperature`). Water temp: `sensor.haynes_res_pool_last_temp`.
  Pump: `binary_sensor.haynes_res_vsf` + `sensor.haynes_res_vsf_rpm`.

### Bike chargers (Z-Wave Zooz plugs in the shed)
- Switches: `switch.shed_ebike_power_switch`, `switch.shed_mombike_power_switch`.
- Instant draw: `sensor.shed_{ebike,mombike}_power_switch_electric_consumption_w`.
- **Charging detection helpers** (created 2026-08-17 via MCP; no repo YAML per helper convention):
  - `statistics` helpers → `sensor.e_bike_charger_power_15m_avg`,
    `sensor.mom_bike_charger_power_15m_avg` (mean of the W sensor, `max_age` 15 min,
    sampling_size 500, precision 1).
  - `threshold` helpers → `binary_sensor.e_bike_charging`, `binary_sensor.mom_bike_charging`
    (upper 5 W, hysteresis 2 → on above 7 W avg, off below 3 W avg; device_class
    `battery_charging`).
  - Semantics: charger drawing meaningful power in the last ~15 min = charging. Trickle/idle
    (<3 W avg) = done. Card icon: `mdi:battery-charging` (green pulse) → charging,
    `mdi:battery-check` → powered but done, `mdi:power-plug-off` → switch off.
  - Gotcha: statistics/threshold helpers created via config flow auto-prefix the source device name
    into the entity_id (`sensor.shed_shed_ebike_power_switch_...`). Both were renamed via the entity
    registry right after creation — if a helper is ever recreated, re-apply the clean entity_id.

### First floor lights
- `light.first_floor_chaos_lights` — group helper (created 2026-08-17) mirroring exactly what the
  **Upstairs Foyer Chaos** Inovelli Config-1x button turns off (see
  `agent-docs/button-mappings.md`):
  `light.downstairs_kitchen_sink_inovelli_presence`, `light.downstairs_kitchen_island_inovelli_dimmer`,
  `light.downstairs_kitches_under_cabinet_inovelli_dimmer` (typo is real), `light.downstairs_kitchen_lights`
  (itself a Hue group — nesting is fine). If the switch mapping changes, update the group membership too.

### Ported from Kellie Mobile (shared entities — do not fork without reason)
- Bedroom scenes: `script.kellie_mobile_primary_bedroom_{sleep,bedtime,relaxed,focused}` (shared).
- Locks status text: `input_text.kellie_entry_locks_status`, maintained by
  `automation.kellie_mobile_entry_locks_status`. Reused as-is; if Tom ever needs different status
  logic, provision a `input_text.tom_entry_locks_status` + automation instead of changing Kellie's.
- Locks use the pending-state pattern: buttons set `input_select.<door>_lock_pending` to
  `locking`/`unlocking`; an automation elsewhere performs the lock action and clears/errors the
  pending state. Cards never call `lock.lock` directly.
- Garage: `cover.ratgdov25i_4a0325_door` (Tesla), `cover.ratgdov25i_dbfa50_door` (Wagoneer),
  camera `camera.garage_g5_dome_medium_resolution_channel`.

## How to edit / regenerate

- Small edits: `ha_config_get_dashboard(url_path="tom-mobile", entity_id=...)` →
  `ha_config_set_dashboard(python_transform=..., config_hash=<FULL hash>)`. Never truncate the hash.
- `find_card` cannot see inside bubble pop-up `cards:` lists — for popup edits, index by card
  position (popups are the last 6 cards of section 0) or do a full-config get.
- The original seed was generated by a Python builder script (session scratchpad,
  `build_tom_mobile.py`) that emitted both the live JSON and `tom-mobile.yaml`. For large
  restructures, that pattern (build dict in Python → dump JSON + YAML → one full-config
  `ha_config_set_dashboard`) beats many incremental transforms.
- After any live edit, sync `home-assistant/dashboards/tom-mobile.yaml`.

## Learnings / conventions carried over

- Bubble-card `styles` JS: sub-button highlight via `.bubble-sub-button-N { background-color: ${...} }`
  (accent = active, `rgba(0,0,0,0.22)` = idle); dynamic icons via `subButtonIcon[i].setAttribute("icon", ...)`
  (0-indexed, main buttons then bottom). Numbering spans all groups in a `sub-buttons` card.
- Separators (`card_type: separator`) give the section headers, same look as Kellie Mobile.
- Sub-button `tap_action: {action: toggle}` toggles that sub-button's own entity.
- `perform-action` and `call-service` are interchangeable; newer cards here use `perform-action`.

## Backlog / iteration ideas

- Tom-specific `input_text.tom_entry_locks_status` if the status line should differ from Kellie's.
- Consider a Snapshot Info section (weather/calendar) and the health-check card like Kellie's.
- Pool: `switch.haynes_res_pool_high` (high-speed pump) could join the pool popup.
- Docs site: add a `docs/features/` page once the dashboard stabilizes.
