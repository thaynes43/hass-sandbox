# Smart Switch Button Mappings (Current + Planned)

This document is the **requirements + current-state reference** for how every smart switch button should behave in this Home Assistant setup.

- **Current state** is derived from the YAML automations under `automations/switch-buttons/**`.
- **Planned state** is documented inline (blank cells / TODO notes) so we can implement behavior consistently later.
- This is also intended to drive a later effort: **family-facing infographic cards** (so clarity and consistency matter).

---

## Conventions

### Click types we document

- **1x**: single click / tap
- **2x**: double click / tap
- **3x**: triple click / tap

We usually don’t use 4x/5x (supported by some devices), but can add later.

### How to read “Action”

Cells are written as a short “what it does” description, followed by the key HA target(s) (light / scene / script / helper) when known.

### “Current vs Planned”

- **Current**: implemented in HA today (via an automation in this repo)
- **Planned**: left blank or marked TODO to be implemented later

---

## Device Families & Button Layouts

### Inovelli Blue (Zigbee2MQTT device actions)

We treat Inovelli mappings as **consistent across switch variants**:

- **Presence dimmer** (mmWave) → typically `*_inovelli_presence`
- **Dimmer** → typically `*_inovelli_dimmer`
- **On/Off** → typically `*_inovelli_on_off`

**Physical buttons**

- **Up paddle**
- **Down paddle**
- **Config button** (small button)

**Wiring / companion switches (AUX)**

In a 3-way+ wiring, an Inovelli smart switch can be configured for **AUX companion switches**.

- AUX companions are **separate physical switches wired to the smart switch**.
- They often **mimic** the smart switch, and may have their own **scene controls** depending on the AUX model + configuration.
- AUX companions generally **do not appear as entities** in Home Assistant, so we document their expected behavior here as requirements.

**How we detect AUX wiring**

If Home Assistant shows the smart switch’s SwitchType selector as Aux, we treat that as “AUX companions present”:

- Example: `select.<switch>_switchtype` = `Aux Switch` (or `3-Way Aux Switch`)
- If the SwitchType is `Single Pole`, that usually means the switch is standalone (or a smart-switch group where each is still a smart switch).

**Implementation source (current)**

- Blueprint: `blueprints/z2m-v2-0-inovelli-blue-series-2-in-1-switch-dimmer.yaml`
- Automations: `automations/switch-buttons/inovelli-button-mapping/*.yaml`

### Zooz ZEN32 (in-wall scene controller)

**Physical buttons** (typical engraved layout)

- **Big button** (top) — usually “Light”
- **Top-left** — usually “Fan On”
- **Top-right** — usually “Fan Off”
- **Bottom-left** — usually “Shade Up”
- **Bottom-right** — usually “Shade Down”

**Implementation source (current)**

- Blueprint: `blueprints/ZEN32-control-track.yaml`
- Automations: `automations/switch-buttons/zen32-button-mapping/*.yaml`

### Zooz ZEN37 (battery scene controller)

**Physical buttons**

- **Button 1**: large top
- **Button 2**: large bottom
- **Button 3**: small bottom-left
- **Button 4**: small bottom-right

**Implementation source (current)**

- Blueprint: `blueprints/Zen37-ZwaveJS-blueprint.yaml`
- Automations: `automations/switch-buttons/zen37-button-mapping/*.yaml`

---

## Inovelli Blue — Current Mappings

Notes:

- Many Inovelli locations currently use **Config 2x** to **toggle hold** (sometimes per-switch, sometimes group hold for staircase pairs).
- Unless explicitly listed below, **Up/Down paddle multi-clicks (2x/3x)** and **Config 1x/3x** are currently **unassigned**.

**Detected AUX wiring (from Home Assistant `select.*_switchtype`)**

The following smart switches are currently configured for AUX companions (so you should assume **>= 1 AUX** at that location):

- `select.basement_concessions_inovelli_presence_switchtype` = `Aux Switch`
- `select.basement_hallway_inovelli_presence_switchtype` = `Aux Switch`
- `select.basement_rumpus_room_inovelli_presence_switchtype` = `Aux Switch`
- `select.garage_interior_side_inovelli_presence_switchtype` = `Aux Switch`
- `select.garage_interior_mudroom_inovelli_presence_switchtype` = `Aux Switch`
- `select.downstairs_entrance_inovelli_presence_switchtype` = `Aux Switch`
- `select.downstairs_mudroom_inovelli_presence_switchtype` = `Aux Switch`
- `select.downstairs_kitchen_sink_inovelli_presence_switchtype` = `Aux Switch`
- `select.downstairs_kitchen_recessed_inovelli_presence_switchtype` = `Aux Switch`
- `select.upstairs_primary_hall_inovelli_presence_switchtype` = `Aux Switch`
- `select.upstairs_primary_bedroom_foyer_lights_inovelli_presence_switchtype` = `Aux Switch`
- `select.upstairs_blue_room_foyer_lights_inovelli_presence_switchtype` = `Aux Switch`
- `select.upstairs_pink_room_foyer_lights_inovelli_presence_switchtype` = `Aux Switch`
- `select.downstairs_front_door_inovelli_on_off_switchtype` = `Aux Switch`
- `select.basement_bathroom_fan_light_inovelli_on_off_switchtype` = `Aux Switch`
- `select.garage_driveway_inovelli_dimmer_switchtype` = `3-Way Aux Switch`
- `select.upstairs_staircase_foyer_lights_inovelli_dimmer_switchtype` = `3-Way Aux Switch`

