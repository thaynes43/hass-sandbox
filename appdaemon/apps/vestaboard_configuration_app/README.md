# Vestaboard Configuration App

Configuration bridge between the Lovelace card and the Vestaboard controller app.
Manages the persistent frame library (save, update, delete, list static frames)
and forwards push/automation control commands to the controller.

## Architecture

```
Lovelace Card
  → script.vestaboard_configuration_relay
  → event: vestaboard_configuration_command
  → VestaboardConfigurationApp
  → sensor.vestaboard_configuration_status  (status + library published here)
  → event: vestaboard_controller_command    (push/automation commands forwarded)
  → VestaboardControllerApp
```

The card never talks to the controller app directly — all commands pass through
this app so library state stays consistent and the sensor is always up to date.

## Configuration

### `apps-prod.yaml` entry

```yaml
vestaboard_configuration_app:
  module: vestaboard_configuration_app.vestaboard_configuration_app
  class: VestaboardConfigurationApp
  disable: true
  ha_url: !secret ha_url
  ha_token_env: HA_TOKEN
  frame_library_path: /media/vestaboard/frame-library.json
  creators:
    - Mom
    - Tom
    - Jackson
    - Penelope
    - Anonymous
```

### `apps-dev.yaml` entry

```yaml
vestaboard_configuration_app_dev:
  module: vestaboard_configuration_app.vestaboard_configuration_app
  class: VestaboardConfigurationApp
  ha_url: !secret ha_url
  ha_token_env: HA_TOKEN
  frame_library_path: /tmp/vestaboard-dev/frame-library.json
  creators:
    - Mom
    - Tom
    - Jackson
    - Penelope
    - Anonymous
```

### Config keys

| Key | Required | Default | Description |
|-----|----------|---------|-------------|
| `ha_url` | Yes | — | Home Assistant base URL for provisioning |
| `ha_token_env` | Yes | — | Env var name holding the HA long-lived token |
| `frame_library_path` | No | `/media/vestaboard/frame-library.json` | Filesystem path for frame storage |
| `creators` | No | `["Mom", "Tom", "Jackson", "Penelope", "Anonymous"]` | Initial creator dropdown options |

## Self-Provisioned Entities

On startup the app provisions these entities via `HAProvisioner` (idempotent):

| Entity | Type | Purpose |
|--------|------|---------|
| `script.vestaboard_configuration_relay` | Script | Relay card → AppDaemon commands |
| `input_select.vestaboard_creator` | Helper | Creator dropdown for the card |

## Sensor

`sensor.vestaboard_configuration_status` — written by this app on every
library change and on every controller status update.

Attributes:

| Attribute | Source | Description |
|-----------|--------|-------------|
| `library` | frame library | JSON array of all saved frames |
| `creators` | runtime list | Current creator names |
| `current_frame` | controller mirror | Characters currently on the board |
| `current_source` | controller mirror | Source that owns the board |
| `current_ttl_expires` | controller mirror | ISO timestamp when TTL expires |
| `queue` | controller mirror | LIFO frame queue |
| `fallback_source` | controller mirror | What takes over after queue drains |
| `automations` | controller mirror | Active automation configs |
| `status` | static | Always `"ok"` |

## Supported Card Commands

Commands are received via the relay script and dispatched by the app.

### Library management

| Command | Payload keys | Description |
|---------|-------------|-------------|
| `save_frame` | `frame`, `name`, `creator`, `rating` | Add a new frame to the library |
| `update_frame` | `frame_id`, `name?`, `rating?`, `creator?`, `characters?` | Update mutable fields |
| `delete_frame` | `frame_id` | Remove a frame |
| `save_art_to_library` | `frame`, `name`, `creator?` | Save AI-generated art |

### Push to controller

| Command | Payload keys | Description |
|---------|-------------|-------------|
| `push_frame` | `frame`, `ttl_minutes?` | Push a raw frame (overrides TTL) |
| `push_library_frame` | `frame_id`, `ttl_minutes?`, `respect_ttl?` | Push a saved library frame |
| `push_frame_respect_ttl` | `frame`, `ttl_minutes?` | Push a frame, respecting active TTL |

### Automation control (forwarded to controller)

| Command | Payload keys | Description |
|---------|-------------|-------------|
| `toggle_automation` | `automation_id`, `enabled` | Enable/disable an automation |
| `set_automation_config` | `automation_id`, `ttl_minutes?`, `expiration_minutes?` | Update automation timing |
| `generate_art` | `subject` | Ask the controller to generate AI art |

### Misc

| Command | Payload keys | Description |
|---------|-------------|-------------|
| `refresh_status` | — | Force re-publish the status sensor |
| `add_creator` | `name` | Add a creator to the dropdown |

## Frame Library Storage

Frames are stored as JSON at `frame_library_path`. The file is written atomically
(temp file + rename) so partial writes cannot corrupt the library.

Default prod path: `/media/vestaboard/frame-library.json`

The `/media` directory is a shared volume between the HA container and the
AppDaemon pod. The subdirectory `/media/vestaboard/` must exist; it is created
automatically by the library on first write.

## Manual Prerequisites

The app provisions most entities automatically. The following require a one-time
manual step:

1. **Lovelace resource** — register `vestaboard_configuration_card.js` in HA
   under Settings → Dashboards → Resources (or use the MCP
   `ha_config_set_dashboard_resource` tool).

2. **Media directory** (optional) — the library creates `/media/vestaboard/`
   automatically on first save, but if you need it to exist before first run
   (e.g. for a `local_file` camera) create it manually:
   ```bash
   mkdir -p /media/vestaboard
   ```

## Initial Library

On first startup (empty library) the app seeds one frame:

| Name | Creator | Rating |
|------|---------|--------|
| Hello World | Tom | 3 |
