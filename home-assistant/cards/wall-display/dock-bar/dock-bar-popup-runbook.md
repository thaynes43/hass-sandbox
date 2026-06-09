# Dock Bar Popup Runbook

This runbook is for building repeatable `dock-bar` popup cards for the wall display.

Scope:
- Repo-only Lovelace popup authoring unless the user explicitly asks for live Home Assistant updates.
- These instructions are for popup files in [`home-assistant/cards/wall-display/dock-bar`](/Users/thaynes/src/labspace/hass-sandbox/home-assistant/cards/wall-display/dock-bar).
- Preserve the seeded popup shell at the top of each file. Replace only the placeholder body below it.

References:
- Home Assistant scope and copy/paste communication rules: `.agents/rules/ha-change-scope-communication.md`
- Bubble style reference: [`bubble-card-custom-style-playbook.md`](/Users/thaynes/src/labspace/hass-sandbox/home-assistant/cards/wall-display/bubble-cards/bubble-card-custom-style-playbook.md)
- First worked example: [`primary-popup.yaml`](/Users/thaynes/src/labspace/hass-sandbox/home-assistant/cards/wall-display/dock-bar/primary-popup.yaml)
- Approved popup card library: [`dock-bar-approved-card-library.md`](/Users/thaynes/src/labspace/hass-sandbox/home-assistant/cards/wall-display/dock-bar/dock-bar-approved-card-library.md)

## Core Rules

1. Do not use Glass or frosted-glass styling in dock-bar popups.
2. Do not use `backdrop-filter` or repeated blur effects in popup internals.
3. Do not use `custom:badge-card` inside Bubble popups.
4. Keep popup cards action-focused. Put rich status chips on full pages, not popups.
5. Avoid empty space. If a small area leaves a large blank region, merge a nearby smaller area underneath it in the same stack.
6. Prefer grouped entities over individual members when the grouped entity is clear and useful.
7. If a grouped entity is unclear, err on the side of adding too much rather than hiding useful controls.
8. Do not include camera feeds in dock-bar popups.
9. Follow the response-scope format from `.agents/rules/ha-change-scope-communication.md` when reporting completed work.
10. Do not duplicate markdown summary sensor data with extra read-only room cards below unless that status is uniquely important to act on or monitor.

## MCP Discovery Workflow

Use this exact order when the user gives you one or more areas to include.

### 1. Confirm the areas

Use:
- `ha_config_list_areas`

Goal:
- Find the exact Home Assistant area names and IDs.
- Preserve the user’s requested area order unless they ask for a different arrangement.

### 2. Pull the devices for each area

Use:
- `ha_get_device(area_id="...")`

Goal:
- Find the main device-backed controls in each area.
- This is usually the fastest way to see lights, fans, covers, TVs, speakers, thermostats, locks, and major appliances.

What to look for:
- readable device names
- grouped or helper-backed entities
- media players that map to actual room hardware
- Hunter Douglas shade devices where the entity IDs are ugly but the device names are readable

### 3. Sweep the area entities

Use:
- `ha_search_entities(query="", area_filter="Area Name", group_by_domain=true)`

Goal:
- Catch area-tagged helpers, grouped entities, and strays that `ha_get_device` can miss.

Use this to find:
- grouped lights
- cover groups
- helper lights or switches
- area sensors
- battery or door/contact sensors
- Unifi Protect sensor entities such as motion, person, doorbell, contact, or battery state

### 4. Verify states and friendly names

Use:
- `ha_get_states([...])`
- `ha_get_state(entity_id="...")`

Goal:
- Confirm friendly names, states, and whether an entity is worth showing.
- Check whether grouped entities already exist.
- Confirm cover names when the raw entity IDs are unreadable.

### 5. Run targeted searches for suite-wide controls

Use:
- `ha_search_entities(query="...")`

Typical searches:
- `thermostat`
- `ecobee`
- `sonos`
- `shade`
- `tv`
- `lock`
- `air filter`
- `appliance`

Goal:
- Find top-level controls that should sit under the popup summary rather than inside one room block.

## Entity Selection Rules

### Include these first

- lights and smart switches shown as `light.*`
- fans
- grouped window coverings
- individual room window coverings
- Sonos or TV media players
- thermostats
- locks
- appliances or air filters if they matter in that area

### Include these second

- temperature
- humidity
- CO2
- VOCs
- door/contact status
- battery status
- useful camera-adjacent sensor data such as motion detected, person detected, doorbell pressed, or Protect battery/contact state

