# Occupancy-based lighting: add or update a zone

### When to use this

Use this playbook when adding a new occupancy-based lighting zone, migrating an existing zone to current patterns, or updating holds/cards for a zone. It covers the full 7-step workflow, MCP call sequences, hold rules, card patterns, and Inovelli naming conventions.

### Critical rule: always use `normal_mode_input_select`, never hardcode `normal_mode`

When passing "normal mode" to scripts or cards, **always** use the input_select form:

```yaml
normal_mode_input_select: input_select.<zone>_mmwave_normal_mode
```

**Never** use the deprecated hardcoded form:

```yaml
normal_mode: Occupancy (default)
```

This applies everywhere: cards, switch-button automations, and entity-defined hold/clear scripts. The input_select form reads from the zone's dropdown helper at runtime, so changing the helper propagates everywhere. Hardcoded `normal_mode` breaks hold restore behavior.

If a fallback `normal_mode` is unavoidable for legacy reasons, it must exactly match that zone's helper value. For automation-owned zones this means `Disabled`, not `Occupancy (default)`.

---

### Workflow: add a new occupancy-based lighting zone (7 steps)

**Step 1 — Decide zone ownership**

- **Control switch**: the Inovelli (or other device) that owns:
  - mmWave normal-mode helper (`input_select.<zone>_mmwave_normal_mode`)
  - manual hold toggle (switch button mapping)
  - auto-hold integration (global hold/clear scripts)
- **Extra sensors**: additional occupancy sensors that influence lights but do **not** own holds/mmWave mode.

**Step 2 — Create helpers via MCP (3 calls)**

- `input_select.<zone>_mmwave_normal_mode` — options must match `select.<switch_name>_mmwavecontrolwireddevice`. Default: **`Disabled`** if zone automations own the load.
- `input_select.<zone>_occupancy_off_delay` — stores `HH:MM:SS` strings. Recommended default: `00:02:00`.
- `input_text.<zone>_led_color` — caches/restores LED colors during holds. Safe default value: `255`.

**Step 3 — Wire helper → device sync (1 call: update automation)**

Update `automations/inovelli_mmwave_normal_mode_sync_input_select.yaml` so it:
- Triggers on `input_select.<zone>_mmwave_normal_mode`
- Routes to `select.<switch_name>_mmwavecontrolwireddevice`

**Step 4 — Create the two zone automations (2 calls)**

Create under `automations/occupancy-based-lighting/`:

- **Motion detected → lights on**
  - Trigger: state → any relevant occupancy sensor → `on`
  - Action: `light.turn_on` your target light/group

- **Motion cleared → lights off**
  - Trigger: template asserting **all** relevant sensors are `off`
  - Actions (order matters):
    1. **Hold guard**: block if on manual/auto hold (LED1 reserved colors)
    2. `delay:` from `input_select.<zone>_occupancy_off_delay` (with fallback)
    3. Re-check sensors are still `off`
    4. Re-check hold guard again
    5. `light.turn_off`
  - `mode: restart`

**Step 5 — Wire manual hold (switch button mapping) (1 call)**

In `automations/switch-buttons/**` for the control switch, add/update `config_double` to call:

```yaml
service: script.inovelli_toggle_mmwave_hold_led_indicator
data:
  switch_name: <switch_name>
  normal_mode_input_select: input_select.<zone>_mmwave_normal_mode
  hold_color: input_text.inovelli_manual_hold
  prev_led_color_helper: input_text.<zone>_led_color
  automation_entity_ids:
    - automation.light_<zone>_motion_detected_lights_on
    - automation.light_<zone>_motion_cleared_lights_off
```

**Step 6 — Register auto-hold (global hold/clear scripts) (2 calls)**

Update **for the control switch only**:

- `scripts/inovelli/entities-defined/hold_all_inovelli_presence_controlled_switches.yaml`
- `scripts/inovelli/entities-defined/clear_hold_on_all_inovelli_presence_controlled_switches.yaml`

Both need: `switch_name`, `helper: input_text.<zone>_led_color`, `automation_entity_ids`.
Clear script also needs: `normal_mode_input_select: input_select.<zone>_mmwave_normal_mode`.

**Step 7 — Create cards (repo only; place manually in HA)**

Create `cards/<area>/<zone>/` with:
- `*-open-popups.yaml`
- `*-history.yaml`
- `*-advanced.yaml`

