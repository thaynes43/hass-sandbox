# Tom Mobile Dashboard (`tom-mobile`)

Seed doc for the **Tom Mobile** storage-mode dashboard (sidebar title "Tom Mobile", icon
`mdi:face-man-shimmer`, `require_admin: false`). Created 2026-08-17. This is the Tom-facing
counterpart to **Kellie Mobile** (`kellie-mobile`) and will grow over time — read this before
iterating on it.

- **Live source of truth**: the storage dashboard in HA (edit via MCP, see
  `.agents/playbooks/ha-dashboard.md`).
- **Repo backup**: `home-assistant/dashboards/tom-mobile.yaml` — keep it in sync after any live edit
  (same convention as `wall-display.yaml`).

## Layout (five tabs, each a sections view with `max_columns: 1`)

Tom split the single view into tabs on 2026-09-29. Tab order, `path` and icon:

| Tab | `path` | Icon | Holds |
|---|---|---|---|
| Main (default) | `main` | `mdi:home-assistant` | high-priority and general cards (no view header: Tom dropped the "Welcome Tom!" one on 2026-09-29) |
| Outdoor | `outdoor` | `mdi:tree` | outdoor lights, pool, bike chargers |
| Basement | `basement` | `mdi:home-floor-b` | Climate (Basement Climate), then Status (humidors, UPS load, leak detection), then Main Lights (Concessions, Rumpus, Great Hall), then the Movie Room cards, then Other Rooms (Server Room Lights, Storage Room Lights) |
| First floor | `first-floor` | `mdi:home-floor-1` | placeholder ("No cards yet.") |
| Second floor | `second-floor` | `mdi:home-floor-2` | placeholder ("No cards yet.") |