These are secondary status items. In popups, keep them condensed.

### Security camera rule

Do not include:
- `camera.*` entities
- live camera feeds
- camera thumbnails
- picture glance or camera cards
- Unifi Protect camera stream cards

Allowed:
- related `binary_sensor.*` or `sensor.*` entities when they provide compact, useful status

Good examples:
- motion detected
- person detected
- doorbell pressed
- gate/door contact state
- camera battery state

Where to put them:
- prefer the top markdown summary
- use a normal popup card only if the status is important and compact

Counting rule:
- popup card counts exclude camera feeds entirely
- do not reserve popup card slots for a future camera stream

### Prefer grouped entities

Lights:
- Prefer Zigbee2MQTT or helper light groups when they clearly represent the room.
- If `light.upstairs_primary_bed_lights` exists, do not also show all five recessed members unless the grouping is wrong.
- Still include other useful lights that are not part of that group, such as nightstands, fan lights, lamps, or nook lights.

Covers:
- Put room-spanning shade groups in the top summary section.
- Put individual shades in the room section below.
- Use readable names from device context when the raw entity names are bad.
- For room shade groups, prefer the scene-driven room shade controller over the stock Bubble `cover` card.
- For single-shade rooms, use the same scene-driven controller if Hunter Douglas scenes exist and are the preferred controls.
- Leave individual shade members on the standard Bubble `cover` card.

Media players:
- Prefer the Sonos entity that does not include `airplay` in the name.
- Use TVs when they are clearly room-level devices.
- The known exception is `media_player.ls50_wireless_ii_174476_airplay`, which is only for the rumpus room speaker.

### If uncertain

- Include the candidate entity.
- Keep the name readable.
- Let the user prune after the first pass.

## Popup Layout Recipe

Every popup should follow this order.

### 1. Seeded popup shell

Leave the existing `custom:bubble-card` popup wrapper untouched.

### 2. Summary card

Immediately below the popup shell, add one markdown summary card.

Purpose:
- show the overall popup title
- show condensed status text for key sensors across the included areas

Rules:
- one markdown card only
- no glass styling
- no transparent tricks
- use a plain card background
- aim for one or two lines of status text, not four stacked paragraphs
- use short human-readable labels like `Bedroom`, `Bathroom`, `Closet`, `Cloffice`
- this is the preferred place for ambient sensor rollups such as temperature, humidity, CO2, VOC, occupancy, and laundry-ready status

Recommended content pattern:
- line 1: two major areas
- line 2: the remaining smaller areas

### 3. Summary controls row

After the markdown summary, add a compact top grid for suite-level or shared controls.

Recommended grid:
- `type: grid`
- `columns: 3`
- `square: false`

Include in this order:
1. combined light groups
2. grouped shades
3. thermostat
4. Sonos/media players
5. TVs
6. locks or other standalone controls

Notes:
- Avoid huge one-card rows.
- Bubble media cards are allowed here, but keep the count low and use the compact approved pattern.
- If the popup has no room-spanning or top-level controls, omit this row entirely and move straight to the area blocks.

### 4. Area blocks

After the summary grid, use a parent 2-column grid.

Pattern:
- each area is one `vertical-stack`
- each stack starts with a native `heading` card
- each stack then contains a 2-column grid of bubble cards

Recommended parent:
- `type: grid`
- `columns: 2`
- `square: false`

Recommended inner room grid:
- `type: grid`
- `columns: 2`
- `square: false`

Rules:
- cards in area grids should usually be interactive controls
- avoid adding read-only Bubble status cards that simply repeat what is already shown in the markdown summary
- keep read-only cards only when the status is unique and operationally important, such as appliance state that affects what the user should do next

Single-area exception:
- if the popup only contains one area, do not add a duplicate room heading or subsection under the main summary heading
- in that case, place the room controls directly in one compact grid below the summary card
- only use room subsection headings when the popup includes multiple areas or clearly separate control groups

### 5. Minimize empty space

Do not leave a whole quadrant mostly empty.

If one area is tiny:
- move the next small area into the same `vertical-stack` below it
- add another `heading` card
- then add that area’s grid under the heading

This is the preferred fix for scroll reduction inside the popup.

## Card Recipes

### Bubble button

Use for:
- lights
- switches presented as lights
- fans
- simple status buttons the user may still tap

Defaults:
- `type: custom:bubble-card`
- `card_type: button`
- `button_type: slider` for dimmable lights
- `button_type: state` for simple status controls