### Basement — Movie Room — Inovelli (Presence)

- **Type**: Inovelli Blue (presence)
- **HA entity**: `light.basement_movie_inovelli_presence`
- **Source automation**: `automations/switch-buttons/inovelli-button-mapping/basement_movie_room_inovelli_blue_switch_behavior.yaml`

| Button | 1x | 2x | 3x | Notes |
|---|---|---|---|---|
| Up paddle | (default local behavior) | **Set Movie Room Lights 100% @ 2750K** → `light.basement_movie_room_lights` |  |  |
| Down paddle | (default local behavior) | **Set Movie Room Lights 33% @ 2750K** → `light.basement_movie_room_lights` |  |  |
| Config | **Toggle ambient lighting** → `light.basement_movie_room_ambient_lighting` | **Toggle hold** |  |  |

**AUX companion switches (requirements)**

- **AUX count**: TODO
- **AUX behavior**: TODO (mimic main? any scene buttons?)

### Basement — Rumpus Room — Inovelli (Presence)

- **Type**: Inovelli Blue (presence)
- **HA entity**: `light.basement_rumpus_room_inovelli_presence`
- **SwitchType (HA)**: `select.basement_rumpus_room_inovelli_presence_switchtype` = `Aux Switch`
- **Source automation**: `automations/switch-buttons/inovelli-button-mapping/switch_inovelli_blue_basement_rumpus_room_press_or_hold_switch_mappings.yaml`

| Button | 1x | 2x | 3x | Notes |
|---|---|---|---|---|
| Up paddle | Local on (load) |  |  | Multi-clicks unassigned |
| Down paddle | Local off (load) |  |  | Multi-clicks unassigned |
| Config |  | **Toggle hold** |  |  |

**AUX companion switches (requirements)**

- **AUX count**: TODO (>= 1)
- **AUX behavior**: TODO

### Basement — Concessions — Inovelli (Presence)

- **Type**: Inovelli Blue (presence)
- **HA entity**: `light.basement_concessions_inovelli_presence`
- **SwitchType (HA)**: `select.basement_concessions_inovelli_presence_switchtype` = `Aux Switch`
- **Source automation**: `automations/switch-buttons/inovelli-button-mapping/switch_inovelli_blue_basement_concessions_press_or_hold_switch_mappings.yaml`

| Button | 1x | 2x | 3x | Notes |
|---|---|---|---|---|
| Up paddle | Local on (load) |  |  | Multi-clicks unassigned |
| Down paddle | Local off (load) |  |  | Multi-clicks unassigned |
| Config |  | **Toggle hold** |  |  |

**AUX companion switches (requirements)**

- **AUX count**: TODO (>= 1)
- **AUX behavior**: TODO

### Basement — Hallway — Inovelli (Presence)

- **Type**: Inovelli Blue (presence)
- **HA entity**: `light.basement_hallway_inovelli_presence`
- **SwitchType (HA)**: `select.basement_hallway_inovelli_presence_switchtype` = `Aux Switch`
- **Source automation**: `automations/switch-buttons/inovelli-button-mapping/switch_inovelli_blue_basement_hallway_press_or_hold_switch_mappings.yaml`

| Button | 1x | 2x | 3x | Notes |
|---|---|---|---|---|
| Up paddle | Local on (load) |  |  | Multi-clicks unassigned |
| Down paddle | Local off (load) |  |  | Multi-clicks unassigned |
| Config |  | **Toggle hold** |  |  |

**AUX companion switches (requirements)**

- **AUX count**: TODO (>= 1)
- **AUX behavior**: TODO

### Basement — Bathroom Vanity — Inovelli (Presence)

- **Type**: Inovelli Blue (presence)
- **HA entity**: `light.basement_bathroom_vanity_inovelli_presence`
- **Source automation**: `automations/switch-buttons/inovelli-button-mapping/switch_inovelli_blue_basement_bathroom_vanity_press_or_hold_switch_mappings.yaml`

| Button | 1x | 2x | 3x | Notes |
|---|---|---|---|---|
| Up paddle | Local on (load) |  |  | Multi-clicks unassigned |
| Down paddle | Local off (load) |  |  | Multi-clicks unassigned |
| Config |  | **Toggle hold** |  |  |
| AUX (placeholder) |  |  |  | TODO |

### Basement — Server — Inovelli (Presence)

