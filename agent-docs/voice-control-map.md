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
  Voice has **no hold tools** (Tom, 2026-09-19); if one is ever added it must call the same
  script with the same arguments as the switch mapping.
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
| Rumpus ZEN37 1 ×2 | lights + lamp 2750 K 100 % | `voice_rumpus_room_bright` |
| Rumpus ZEN37 2 ×2 | lights + lamp 2750 K 50 % | `voice_rumpus_room_dim` |
| Rumpus ZEN37 2 ×3 | `script.toggle_hue_colors_for_multiple_targets` over 10 bulbs | `voice_rumpus_room_color_toggle` |

No voice hold tools: Tom ruled them out on 2026-09-19 (holds are not used day to day).

Media: TV auto-off `automation.media_basement_movie_room_no_occupancy_tv_off` (20 min, spoken
warnings on the satellite). No HA automation turns the TV/AVR on, but the AVR (Sony STR-AZ5000ES,
"Works with Sonos") **wakes itself** when the Sonos Port `media_player.movie_room` plays: power on
after ~2 s, volume tracks the Port, input switches to `Sonos` (AVR recorder history, 2026-09-19
17:08) — so music sent to this room is audible even with everything "off". Four AVR registrations:
`media_player.str_az5000es` (songpal — the controllable one: power, volume, inputs), `_2` (DLNA),
`_2210` (Chromecast) and `_2210_2` (Music Assistant's wrapper of the Chromecast; no area, does not
group with Sonos) — do not expose until phase 3. The KEF LS50s (the
Rumpus Room PC's speakers over HDMI) have three registrations: the two non-Music-Assistant ones
(`media_player.ls50_wireless_ii_174476_2`, `_3`) stay unexposed; the Music Assistant one
(`media_player.ls50_wireless_ii_174476_4`, "Rumpus Room Speakers") is in the Rumpus Room area and
exposed since 2026-09-19 so that box's music has somewhere to play — it plays there like in any
other room, with no volume handling (the speakers keep their own level per input; the earlier
save / 50 % / restore logic was removed on 2026-09-19) (`.agents/plans/voice-assist-rollout.md`, Phase 3).

Thermostats (2026-09-20): `script.voice_thermostat` is the one voice tool for thermostat **modes**
(cool / heat / dry / fan only / auto / off) and for the pair of basement mini splits — Assist has no
set-mode intent, and its set-temperature intent takes a single thermostat, so "set the basement
thermostats to dry mode" failed on both counts. Fixed list: `rumpus_room`, `movie_room`, `basement`
(both Cielo units), `downstairs`, `upstairs` (the ecobees; no dry / fan only). A single thermostat's
temperature still goes through the built-in intent — which cannot match a Cielo unit that is **off**
(no target-temperature feature in that state); the tool can, when it is given a mode with the
temperature.

Defects found (open): Movie ZEN37 buttons 3 ×2 / 4 ×2 call
`number.movie_room_breeze_target_temperature`, which does not exist (the Cielo controller has
only `climate.movie_room_breeze`) while `button-mappings.md` documents them as working;
`light.basement_hall_night_light` in the repo's night-light watchdog does not exist live; six
live ZEN37 hold-dim helper automations and four scripts the buttons call
(`toggle_hue_colors`, `toggle_hue_colors_for_multiple_targets`,
`cycle_gradient_scene_using_same_light_color`, `zen32_hard_reset`) have no repo mirror.

## Exterior

No exterior satellite. Exposed since 2026-09-19 (Ruling 1, "ask + secure only"): the six
status sensors and the two secure-direction door scripts listed under *Dangerous-set status*
below. Lights since 2026-09-19 (Tom approved): the porch group, front door light,
`light.landscape_lights` (new HA group: Lily + Calla), lamp post, driveway, back-yard dimmer,
garage interior lights and the back-yard flood/spotlight — spoken names are aliases, nothing was
renamed. Three light relays (`switch.back_yard_retaining_wall_lights_relay`,
`switch.shed_exterior_lights_shelly_relay`, `switch.back_yard_backyard_motion_light_relay`) are on
the guard's `switch_allowlist` from v1.18.6. No exterior speaker yet. Only two switches have any
button mapping (Config 2x = hold on the back-yard spotlight and the garage mudroom switch).

Schedules (all unconditional, manual state simply lasts until the next edge):

| Lights | On | Off |
|---|---|---|
| Front yard Lily (100 %) + Calla (25 %), porch group (25 %), front door light | sunset | 00:00 |
| Patio retaining wall relay (`switch.back_yard_retaining_wall_lights_relay`) | sunset | 00:00 |
| Shed flowerbox relay (`switch.shed_exterior_lights_shelly_relay`) | sunset | 00:00 |
| Driveway (`light.garage_driveway_inovelli_dimmer`), back yard (`light.garage_back_yard_inovelli_dimmer`) | sunset | 01:00 |
| Back-yard motion-light relay | 00:00 | sunrise |
| Lamp post (`light.front_yard_lamp_post_hue_edison`) | nothing | nothing |

- **Hot tub mode** (fixed 2026-09-19): `automation.switch_back_yard_spa_lights_manage_spotlight_auto_hold`
  auto-holds the spotlight switch and turns the flood light off while either spa light is on, and
  releases when neither is on any more; it calls `script.voice_hot_tub_mode_on/_off`, which hold the
  parameters and are also the voice tools (Mode Off refuses while a spa light is on).
- **Back-yard spotlight** (`light.downstairs_kitchen_back_yard_spotlight`) is the contested one:
  three automations (slider opens, camera person/animal, auto-off after 5 min clear with a hard
  45 min cap) — all disabled while the paddle hold is engaged. A plain voice turn-on is fought.
- **Garage interior** (`light.garage_interior_lights`): a voice/paddle turn-on arms a 5 min floor
  timer, then occupancy turns it off ~2 min after clear; voice off is respected.
- Shed fan is occupancy-owned (door/motion → on, 5 min still → off).
- Holiday scenes/automations (Halloween, Christmas) are enabled by hand, have no mode helper and
  reference several entities that are unavailable or gone out of season.
- Front yard has two Z2M groups (`light.front_yard_hue_lily_lights`, `…_calla_lights`); the
  single voice handle is the HA group `light.landscape_lights` (2026-09-19).
- Patio/shed lights and the motion flood relay are `switch.*` and allowlisted by id (v1.18.6);
  the shed fan stays denied (occupancy-owned, and not a light).
- Every `fan.*` outside is spa equipment. Opener bulbs `light.ratgdov25i_*_light` pass a
  domain-based deny list and should be denied by name.

Dangerous-set status: locks `lock.front_door_lock`, `lock.side_door_lock`, `lock.bulkhead_lock`
(must be locked) and `lock.mudroom_door_lock` (interior garage↔mudroom door, **expected
unlocked** by `automation.wall_display_entry_locks_status`); garage doors
`cover.ratgdov25i_4a0325_door` (Tesla) and `cover.ratgdov25i_dbfa50_door` (Wagoneer). **Built
2026-09-19:** read-only Template sensor helpers
`sensor.{front_door,side_door,bulkhead,mudroom_door}_lock_state` and
`sensor.{tesla,wagoneer}_garage_door_state` (text states — a `device_class: lock` binary sensor
was misread by the LLM), `script.voice_lock_all_doors` (front, side, bulkhead; lock-only) and
`script.voice_close_garage_doors` (close-only). The `input_text.*_entry_locks_status` roll-ups
stay invisible to Assist (no tool reads `input_text`).

Defects found (open; the dead spa-light auto-hold trigger was fixed on 2026-09-19):
`input_select.garage_interior_mudroom_mmwave_normal_mode` reads
"Occupancy (default)" while the switch is `Disabled`, so the next Config 2x re-enables local
mmWave control instead of holding; the spotlight mapping passes an `input_number` as
`prev_led_color_helper` (written with `input_text.set_value`, so it never updates);
`automation.notify_garage_ratgdo_door_open_close_tom_iphone` is off; 14 exterior automations
have no repo mirror.

## Second floor

Three control layers — only one is visible to HA:

1. **Zigbee binding / Smart Bulb Mode** (paddle groupcasts straight to the Hue group; HA sees no
   press): primary bed, nook, hall, closet, bath recessed, all upstairs-foyer switches. Nothing to
   mirror — wall and voice are peers setting the same group.
2. **HA automation on a button event**: Inovelli **config button** 1x/2x only (no paddle
   multi-taps are mapped anywhere on this floor) and the five ZEN32 scene controllers.
3. **Device-local mmWave** (`MmwaveControlWiredDevice` ≠ Disabled): primary bath vanity
   (Wasteful Occupancy), water closet, kids bath vanity, laundry. HA owns only the off.

| Room | Physical concept → target | Ownership / voice caveat |
|---|---|---|
| Primary Bedroom | ZEN32: big = toggle `light.upstairs_primary_nightstand_lights` (Z2M); TL/TR = fan on/off; BL 1x/2x = `scene.laundry_gateway_main_bedroom_tilt_open` / `_open`; BR = `_close`. Bed + nook dimmers are binding-only | no occupancy lighting; nightstands timer 19:00 on / 02:00 off (targets the HA group `light.primary_bedroom_nightstand_lights`, its only consumers) |
| Primary Bathroom | recessed Config 1x = **all four loads** (vanity, shower, fan light, `light.upstairs_primary_bath_lights`) on/off + shower delay; vanity Config 1x = all off; shower Config 1x = shower + fan light | off automation: 0 s, or 5 min when `input_boolean.upstairs_primary_bathroom_use_shower_delay` is armed — and **any** turn-on of shower/fan light/recessed (voice included) arms it |
| Primary Cloffice | ZEN32: big 1x/2x = toggle / 100 % 2823 K `light.upstairs_primary_cloffice_lights`; TL/TR = Kellie's / Tom's **bedroom** nightstand bulb @20 %, 2x = `light.den_hue_iris_light`; BL = cloffice tilt-open / open; BR = close / **privacy** scene | nothing automated but the night light |
| Primary Closet | Config 2x hold only | door contact turns the light on/off instantly, no delay |
| Primary Hallway | Config 2x hold only | motion on (ungated) + off after 2 min; 93 state changes/24 h — voice lasts minutes without a hold |
| Upstairs Foyer | six Inovellis, binding only | `light.upstairs_foyer_lights` has no automation and is not exposed |
| Blue / White / Pink rooms | ZEN32: big 1x = `light.<room>_fan_light`, 2x = nightstand bulb (Jackson / Penelope; White has none), TL/TR = `fan.<room>_fan_fan` on/off, BL = tilt-open / open scenes, BR = close | no occupancy, timers or holds; Blue Room TV off at 01:00 |
| Kids Bathroom | vanity local mmWave, Config 2x hold | off after 5 min |
| Laundry | fully device-local mmWave | hold works only via the mmWave select |

Shades: three parallel handles. Every ZEN32 button and both schedules (tilt-open 06:45
weekdays / 09:00 weekends, close at sunset, each sent twice) use the **gateway scenes**
(`scene.laundry_gateway_<room>_{open,close,tilt_open}`, `scene.upstairs_gateway_cloffice_*`);
voice currently has the `cover.*` groups, which can open/close but have no spoken path to
**tilt-open — the everyday morning position**. Upstairs 1x = tilt-open, 2x = open (inverted vs
downstairs).

Holds (reference only — Tom ruled on 2026-09-19 that voice gets **no hold tools**; the one
possible exception, the hot-tub flood light, is an open question in the plan): the deterministic
pair is `script.inovelli_set_mmwave_hold_led_indicator` /
`script.inovelli_clear_mmwave_hold_restore_led_indicator` (omit `hold_color` on clear so a wall
hold can be released); the wall buttons use the toggle script. Holds never expire. Rooms with a
hold: primary hall, closet, vanity, shower, water closet, laundry, kids vanity. The motion-cleared
automations re-read the LED number themselves, so anything that holds must paint the LEDs, not
just disable automations.

Voice tools on this floor (2026-09-19): `script.voice_primary_bathroom_lights_on` / `_lights_off`
/ `_shower_lights` (the three bathroom config buttons), `script.voice_cloffice_bright` (cloffice
ZEN32 big 2x) and the Iris lamp `light.den_hue_iris_light`; shades go through
`script.voice_shades`. The floor's thermostat is `climate.second_floor_ecobee` ("upstairs"); its
**mode** (cool / heat / auto / off) goes through `script.voice_thermostat` — see *Basement*.

**Never expose `switch.upstairs_*_scene_controller`** — the ZEN32 relay is line power to the
Modern Forms fan module.

Wrong handles found on 2026-09-18 — all three corrected on Tom's rulings (2026-09-18/19):
"nightstand lights" moved from the HA group to the Z2M group
`light.upstairs_primary_nightstand_lights`; "bedroom lights" is the new HA group
`light.primary_bedroom_lights` (ceiling + nook + nightstands) instead of an alias on the
five-room suite group; `light.upstairs_primary_bath_lights` now answers only to "bathroom
recessed lights", and the whole bathroom is the `voice_primary_bathroom_lights_on/_off` pair.
Kids' rooms: Tom ruled on 2026-09-19 that voice gets everything the ZEN32 does, from any box —
fan light, fan, nightstand bulb, shades (through `script.voice_shades`), Sonos and TV in all three
rooms; the ZEN32 relay switches stay unexposed.

Defects found (open): `script.single_button_dimming_start/_stop` unavailable since the
2026-09-18 restart (`_2` twins healthy) — cloffice hold-dimming dead; ZEN32 big 3x hard reset dead
on all five controllers (relay-control selects disabled by integration); kids' fan watchdogs off
and malformed (`unavailable_fan_entity:` key, non-existent `fan.blue_room_fan_light`);
`automation.watchdog_reset_unknown_night_light` off; bath fan-light Config 1x off does not clear
the shower-delay boolean; `button-mappings.md` wrongly lists Primary Hall and the two foyer
presence switches as unmapped.

## First floor

Exposed since 2026-09-19 (before that only the living room lamp, the kitchen and living room
Sonos and the global Play Music script were): the kitchen main / island / under-cabinet / sink
lights, the living room recessed lights, sconces, fan light, fan and lamp, `climate.first_floor_ecobee`,
the study lights, bookshelf, fan light, fan and `media_player.study`, the dining table and cove
lights, the mudroom and entrance lights, the foyer chandelier and the bathroom group
`light.downstairs_bathroom_lights`. There are no room/lighting scenes anywhere in the house — all
51 scenes are PowerView shades or holiday sets.

Voice tools on this floor (2026-09-19): `script.voice_kitchen_lights_off` (= Upstairs Foyer Chaos
Config 1x, every kitchen light off) and `script.voice_entrance_all_off` (= Entrance Config 1x,
entrance + mudroom + the three bathroom loads off). Both are literal copies of the button actions
and are allowed by the guard's `script.voice_*` pattern (per-name entries from v1.18.5, the pattern
since v1.19.2). The **mode** of
`climate.first_floor_ecobee` ("downstairs": cool / heat / auto / off) goes through
`script.voice_thermostat` (2026-09-20) — see *Basement*.