Use `cards/rumpus-room/` and `cards/concessions/` as reference "new format" templates.

---

### MCP call sequence (token-efficient)

1. **Verify exact entity IDs** (1 call — batch): occupancy sensors, target light/group, control switch mmWave select
2. **Get mmWave normal-mode options** from the device (1 call): `ha_get_state` on `select.<switch_name>_mmwavecontrolwireddevice` — `attributes.options` is what you use for the `input_select` options
3. **Create the 3 helpers** (3 calls)
4. **Update sync automation + create zone automations** (3 calls)
5. **Wire manual hold + register auto-hold** (3 calls)
6. **One verification pass** (1 call — batch all new helper entity_ids)

---

### Known-good examples: helpers

**mmWave normal-mode input_select:**

```json
{
  "tool": "ha_config_set_helper",
  "arguments": {
    "helper_type": "input_select",
    "name": "Basement Server Room mmWave Normal Mode",
    "icon": "mdi:motion-sensor",
    "options": ["Disabled", "Occupancy (default)", "Vacancy", "Wasteful Occupancy", "Mirrored Occupancy", "Mirrored Vacancy", "Mirrored Wasteful Occupancy"],
    "initial": "Disabled"
  }
}
```

**Occupancy off-delay input_select:**

```json
{
  "tool": "ha_config_set_helper",
  "arguments": {
    "helper_type": "input_select",
    "name": "Basement Server Room Occupancy Off Delay",
    "icon": "mdi:timer-outline",
    "options": ["00:00:00", "00:00:15", "00:00:30", "00:01:00", "00:02:00", "00:05:00", "00:15:00", "00:30:00"],
    "initial": "00:02:00"
  }
}
```

**LED color cache input_text:**

```json
{
  "tool": "ha_config_set_helper",
  "arguments": {
    "helper_type": "input_text",
    "name": "Basement Server Room LED Color",
    "icon": "mdi:led-strip-variant",
    "initial": "255"
  }
}
```

---

### Known-good example: motion-cleared automation

```yaml
alias: "Light: Basement Server Room Motion Cleared Lights Off"
trigger:
  - platform: template
    value_template: >
      {{ is_state('binary_sensor.basement_server_inovelli_presence_occupancy', 'off') }}
condition: []
action:
  - condition: template
    value_template: >
      {{ states('sensor.basement_server_inovelli_presence_led_effect_one') not in
         [states('input_text.inovelli_manual_hold'), states('input_text.inovelli_auto_hold')] }}
  - delay: >
      {{ states('input_select.basement_server_room_occupancy_off_delay') | default('00:02:00') }}
  - condition: template
    value_template: >
      {{ is_state('binary_sensor.basement_server_inovelli_presence_occupancy', 'off') }}
  - condition: template
    value_template: >
      {{ states('sensor.basement_server_inovelli_presence_led_effect_one') not in
         [states('input_text.inovelli_manual_hold'), states('input_text.inovelli_auto_hold')] }}
  - service: light.turn_off
    target:
      entity_id: light.basement_server_room
mode: restart
```

---

### Common pitfalls

| Symptom | Cause | Fix |
|---------|-------|-----|
| Hold restore sets wrong mode after clearing | Script uses `normal_mode: Occupancy (default)` (hardcoded) | Replace with `normal_mode_input_select: input_select.<zone>_mmwave_normal_mode` |
| Switch autonomously controls load despite automations | `input_select.<zone>_mmwave_normal_mode` not set to `Disabled` | Set to `Disabled` via HA UI so automations own the load |
| Lights turn off immediately despite hold | Hold guard only checked before delay, not after | Add second hold check after the `delay:` action |
| Occupancy off delay not respected | Using `for:` on trigger instead of action `delay:` | Remove trigger `for:`, use action `delay:` reading from the helper |
| Only control switch in hold registry | Extra sensors (non-control) added to hold/clear scripts | Only add the single control switch to hold/clear registries |
| Lights turn off with someone standing there | mmWave detection area does not cover where people actually stand, and/or sensitivity below `High` | Measure with `mmwavetargetinforeport`, then re-tune — see *Reference: tuning the mmWave detection zone* |

---

### After creating (don't forget)