- **Type**: Inovelli Blue (presence)
- **HA entity**: `light.basement_server_inovelli_presence`
- **Source automation**: `automations/switch-buttons/inovelli-button-mapping/switch_inovelli_blue_basement_server_press_or_hold_switch_mappings.yaml`

| Button | 1x | 2x | 3x | Notes |
|---|---|---|---|---|
| Up paddle | Local on (load) |  |  | Multi-clicks unassigned |
| Down paddle | Local off (load) |  |  | Multi-clicks unassigned |
| Config |  | **Toggle hold** |  |  |
| AUX (placeholder) |  |  |  | TODO |

### Basement — Staircase (Upper + Lower) — Inovelli (Presence) — Group Hold

- **Type**: Inovelli Blue (presence) x2
- **HA entities**:
  - `light.basement_upper_staircase_inovelli_presence`
  - `light.basement_lower_staircase_inovelli_presence`
- **Source automations**:
  - `automations/switch-buttons/inovelli-button-mapping/switch_inovelli_blue_basement_upper_staircase_press_or_hold_switch_mappings.yaml`
  - `automations/switch-buttons/inovelli-button-mapping/switch_inovelli_blue_basement_lower_staircase_press_or_hold_switch_mappings.yaml`

| Button | 1x | 2x | 3x | Notes |
|---|---|---|---|---|
| Up paddle (either switch) | Local on (that switch’s load) |  |  | Multi-clicks unassigned |
| Down paddle (either switch) | Local off (that switch’s load) |  |  | Multi-clicks unassigned |
| Config (either switch) |  | **Toggle hold (group)** |  |  |
| AUX (placeholder) |  |  |  | TODO |

### Downstairs — Entrance — Inovelli (Presence)

- **Type**: Inovelli Blue (presence)
- **HA entity**: `light.downstairs_entrance_inovelli_presence`
- **SwitchType (HA)**: `select.downstairs_entrance_inovelli_presence_switchtype` = `Aux Switch`
- **Source automation**: `automations/switch-buttons/inovelli-button-mapping/switch_inovelli_blue_downstairs_entrance_press_or_hold_switch_mappings.yaml`

| Button | 1x | 2x | 3x | Notes |
|---|---|---|---|---|
| Up paddle | Local on (load) |  |  | Multi-clicks unassigned |
| Down paddle | Local off (load) |  |  | Multi-clicks unassigned |
| Config | **All Off (Entrance + Mudroom + Bathroom)** → `light.downstairs_entrance_lights`, `light.downstairs_mudroom_lights`, `light.downstairs_bathroom_vanity_inovelli_presence`, `light.downstairs_bathroom_fan_light_inovelli_on_off`, `light.downstairs_bathroom_shower_inovelli_on_off` |  |  | Config 1x is mapped here |

**AUX companion switches (requirements)**

- **AUX count**: TODO (>= 1)
- **AUX behavior**: TODO

### Downstairs — Mudroom — Inovelli (Presence)

- **Type**: Inovelli Blue (presence)
- **HA entity**: `light.downstairs_mudroom_inovelli_presence`
- **SwitchType (HA)**: `select.downstairs_mudroom_inovelli_presence_switchtype` = `Aux Switch`
- **Source automation**: `automations/switch-buttons/inovelli-button-mapping/switch_inovelli_blue_downstairs_mudroom_press_or_hold_switch_mappings.yaml`

| Button | 1x | 2x | 3x | Notes |
|---|---|---|---|---|
| Up paddle | Local on (load) |  |  | Multi-clicks unassigned |
| Down paddle | Local off (load) |  |  | Multi-clicks unassigned |
| Config |  | **Toggle hold** |  |  |

**AUX companion switches (requirements)**

- **AUX count**: TODO (>= 1)
- **AUX behavior**: TODO

### Downstairs — Bathroom Vanity — Inovelli (Presence)

- **Type**: Inovelli Blue (presence)
- **HA entity**: `light.downstairs_bathroom_vanity_inovelli_presence`
- **Source automation**: `automations/switch-buttons/inovelli-button-mapping/switch_inovelli_blue_downstairs_bathroom_vanity_press_or_hold_switch_mappings.yaml`

| Button | 1x | 2x | 3x | Notes |
|---|---|---|---|---|
| Up paddle | Local on (load) |  |  | Multi-clicks unassigned |
| Down paddle | Local off (load) |  |  | Multi-clicks unassigned |
| Config |  | **Toggle hold** |  |  |
| AUX (placeholder) |  |  |  | TODO |

### Downstairs — Kitchen Recessed — Inovelli (Presence)

- **Type**: Inovelli Blue (presence)
- **HA entity**: `light.downstairs_kitchen_recessed_inovelli_presence`
- **SwitchType (HA)**: `select.downstairs_kitchen_recessed_inovelli_presence_switchtype` = `Aux Switch`
- **Source automation**: `automations/switch-buttons/inovelli-button-mapping/switch_inovelli_blue_kitchen_enterance_press_or_hold_switch_mappings.yaml`

