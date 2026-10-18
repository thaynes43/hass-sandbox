# Inovelli mmWave local-control audit

Status: Done 2026-10-18. Two conflicts fixed live; four local-only zones and two mixed zones left as they are (see Decisions for Tom).

Saga: Home Assistant reliable room control (see
[007](007-movie-room-mmwave-owner-validation.md)). Epic: occupancy lighting that HA
owns end to end, so no switch cuts its load on its own radar alone.

## Why

On 2026-10-18 the garage lights went off with Tom standing in the room. The mudroom
Inovelli switch had firmware local control on (`MmwaveControlWiredDevice` =
"Occupancy (default)"). In that mode the switch cuts its own load when its own radar
clears, ignoring every other sensor and automation. Its helper
`input_select.garage_interior_mudroom_mmwave_normal_mode` had
`initial: "Occupancy (default)"`, which reset it on each HA restart. It is now
`initial: Disabled` (#263). Tom asked for an audit of every Inovelli presence switch
against his automations and the switches' current and default occupancy settings.

## Scope

One outcome: every Inovelli presence switch is classified, and the clear-cut conflicts
are fixed live.

- Included: all `select.*_mmwavecontrolwireddevice` entities and their
  `input_select.<zone>_mmwave_normal_mode` helpers (state and `initial`).
- Excluded: editing automations or scripts, detection areas, sensitivity, hold times,
  off-delay helpers, and Z2M settings other than the mode select. No restarts, reloads,
  light toggling or physical tests.

Classes:
- **A, conflict:** HA owns both on and vacancy-off, but local control is not Disabled
  (device, helper or `initial`). Fix.
- **B, latent:** the device is Disabled but the helper state or `initial` would push a
  non-Disabled mode on the next restart or clear-hold. Fix.
- **C, local-only:** no HA vacancy-off path; the switch's own mode turns the lights off.
  Leave it.
- **D, mixed or unclear:** leave it and record the trade-off with a recommendation.

## Acceptance

- Results table below covers every switch: load, HA on/off automations, device / helper
  / `initial` (before and after), class, action.
- Each A and B fix: helper `initial` = Disabled, helper state = Disabled, device select
  reads Disabled (live read-back). Prior values recorded for rollback.
- D-class items listed with a recommendation for Tom.
- PR merged with the evidence; agent-docs/playbooks updated where they track per-zone
  modes.

## Dependencies

None. The garage fix (#263) is already merged and live.

## Ownership

One Claude Code subagent session (Opus 5.5), worktree
`~/work/inovelli-local-control-audit`, branch `agent/inovelli-local-control-audit`.
Runtime writes limited to the `input_select.*_mmwave_normal_mode` helpers (config +
state) and, through the existing sync automation, the matching device selects.

## Budget and stop criteria

About 90 minutes, no subagents, no CPU-heavy loops. Stop when acceptance is met, at the
budget, or if a fix needs new plumbing (record it here instead). Unfinished work goes
under *Next step* below.

## Rollback

Set each changed helper back to its recorded prior `initial` and state with
`ha_config_set_helper` + `input_select.select_option`; the sync automation pushes it to
the device.

## Results

Live read 2026-10-18 ~23:50 UTC (states, helper configs, registries, every automation and
script config, Z2M group membership), fixes applied 23:54–23:55 UTC, then everything re-read.
Abbreviations: Occ = "Occupancy (default)", Wasteful = "Wasteful Occupancy", Dis = Disabled,
none = no `initial` (restores last state). Helpers are `input_select.<helper>_mmwave_normal_mode`.

Mode semantics (zigbee-herdsman-converters, read from the Z2M pod): Occupancy = auto on and
auto off; Vacancy = auto off only; **Wasteful Occupancy = auto on only, never auto off**;
Disabled = no local control. Only Occupancy and Vacancy can cut a load while someone is in the
room.

### Switches with a normal-mode helper (19 switches, 18 helpers)

| Switch | Load (Z2M group) | HA on / off from occupancy | Device | Helper state | `initial` | Class | Action |
|---|---|---|---|---|---|---|---|
| downstairs_mudroom | `light.downstairs_mudroom_lights` (2 Hue + switch) | on: mudroom radar or mudroom night light; off: 2 min after mudroom radar, night light, bath vanity radar and bath night light all clear | Occ→Dis | Occ→Dis | Occ→Dis | **A** | fixed |
| upstairs_kids_bathroom_vanity | `light.upstairs_kids_bathroom_vanity_inovelli_presence` | on: vanity radar or kids-bath night light; off: 5 min after both clear | Occ→Dis | Occ→Dis | Occ→Dis | **A** | fixed |
| basement_bathroom_vanity | switch's own load | none for the vanity (HA only runs the fan light) | Occ | Occ | none | C | none |
| downstairs_bathroom_vanity | switch's own load | none for the vanity (HA's "non-vanity lights off" runs fan + shower) | Occ | Occ | Occ | C | none |
| upstairs_laundry (`inovlli` sic) | switch's own load | none | Occ | Occ | Occ | C | none |
| upstairs_primary_water_closet | switch's own load | none | Occ | Occ | Occ | C | none |
| basement_server | `light.basement_server_inovelli_presence` (group `basement_server_room_lights`: 2 Hue + switch) | on: switch radar or FP2; off: delay after both clear | Wasteful | Wasteful | none | D | none, see Decisions |
| upstairs_primary_bath_vanity | switch's own load | on: none (the switch's Wasteful auto-on); off: primary-bath motion-cleared turns off all four bath loads | Wasteful | Wasteful | none | D | none, see Decisions |
| upstairs_primary_bath_shower | switch's own load | on: manual; off: primary-bath motion-cleared | Dis | Dis | none | OK | none |
| downstairs_entrance | `light.downstairs_entrance_lights` | on + off (entrance or mudroom radar, 2 min) | Dis | Dis | Dis | OK | none |
| garage_interior_mudroom | `light.garage_interior_lights` | on + off (both switches, camera, doors) | Dis | Dis | Dis | OK | fixed earlier the same day (#263) |
| basement_rumpus_room | `light.basement_rumpus_room_lights` + lamp | on + off (FP2s, FP300, switch) | Dis | Dis | none | OK | none |
| basement_concessions | `light.basement_concessions_lights` | on + off (switch radar, Q sensor, 30 s) | Dis | Dis | none | OK | none |
| basement_hallway | `light.basement_hall_lights` | on + off (switch radar, Q sensor) | Dis | Dis | none | OK | none |
| basement_movie | `light.basement_movie_room_lights` + ambient | off only (switch + FP2 01, 5 min); on is manual | Dis | Dis | none | OK | none |
| basement_upper_staircase | `light.basement_staircase_lights` | on + off (both staircase radars, 1 min) | Dis | Dis | none | OK | none |
| basement_lower_staircase | load-less, paddle-bound both ways | same zone as upper | Dis | Dis (shared `basement_staircase` helper) | none | OK | none; bindings kept |
| upstairs_primary_closet | `light.upstairs_primary_closet_lights` | door contact on/off (ZSE41), not occupancy | Dis | Dis | Dis | OK | none |
| upstairs_primary_hall | `light.upstairs_primary_hall_lights` | on + off (switch radar, night light, 2 min) | Dis | Dis | Dis | OK | none |

B (device Disabled but helper state or `initial` non-Disabled): none found. Every helper agreed
with its device before and after.

### Switches without a helper (14), all OK, no action

All read Disabled, and nothing in HA writes their mode: no helper, no sync-automation route,
no clear-hold entry, and no automation or script hardcodes a `normal_mode` for them. Their loads
are manual or Zigbee-bound: dining table corner and wall, downstairs foyer chaos, kitchen chaos,
kitchen recessed, kitchen sink, living room recessed, study recessed, upstairs foyer chaos, blue-room,
pink-room and primary-bedroom foyer switches, primary bed. `garage_interior_side` is the garage
aux companion: Disabled, no relay authority, and its radar feeds the HA garage automations.

### Evidence for the A class

History 2026-10-11 23:54 → 2026-10-12 23:54 UTC. Fingerprint: the switch's load goes off at the
same instant (within 1.3 s) as its own radar clears, while HA's off path waits 2 or 5 minutes.
- Mudroom: 27 of 41 load-offs match. In 11 of them another zone sensor still read occupied, for
  example 17:06:51 and 23:47:38 UTC, when the mudroom night light, the bath vanity radar and the
  bath night light were all on.
- Kids bath vanity: 17 of 17 load-offs match. The configured 5-minute hold never applied.

### Changes (rollback values)

| Entity | Before | After |
|---|---|---|
| `input_select.downstairs_mudroom_mmwave_normal_mode` `initial` | "Occupancy (default)" | Disabled |
| `input_select.downstairs_mudroom_mmwave_normal_mode` state | "Occupancy (default)" | Disabled |
| `select.downstairs_mudroom_inovelli_presence_mmwavecontrolwireddevice` (via sync automation) | "Occupancy (default)" | Disabled (23:54:54 UTC) |
| `input_select.upstairs_kids_bathroom_vanity_mmwave_normal_mode` `initial` | "Occupancy (default)" | Disabled |
| `input_select.upstairs_kids_bathroom_vanity_mmwave_normal_mode` state | "Occupancy (default)" | Disabled |
| `select.upstairs_kids_bathroom_vanity_inovelli_presence_mmwavecontrolwireddevice` (via sync automation) | "Occupancy (default)" | Disabled (23:55:01 UTC) |

Both loads were off when the mode changed. The sync automation runs in `single` mode, so the
two helpers were changed one at a time and each device was read back before the next.

## Decisions for Tom (nothing changed)

- **Server room (Wasteful, HA owns on and off):** Wasteful never turns the lights off, so it
  cannot cause a garage-style cut-off. Its only effect is a local auto-on, which also works
  when HA is down, in the room you go to when the cluster is broken. Recommendation: keep it.
  Setting it to Disabled would be harmless but would lose that fallback.
- **Primary bath vanity (Wasteful, switch on, HA off):** this is the safe split. Disabled would
  leave the vanity with no auto-on. Recommendation: keep it.
- **Local-only zones (laundry, water closet, basement bath vanity, downstairs bath vanity):**
  the switch's own radar is the only off path, so each cuts its lights when its own radar clears.
  To make HA own these, a separate story has to build the HA automations first, then set the
  helper and `initial` to Disabled. A middle path is to copy the primary vanity: set Wasteful
  and add an HA vacancy-off with a delay, using the room's night-light sensor where there is
  one (both bath vanities). Recommendation: only if a cut-off has actually bothered someone.

## Not verified

- Behaviour across an HA restart (no restart was allowed). The helper config read-back shows
  `initial: Disabled` on both helpers, which is what makes HA restore Disabled.
- Live occupancy behaviour after the change (no physical tests). The next occupied period in
  each zone should show the load staying on until HA's 2 or 5 minute off-delay.

## Minor findings (no action)

- `input_select.basement_staircase_mmwave_normal_mode` has 6 options; it lacks
  "Mirrored Wasteful Occupancy". Harmless while it reads Disabled.
- The live sync automation's entity id is
  `automation.rumpus_inovelli_sync_helper_normal_mode_device`, not the repo file name.
