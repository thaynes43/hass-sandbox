# Dock-Bar Approved Card Library

This file is the approved card library for `dock-bar` popups on the wall display.

Use these patterns repeatedly across popup builds unless the user explicitly asks for an exception.

## Rules

- Prefer `custom:bubble-card` for actionable controls.
- Keep cards compact and popup-safe.
- Do not use Glass styling.
- Do not use `custom:badge-card` inside popups.
- Do not use camera cards or camera feeds.
- Use one summary markdown card per popup.

## Summary Markdown Card

Use for:
- popup title
- compact room status
- sensor rollup
- Protect sensor rollup without camera feeds

Pattern:

```yaml
- type: markdown
  text_only: true
  card_mod:
    style: |
      ha-card {
        border-radius: 28px;
        padding: 10px 18px 12px;
        background: var(--ha-card-background, var(--card-background-color));
        border: none;
        box-shadow: none;
      }
      h2 {
        margin: 0 0 8px;
        color: var(--primary-text-color);
        font-size: 1.15rem;
        font-weight: 650;
        letter-spacing: 0.01em;
      }
      p {
        margin: 0;
        color: var(--secondary-text-color);
        line-height: 1.35;
        font-size: 0.95rem;
      }
      p + p {
        margin-top: 6px;
      }
      strong {
        color: var(--primary-text-color);
        font-weight: 600;
      }
  content: |
    ## Popup Title

    <p><strong>Area One</strong> status text &nbsp;•&nbsp; <strong>Area Two</strong> status text</p>
    <p><strong>Area Three</strong> status text &nbsp;•&nbsp; <strong>Area Four</strong> status text</p>
```

## Dimmable Light

Use for:
- grouped room lights
- lamps
- fan lights
- dimmable smart switches exposed as lights

Pattern:

```yaml
- type: custom:bubble-card
  card_type: button
  button_type: slider
  entity: light.example
  name: Example Light
  icon: mdi:lightbulb
  show_state: true
  show_attribute: true
  attribute: brightness
  allow_light_slider_to_0: true
  tap_action:
    action: more-info
  button_action:
    tap_action:
      action: toggle
  slider_fill_orientation: left
  slider_value_position: right
```

## Non-Dimmable Light Or Switch-Light

Use for:
- on/off lighting
- utility lights
- night lights without brightness

Pattern:

```yaml
- type: custom:bubble-card
  card_type: button
  button_type: state
  entity: light.example
  name: Example Light
  icon: mdi:lightbulb
  show_state: true
  tap_action:
    action: more-info
  button_action:
    tap_action:
      action: toggle
```

## Fan

Use for:
- ceiling fans
- bath fans when exposed as `fan.*`

Pattern:

```yaml
- type: custom:bubble-card
  card_type: button
  button_type: slider
  entity: fan.example
  name: Ceiling Fan
  icon: mdi:fan
  show_state: true
  show_attribute: true
  attribute: percentage
  tap_action:
    action: more-info
  button_action:
    tap_action:
      action: toggle
```

## Standard Cover

Use for:
- individual shades
- secondary shades inside a room
- any shade that does not need Hunter Douglas scene shortcuts

Pattern:

```yaml
- type: custom:bubble-card
  card_type: cover
  entity: cover.example
  name: Room Shade
  show_state: true
```

Naming rule:
- always use readable, room-specific names such as `Primary Bathroom Shade`

## Scene-Driven Room Shade Controller

Use for:
- room shade groups
- rooms with a single shade where the Hunter Douglas scenes are the preferred controls

Pattern:

```yaml
- type: custom:bubble-card
  card_type: button
  button_type: name
  entity: cover.example_room_shades
  name: Room Shades
  show_state: true
  rows: 1.719
  card_layout: large-sub-buttons-grid
  button_action:
    tap_action:
      action: more-info
  sub_button:
    main: []
    bottom:
      - entity: scene.gateway_room_tilt_open
        name: Tilt Open
        icon: mdi:blinds-horizontal
        show_name: true
        show_icon: true
        show_state: false
        tap_action:
          action: call-service
          service: scene.turn_on
          target:
            entity_id: scene.gateway_room_tilt_open
      - entity: scene.gateway_room_open
        name: Open
        icon: mdi:arrow-up-bold
        show_name: true
        show_icon: true
        show_state: false
        tap_action:
          action: call-service
          service: scene.turn_on
          target:
            entity_id: scene.gateway_room_open
      - entity: scene.gateway_room_privacy
        name: Privacy
        icon: mdi:blinds-horizontal-closed
        show_name: true
        show_icon: true
        show_state: false
        tap_action:
          action: call-service
          service: scene.turn_on
          target:
            entity_id: scene.gateway_room_privacy
      - entity: scene.gateway_room_close
        name: Close
        icon: mdi:arrow-down-bold
        show_name: true
        show_icon: true
        show_state: false
        tap_action:
          action: call-service
          service: scene.turn_on
          target:
            entity_id: scene.gateway_room_close
    bottom_layout: rows
```

Rules:
- use this for the room/group shade card only
- leave individual shade members on the standard Bubble `cover` card
- if there is no privacy scene, omit it and use three scene buttons
- if there is a privacy scene, keep both `Privacy` and `Close` unless the user asks to trim for space
- this pattern is intentionally larger than a standard cover card
- scene entities should be area-tagged first so they are easy to find from the runbook

## Compact Media Player

Use for:
- Sonos
- TV media players when they are worth exposing directly

Pattern:

```yaml
- type: custom:bubble-card
  card_type: media-player
  entity: media_player.example
  name: Room Sonos
  show_name: true
  show_state: true
  show_attribute: false
  scrolling_effect: false
  card_layout: normal
  rows: 1
  button_action:
    tap_action:
      action: more-info
```

Rules:
- prefer the non-AirPlay Sonos entity
- keep media players in the summary section when possible
- do not use custom launch buttons
- do not create tall media cards in popups

## TV Or Appliance Shortcut

Use for:
- TVs that do not need full transport controls
- appliances where `more-info` is more useful than inline controls
- locks or specialty controls that should stay compact

Pattern:

```yaml
- type: custom:bubble-card
  card_type: button
  button_type: name
  entity: media_player.example_tv
  name: Room TV
  icon: mdi:television
  show_state: true
  button_action:
    tap_action:
      action: more-info
```

## Thermostat Summary Card

Use for:
- top-level climate control in the summary row

Pattern:

```yaml
- type: custom:bubble-card
  card_type: button
  button_type: name
  card_layout: large-2-rows
  entity: climate.example
  name: Upstairs Ecobee
  icon: mdi:thermostat
  show_state: true
  sub_button:
    main:
      - entity: sensor.example_temperature
        name: ""
        show_state: true
        show_name: false
        show_icon: true
        show_background: false
        icon: mdi:home-thermometer
        tap_action:
          action: more-info
      - entity: sensor.example_target_temperature
        name: ""
        show_state: true
        show_name: false
        show_icon: true
        show_background: false
        icon: mdi:target
        tap_action:
          action: more-info
      - entity: sensor.example_humidity
        name: ""
        show_state: true
        show_name: false
        show_icon: true
        show_background: false
        icon: mdi:water-percent
        tap_action:
          action: more-info
    bottom: []
  button_action:
    tap_action:
      action: more-info
```

## Compact Status Card

Use for:
- door/contact state
- battery state
- humidity
- VOC
- CO2
- temperatures that should stay in the room grid instead of markdown

Pattern:

```yaml
- type: custom:bubble-card
  card_type: button
  button_type: state
  entity: sensor.example
  name: Temperature
  icon: mdi:thermometer
  show_state: true
  tap_action:
    action: more-info
```

## Room Heading

Use for:
- section labels inside each area stack

Pattern:

```yaml
- type: heading
  heading: Room Name
  heading_style: subtitle
```

## Layout Standards

- one summary markdown card at the top
- one summary grid after the markdown card
- one parent `grid` with `columns: 2` for room stacks
- one inner `grid` with `columns: 2` inside each room stack
- merge small areas in the same stack when it reduces empty space

## Do Not Use

- `custom:badge-card` inside popups
- camera cards
- picture cards
- custom launch buttons on media players
- tall media-player layouts
- glass styling