| Button | 1x | 2x | 3x | Notes |
|---|---|---|---|---|
| Up paddle | Local on (load) |  |  | Multi-clicks unassigned |
| Down paddle | Local off (load) |  |  | Multi-clicks unassigned |
| Config | **Toggle kitchen island dimmer** → `light.downstairs_kitchen_island_inovelli_dimmer` |  |  | Currently mapped on Config 1x (not paddle) |

**AUX companion switches (requirements)**

- **AUX count**: TODO (>= 1)
- **AUX behavior**: TODO

### Garage — Mudroom Door — Inovelli (Presence)

- **Type**: Inovelli Blue (presence)
- **HA entity**: `light.garage_interior_mudroom_inovelli_presence`
- **SwitchType (HA)**: `select.garage_interior_mudroom_inovelli_presence_switchtype` = `Aux Switch`
- **Source automation**: `automations/switch-buttons/inovelli-button-mapping/switch_inovelli_blue_garage_mudroom_door_press_or_hold_switch_mappings.yaml`

| Button | 1x | 2x | 3x | Notes |
|---|---|---|---|---|
| Up paddle | Local on (load) |  |  | Multi-clicks unassigned |
| Down paddle | Local off (load) |  |  | Multi-clicks unassigned |
| Config |  | **Toggle hold** |  |  |

**AUX companion switches (requirements)**

- **AUX count**: TODO (>= 1)
- **AUX behavior**: TODO

### Upstairs — Kids Vanity — Inovelli (Presence)

- **Type**: Inovelli Blue (presence)
- **HA entity**: `light.upstairs_kids_bathroom_vanity_inovelli_presence`
- **Source automation**: `automations/switch-buttons/inovelli-button-mapping/switch_inovelli_blue_upstairs_kids_vanity_press_or_hold_switch_mappings.yaml`

| Button | 1x | 2x | 3x | Notes |
|---|---|---|---|---|
| Up paddle | Local on (load) |  |  | Multi-clicks unassigned |
| Down paddle | Local off (load) |  |  | Multi-clicks unassigned |
| Config |  | **Toggle hold** |  |  |
 
**AUX companion switches (requirements)**

- **AUX count**: TODO
- **AUX behavior**: TODO

### Upstairs — Laundry — Inovelli (Presence)

- **Type**: Inovelli Blue (presence)
- **HA entity**: `light.upstairs_laundry_inovlli_presence`
- **Source automation**: `automations/switch-buttons/inovelli-button-mapping/switch_inovelli_blue_upstairs_laundry_press_or_hold_switch_mappings.yaml`

| Button | 1x | 2x | 3x | Notes |
|---|---|---|---|---|
| Up paddle | Local on (load) |  |  | Multi-clicks unassigned |
| Down paddle | Local off (load) |  |  | Multi-clicks unassigned |
| Config |  | **Toggle hold** |  | Note: entity id includes `inovlli` typo; mapping uses same base name |
 
**AUX companion switches (requirements)**

- **AUX count**: TODO
- **AUX behavior**: TODO

### Upstairs — Primary Closet — Inovelli (Presence)

- **Type**: Inovelli Blue (presence)
- **HA entity**: `light.upstairs_primary_closet_inovelli_presence`
- **Source automation**: `automations/switch-buttons/inovelli-button-mapping/switch_inovelli_blue_upstairs_primary_closet_press_or_hold_switch_mappings.yaml`

| Button | 1x | 2x | 3x | Notes |
|---|---|---|---|---|
| Up paddle | Local on (load) |  |  | Multi-clicks unassigned |
| Down paddle | Local off (load) |  |  | Multi-clicks unassigned |
| Config |  | **Toggle hold** |  |  |
 
**AUX companion switches (requirements)**

- **AUX count**: TODO
- **AUX behavior**: TODO

### Upstairs — Primary Water Closet — Inovelli (Presence)

- **Type**: Inovelli Blue (presence)
- **HA entity**: `light.upstairs_primary_water_closet_inovelli_presence`
- **Source automation**: `automations/switch-buttons/inovelli-button-mapping/switch_inovelli_blue_upstairs_primary_water_closet_press_or_hold_switch_mappings.yaml`

| Button | 1x | 2x | 3x | Notes |
|---|---|---|---|---|
| Up paddle | Local on (load) |  |  | Multi-clicks unassigned |
| Down paddle | Local off (load) |  |  | Multi-clicks unassigned |
| Config |  | **Toggle hold** |  |  |
 
**AUX companion switches (requirements)**

- **AUX count**: TODO
- **AUX behavior**: TODO

### Upstairs — Primary Bathroom: Recessed / Fan Light / Shower / Vanity (Coordinated behaviors)

These switches share coordinated “whole bathroom” behaviors using templates/conditions and the helper:

- `input_boolean.upstairs_primary_bathroom_use_shower_delay`

#### Primary Bath Recessed — Inovelli (Dimmer)

- **Type**: Inovelli Blue (dimmer)
- **Source automation**: `automations/switch-buttons/inovelli-button-mapping/switch_inovelli_blue_upstairs_primary_bath_recessed_press_or_hold_switch_mappings.yaml`

| Button | 1x | 2x | 3x | Notes |
|---|---|---|---|---|
| Up paddle | Local on (load) |  |  | Multi-clicks unassigned |
| Down paddle | Local off (load) |  |  | Multi-clicks unassigned |
| Config | **“Whole bath” toggle** (turns on/off multiple bath lights + toggles shower-delay boolean) |  |  | Targets: vanity/shower/fan-light/bath-lights |

#### Primary Fan Light — Inovelli (On/Off)

- **Type**: Inovelli Blue (on/off)
- **Source automation**: `automations/switch-buttons/inovelli-button-mapping/switch_inovelli_blue_upstairs_primary_fan_light_press_or_hold_switch_mappings.yaml`

| Button | 1x | 2x | 3x | Notes |
|---|---|---|---|---|
| Up paddle | Local on (load) |  |  | Multi-clicks unassigned |
| Down paddle | Local off (load) |  |  | Multi-clicks unassigned |
| Config | **“Shower trio” toggle** (shower + bath-lights + fan-light, plus shower-delay boolean ON when turning on) |  |  | Targets: `light.upstairs_primary_bath_shower_inovelli_presence`, `light.upstairs_primary_bath_lights`, `light.upstairs_primary_bath_fan_light_inovelli_on_off` |

#### Primary Shower — Inovelli (Presence)

- **Type**: Inovelli Blue (presence)
- **HA entity**: `light.upstairs_primary_bath_shower_inovelli_presence`
- **Source automation**: `automations/switch-buttons/inovelli-button-mapping/switch_inovelli_blue_upstairs_primary_shower_press_or_hold_switch_mappings.yaml`

| Button | 1x | 2x | 3x | Notes |
|---|---|---|---|---|
| Up paddle | Local on (load) |  |  | Multi-clicks unassigned |
| Down paddle | Local off (load) |  |  | Multi-clicks unassigned |
| Config | **Toggle shower + fan light** (plus shower-delay boolean ON when turning on) | **Toggle hold** |  |  |

#### Primary Vanity — Inovelli (Presence)

- **Type**: Inovelli Blue (presence)
- **HA entity**: `light.upstairs_primary_bath_vanity_inovelli_presence`
- **Source automation**: `automations/switch-buttons/inovelli-button-mapping/switch_inovelli_blue_upstairs_primary_vanity_press_or_hold_switch_mappings.yaml`

| Button | 1x | 2x | 3x | Notes |
|---|---|---|---|---|
| Up paddle | Local on (load) |  |  | Multi-clicks unassigned |
| Down paddle | Local off (load) |  |  | Multi-clicks unassigned |
| Config | **All-off** (vanity + shower + fan-light + bath-lights, plus shower-delay boolean OFF) | **Toggle hold** |  |  |

---

## ZEN32 — Current Mappings

All ZEN32 mappings come from their per-location automation, and share the same underlying event model:

- **Big button**: `scene_5` (1x), `scene_52` (2x), `scene_53` (3x)
- **Top-left**: `scene_1` (1x), `scene_12` (2x), `scene_13` (3x)
- **Top-right**: `scene_2` (1x), `scene_22` (2x), `scene_23` (3x)
- **Bottom-left**: `scene_3` (1x), `scene_32` (2x), `scene_33` (3x)
- **Bottom-right**: `scene_4` (1x), `scene_42` (2x), `scene_43` (3x)

### Downstairs — Livingroom — ZEN32 (Engraved)

- **Source automation**: `automations/switch-buttons/zen32-button-mapping/switch_zen32_downstairs_livingroom_press_or_hold_scene_mapping.yaml`

| Button | 1x | 2x | 3x | Notes |
|---|---|---|---|---|
| Big (Light) | Toggle fan light → `light.livingroom_fan_light` |  | Hard reset helper script → `script.zen32_hard_reset` |  |
| Top-left (Fan On) | Fan on → `fan.livingroom_fan_fan` |  |  |  |
| Top-right (Fan Off) | Fan off → `fan.livingroom_fan_fan` |  |  |  |
| Bottom-left (Shade Up) | Open scene → `scene.laundry_gateway_family_room_open` | Tilt open scene (2x) → `scene.laundry_gateway_family_room_tilt_open` |  |  |
| Bottom-right (Shade Down) | Close scene → `scene.laundry_gateway_family_room_close` |  |  |  |

### Downstairs — Office (Study) — ZEN32 (Engraved)

- **Source automation**: `automations/switch-buttons/zen32-button-mapping/switch_zen32_downstairs_office_press_or_hold_scene_mapping.yaml`

