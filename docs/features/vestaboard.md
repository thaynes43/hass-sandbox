# Vestaboard

Control a physical Vestaboard flip board from Home Assistant. A custom Lovelace card provides a three-tab interface for designing frames, managing a personal library, and configuring board automations — all without touching YAML after initial setup.

<!-- TODO: Add screenshot of the Vestaboard configuration card showing the frame editor tab -->

## Overview

The Vestaboard integration brings a physical split-flap display board into the smart home. Frames (6-row x 22-column character grids) are pushed to the board through a queue managed by AppDaemon. Two cooperating apps handle the system:

1. **vestaboard_controller_app** — owns the board. Manages the frame queue with TTL and expiration, coordinates board automations via HA events, and communicates with the Vestaboard hardware via its local API.
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
                 │  manages frame queue + board writes
                 ▼
            Vestaboard hardware (local HTTP API)

Automation apps (calendar_clock, ai_art_generator, ...)
  └─ fire vestaboard_controller_command: register_automation       (on startup)
  └─ fire vestaboard_controller_command: deregister_automation     (on terminate)
  └─ fire vestaboard_controller_command: push_automation_frame     (when content is ready)
  └─ fire vestaboard_controller_command: push_ai_art_preview_result  (ai_art_generator preview mode)
  └─ fire vestaboard_controller_command: update_next_fire_time     (notify controller of next scheduled fire)
       └─ vestaboard_controller_app handles push → board

Card Preview button
  └─ preview_automation command (with automation_id)
       └─ vestaboard_controller_app fires vb_auto_generate → automation generates frame → board
```

All card-to-AppDaemon communication uses the relay script pattern — the card calls a HA script that fires an event, which AppDaemon listens for. This works for non-admin users.

All automation-to-controller communication is also event-based. Automation apps fire `vestaboard_controller_command` events with commands such as `register_automation`, `push_automation_frame`, `deregister_automation`, `push_ai_art_preview_result`, and `update_next_fire_time`. The controller responds with per-automation events (`vb_auto_config`, `vb_auto_enabled`, `vb_auto_generate`) that include an `automation_id` field in the event data — each automation listens for these and filters by its own ID. This means automation apps can run in a different AppDaemon instance than the controller — no AppDaemon `dependencies:` entries or `get_app()` calls are needed.

## Frame queue

The controller maintains a FIFO frame queue. Each queued frame carries:

- **TTL** — how many seconds the frame holds the board before the next frame is promoted
- **override_ttl** — immediately pre-empts whatever is on the board regardless of active TTL
- **max_age_s** — absolute expiry since creation; frame is discarded even if never shown
- **should_expire** — if true, the frame auto-leaves the board when TTL elapses; if false, the frame holds the board until displaced

When a frame is displaced from the board (by a force-push or a new frame after TTL expiry), it moves to the fallback queue with its remaining TTL preserved. The fallback queue is consulted before the pending queue — displaced frames resume before new content shows. Fallback frames with less than 30 seconds of remaining TTL are pruned.

## Board automations

Automation apps are independent AppDaemon apps that register with the controller on startup by firing a `vestaboard_controller_command` event. The controller fires a `vestaboard_controller_ready` event on startup so automations automatically re-register after a controller restart.

| Automation | Description |
|------------|-------------|
| **calendar_clock** | Displays the current month calendar and time, updating every 60 seconds |
| **messages_from_library** | Selects a random message from the frame library on a configurable random interval |
| **art_from_library** | Selects random pixel art from a bundled art library on a configurable random interval |
| **ai_art_generator** | Uses an LLM to generate novel Vestaboard pixel art; supports on-demand and preview modes |
| **ai_message_generator** | Uses an LLM to generate witty messages; falls back to a curated list if AI is unavailable |
| **calendar_summary** | Monitors HA calendar entities and shows upcoming event reminders (multi-instance) |
| **weather_schedule** | Displays weather conditions from a HA weather entity at configured daily times |

Each automation that uses AI is independently configured with its own `ai_provider_conf` bundle so different models can be used per automation.

## Configuration card

The `custom:vestaboard-configuration-card` provides three tabs:

### Editor tab

An interactive 6x22 character grid. Click any cell to cycle through characters and colors. Supported characters are defined by the Vestaboard character encoding (letters, numbers, symbols, and a set of color blocks). Buttons at the bottom push the current frame to the board or save it to the personal library.

### Library tab

Displays all saved frames from the shared frame library (`frame_library_path`). Each entry shows the creator name, a preview of the frame, and push/edit/delete controls. Library entries persist across restarts via the JSON file on disk.

### Vestaboard+ tab

Lists all registered automations with toggle switches, a **Preview** button for instant on-demand generation, and per-automation config controls. Click Preview on any automation to immediately generate and display a frame on the board — useful for testing without waiting for the automation's natural schedule. Changes take effect immediately in the controller app via the relay event system.

## Configuration

Add the controller and configuration apps to `apps.yaml`. Each automation is a separate app entry — no `dependencies:` or `controller_app:` keys are needed.

```yaml
vestaboard_controller:
  module: vestaboard_apps.vestaboard_controller.vestaboard_controller_app
  class: VestaboardControllerApp
  disable: true
  ha_url_env: HA_URL
  ha_token_env: TOKEN
  vestaboard_ip_env: VESTABOARD_IP
  vestaboard_api_key_env: VESTABOARD_API_KEY
  tick_interval_s: 15
  frame_library_path: /media/vestaboard/frame-library.json
  automation_config_path: /media/vestaboard/automation-config.yaml
  sleep_window:
    enabled: true
    start: "01:00:00"
    end: "06:45:00"