A bubble-card pop-up only opens from cards in its own view, so each pop-up lives in the same tab
as the cards that open it: Main has `#health-check-popup`, `#tom-primary-bedroom`,
`#tom-climate-control`, `#tom-locks` and `#tom-garage-doors`; Outdoor has `#tom-pool-lights` and
`#tom-pool-heat`; Basement has `#tom-basement-climate`, `#tom-movie-ambient`,
`#tom-movie-receiver`, `#tom-server-room-ups`, `#tom-cigars`, `#tom-leak-detection`,
`#tom-concessions-lights`, `#tom-rumpus-lights` and `#tom-great-hall-lights`. Each tab's pop-ups sit at the end of its card list. Nothing links into a
view by path or index: `/tom-mobile` (Tom's default panel) opens Main. When a floor tab gets
cards, replace its "No cards yet." markdown card.

**Main**

| Section (bubble separator) | Cards |
|---|---|
| (none, first card) | "Phone Assist" bubble button: status badge from the **Voice** health checker, state line `sensor.phone_assist_status`, sub-buttons **Chat** / **Voice** (see *Assist on Tom's iPhone* below) |
| First Floor | "First Floor Lights" toggle (`light.first_floor_chaos_lights`) |
| Bedroom | Primary Bedroom scene card (ported from Kellie Mobile) → `#tom-primary-bedroom` |
| Climate | Climate Control card w/ 68°/72° presets (ported) → `#tom-climate-control` |
| Doors & Locks | Locks card → `#tom-locks`, Garage Doors card → `#tom-garage-doors` |
| Health Checks | `custom:health-check-card` → `#health-check-popup` (`custom:health-check-detail-card`, relay `health_check_relay`) — same as wall-display/unifi-connect (`home-assistant/cards/wall-display/health-check-{card,popup}.yaml`) |

First Floor Lights, Primary Bedroom and Climate Control (both floors' ecobees; its pop-up also has
the basement mini splits) are on Main because the 2026-09-29 split moved only the outdoor and Movie
Room cards off it.

**Outdoor**

| Section (bubble separator) | Cards |
|---|---|
| Outdoors | "Outdoor Lights" bubble button: Calla / Lily / Floodlight / Motion toggles + Flood Hold |
| Pool | "Pool" bubble button: Lights toggle, Color → `#tom-pool-lights`, Water temp + Set point chips → `#tom-pool-heat` |
| Bike Chargers | 2-col grid: E-Bike / Mom Bike switch cards with dynamic charging icon + live W draw |

**Basement**

| Section (bubble separator) | Cards |
|---|---|
| Climate | "Basement Climate" (Dry / 72° / Off per mini split) → `#tom-basement-climate` — see *Movie Room* below |
| Status | "Humidors": the aggregate humidity status only ("All OK" / "Check: …"); tap → `#tom-cigars` (humidity, temperature and battery per humidor) — see *Cigars* below. "UPS Load": one chip per UPS with its load %, tinted yellow at 70 % and red at 85 %; tap → `#tom-server-room-ups` (live W + 6 h graph per UPS) — see *Server Room* below. "Leak Detection": Dry / Wet across the five Z-Wave leak sensors; tap → `#tom-leak-detection` (state + battery per sensor) — see *Leak Detection* below |
| Main Lights | "Concessions", "Rumpus", "Great Hall": power icon toggles the room, state line = brightness (+ ` · Hold` while held), **Bright** / **Dim**; tap → that room's pop-up (hold is set there) — see *Main Lights* below |
| Movie Room | "Receiver" (tap → `#tom-movie-receiver`, power icon, −/dB/+, current input; the pop-up has the volume slider, input buttons and Plex/YouTube/Xbox presets), "Recessed" (brightness slider + Bright, Dim, Colors), "Ambient" (tap → `#tom-movie-ambient`, power icon toggles, ◀ ▶ previous/next scene; the pop-up has the dimmer: effects + all 75 gradient scenes) — see *Movie Room* below |
| Other Rooms | "Server Room Lights": **Lights** (group on/off) and **Hold** (the switch's manual hold) sub-buttons — see *Server Room* below. "Storage Room Lights": **Lights** toggle only — see *Storage Room* below |

Tom asked on 2026-09-30 for a Status section at the top of the tab. It holds Humidors, UPS Load and Leak Detection.
Humidors and UPS Load moved there from the Cigars and Server Room sections, and the Cigars section is gone.
Later that day Tom moved Basement Climate out of Movie Room into its own Climate section above Status (high priority).

Pop-up hashes are all `#tom-*`. Ported Kellie cards are verbatim copies except the hash renames —
if Kellie Mobile's versions get improved, consider porting the improvements here (and vice versa).

## Assist on Tom's iPhone (added 2026-09-26)

Tom's iPhone app is logged in as **`thaynes`, a non-admin user**, and he wants it kept that way. The
app's Assist widget, Control Center control, Action Button and "Assist in app" Shortcut all pick a
pipeline from a list the app caches on the phone, and that list starts empty ("Can't find your Assist
pipeline? Open Assist in the app to refresh pipelines list."). The app refills it every time its
native Assist screen opens, which calls `assist_pipeline/pipeline/list`. Core allows that call and
`assist_pipeline/run` for non-admins; only creating, editing and setting the preferred pipeline need
admin. The app does not refresh the list at launch or on a timer. A pipeline created later reaches the
pickers after native Assist is opened again, or after the refresh button in an in-app picker is tapped.
(Read from iOS@`2bca3df`, frontend@`5c187a4` and core@`087bf87` source on 2026-09-26.)

- **Pipeline:** Phone Assist `01m3fwd8phf6qxyaax31evjt7a`, a roomless agent with no persona (Tom wanted
  "something equivalent to Siri"). Its settings and prompt are in `agent-docs/voice-agent-prompts.md`.
- **Card** (the first card; Tom asked on 2026-09-26 for a status badge plus Chat and Voice as sub-buttons, so he can choose):
  - **Chat** / **Voice** sub-buttons: `tap_action: {action: assist, pipeline_id: 01m3fwd8phf6qxyaax31evjt7a,
    start_listening: false | true}`. Chat opens Assist ready to type; Voice opens it listening.
    Bubble Card passes the action to HA's `hass-action`, and in the app the frontend sends `assist/show`
    with the pipeline id. The app honours both fields, but its own Assist start-mode setting
    (auto/voice/text) can override `start_listening`.
  - **Status badge:** the main icon and its tint come from the AppDaemon **Voice** health checker
    (`checkers.voice.status` on `sensor.health_check_status`, `health_checks/checker_apps/voice_health_checker`):
    - `ok` = `mdi:check-circle` (green);
    - `critical` = `mdi:alert-circle` (red);
    - `warning`/`degraded` = `mdi:alert` (amber);
    - missing = `mdi:help-circle` (grey).

    The icon is set by the card's `styles` JS, as the bike charger cards do.
  - **State line:** `sensor.phone_assist_status`, a Template helper (config entry `01M3FYTH3MPEWWV0ZVMECW2RFE`,
    created 2026-09-26, live only). It reads `Ready`, `<check names> down`, `<check names> unknown` (masked by
    a dependency, or not run yet), or `Unknown` (no Voice checker reporting). Its state template, as live
    (Settings → Helpers → Phone Assist Status; recreate it as a Template sensor with this state if it is lost):
    ```jinja
    {%- set c = (state_attr('sensor.health_check_status', 'checkers') or {}).get('voice') -%}
    {%- if c is not mapping -%}Unknown
    {%- elif c.status == 'ok' -%}Ready
    {%- else -%}
    {%- set bad = (c.checks or []) | rejectattr('status', 'eq', 'ok') | list -%}
    {%- set down = bad | rejectattr('status', 'eq', 'unknown') | map(attribute='name') | list -%}
    {%- if down -%}{{ down | join(', ') }} down
    {%- elif bad -%}{{ bad | map(attribute='name') | join(', ') }} unknown
    {%- else -%}{{ c.status | title }}
    {%- endif -%}
    {%- endif -%}
    ```
  - **Tapping the card** (icon or body) opens `#health-check-popup`, where the Voice checker's three checks
    and their details are listed.
  - Why a checker: HA's `stt.*`, `tts.*` and `conversation.*` entities stay "available" while the servers
    behind them are down (10 days of history, 2026-09-26: they went unavailable only for seconds during
    entry reloads), so they cannot drive a status.
- **Other ways in for a non-admin:**
  - the dashboard's Assist item (in the ⋮ menu at phone width; there is no admin check);
  - `?conversation=1` on a dashboard URL;
  - `homeassistant://assist?pipelineId=01m3fwd8phf6qxyaax31evjt7a&startListening=true` from a
    Shortcut, which needs no cached list.
  There is no Assist panel, so Assist cannot go in the sidebar.
- **Checked in a browser 2026-09-26** (admin session, Chromium at 390×844 with a fake microphone):
  Voice opened Assist on "Phone Assist" listening, and the pipeline's debug runs showed the STT run;
  Chat opened it in text mode. Not checked on the phone itself.

## Key entities

### Outdoors
- Front yard: `light.front_yard_hue_calla_lights`, `light.front_yard_hue_lily_lights` (Hue groups).
- Backyard floodlight ("spotlight" elsewhere): `light.downstairs_kitchen_back_yard_spotlight`
  (Inovelli Blue dimmer).
- **Motion**: `switch.back_yard_backyard_motion_light_relay` (Shelly 1 Mini Gen4) — powers the
  backyard motion-sensing fixture. Plain toggle sub-button (`.bubble-sub-button-5`, accent tint
  when on). Scheduled by
  `automation.switch_back_yard_midnight_and_sunrise_manage_motion_light` (on at midnight, off at
  sunrise; repo copy under `home-assistant/automations/back-yard/`).
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
- Heat — **the integration exposes the pool body twice; use the `water_heater` entity for
  thermostat UI**:
  - `water_heater.haynes_res_pool` — single set point (`temperature` attr), operation_list
    `[off, UltraTemp]`, `heating_status` attr. Renders as a single heat-style dial in the
    `thermostat` card. This is what Tom Mobile, the wall display, and unifi-connect all use.
  - `climate.haynes_res_pool` — only hvac_mode is `heat_cool` with dual set points
    (`target_temp_low` = heat set point, `target_temp_high` mirrors the pool max temp /
    `number.haynes_res_pool_max_temperature`). A thermostat card on this entity shows a confusing
    heat/cool dual-handle UI — avoid. (The seed originally used it; fixed 2026-08-17.)
- Water temp: `sensor.haynes_res_pool_last_temp`. Pump: `binary_sensor.haynes_res_vsf` +
  `sensor.haynes_res_vsf_rpm`.

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

### Movie Room (added 2026-09-28)
Tom asked for one card each for the receiver (volume, inputs, power), the lights and the thermostats.
Every button reuses a script the voice agents or the wall switches already use, so all three stay in step.
- **Receiver** = `media_player.str_az5000es` (songpal; the Sony STR-AZ5000ES, named "Movie Room
  Receiver" and put in the Movie Room area that day). There are **two cards**, swapped by section
  `visibility` on the receiver being `on`. With the receiver on it is a name card like Ambient: tapping
  the card opens pop-up `#tom-movie-receiver` (`button_action`; so do the current-input button top right
  and the dB readout), the icon is a power button (`tap_action: toggle`, tinted with the accent colour
  while on), and the bottom row is −, the dB readout and +. It was a slider card for the first few minutes
  of the makeover: Tom tapped the card to open the pop-up, hit the power icon and switched the receiver
  off, so the volume slider now lives only in the pop-up.
  With it off, a one-row card with a power icon: tapping anywhere turns it on. One card with hidden
  sub-buttons left a blank row, because the card's height (`rows`) is fixed. Makeover 2026-09-28 (Tom:
  "The Receiver card with all the presets could use the same makeover. We can have input select and
  presets on the popup and volume + power on the card"): the Input select and the presets row moved to
  the pop-up.
  - **`#tom-movie-receiver` pop-up**: the volume slider card (power icon, slider, −/dB/+; shown only
    while the receiver is on), an Input card (one button per source, 3 per row: PS5, Xbox, Shield, HTPC, Sonos,
    Bluetooth, Source, TV; tap = `media_player.select_source`; the current source is tinted) and the
    Presets card below.
  - **Presets** (2026-09-28, the pop-up's Presets card, groups mode): one button per
    `input_number.movie_room_receiver_preset_<name>` (Plex, YouTube, Xbox; -35 to -3.5 dB, the same range as the slider above, so a preset can never pin the slider below its floor; below -19 dB the receiver moves in whole dB, so a half-dB preset there lands on a neighbouring step), showing its icon and saved level. The names are hidden (`show_name: false`, since the Xbox preset
    on 2026-09-28): with three presets each button is 114 px at 390 px, and "YouTube · -29.0 dB" overflowed
    and scrolled. The brand icons already identify each app. A fourth preset would shrink each to about 84 px (the
    volume row's width), untested with icon + level: screenshot at 390 px before adding one.
    Tap = `script.voice_movie_room_receiver_volume` `preset`; hold = `save_preset` with a
    confirmation ("Save the current volume as the Plex level?"), so Tom dials a level in by ear
    and holds the button. A preset lights up (accent) while the receiver sits at its level.
    Sub-button numbering on the Presets card: 1, 2, 3 = Plex, YouTube, Xbox. Adding a preset = a new
    helper with that prefix (live, via `ha_config_set_helper`) + a button in the Presets group
    **+ a `.bubble-sub-button-<n>` block in the Presets card's `styles`** naming that helper (the
    highlight is per index; without it the button works but never lights); voice picks the
    preset up by itself. Hold + `confirmation` works on these sub-buttons: Bubble dispatches the
    hold as HA's `hass-action`, and HA shows "Are you sure?" with Cancel/OK (checked 2026-09-28:
    Cancel runs nothing, OK runs `save_preset`).
  - **Volume ceiling -3.5 dB** (Tom: "I usually won't go louder than -3.5dB"). The slider has
    `min_value: 15` (-35 dB; was 29 = -20 dB until 2026-09-28, when YouTube turned out to sit at -29) and `max_value: 62` (-3.5 dB). Bubble Card v3.4.0 clamps the value to
    [min, max] and then sends `volume_level` = value / 100 (read from the installed `bubble-card.js`), so a
    full-right drag sends 0.62. songpal truncates (`int(volume_level × 100)`), and of all steps 0–100 only
    29, 57 and 58 do not survive `/100` → `×100` in float64 (checked over the whole range), so a slider
    drag can land one step below those three: 0.5 dB at 57/58, 1 dB at 29, which is below the knee (only
    ever lower, never past the cap); the script nudges its level by 1e-6 to hit them exactly.
    − and + call `script.voice_movie_room_receiver_volume` with `down`/`up` and
    1 dB, and that script enforces the same ceiling (`max_db`). **No card opens more-info on the
    receiver**: HA's own media player dialog has an unbounded volume slider. The icon is the power
    button on both cards; on the "off" card, tapping anywhere turns the receiver on. Nothing here can stop the physical
    remote.
  - **dB readout** = `sensor.movie_room_receiver_volume`, a Template helper (entry
    `01M3MJNHG41WNX9H2QM6SZM5BV`, live only). It is unknown while the receiver is off. State:
    `{% set v = state_attr('media_player.str_az5000es', 'volume_level') %}{% if is_state('media_player.str_az5000es', 'on') and v is number %}{% set s = (v * 100) | round(0) %}{{ ((s - 69) / 2) if s >= 31 else (s - 50) }}{% else %}{{ none }}{% endif %}`.
    Scale: 0–100 steps, 0.5 dB per step from step 31 (-19 dB) up with step 69 = 0.0 dB, and 1 dB per
    step below step 31, fitted to two of Tom's display readings (see the script mirror's header). The sensor, the script and the slider bounds must change together.
- **Recessed** (was "Lights" until 2026-09-28) = `light.basement_movie_room_lights` (the card body is
  the brightness slider, and the icon toggles them). The bottom row is Bright, Dim and Colors, the
  `script.voice_movie_room_*` tools (Tom dropped Red from the card on 2026-09-30;
  `script.voice_movie_room_red_night_mode` is unchanged), which do exactly what the scene controller and wall switch buttons
  do (`agent-docs/voice-control-map.md`).
- **Ambient** (2026-09-28; Tom asked for the ambient lights on the dashboard) = a name card on
  `light.basement_movie_room_ambient_lighting`: tapping the card opens `#tom-movie-ambient`
  (`button_action`), the power icon toggles the lights (`tap_action`; tinted with the accent colour while they
  are on, like the receiver's power button), and two icon-only
  sub-buttons step the scene: ◀ (`script.voice_movie_room_ambient_scene` with `scene: previous`) and
  ▶ (next, = ZEN37 button 4). Tom, the same evening: "If we want a lot of scenes on the dashboard a
  popup would be better than sub buttons", then asked for the card itself to open the pop-up with the
  dimmer at its top, and a power icon as the on/off toggle. (It started as a slider card with a "Scene"
  button and a small grid button; he could not tell what they did.)
  - `#tom-movie-ambient` pop-up: the dimmer first (a slider card on the same light, power icon toggles, no
    scene buttons: Tom called ◀ ▶ there redundant), an Effects card (the 12 effects, 3 per row; a solid-red button
    sat in its header until Tom asked what it was, and red is voice-only now), then Warm (29) / Cool (12) / Multicolour (34) scene cards. Warm =
    4+ warm palette colours (hue < 75° or ≥ 290°), Cool = at most 1, Multicolour = the rest. Each scene
    button's background is its palette as a left-to-right gradient, and the current scene (state of
    `select.basement_movie_hue_gradient_65_gradient_scene`) gets a white outline. Rows hold 3 buttons,
    or 2 when a label is longer than 11 characters (bold labels over 11 characters overflow at 114 px).
    Checked at 390 px with Playwright: nothing overflows, and tapping a scene changes the selects.
- **Basement Climate**: one row per mini split (`climate.movie_room_breeze`, `climate.rumpus_room_breeze`;
  Tom sets them individually): a "Movie · Heat" chip (`fill_width: false`, `width: 34`; tap = that
  unit's own thermostat dialog), then Dry, 72° and Off. Those three call `script.voice_thermostat`
  (`thermostat: movie_room|rumpus_room`, `mode: dry` / `mode: heat, temperature: 72` / `mode: off`),
  which already handles the Cielo quirks. Each one gets the accent tint when the unit is in that state
  (72° = heat with a 72 target). Tapping the card opens `#tom-basement-climate`, which has both
  thermostat cards with the hvac-mode bar. `#tom-climate-control` still lists all four thermostats.
- Groups mode: the climate card's `sub_button.bottom` holds entries like `{name, group: [...],
  buttons_layout: inline}`, and `bottom_layout: rows` puts each group on its own row. The `.bubble-sub-button-N`
  numbering runs across all the groups, main buttons first.
- Four sub-buttons per bottom row is the most that fit at iPhone width; five get truncated (Outdoor Lights).

### Server Room (added 2026-09-29)
- **Server Room Lights** (named "Lights" until 2026-09-30, when the section became *Other Rooms*; Tom: "Sometimes I am behind the server rack and the mmwave sensors can't see me"): a name card on
  `light.basement_server_room_lights` (Z2M group: the two Hue bulbs + the Inovelli). The body does nothing; the icon
  opens more-info. Sub-button 1 **Lights** toggles the group (accent while on). Sub-button 2 **Hold** calls
  `script.inovelli_toggle_mmwave_hold_led_indicator` with exactly the payload of the switch's double-tap mapping
  (`automation.switch_inovelli_blue_basement_server_press_or_hold_switch_mappings`): `switch_name:
  basement_server_inovelli_presence`, `normal_mode_input_select: input_select.basement_server_room_mmwave_normal_mode`,
  `hold_color: input_text.inovelli_manual_hold`, `prev_led_color_helper: input_text.basement_server_led_color`, and
  both `automation.light_fp2_server_room_motion_{detected_lights_on,cleared_lights_off}`. Holding sets MmwaveControlWiredDevice
  to `Disabled` (the zone's normal mode is `Wasteful Occupancy`, so the switch itself would otherwise turn the load off),
  turns both FP2 automations off and paints LEDs 1–7 the manual-hold colour; clearing restores all three. Hold is lit
  (amber, `mdi:lock`) on the script's own `is_on_hold` test: LED 1 in a hold colour, or mmWave control `Disabled` while
  the normal mode is not, or either automation off. So a lit button always means "a tap clears it", auto holds included.
  Checked live 2026-09-29 by tapping the card at 390 px: on → both automations off, select `Disabled`, LEDs 90; off →
  all restored (`Wasteful Occupancy`, LEDs 255, automations on).

The **UPS Load** card below has been in the Status section at the top of the tab since 2026-09-30. Its styles
and pop-up did not change when it moved.

Tom: "a bubble card that shows what % each UPS is at … 70% goes yellow and 85% goes red. GPUs need
headroom", and a pop-up with each UPS's live W and a 6-hour W graph. The % is UPS **load**, not battery.
- **UPSes** (NUT, one config entry each): APC 2700W = Smart-UPS X 3000 (`nut2700.haynesnetwork`), APC 900W =
  Back-UPS RS 1500MS2 (`nut01.haynesnetwork`, `nominal_real_power` 900 W).

  | UPS | Load % | Watts |
  |---|---|---|
  | APC 2700W | `sensor.apc_2700w_load` | `sensor.apc_2700w_current_real_power` (NUT `ups.realpower`, measured) |
  | APC 900W | `sensor.apc_900w_01_load` | `sensor.apc_900w_01_watt_load` (derived, see below) |

  The 900W reports no real power. `sensor.apc_900w_01_watt_load` is an older YAML template sensor in the HA pod's
  `/config/packages/sensors.yaml` (not a UI helper): load % × `nominal_real_power` × 0.97. Its load is a whole
  number, so the watts move in steps of about 9 W and its graph looks jumpier than the 2700W's.
  `sensor.apc_2700w_watt_load` and `sensor.ups_watt_load` are orphaned templates (always unavailable); don't use them.
- **Card**: a name card; tapping the card or either chip opens the pop-up. Chip colours come from the card's
  `styles` JS, the same technique as Climate Control: `Math.round(load)` ≥ 85 = `--red-color`, ≥ 70 = `--amber-color`
  (each `color-mix`ed 60 % with transparent; the theme has no `--rgb-red/amber-color`), otherwise the idle
  `rgba(0,0,0,0.22)`; unavailable stays idle. Each chip has two `background-color` declarations: a plain rgba of
  the same colour first, then the `color-mix` one. An engine without `color-mix` drops only the second and still
  shows the warning. Sub-button numbering: 1 = APC 2700W, 2 = APC 900W.
- Both load sensors have display precision 0 (entity setting, 2026-09-29): NUT sends "58.90" or "55", and the
  chip showed "58.90%". The chip therefore always shows a whole number, and the threshold compares that same
  rounded number, so a chip that reads "70%" is always yellow.
- **`#tom-server-room-ups`**: one `custom:mini-graph-card` per UPS (HACS v0.13.0): `hours_to_show: 6`,
  `height: 140` (about 95 px at 390 px width), min/max labels, fill fade. A `card_mod` grid puts the name and the
  live W on one row. mini-graph-card normally stacks them. Re-check it at 390 px after a HA, mini-graph-card
  or card-mod upgrade.
- **Adding a UPS**: a chip in `sub_button.bottom`, a `.bubble-sub-button-<n>` block in `styles` for its load
  sensor, and a mini-graph card in the pop-up. Two chips with these long labels ("APC 2700W · 55%") fill the
  row at 390 px. Before adding a third, screenshot at 390 px and shorten the names if it truncates.
- Checked 2026-09-29 at 390 px with Playwright: tapping the card body and a chip both open the pop-up. The
  threshold logic was rendered on a throwaway preview dashboard at 69 / 69.4 / 69.5 / 70 / 84 / 84.6 / 85 / unavailable.

### Storage Room (added 2026-09-30)
- **Storage Room Lights**: laid out like Server Room Lights, in the *Other Rooms* section (`mdi:floor-plan`; it was
  "Server Room" until 2026-09-30). The card is on `light.basement_storage_inovelli_dimmer` (an Inovelli Blue dimmer
  without mmWave; smart bulb mode off, output mode On/Off). Sub-button 1 **Lights** toggles it, accent while on.
- The ZSE41 door contact drives it: `automation.light_zse41_storage_door_{open_turn_on_lights,closed_turn_off_light}`
  trigger on `binary_sensor.basement_storage_open_closed_sensor_window_door_is_open` and have no conditions.
- **No Hold**: no hold exists for this room (no helpers, no condition on either automation, nothing else pauses
  them; checked 2026-09-30), and Tom said not to build one. A light turned on from the card stays on until the door
  closes or it is toggled off.
- Checked 2026-09-30 at 390 px: the sub-button turned the light on (accent tint) and off again.

### Cigars (added 2026-09-29)
Tom: a pop-up with his cigar sensors, named as on the Basement Tablet dashboard, and a main button that shows only the
aggregate status. The sensors are all in the **Concessions** area, so the card had its own section rather than sitting
under Server Room. Since 2026-09-30 it sits in the Status section at the top of the tab.
- **Aggregate**: `sensor.cigar_humidity_summary`, a YAML template in the HA pod's `/config/packages/sensors.yaml` (also a
  badge on Haynes Home and Basement Tablet). It reads `OK`, or `Check: <names>` built from the `out_of_range` attribute of
  `binary_sensor.cigar_humidity_all_ok` (`/config/packages/binary_sensors.yaml`, band 60–70 % for all nine). The card
  spells `OK` as "All OK" with CSS only (`.bubble-state` font-size 0 + `::after` content), so Bubble's own state updates
  never fight it; `Check: …` is shown as-is with an amber icon, and no data (`unavailable`/`unknown`) gets a red icon,
  so a dead template never passes for "All OK". The summary has `availability: has_value(binary_sensor.cigar_humidity_all_ok)`
  (added 2026-09-29; before that a dead binary sensor made it read `OK`). The names in `Check: …` are the device names:
  sensors 03/04 were renamed from "Jar 01"/"Jar 02" to "NW Jar"/"CC Jar" on 2026-09-29 to match the dashboards.
- The binary sensor's own state was always `on` until 2026-09-29 (a `{% set %}` inside a `for` loop does not escape it
  in Jinja); it now uses a `namespace`. Backups of the old files: `binary_sensors.yaml.bak-scoping-20260929`,
  `sensors.yaml.bak-cigar-availability-20260929`.
- **`#tom-cigars`**: one Bubble `sub-buttons` card, one row per humidor, in the Basement Tablet's order and names: name ·
  humidity · temperature · battery (tap = more-info). A humidity chip turns amber when the binary sensor's `per_sensor` entry for
  it is not `in_range: true` (out of band or unavailable), so the chips and the "Check" line always agree and the band
  lives only in the YAML. One IIFE in `styles` does this from a `[sub-button, entity]` map. Chips have
  `state_background: false` (Bubble otherwise paints a numeric sensor's chip as "on"). Numbering: row *i* (0-based) =
  sub-buttons 4i+1 / 4i+2 / 4i+3 / 4i+4 (name, humidity, temperature, battery). `rows: 6.45` fits nine rows exactly;
  adding a humidor = a row, a `[4i+2, entity]` entry in that map, about +0.7 rows, and the sensor in both YAML templates.
- **Battery chip** (Tom, 2026-09-30): the humidor sensor's own battery sensor (`sensor.basement_aqara_w100_0N_battery`
  for the Aqaras, `sensor.cigar_humidity_sensor_0N_battery_level` for the ZSE44s). Four chips only fit at 390 px with
  the name chip at `width: 27` and the battery chip at `fill_width: false, width: 21`, with humidity and temperature
  filling the rest. That is the narrowest setting where no chip text scrolls ("Tupperdor 04", "64.8 °F", "100%").
  At 26/18 the names and "100%" start to marquee. The `4i+2` amber map was checked on 2026-09-30 by overriding
  `per_sensor` in the browser for MON1800 and Cooler: only sub-buttons 2 and 34 (their humidity chips) turned amber. Re-check at 390 px before changing any of them. The ZSE44 battery
  sensors have display precision 0 (entity setting, 2026-09-30), so they read "43%", not "43.0%".

  | Name | Humidity | Temperature |
  |---|---|---|
  | MON1800 | `sensor.basement_aqara_w100_01_humidity` | `…_01_temperature` |
  | MA50 | `sensor.basement_aqara_w100_02_humidity` | `…_02_temperature` |
  | CC Jar | `sensor.cigar_humidity_sensor_04_humidity` | `…_04_air_temperature` |
  | NW Jar | `sensor.cigar_humidity_sensor_03_humidity` | `…_03_air_temperature` |
  | Tupperdor 01–04 | `sensor.cigar_humidity_sensor_{01,02,06,07}_humidity` | `…_air_temperature` |
  | Cooler | `sensor.cigar_humidity_sensor_05_humidity` | `…_05_air_temperature` |

### Leak Detection (added 2026-09-30)
Tom: a Leak Detection card built like the Cigars one. The card shows the aggregate status, and its pop-up lists each
Z-Wave leak sensor with its state and battery. It is the third card in the Status section.
- **Aggregate**: `binary_sensor.basement_leak_sensors` ("Basement Leak Sensors"), a binary sensor **Group helper**
  (config entry `01M3S8HRHWY3B1GDVXB1BWPTAE`, created 2026-09-30 via `ha_config_set_helper`, live only). Its members
  are the five sensors below, in "any" mode: it is `on` when any member is on, it ignores unavailable members while at
  least one still reports, and it is unavailable when all are. A group helper has no device class of its own, so its
  "Show as" is set to Moisture in the entity registry. That setting makes the state read Dry / Wet.
- It is deliberately not `binary_sensor.water_leak_sensor`. That older Template helper (a badge on Haynes Home and
  Basement Tablet) matches any `binary_sensor` whose id contains `water_leak_detected`, and it reads `off` (Dry) even
  when every sensor is unavailable. The group has explicit members, the same five as the pop-up, so the card and the
  pop-up always agree.
- **Card**: a name card, tap → `#tom-leak-detection`. Dry is a plain card. Wet gets a red card background, a red icon
  and a bold upper-case "WET". Anything else gets an amber icon: the group unavailable/unknown, **or** the group
  `off` while any member (read from the group's own `entity_id` attribute) is not `on`/`off`. An "any"-mode group
  stays `off` when one probe dies, so without the member check a dead battery would read as a plain Dry on the card
  while the pop-up paints that row amber (review finding on #222). The colours use the Humidors technique: a plain rgba
  rule, then a `color-mix` one.
- **`#tom-leak-detection`**: one Bubble `sub-buttons` card with one row per sensor: name · leak state · battery (tap =
  more-info). A state chip turns red on Wet and amber when unavailable/unknown. One IIFE in `styles` does this from a
  `[sub-button, entity]` map. Row *i* = sub-buttons 3i+1 / 3i+2 / 3i+3, and the state chip is 3i+2. `rows: 3.7` fits
  five rows. Adding a sensor means adding it to the group helper (`ha_config_set_helper` update with the entry id),
  adding a row and a `[3i+2, entity]` entry, and adding about 0.7 to `rows`. The five battery sensors have display
  precision 0 (2026-09-30).

  | Name | Leak sensor | Battery | Area |
  |---|---|---|---|
  | Sump Pump | `binary_sensor.basement_storage_sump_pump_leak_sensor_water_leak_detected` | `sensor.basement_storage_sump_pump_leak_sensor_battery_level` | Storage Room |
  | Ejector | `binary_sensor.basement_storage_trap_leak_sensor_water_leak_detected` | `sensor.basement_storage_trap_leak_sensor_battery_level` | Storage Room |
  | Water Main | `binary_sensor.basement_server_room_water_main_leak_sensor_water_leak_detected` | `sensor.basement_server_room_water_main_leak_sensor_battery_level` | Server Room |
  | Server AC | `binary_sensor.basement_server_room_ac_leak_sensor_water_leak_detected` | `sensor.basement_server_room_ac_leak_sensor_battery_level` | Server Room |
  | Water Meter | `binary_sensor.basement_movie_room_water_meter_leak_sensor_water_leak_detected` | `sensor.basement_movie_room_water_meter_leak_sensor_battery_level` | Movie Room |

  All five are Zooz ZSE42s. The names spell out the Haynes Home badges (Sump, Ejector, Main, Server AC, Movie).
- Not included: the `…_moisture_alarm` entities of the ZSE44 temperature/humidity sensors (the seven cigar sensors,
  `server_room_temperature_sensor_01`, `shed_temperature_sensor`, `upstairs_cloffice_temperature_sensor_01`). They are
  humidity alarms, not leak probes. The house has no non-Z-Wave leak sensors (checked 2026-09-30).
- Checked 2026-09-30 at 390 px with Playwright on the live dashboard: the card reads Dry, and tapping it opens the
  pop-up with five Dry rows and a battery % on each. The Wet and unavailable styling was rendered by overriding states
  in the browser only, including one probe unavailable while the group stays `off` (the card icon turns amber). Never set these sensors on the server to test: the `flood_watch_zse42_*` automations act on them.

### Main Lights (added 2026-09-30)
Tom: one quick-control card per basement room, directly under Status, with finer controls in a pop-up.

| Card | Light group(s) | Switch (hold) | Pop-up |
|---|---|---|---|
| Concessions | `light.basement_concessions_lights` | `basement_concessions_inovelli_presence` | `#tom-concessions-lights` |
| Rumpus | `light.basement_rumpus_room_lights` (Recessed) + `light.basement_rumpus_room_lamp` (Lamp) | `basement_rumpus_room_inovelli_presence` | `#tom-rumpus-lights` |
| Great Hall (Tom's name for the Basement Hallway; nothing renamed in HA) | `light.basement_hall_lights` | `basement_hallway_inovelli_presence` | `#tom-great-hall-lights` |

- **Card**: a name card (body tap → pop-up, no slider body). The `mdi:power` icon toggles the room and is tinted
  while any group is on. Rumpus has two groups, so its icon runs `script.rumpus_room_lights_toggle` (both off when
  either is on, else both on; repo copy `home-assistant/scripts/dashboard/`). The state line is a `.bubble-state::after`
  override: `30%` / `Off`, and for Rumpus `Recessed 30% · Lamp 100%`, with ` · Hold` appended while the room's switch
  is on hold. Bubble's CSS processing drops the space after a comma inside `content`, so use ` · ` as the separator.
- **Bright** = `light.turn_on` 100 % at 2750 K (Rumpus: both groups). **Dim** = brightness 30 % only, the switches'
  double-tap-down level (76/255); on Rumpus it dims only the Recessed and leaves the Lamp alone (Tom's habit).
  No Hold button on the card (Tom, 2026-09-30: too easy to press by accident); hold is set only from the pop-up. The
  state line's ` · Hold` is passive and uses the Server Room card's held test (LED1 = a hold colour, mmWave Disabled
  while the helper is not, or a motion automation off).
- **Pop-ups**: per group one slider card (body = brightness, icon = on/off, state = brightness %) with two
  full-width sub-button sliders on their own rows, `light_slider_type: white_temp` and `hue` (Bubble 3.4.1). Then a
  "Manual hold" card: tap = `script.inovelli_toggle_mmwave_hold_led_indicator` with the room's payload; amber,
  `mdi:lock` and "On · motion paused" while held.
- Checked 2026-09-30 at 390 px on the live dashboard: every sub-button, the Rumpus icon from mixed/off/on, the pop-up
  Lamp power icon, all three holds and the pop-up hold card (the card Hold buttons before they were removed), with state read back from HA after each tap.

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
  position (each tab's pop-ups are the last cards of its section 0) or fetch one tab with
  `ha_config_get_dashboard(url_path="tom-mobile", view_path="<tab path>")`.
- The `python_transform` sandbox cannot see local variables from inside a `lambda` or a
  comprehension (`NameError`), so write card lists out as `[c[0], c[7], ...]`.
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
- Consider a Snapshot Info section (weather/calendar) like Kellie's.
- Pool: `switch.haynes_res_pool_high` (high-speed pump) could join the pool popup.
- Docs site: add a `docs/features/` page once the dashboard stabilizes.
