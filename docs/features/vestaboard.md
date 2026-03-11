# Vestaboard

Control a physical Vestaboard flip board from Home Assistant. A custom Lovelace card provides a three-tab interface for designing frames, managing a personal library, and configuring board automations — all without touching YAML after initial setup.

<!-- TODO: Add screenshot of the Vestaboard configuration card showing the frame editor tab -->

## Overview

The Vestaboard integration brings a physical split-flap display board into the smart home. Frames (6-row x 22-column character grids) are pushed to the board through a queue managed by AppDaemon. Two cooperating apps handle the system:

1. **vestaboard_controller_app** — owns the board. Manages the frame queue with TTL and expiration, runs board automations, and communicates with the Vestaboard hardware via its local API.
2. **vestaboard_configuration_app** — owns the card. Provides a Lovelace configuration card where family members can design frames, save to a personal library, push frames to the board, and toggle automations on or off.

## Architecture

```
custom:vestaboard-configuration-card (Lovelace)
  └─ calls script.vestaboard_configuration_relay
       └─ fires vestaboard_configuration_command event
            └─ vestaboard_configuration_app (AppDaemon)
                 │  forwards push/library commands
                 ▼
            vestaboard_controller_app (AppDaemon)
                 │  manages frame queue + automations
                 ▼
            Vestaboard hardware (local HTTP API)
```

All card-to-AppDaemon communication uses the relay script pattern — the card calls a HA script that fires an event, which AppDaemon listens for. This works for non-admin users.

## Frame queue

The controller maintains a LIFO frame queue. Each queued frame carries:

- **TTL** — how many seconds the frame stays at the front of the queue before expiring
- **override_ttl** — how many seconds to show a newly pushed frame before returning to the previous frame (used when a user pushes a one-off frame without clearing automation content)
- **Expiration** — absolute time after which the frame is discarded even if never shown

When the queue empties, the controller falls back to the lowest-priority automation that is enabled and has content to display.

## Board automations

Automations run in the background and inject frames into the queue at their assigned priority. They are individually enabled/disabled from the configuration card.

| Automation | Description |
|------------|-------------|
| **calendar_clock** | Displays the current time and date, updating on a configurable tick interval |
| **random_message** | Selects and displays a random message from a curated list or AI-generated content |
| **random_art** | Selects a random frame from the library and displays it |
| **ai_art_generator** | Uses an AI text provider to generate a novel character-art frame |
| **calendar_summary** | Monitors HA calendar entities and shows upcoming event reminders |

Each automation that uses AI is independently configured with its own `ai_provider_conf` bundle so different models can be used per automation.

## Configuration card

The `custom:vestaboard-configuration-card` provides three tabs:

### Editor tab

An interactive 6x22 character grid. Click any cell to cycle through characters and colors. Supported characters are defined by the Vestaboard character encoding (letters, numbers, symbols, and a set of color blocks). Buttons at the bottom push the current frame to the board or save it to the personal library.

### Library tab

Displays all saved frames from the shared frame library (`frame_library_path`). Each entry shows the creator name, a preview of the frame, and push/delete controls. Library entries persist across restarts via the JSON file on disk.

### Automations tab

Lists all configured automations with toggle switches. Changes take effect immediately in the controller app via the relay event system.

## Configuration

Add both apps to `apps.yaml`. The controller and configuration apps must be configured together.

```yaml
vestaboard_controller:
  module: vestaboard_controller_app.vestaboard_controller_app
  class: VestaboardControllerApp
  ha_url_env: HA_URL
  ha_token_env: TOKEN
  vestaboard_ip_env: VESTABOARD_IP
  vestaboard_api_key_env: VESTABOARD_API_KEY
  tick_interval_s: 15
  automations:
    calendar_clock:
      enabled: true
    random_message:
      enabled: false
      ai_provider_conf:
        simple_text: openai-default
    random_art:
      enabled: false
    ai_art_generator:
      enabled: false
      ai_provider_conf:
        simple_text: openai-default
    calendar_summary:
      enabled: false
      calendar_entities:
        - calendar.family
      reminder_minutes: 15

vestaboard_configuration:
  module: vestaboard_configuration_app.vestaboard_configuration_app
  class: VestaboardConfigurationApp
  ha_url_env: HA_URL
  ha_token_env: TOKEN
  frame_library_path: /media/vestaboard/frame-library.json
  creators:
    - Mom
    - Dad
    - Jackson
    - Penelope
    - Anonymous
```

Required environment variables:

| Variable | Description |
|----------|-------------|
| `HA_URL` | Home Assistant base URL |
| `TOKEN` | Long-lived HA access token |
| `VESTABOARD_IP` | Local IP address of the Vestaboard |
| `VESTABOARD_API_KEY` | Vestaboard local API key |

## Manual setup

After deploying, complete these manual steps:

1. **Create the frame library directory** on the media volume (e.g. `/media/vestaboard/`) — the app will create `frame-library.json` on first save.
2. **Add the Lovelace resource** — register `vestaboard-configuration-card.js` in the dashboard resource list and bump `?v=N` after updates.
3. **Add the card** to a dashboard view:

```yaml
type: custom:vestaboard-configuration-card
status_entity: sensor.vestaboard_configuration_status
relay_script: vestaboard_configuration_relay
```

See `appdaemon/apps/vestaboard_controller_app/README.md` and `appdaemon/apps/vestaboard_configuration_app/README.md` for complete configuration references.