- [ ] Helpers exist in HA and states are correct
- [ ] `inovelli_mmwave_normal_mode_sync_input_select.yaml` includes the new zone
- [ ] Both zone automations are enabled in HA
- [ ] Switch-button automation references `normal_mode_input_select` (not `normal_mode`)
- [ ] Control switch is registered in both global hold and clear scripts
- [ ] Cards created in `cards/<area>/<zone>/` and placed on dashboard
- [ ] No `normal_mode:` (hardcoded) references remain for this zone

---

## Reference: holds

Holds temporarily disable occupancy control and indicate state via **reserved LED colors** on the Inovelli LED bar.

### Hold types

- **Manual hold** — triggered by physical switch action, identified by LED color stored in `input_text.inovelli_manual_hold`
- **Auto/automation hold** — triggered by automations/scripts, identified by `input_text.inovelli_auto_hold`

### Hold rules (must follow)

- **Reserved colors are state**: any LED bar color used to indicate "hold" is reserved for that purpose only.
- **Manual vs automation holds must differ**: they use different `input_text` helpers (different LED colors) so they can be distinguished.
- **Clearing behavior**: automations clearing holds must only clear holds matching the **automation hold** color, so manual holds are preserved.

---

## Reference: Inovelli entity naming

For Inovelli "presence dimmer" devices, entity IDs are consistent across zones. Pick a known-good zone and **substitute the switch name** everywhere.

Common suffixes (after `<switch_name>_`):
- `select.<switch_name>_mmwavedetectsensitivity`
- `select.<switch_name>_mmwavecontrolwireddevice`
- `select.<switch_name>_outputmode`
- `number.<switch_name>_mmwaveholdtime`
- `number.<switch_name>_mmwavestaylife`
- `number.<switch_name>_mmwavedepthmin` / `mmwavedepthmax`
- `number.<switch_name>_mmwavewidthmin` / `mmwavewidthmax`
- `number.<switch_name>_mmwaveheightmin` / `mmwaveheightmax`
- `sensor.<switch_name>_mmwave_control_commands`
- `select.<switch_name>_mmwavetargetinforeport` — enable to get live target coordinates
- `sensor.<switch_name>_mmwave_targets`

> **The `mmwave{width,depth,height}{min,max}` numbers do NOT apply the detection area on their
> own** — the radar keeps the old zone until `setDetectionArea` is commanded. See
> *Reference: tuning the mmWave detection zone* below.

---

## Reference: tuning the mmWave detection zone (geometry)

The VZM32 radar only reports presence for targets inside **detection area 1**. Everything
outside it is invisible to `binary_sensor.<switch_name>_occupancy`, no matter how strong the
return — a person standing still a metre away reports nothing if they are outside the box.

### Coordinate convention (all values in cm, from the switch)

| Axis | Entity | Meaning |
|------|--------|---------|
| width | `mmwavewidthmin` / `mmwavewidthmax` | **signed** lateral: negative = left of the switch facing away from the wall, positive = right. `0` is straight out from the switch (boresight). |
| depth | `mmwavedepthmin` / `mmwavedepthmax` | distance out from the wall; always `>= 0` |
| height | `mmwaveheightmin` / `mmwaveheightmax` | negative = below the switch, positive = above |

**The boresight trap**: the detection window is the *interval* `width_min … width_max` — the radar
sees a target only where `width_min <= x <= width_max` — so a positive `widthmin` (or a negative
`widthmax`) blanks the area directly in front of the switch. With `widthmin: 55, widthmax: 300` the
radar sees **only** the 55–300 cm slice to the right of the switch: the whole left side and
boresight fall outside the box, so someone standing at the switch is never seen. Unless the zone is
deliberately an off-to-one-side target (a vanity, a rack aisle), the width window should straddle `0`.

### Setting the zone (the number entities alone do NOT apply it)

`number.<switch_name>_mmwave{width,depth,height}{min,max}` write bare ZCL attributes. The radar
does not adopt them until the `setDetectionArea` **command** is issued, which Home Assistant does
not expose. Writing only the numbers leaves the device running the *old* zone while HA and the
z2m attribute state both show the new one — they silently disagree.

Apply the zone with the z2m composite over MQTT, which issues `setDetectionArea` and then
re-queries the device:

```yaml
action: mqtt.publish
data:
  # the z2m *friendly name*, not the HA entity slug — they coincide for the Inovelli
  # presence switches, but not for every device (see agent-docs/hue-power-on-behavior.md)
  topic: zigbee2mqtt/<switch_name>/set
  payload: >-
    {"mmwave_detection_areas": {"area1": {"width_min": -150, "width_max": 300,
     "height_min": -300, "height_max": 300, "depth_min": 0, "depth_max": 250}}}
```