| Area | Physical controls (mapped buttons only) | Concepts → handle | Ownership / voice caveat |
|---|---|---|---|
| Kitchen | recessed Inovelli Config 1x = toggle island; upstairs "Foyer Chaos" Config 1x = **all kitchen lights off** (sink + island + under-cabinet + group); sink/island/under-cabinet unmapped | main `light.downstairs_kitchen_lights` (10 Hue + switch), island `…_island_inovelli_dimmer`, under-cabinet `…_kitches_under_cabinet_inovelli_dimmer` (sic), sink `…_kitchen_sink_inovelli_presence`; all off = `script.voice_kitchen_lights_off` | fully manual, nothing fights |
| Livingroom | ZEN32: big = `light.livingroom_fan_light`; TL/TR = `fan.livingroom_fan_fan` on/off; BL = `scene.laundry_gateway_family_room_open` (2x tilt-open); BR = close | recessed `light.downstairs_livingroom_lights`, lamp, sconce `…_sconce_inovelli_dimmer`, fan light, fan, `climate.first_floor_ecobee`; **no unified "living room lights"** | manual; lamp: `automation.lamp_todo` sunset → 20 % 2500 K, off 02:00 |
| Study | ZEN32, same layout → `light.study_fan_light`, `fan.study_fan_fan`, `scene.laundry_gateway_office_*` | main `light.downstairs_study_lights`, bookshelf `…_study_bookshelf_inovelli_dimmer` | manual |
| Dining Room | none mapped | table `light.downstairs_dining_room_table_light`, cove `…_cove_inovelli_dimmer` | ZEN20 strip outlets 4+5 on at sunset, off 23:00 |
| Mudroom | Config 2x = hold | `light.downstairs_mudroom_lights` | occupancy on/off (2 min) **and** the switch still has local mmWave — relights within seconds while occupied |
| Entrance | Config 1x = **all off on the way out** (entrance + mudroom + 3 bath loads); Config 2x = hold | `light.downstairs_entrance_lights`; all off = `script.voice_entrance_all_off` | occupancy on, off after 2 min; voice off sticks until re-entry |
| First Floor Bathroom | vanity Config 2x = hold | three separate loads grouped as `light.downstairs_bathroom_lights` (HA light group, 2026-09-19); `cover.1_4` | vanity is firmware-driven; shower + fan light off after 2 min |
| Foyer | three "chaos" Inovellis, unmapped | chandelier `light.foyer_chaos_light_switches`; the overhead recessed are `light.upstairs_foyer_lights` — "foyer lights" is ambiguous | manual |