### Bubble cover

Use for:
- individual shades
- secondary shades within a room
- any shade that does not need scene shortcuts

Defaults:
- `type: custom:bubble-card`
- `card_type: cover`
- `show_state: true`

Naming:
- never leave vague names like `Shade`
- rename to room-specific names such as `Primary Bathroom Shade`

### Scene-driven room shade controller

Use for:
- room shade groups
- rooms with one shade when the Hunter Douglas scenes are preferred over the stock cover arrows

Rules:
- use `card_type: button`
- use `button_type: name`
- use `card_layout: large-sub-buttons-grid`
- wire the sub-buttons to `scene.turn_on`
- if available, use `Tilt Open`, `Open`, `Privacy`, `Close`
- if no privacy scene exists, use `Tilt Open`, `Open`, `Close`
- do not use this pattern for individual member shades inside the room grid

### Bubble media player

Use for:
- Sonos
- TVs when useful

Rules:
- keep in the top summary section unless there is a strong reason otherwise
- do not overfill the popup with media players
- prefer readable room names
- use the compact supported pattern, not custom tall layouts
- do not add custom launch buttons, extra rows, or oversized sub-button layouts

Approved compact pattern:
- `type: custom:bubble-card`
- `card_type: media-player`
- `show_name: true`
- `show_state: true`
- `show_attribute: false`
- `scrolling_effect: false`
- `card_layout: normal`
- `rows: 1`

Reason:
- the more customized media-player layouts rendered poorly in the popup and created a fake "large card" class
- the compact documented Bubble pattern is the current standard going forward

### Heading cards

Use:
- native `heading` cards for room labels

Do not use:
- markdown headers for each room section

Reason:
- native headings behave better and do not need hidden card shells

## Styling Rules

This section is strict for dock-bar popups.

### Allowed

- Bubble cards using light, plain styling
- plain markdown card with modest padding and standard HA card background
- heading cards
- small radius, no heavy shadow

### Not allowed

- frosted glass styling
- multi-layer glow/shadow stacks
- blur on every card
- `backdrop-filter`
- `-webkit-backdrop-filter`
- heavy `card_mod` overrides on Bubble internals

### Markdown summary styling

Use:
- standard HA card background
- `border: none`
- `box-shadow: none`
- readable typography

Do not use:
- gradient glass shell
- blur
- fake chip layouts that rely on popup-host CSS quirks

### Badge cards

Rule:
- `custom:badge-card` is allowed on full dashboard pages
- `custom:badge-card` is not allowed inside these Bubble popups

Reason:
- it rendered well on dashboards and poorly inside popups during the Primary Suite iteration

## Naming Rules

- Use human-readable names.
- Shorten names where width matters.
- Keep room context in cover names and unusual controls.
- Prefer device-friendly room labels over raw entity labels.

Examples:
- `Primary Bathroom Shade`
- `Nightstands`
- `Closet`
- `Cloffice`
- `Bed Lights`

## Validation Checklist

Before handing the popup back:

1. Parse the YAML locally.
2. Verify the popup shell at the top is untouched.
3. Confirm no glass styling remains.
4. Confirm no `custom:badge-card` remains.
5. Confirm there is one summary markdown card only.
6. Confirm area order matches the user request unless intentionally changed.
7. Confirm grouped top-level controls are in the summary section.
8. Confirm individual room controls are in the room grids.
9. Confirm room names and cover names are readable.
10. Check for large blank regions and compress them if possible.
11. Confirm there are no `camera.*` entities or camera cards in the popup.
12. If Protect data is included, confirm it is sensor-only and kept compact.

Example validation command:

```sh
ruby -e 'require "yaml"; YAML.load_file("home-assistant/cards/wall-display/dock-bar/primary-popup.yaml"); puts "YAML OK"'
```

## First-Pass Delivery Standard

A first pass is successful when:
- the popup has all major controls for the requested areas
- the layout is predictable
- no glass styling is used
- there is minimal wasted space
- the user can iterate by correcting names, sizing, or inclusion choices rather than rebuilding the popup structure

## Current Gold-Standard Starting Point

Use [`primary-popup.yaml`](/Users/thaynes/src/labspace/hass-sandbox/home-assistant/cards/wall-display/dock-bar/primary-popup.yaml) as the current baseline for:
- summary markdown pattern
- compact top summary grid
- 2-column room layout
- merged small-area stacking to reduce empty space
- popup-safe styling choices
- compact Bubble media-player usage