Set the number entities to the same values too, so the HA-facing config matches what the radar runs.

### Verifying

`mmwave_detection_areas.area1` in the device's z2m payload is the **device's own answer** to a
`query_areas`, so it is the source of truth. Confirm the round trip:

```bash
kubectl logs -n home-automation deploy/zigbee2mqtt --since=2m \
  | grep "topic 'zigbee2mqtt/<switch_name>'" | tail -1
```

If `mmwave_detection_areas.area1` still shows the old box, the command did not land — re-publish.

**No output is not a pass** — it is ambiguous, and both readings are bad:

- the publish happened but aged out of the window (z2m is chatty and the container log rotates,
  so even `--since=3h` can start only minutes ago), or
- **nothing was ever published**: a `/set` to a topic that is not a z2m friendly name is dropped
  silently, so `setDetectionArea` is never commanded and no state line is ever logged.

Don't try to tell them apart from a wider `--since`. Re-publish while tailing the log
(`kubectl logs -f -n home-automation deploy/zigbee2mqtt | grep <switch_name>`) so you see the
command and the device's answer as they happen. HA's cached attributes cannot settle this, which is
the whole point of reading the device's own answer.

### Measuring where people actually are (do this before guessing at numbers)

Do not tune the box by eye. The device will tell you exactly where it sees people:

```yaml
action: select.select_option
target: { entity_id: select.<switch_name>_mmwavetargetinforeport }
data: { option: "Enable" }
```

The device then publishes `mmwave_targets` roughly once a second — a list of
`{id, x, y, z, dop}` where **x = width, y = depth, z = height** in cm, on the same axes as the
detection box, and `dop` is doppler. Walk the zone (or have someone stand where the misses happen)
and collect a minute of samples:

```bash
kubectl logs -n home-automation deploy/zigbee2mqtt --since=6m \
  | grep "topic 'zigbee2mqtt/<switch_name>'" \
  | grep -o '"mmwave_targets":\[[^]]*\]'
```

Then set the box around the observed `x`/`y` spread with margin. This turns the whole exercise from
guesswork into measurement — on the concessions zone it showed people standing at `x ≈ -173`
(1.7 m to the **left**), `y ≈ 264`, while the configured box was `width 55..300, depth 0..100`:
wrong on both axes, and a first "widened" guess of `width -150..300, depth 0..250` was *still*
wrong on both. Only the measurement settled it.

A target with a **constant non-zero `dop` that never moves** is a machine, not a person (a
compressor or a fan). If one sits inside your detection box and holds occupancy on, that is what
interference areas are for.

**Turn reporting back off when you are done** — it is a ~1 Hz MQTT publish and HA state write per
device, which is real recorder growth if left on.

### Interference (mask) areas

`mmwave_interference_areas` carve *exclusions* out of the detection area; targets inside them are
never reported. They do not appear as HA entities at all — only in the z2m payload — so a zone can
be silently masked with no sign of it in Home Assistant. Several switches carry a ~100 cm-deep mask
band that came from the device's own interference auto-detect, not from hand tuning. When a zone
misses people, check the mask as well as the detection box.

### Sensitivity

`select.<switch_name>_mmwavedetectsensitivity` defaults to **High (default)**. Anything lower is a
deliberate choice; `Medium` on a zone that misses stationary people is worth putting back to High.

---

## Migration notes

### `normal_mode` → `normal_mode_input_select`

When migrating any script call or card that uses a hardcoded `normal_mode:` parameter:

1. Find a block targeting the switch: `switch_name: <zone>_inovelli_presence`
2. If that same block contains `normal_mode: ...`
3. Replace with `normal_mode_input_select: input_select.<zone>_mmwave_normal_mode`

Applies to: cards (`cards/**`), switch-button automations (`automations/switch-buttons/**`), entity-defined scripts (`scripts/inovelli/entities-defined/**`).

### Per-zone "lights off delay" (replacing hardcoded trigger `for:`)

- Remove the trigger `for:` entirely
- Add action `delay:` reading from `input_select.<zone>_occupancy_off_delay` with fallback to the old value
- Add post-delay condition to verify zone is still empty
- Use `mode: restart`