- **Shades:** the `cover.*_shades` groups are status/LED-tracking only; every wall button and both
  schedules (close at sunset, open downstairs at sunrise, each fired twice a few minutes apart) use
  the gateway scenes. Silhouette tilt-open = position 0 / tilt 100, still "closed" to HA. Voice
  should get per-room scripts wrapping the open / close / tilt-open scenes, not the cover groups.
  Downstairs 1x = open, 2x = tilt-open (upstairs is the reverse).
- **Cleaners Mode** (odd ISO weeks, Monday 09:00–17:00) holds Entrance/Mudroom/Bath, brightens
  Entrance + Mudroom + Kitchen + Livingroom, stops fans, and restores the 09:00 snapshot at 17:00.
- **Media:** the Sonos players are `music_assistant` only (native `sonos` entry ignored) — no
  turn_on/turn_off. The Frame TV has two entities (`…_the_frame_75_2` dlna, `…_the_frame_75` MA).
  The Play Music script already reaches every first-floor Sonos by area.
  Since 2026-09-19 two more music tools work from any box: `script.voice_move_music` ("move the
  music to the living room": same song, new room) and `script.voice_group_music` ("play this in
  the kitchen too" / "stop it in the kitchen": a synced Sonos group).
  House-wide rule since 2026-09-19: **"stop the music" stops AND ungroups** (sentence-trigger
  automation `automation.voice_stop_the_music_stop_and_ungroup`, never touches a soundbar on its TV
  input); **"pause" keeps the group**. Since 2026-09-20 the automation also matches "stop **all**
  (the) music", "turn off all the music" and "ungroup the speakers" (= everywhere, handled locally in
  ~0.1 s), and every other phrasing reaches the agent's `script.voice_stop_music` (empty room =
  the whole house; a named room = that room plus everything grouped with it). Before that tool
  existed the agent answered a stop request with `HassTurnOff` (nothing stops: Music Assistant Sonos
  players have no turn_off) or `HassMediaPause` (pauses the whole group and keeps it grouped).
- **Never expose:** the two locks, the Café ovens/fridge (`water_heater.*`) and appliance
  switches, `switch.downstairs_{livingroom,study}_scene_controller` (fan mains power),
  `switch.zigbee2mqtt_bridge_permit_join` (sits in Dining Room), the dining strip outlets, printer
  switch, Voice PE mute/LED entities.

Defects found (open): `cover.kitchen_shade` unresponsive for ~3 days; Study shade buttons on both
wall displays call non-existent `scene.laundry_gateway_study_*` (real: `…_office_*`) in the live
dashboards and the repo cards; Mudroom switch mmWave is "Occupancy (default)" while HA owns the
load; foyer/kitchen night-light automations reference `binary_sensor.study_sensor_occupancy` /
`…dining_room_sensor_occupancy` (real ids have an `ecobee_` prefix);
`light.downstairs_foyer_night_light` is assigned to the Basement Hallway area; disabled holiday
automations target removed Twinkly lights; all first-floor schedule automations are live-only.
Note: `floor_name(area_id)` returns None for areas whose id differs from their name — a template
artifact, not a registry fault.
