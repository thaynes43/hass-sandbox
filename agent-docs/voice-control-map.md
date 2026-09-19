# Voice control map — what already controls the house

Agent-facing reference for the voice-assistant exposure work
(`.agents/plans/voice-assist-rollout.md`). Mapped read-only from the live HA config on
2026-09-18; live wins over the repo mirrors. Purpose: voice handles must match the concepts the
wall switches and scene controllers already give people, and must not fight the automations.

## House-wide mechanisms

- **Light groups are Zigbee2MQTT groups** (`platform: mqtt`), not HA helper groups. Their
  friendly name is the machine id; voice names come from entity aliases.
- **Hold = LED colour, not a boolean.** `input_text.inovelli_manual_hold` = 90 (a person),
  `input_text.inovelli_auto_hold` = 130 (cleaners mode). Motion automations gate on
  `number.<switch>_defaultled1colorwhenon not in [90, 130]`. Config 2x on an Inovelli calls
  `script.inovelli_toggle_mmwave_hold_led_indicator` (staircase:
  `script.inovelli_toggle_hold_group_of_switches`), which disables that zone's motion
  automations, caches the LED colour in `input_text.<zone>_led_color` and paints the hold colour.
  A voice "hold" must call the same script with the same arguments as the switch mapping.
- Voice on/off = paddle press: a light turned off in an occupied zone stays off until the next
  off→on occupancy edge; a light turned on is turned off `off_delay` after the zone clears.

## Basement

| Zone | Off delay | Motion ON | Notes |
|---|---|---|---|
| Movie Room | 5 min | none (off-only) | auto-off hits recessed **and** ambient |
| Rumpus Room | 5 min | yes, ungated | fp2_01 excluded from the clear check (covers the movie side) |
| Concessions | 30 s | yes, ungated, 2750 K/100 % | voice dim/off is overwritten within seconds unless held |
| Hallway | 30 s | yes, hold-gated | driven from Rumpus ZEN37 buttons 3/4 together with Concessions |
| Bathroom | 0 s | fan light, switch-local mmWave | voice control pointless |
| Staircase | 1 min | both switches, hold-gated | **never exposed** (safety) |
| Storage | door contact | instant on/off | not exposed |
| Server Room | — | — | **off-limits**: PDUs, SSIDs, alarm panel |

Handles: `light.basement_movie_room_lights` (4 recessed + switch),
`light.basement_movie_room_ambient_lighting` (gradient 65/75, left/right floor lamp, play 01/02 —
the "ambient" concept; `light.basement_movie_floor_lamps` is the same minus the play bars and is
used by nothing), `light.basement_rumpus_room_lights` (8 recessed + switch),
`light.basement_rumpus_room_lamp` (ceiling + desk bulb), `light.basement_concessions_lights`,
`light.basement_hall_lights`.

Button → voice script (all `script.voice_*`, zero-argument, mirrored in
`home-assistant/scripts/voice/`):

| Button | Does | Voice script |
|---|---|---|
| Movie ZEN37 1 ×2 / Inovelli up ×2 | recessed 2750 K 100 % | `voice_movie_room_bright` |
| Movie Inovelli down ×2 | recessed 2750 K 33 % | `voice_movie_room_dim` |
| Movie ZEN37 2 ×2 | recessed red 15 % | `voice_movie_room_red_night_mode` |
| Movie ZEN37 2 ×1 | `script.toggle_hue_colors` on recessed | `voice_movie_room_color_toggle` |
| Movie ZEN37 4 ×1 | `script.cycle_gradient_scene_using_same_light_color` (4 gradient selects + 2 play bars) | `voice_movie_room_ambient_scene` |
| Movie Inovelli config ×2 | hold | `voice_movie_room_hold_lights` |
| Rumpus ZEN37 1 ×2 | lights + lamp 2750 K 100 % | `voice_rumpus_room_bright` |
| Rumpus ZEN37 2 ×2 | lights + lamp 2750 K 50 % | `voice_rumpus_room_dim` |
| Rumpus ZEN37 2 ×3 | `script.toggle_hue_colors_for_multiple_targets` over 10 bulbs | `voice_rumpus_room_color_toggle` |
| Rumpus Inovelli config ×2 | hold (both motion automations) | `voice_rumpus_room_hold_lights` |