| Button | 1x | 2x | 3x | Notes |
|---|---|---|---|---|
| Big (Light) | Toggle fan light → `light.study_fan_light` |  | Hard reset helper script → `script.zen32_hard_reset` |  |
| Top-left (Fan On) | Fan on → `fan.study_fan_fan` |  |  |  |
| Top-right (Fan Off) | Fan off → `fan.study_fan_fan` |  |  |  |
| Bottom-left (Shade Up) | Open scene → `scene.laundry_gateway_office_open` | Tilt open scene (2x) → `scene.laundry_gateway_office_tilt_open` |  |  |
| Bottom-right (Shade Down) | Close scene → `scene.laundry_gateway_office_close` |  |  |  |

### Upstairs — Blue Room — ZEN32 (Engraved)

- **Source automation**: `automations/switch-buttons/zen32-button-mapping/upstairs_blue_room_zen32_scene_controller_mapping.yaml`

| Button | 1x | 2x | 3x | Notes |
|---|---|---|---|---|
| Big (Light) | Toggle fan light → `light.blue_room_fan_light` | Toggle nightstand light → `light.upstairs_blue_room_nightstand_hue_bulb_jackson` | Hard reset helper script → `script.zen32_hard_reset` |  |
| Top-left (Fan On) | Fan on → `fan.blue_room_fan_fan` |  |  |  |
| Top-right (Fan Off) | Fan off → `fan.blue_room_fan_fan` |  |  |  |
| Bottom-left (Shade Up) | Tilt open scene → `scene.laundry_gateway_blue_room_tilt_open` | Open scene (2x) → `scene.laundry_gateway_blue_room_open` |  |  |
| Bottom-right (Shade Down) | Close scene → `scene.laundry_gateway_blue_room_close` |  |  |  |

### Upstairs — Pink Room — ZEN32 (Engraved)

- **Source automation**: `automations/switch-buttons/zen32-button-mapping/switch_zen32_upstairs_pink_room_press_or_hold_scene_mapping.yaml`

| Button | 1x | 2x | 3x | Notes |
|---|---|---|---|---|
| Big (Light) | Toggle fan light → `light.pink_room_fan_light` | Toggle nightstand light → `light.upstairs_pink_room_nightstand_hue_bulb_penelope` | Hard reset helper script → `script.zen32_hard_reset` |  |
| Top-left (Fan On) | Fan on → `fan.pink_room_fan_fan` |  |  |  |
| Top-right (Fan Off) | Fan off → `fan.pink_room_fan_fan` |  |  |  |
| Bottom-left (Shade Up) | Tilt open scene → `scene.laundry_gateway_pink_room_tilt_open` | Open scene (2x) → `scene.laundry_gateway_pink_room_open` |  |  |
| Bottom-right (Shade Down) | Close scene → `scene.laundry_gateway_pink_room_close` |  |  |  |

### Upstairs — White Room — ZEN32 (Engraved)

- **Source automation**: `automations/switch-buttons/zen32-button-mapping/upstairs_white_room_zen32_scene_and_state_z_wave_js.yaml`

| Button | 1x | 2x | 3x | Notes |
|---|---|---|---|---|
| Big (Light) | Toggle fan light → `light.white_room_fan_light` |  | Hard reset helper script → `script.zen32_hard_reset` |  |
| Top-left (Fan On) | Fan on → `fan.white_room_fan_fan` |  |  |  |
| Top-right (Fan Off) | Fan off → `fan.white_room_fan_fan` |  |  |  |
| Bottom-left (Shade Up) | Tilt open scene → `scene.laundry_gateway_white_room_tilt_open` | Open scene (2x) → `scene.laundry_gateway_white_room_open` |  |  |
| Bottom-right (Shade Down) | Close scene → `scene.laundry_gateway_white_room_close` |  |  |  |

### Upstairs — Primary Bedroom — ZEN32 (Engraved)

- **Source automation**: `automations/switch-buttons/zen32-button-mapping/switch_zen32_upstairs_primary_bedroom_press_or_hold_scene_mapping.yaml`

| Button | 1x | 2x | 3x | Notes |
|---|---|---|---|---|
| Big (Light) | Toggle nightstand lights → `light.upstairs_primary_nightstand_lights` |  | Hard reset helper script → `script.zen32_hard_reset` |  |
| Top-left (Fan On) | Fan on → `fan.primary_bedroom_fan_fan` |  |  |  |
| Top-right (Fan Off) | Fan off → `fan.primary_bedroom_fan_fan` |  |  |  |
| Bottom-left (Shade Up) | Tilt open scene → `scene.laundry_gateway_main_bedroom_tilt_open` | Open scene (2x) → `scene.laundry_gateway_main_bedroom_open` |  |  |
| Bottom-right (Shade Down) | Close scene → `scene.laundry_gateway_main_bedroom_close` |  |  |  |

### Upstairs — Cloffice — ZEN32 (Custom)