vestaboard_configuration:
  module: vestaboard_apps.vestaboard_configuration.vestaboard_configuration_app
  class: VestaboardConfigurationApp
  disable: true
  ha_url_env: HA_URL
  ha_token_env: TOKEN
  frame_library_path: /media/vestaboard/frame-library.json
  creators:
    - Mom
    - Dad
    - Jackson
    - Penelope
    - Anonymous

calendar_clock:
  module: vestaboard_apps.automations.calendar_clock.calendar_clock_app
  class: CalendarClockApp
  disable: true

art_from_library:
  module: vestaboard_apps.automations.art_from_library.art_from_library_app
  class: ArtFromLibraryApp
  disable: true

messages_from_library:
  module: vestaboard_apps.automations.messages_from_library.messages_from_library_app
  class: MessagesFromLibraryApp
  disable: true
  frame_library_path: /media/vestaboard/frame-library.json

art_generated_by_ai:
  module: vestaboard_apps.automations.ai_art_generator.ai_art_generator_app
  class: AiArtGeneratorApp
  disable: true
  ai_provider_conf:
    simple_text: openai-pixel-art

message_generated_by_ai:
  module: vestaboard_apps.automations.ai_message_generator.ai_message_generator_app
  class: AiMessageGeneratorApp
  disable: true
  ai_provider_conf:
    simple_text: openai-default

calendar_summary_family:
  module: vestaboard_apps.automations.calendar_summary.calendar_summary_app
  class: CalendarSummaryApp
  disable: true
  calendar_entity: calendar.family

weather_schedule:
  module: vestaboard_apps.automations.weather_schedule.weather_schedule_app
  class: WeatherScheduleApp
  disable: true
  weather_entity: weather.first_floor_ecobee
  time_list:
    - "07:30:00"
    - "15:00:00"
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

1. **Create the media directory** on the media volume (e.g. `/media/vestaboard/`) — the app will create `frame-library.json` and `automation-config.yaml` on first use.
2. **Add the Lovelace resource** — register `vestaboard-configuration-card.js` in the dashboard resource list and bump `?v=N` after updates.
3. **Add the card** to a dashboard view:

```yaml
type: custom:vestaboard-configuration-card
status_entity: sensor.vestaboard_configuration_status
relay_script: vestaboard_configuration_relay
```

See `appdaemon/apps/vestaboard_apps/vestaboard_controller/README.md` and `appdaemon/apps/vestaboard_apps/vestaboard_configuration/README.md` for complete configuration references.
