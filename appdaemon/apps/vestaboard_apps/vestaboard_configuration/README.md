# Vestaboard Configuration

Configuration bridge between the Lovelace card and the Vestaboard controller app. Manages the frame library (CRUD for saved messages and art), mirrors controller status into its own sensor, and forwards push and automation commands to the controller.

## How it works

1. On startup provisions `script.vestaboard_configuration_relay` and `input_select.vestaboard_creator` in HA.
2. Loads the `FrameLibrary` from `frame_library_path`. Seeds a "Hello World" frame if the library is empty.
3. Listens for `vestaboard_configuration_command` events from the relay script.
4. Listens for state changes on `sensor.vestaboard_controller_status` and mirrors those attributes into `sensor.vestaboard_configuration_status` so the card only needs to watch a single sensor.
5. Routes commands: library mutations (save/update/delete/move frames) are handled locally; push and automation commands are forwarded to the controller via `fire_event(vestaboard_controller_command)`.

## Architecture

```
Lovelace card
  → hass.callService("script", "vestaboard_configuration_relay", {command, payload})
  → vestaboard_configuration_command event
  → VestaboardConfigurationApp._on_command()
      ├─ Library commands (save_frame, update_frame, delete_frame, move_frame, quick_save_art, save_art_to_library)
      │   → FrameLibrary (local JSON file)
      │   → _publish_status() → sensor.vestaboard_configuration_status
      └─ Forward commands (push_frame, push_library_frame, toggle_automation, set_automation_config,
                           preview_automation, generate_art, generate_ai_message, clear_art_preview)
          → fire_event(vestaboard_controller_command)
          → VestaboardControllerApp

sensor.vestaboard_controller_status (state changes)
  → VestaboardConfigurationApp._on_controller_status()
  → _publish_status() → sensor.vestaboard_configuration_status
```

## Associated card

`vestaboard-configuration-card.js` — Lovelace card that provides the full management UI: frame editor, library browser, automation controls, and AI art generation.

## Dependencies

- `providers.ha_provisioner` — HA entity provisioning
- `providers.secrets` — env var secret resolution
- `vestaboard_apps.vestaboard_configuration.frame_library` — frame library CRUD
- `providers.vestaboard.character_encoding` — used for seeding the Hello World frame

## Self-provisioned entities

| Entity | Type | Purpose |
|--------|------|---------|
| `script.vestaboard_configuration_relay` | Script | Relay for card commands; fires `vestaboard_configuration_command` event |
| `input_select.vestaboard_creator` | Helper | Dropdown of available creator names shown in the card |
| `sensor.vestaboard_configuration_status` | Sensor (via `set_state`) | Merged status: library contents, queue state, automation list, AI art preview |

## Supported commands (via relay script)

| Command | Payload fields | Description |
|---------|---------------|-------------|
| `save_frame` | `frame`, `name`, `creator`, `rating`, `category`, `template`, `refresh_interval_minutes` | Save a new frame to the library (optionally with a template for live HA data) |
| `update_frame` | `frame_id`, any mutable fields | Update metadata or characters of an existing frame |
| `delete_frame` | `frame_id` | Remove a frame from the library |
| `move_frame` | `frame_id`, `category` | Move a frame between categories (`message` / `art`) |
| `quick_save_art` | `frame`, `name`, `creator` | Save AI-generated art with backend-generated defaults |
| `save_art_to_library` | `frame`, `name`, `creator` | Save AI art as a named library frame |
| `push_frame` | `frame`, `ttl_minutes`, `should_expire`, `template`, `refresh_interval_minutes` | Push a frame directly to the board (forwarded to controller, template resolved by controller) |
| `push_library_frame` | `frame_id`, `ttl_minutes`, `respect_ttl` | Look up frame by ID and push it (forwarded to controller) |
| `toggle_automation` | `automation_id`, `enabled` | Enable or disable a board automation (forwarded to controller) |
| `set_automation_config` | `automation_id`, `config` | Update an automation's UI config (forwarded to controller) |
| `generate_art` | `subject` | Generate AI art preview without pushing to board (forwarded to controller as `generate_ai_art_preview`) |
| `generate_ai_message` | — | Generate an AI message and push to board (forwarded to controller) |
| `clear_art_preview` | — | Clear the AI art preview (forwarded to controller) |
| `preview_automation` | `automation_id` | Instantly generate and push a frame from any automation (forwarded to controller) |
| `add_creator` | `name` | Add a new name to the creators list and update `input_select.vestaboard_creator` |
| `refresh_status` | — | Re-publish the configuration status sensor |

## Config reference

| Key | Required | Default | Description |
|-----|----------|---------|-------------|
| `ha_url_env` | Yes | — | Env var name holding the HA base URL |
| `ha_token_env` | Yes | — | Env var name holding the HA long-lived access token |
| `frame_library_path` | No | `/media/vestaboard/frame-library.json` | Filesystem path for the frame library JSON file |
| `creators` | No | `["Mom", "Tom", "Jackson", "Penelope", "Anonymous"]` | Initial list of creator names for the `input_select` |

### YAML example

```yaml
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
```

## Manual setup required

- Ensure `frame_library_path` directory exists and is writable by the AppDaemon container (same directory as used by the controller).
- Add the Lovelace resource for `vestaboard-configuration-card.js` and bump `?v=N` after updates.
- The card must be placed on a dashboard and pointed at `sensor.vestaboard_configuration_status`.

## Upstream/downstream dependencies

- **Upstream**: `vestaboard_controller` — this app reads `sensor.vestaboard_controller_status` and forwards push/automation commands to the controller via `fire_event("vestaboard_controller_command")`. No AppDaemon `dependencies:` entry is needed; communication is purely event-based.
- **Downstream**: None — this app is the leaf node for the user-facing configuration UI.