- **Source automation**: `automations/switch-buttons/zen32-button-mapping/upstairs_cloffice_zen32_scene_and_state_z_wave_js.yaml`
- **Note**: This location uses additional hold/release logic for dimming on the big button.

| Button | 1x | 2x | 3x | Notes |
|---|---|---|---|---|
| Big (Light) | Toggle cloffice lights → `light.upstairs_primary_cloffice_lights` | Turn on 100% @ 2823K → `light.upstairs_primary_cloffice_lights` | Hard reset helper script → `script.zen32_hard_reset` | Hold/Release also mapped (see below) |
| Top-left | Toggle (dim) → `light.upstairs_primary_bed_nightstand_hue_bulb_kellie` (20%) | Toggle → `light.den_hue_iris_light` |  |  |
| Top-right | Toggle (dim) → `light.upstairs_primary_bed_nightstand_hue_bulb_tom` (20%) | Toggle hue colors → `script.toggle_hue_colors` |  |  |
| Bottom-left | Tilt open scene → `scene.upstairs_gateway_cloffice_tilt_open` | Open scene (2x) → `scene.upstairs_gateway_cloffice_open` |  |  |
| Bottom-right | Close scene → `scene.upstairs_gateway_cloffice_close` | Privacy scene (2x) → `scene.upstairs_gateway_cloffice_privacy` |  |  |

**Cloffice hold/release (implemented today)**

- Big button **Hold**: `script.single_button_dimming_start`
- Big button **Release**: `script.single_button_dimming_stop`

---

## ZEN37 — Current Mappings

### Basement — Movie Room — ZEN37

- **Source automation**: `automations/switch-buttons/zen37-button-mapping/basement_movie_room_zen37_scene_controller.yaml`

| Button | 1x | 2x | 3x | Notes |
|---|---|---|---|---|
| Button 1 (top large) | Toggle movie room lights → `light.basement_movie_room_lights` | Set movie room lights 100% @ 2750K → `light.basement_movie_room_lights` | Toggle rumpus room lights → `light.basement_rumpus_room_lights` |  |
| Button 2 (bottom large) | Toggle hue colors → `script.toggle_hue_colors` (movie room lights) | Red night light (15%) → `light.basement_movie_room_lights` | Toggle concessions + hall → `light.basement_concessions_lights`, `light.basement_hall_lights` |  |
| Button 3 (small left) | Toggle ambient lighting → `light.basement_movie_room_ambient_lighting` | Decrease target temp → `number.movie_room_breeze_target_temperature` |  |  |
| Button 4 (small right) | Cycle gradient scenes → `script.cycle_gradient_scene_using_same_light_color` | Increase target temp → `number.movie_room_breeze_target_temperature` |  |  |

### Basement — Rumpus Room — ZEN37

- **Source automation**: `automations/switch-buttons/zen37-button-mapping/switch_zen37_basement_rumpus_room_press_or_hold_scene_mapping.yaml`

| Button | 1x | 2x | 3x | Notes |
|---|---|---|---|---|
| Button 1 (top large) | Toggle rumpus room lights → `light.basement_rumpus_room_lights` | Set lights + lamp 100% @ 2750K → `light.basement_rumpus_room_lights`, `light.basement_rumpus_room_lamp` |  |  |
| Button 2 (bottom large) | Toggle lamp → `light.basement_rumpus_room_lamp` | Set lights + lamp 50% @ 2750K → `light.basement_rumpus_room_lights`, `light.basement_rumpus_room_lamp` | Toggle hue colors (multi-target) → `script.toggle_hue_colors_for_multiple_targets` |  |
| Button 3 (small left) | Toggle concessions → `light.basement_concessions_lights` | Concessions 100% @ 2750K → `light.basement_concessions_lights` |  |  |
| Button 4 (small right) | Toggle hall → `light.basement_hall_lights` | Hall 100% @ 2750K → `light.basement_hall_lights` |  |  |

---

## Inovelli Devices Found in Home Assistant (Missing Mapping Automation)

The items below were found in HA (query: `inovelli` in `light.*`) but do **not** currently have a corresponding mapping automation under `automations/switch-buttons/inovelli-button-mapping/`.

Use these as **placeholders** for planned behavior (and later implementation).

> TODO: For each, decide whether we want standardized mappings (recommended) or per-room exceptions.

### Unmapped Inovelli (placeholders)

For each switch below, fill in desired behavior; then we’ll implement a matching automation.