Media: TV auto-off `automation.media_basement_movie_room_no_occupancy_tv_off` (20 min, spoken
warnings on the satellite). Nothing turns the TV/AVR on. Three duplicate AVR registrations
(`media_player.str_az5000es`, `_2`, `_2210`) and two KEF ones — do not expose until phase 3.

Defects found (open): Movie ZEN37 buttons 3 ×2 / 4 ×2 call
`number.movie_room_breeze_target_temperature`, which does not exist (the Cielo controller has
only `climate.movie_room_breeze`) while `button-mappings.md` documents them as working;
`light.basement_hall_night_light` in the repo's night-light watchdog does not exist live; six
live ZEN37 hold-dim helper automations and four scripts the buttons call
(`toggle_hue_colors`, `toggle_hue_colors_for_multiple_targets`,
`cycle_gradient_scene_using_same_light_color`, `zen32_hard_reset`) have no repo mirror.

## Exterior

No exterior satellite; nothing exterior is exposed yet. Only two switches have any button mapping
(Config 2x = hold on the back-yard spotlight and the garage mudroom switch).

Schedules (all unconditional, manual state simply lasts until the next edge):

| Lights | On | Off |
|---|---|---|
| Front yard Lily (100 %) + Calla (25 %), porch group (25 %), front door light | sunset | 00:00 |
| Patio retaining wall relay (`switch.back_yard_retaining_wall_lights_relay`) | sunset | 00:00 |
| Shed flowerbox relay (`switch.shed_exterior_lights_shelly_relay`) | sunset | 00:00 |
| Driveway (`light.garage_driveway_inovelli_dimmer`), back yard (`light.garage_back_yard_inovelli_dimmer`) | sunset | 01:00 |
| Back-yard motion-light relay | 00:00 | sunrise |
| Lamp post (`light.front_yard_lamp_post_hue_edison`) | nothing | nothing |

- **Back-yard spotlight** (`light.downstairs_kitchen_back_yard_spotlight`) is the contested one:
  three automations (slider opens, camera person/animal, auto-off after 5 min clear with a hard
  45 min cap) — all disabled while the paddle hold is engaged. A plain voice turn-on is fought.
- **Garage interior** (`light.garage_interior_lights`): a voice/paddle turn-on arms a 5 min floor
  timer, then occupancy turns it off ~2 min after clear; voice off is respected.
- Shed fan is occupancy-owned (door/motion → on, 5 min still → off).
- Holiday scenes/automations (Halloween, Christmas) are enabled by hand, have no mode helper and
  reference several entities that are unavailable or gone out of season.
- Front yard has two groups (`light.front_yard_hue_lily_lights`, `…_calla_lights`) and no
  single "landscape lights" handle.
- Patio/shed lights and the shed fan are `switch.*` — deny-by-default in the guard, so exposing
  them needs a `switch_allowlist` entry.
- Every `fan.*` outside is spa equipment. Opener bulbs `light.ratgdov25i_*_light` pass a
  domain-based deny list and should be denied by name.

Dangerous-set status: locks `lock.front_door_lock`, `lock.side_door_lock`, `lock.bulkhead_lock`
(must be locked) and `lock.mudroom_door_lock` (interior garage↔mudroom door, **expected
unlocked** by `automation.wall_display_entry_locks_status`); garage doors
`cover.ratgdov25i_4a0325_door` (Tesla) and `cover.ratgdov25i_dbfa50_door` (Wagoneer). No
read-only mirror sensors exist yet, and the `input_text.*_entry_locks_status` roll-ups are
invisible to Assist (no tool reads `input_text`).

Defects found (open): the spa-light auto-hold automation triggers on the non-existent
`light.westford_spa_light_1`; `input_select.garage_interior_mudroom_mmwave_normal_mode` reads
"Occupancy (default)" while the switch is `Disabled`, so the next Config 2x re-enables local
mmWave control instead of holding; the spotlight mapping passes an `input_number` as
`prev_led_color_helper` (written with `input_text.set_value`, so it never updates);
`automation.notify_garage_ratgdo_door_open_close_tom_iphone` is off; 14 exterior automations
have no repo mirror.

## First floor / Second floor

Mapping in progress (2026-09-18).