| HA entity (light.*) | Type (inferred) | Config 1x | Config 2x | Config 3x | Up 1x | Up 2x | Up 3x | Down 1x | Down 2x | Down 3x | SwitchType (HA) | AUX companions (requirements) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `light.downstairs_kitchen_sink_inovelli_presence` | presence |  |  |  |  |  |  |  |  |  | `Aux Switch` | TODO (>= 1): count + what AUX does |
| `light.downstairs_kitchen_chaos_inovelli_presence` | presence |  |  |  |  |  |  |  |  |  |  |  |  |
| `light.downstairs_foyer_chaos_inovelli_presence` | presence |  |  |  |  |  |  |  |  |  |  |  |  |
| `light.downstairs_dining_room_table_corner_inovelli_presence` | presence |  |  |  |  |  |  |  |  |  |  |  |  |
| `light.downstairs_dining_room_table_wall_inovelli_presence` | presence |  |  |  |  |  |  |  |  |  |  |  |  |
| `light.downstairs_study_recessed_inovelli_presence` | presence |  |  |  |  |  |  |  |  |  |  |  |  |
| `light.downstairs_livingroom_recessed_inovelli_presence` | presence |  |  |  |  |  |  |  |  |  |  |  |  |
| `light.garage_interior_side_inovelli_presence` | presence |  |  |  |  |  |  |  |  |  | `Aux Switch` | TODO (>= 1): count + what AUX does |
| `light.upstairs_primary_hall_inovelli_presence` | presence |  |  |  |  |  |  |  |  |  | `Aux Switch` | TODO (>= 1): count + what AUX does |
| `light.upstairs_primary_bed_inovelli_presence` | presence |  |  |  |  |  |  |  |  |  |  |  |  |
| `light.upstairs_foyer_chaos_inovelli_presence` | presence |  |  |  |  |  |  |  |  |  |  |  |  |
| `light.upstairs_primary_bedroom_foyer_lights_inovelli_presence` | presence |  |  |  |  |  |  |  |  |  | `Aux Switch` | TODO (>= 1): count + what AUX does |
| `light.upstairs_blue_room_foyer_lights_inovelli_presence` | presence |  |  |  |  |  |  |  |  |  | `Aux Switch` | TODO (>= 1): count + what AUX does |
| `light.upstairs_pink_room_foyer_lights_inovelli_presence` | presence |  |  |  |  |  |  |  |  |  | `Aux Switch` | TODO (>= 1): count + what AUX does |

### Unmapped Inovelli (dimmer / on-off loads)

These also exist in HA and may need standardized config-button mappings.

| HA entity (light.*) | Type (inferred) | SwitchType (HA) | AUX companions (requirements) | Notes |
|---|---|---|---|---|
| `light.downstairs_front_door_inovelli_on_off` | on/off | `Aux Switch` | TODO (>= 1): count + what AUX does |  |
| `light.downstairs_bathroom_fan_light_inovelli_on_off` | on/off |  |  |  |
| `light.upstairs_kids_bathroom_fan_light_inovelli_on_off` | on/off |  |  |  |
| `light.basement_bathroom_fan_light_inovelli_on_off` | on/off | `Aux Switch` | TODO (>= 1): count + what AUX does |  |
| `light.downstairs_bathroom_shower_inovelli_on_off` | on/off |  |  |  |
| `light.downstairs_kitchen_island_inovelli_dimmer` | dimmer |  |  |  |
| `light.downstairs_front_door_foyer_lights_inovelli_dimmer` | dimmer |  |  |  |
| `light.downstairs_livingroom_sconce_inovelli_dimmer` | dimmer |  |  |  |
| `light.basement_storage_inovelli_dimmer` | dimmer |  |  |  |
| `light.downstairs_front_porch_inovelli_dimmer` | dimmer |  |  |  |
| `light.downstairs_dining_room_cove_inovelli_dimmer` | dimmer |  |  |  |
| `light.downstairs_study_bookshelf_inovelli_dimmer` | dimmer |  |  |  |
| `light.upstairs_staircase_foyer_lights_inovelli_dimmer` | dimmer | `3-Way Aux Switch` | TODO (>= 1): count + what AUX does |  |
| `light.upstairs_primary_nook_inovelli_dimmer` | dimmer |  |  |  |
| `light.garage_driveway_inovelli_dimmer` | dimmer | `3-Way Aux Switch` | TODO (>= 1): count + what AUX does |  |
| `light.garage_back_yard_inovelli_dimmer` | dimmer |  |  |  |
| `light.downstairs_kitches_under_cabinet_inovelli_dimmer` | dimmer |  |  |  |
| `light.upstairs_primary_bath_recessed_inovelli_dimmer` | dimmer |  |  | Currently mapped (see above) |

---

## Appendix: Source of Truth Files

- Inovelli mappings: `automations/switch-buttons/inovelli-button-mapping/*.yaml`
- ZEN32 mappings: `automations/switch-buttons/zen32-button-mapping/*.yaml`
- ZEN37 mappings: `automations/switch-buttons/zen37-button-mapping/*.yaml`
- Inovelli blueprint: `blueprints/z2m-v2-0-inovelli-blue-series-2-in-1-switch-dimmer.yaml`
- ZEN32 blueprint: `blueprints/ZEN32-control-track.yaml`
- ZEN37 blueprint: `blueprints/Zen37-ZwaveJS-blueprint.yaml`

