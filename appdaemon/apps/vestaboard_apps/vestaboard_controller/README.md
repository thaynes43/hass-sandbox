# Vestaboard Controller

Drives the physical Vestaboard flip-tile display. Manages a LIFO priority queue of frames, dispatches them to the board on a periodic tick, and exposes an event-based automation registration API so each automation app independently publishes frames.

## How it works

1. On startup, provisions `script.vestaboard_controller_relay` in HA via `HAProvisioner`.
2. Reads the current frame from the physical board so the queue has a starting state.
3. Loads the persistent `AutomationConfigStore` from `automation_config_path` to restore previously saved UI settings.
4. Registers a periodic tick (default 15 s) that advances the `FrameQueue` — promoting pending frames when TTLs expire and publishing the updated status sensor.
5. Fires `vestaboard_controller_ready` event so automation apps can (re-)register after a controller restart.
6. Automation apps register by firing a `vestaboard_controller_command` event with `command="register_automation"`. The controller creates a `RemoteAutomationProxy` and fires persisted config back.
7. When an automation generates a frame it fires a `vestaboard_controller_command` event with `command="push_automation_frame"`. The controller pushes it into the LIFO queue and may immediately display it.
8. Commands from the Lovelace card arrive via `script.vestaboard_controller_relay` → `vestaboard_controller_command` event → `_on_command()`.

### Frame queue semantics

| Concept | Meaning |
|---------|---------|
| **LIFO** | Most recently pushed pending frame is shown first |
| **TTL (`ttl_s`)** | Seconds to hold the board before yielding to pending. `None` = no protection; any new frame can replace it |
| **Max age (`max_age_s`)** | Hard expiry since creation time. Frame is dropped from queue without being shown if this passes |
| **Override TTL** | Frame immediately pre-empts whatever is on the board regardless of active TTL |
| **Should expire** | If `True`, frame is dropped entirely after TTL (not moved to fallback stack) |
| **Fallback stack** | Previously displayed frames. Shown when pending queue is empty |
| **Same-source dedup** | A newer push from the same source replaces the older pending frame |

### Sleep window

During the configured sleep window, board writes are suppressed. On wake the currently queued frame is reconciled back to the board.

## Architecture

### Card → Controller

```
Lovelace card
  → hass.callService("script", "vestaboard_controller_relay", {command, payload})
  → vestaboard_controller_command event
  → VestaboardControllerApp._on_command()
```

### Automation → Controller (event-based)

```
Automation app (e.g. CalendarClockApp)
  → fire_event("vestaboard_controller_command", command="register_automation", payload=...)
  → VestaboardControllerApp._handle_register_automation_event()
  → creates RemoteAutomationProxy
  → fires vb_auto_config (with automation_id in data) back to automation

Automation app generates a frame
  → fire_event("vestaboard_controller_command", command="push_automation_frame", payload=...)
  → VestaboardControllerApp._handle_push_automation_frame_event()
  → FrameQueue.push()
  → VestaboardClient.write_frame()  [if frame is immediately displayed]
```

### Controller → Automation (event-based)

```
VestaboardControllerApp
  → fires vb_auto_config (with automation_id in data)    (config updates)
  → fires vb_auto_enabled (with automation_id in data)   (enable/disable)
  → fires vb_auto_generate (with automation_id in data)  (on-demand generate requests)
  → fires vestaboard_controller_ready                     (startup/restart announcement)
```

### Periodic tick

```
run_every(tick_interval_s)
  → FrameQueue.tick()
  → VestaboardClient.write_frame()  [if a frame is promoted]
  → VestaboardControllerApp._publish_status()
  → sensor.vestaboard_controller_status
```

## RemoteAutomationProxy

When an automation registers, the controller creates a `RemoteAutomationProxy` object to store its metadata. This proxy holds the same interface fields the controller uses when communicating back (config schema, preview frame, display name, etc.) without requiring a direct Python object reference to the automation app. This design allows automation apps and the controller to run in **different AppDaemon instances** — useful for cross-instance dev testing and isolated deployments.

## Dependencies

- `providers.vestaboard.vestaboard_client` — Vestaboard local API client
- `providers.vestaboard.character_encoding` — character code utilities
- `providers.ha_provisioner` — HA entity provisioning
- `providers.secrets` — env var secret resolution
- `vestaboard_apps._shared.frame_queue` — LIFO frame queue logic
- `vestaboard_apps._shared.config_store` — persistent automation config store

## Self-provisioned entities

| Entity | Type | Purpose |
|--------|------|---------|
| `script.vestaboard_controller_relay` | Script | Relay for card/automation commands; fires `vestaboard_controller_command` event |
| `sensor.vestaboard_controller_status` | Sensor (via `set_state`) | Publishes queue state, automation list, displayed frame, and AI art preview |

## Supported commands (via relay script or direct event)

| Command | Payload fields | Description |
|---------|---------------|-------------|
| `push_frame` | `characters`, `ttl_s`/`ttl_minutes`, `max_age_s`, `override_ttl`, `should_expire`, `template`, `refresh_interval_minutes` | Push a pre-built frame (optionally with a template for live HA data) |
| `register_automation` | `automation_id`, `automation_type`, `display_name`, `display_description`, `default_ttl_s`, `default_max_age_s`, `default_should_expire`, `DEFAULT_UI_CONFIG`, `config_schema`, `preview_frame` | Register an automation app (fired by automation apps on startup) |
| `deregister_automation` | `automation_id` | Deregister an automation and purge its frames (fired by automation apps on terminate) |
| `push_automation_frame` | `automation_id`, `source_label`, `characters`, `ttl_s`, `max_age_s`, `override_ttl`, `should_expire`, `template`, `refresh_interval_minutes` | Push a frame generated by an automation (optionally with a template) |
| `push_ai_art_preview_result` | `characters`, `subject` | Store an AI art preview result without pushing to board (fired by ai_art_generator) |
| `update_next_fire_time` | `automation_id`, `next_fire_time` | Automation notifies controller of its next scheduled fire time (for display in the status sensor) |
| `activate_automation` | `automation_id` | Enable an automation |
| `deactivate_automation` | `automation_id` | Disable an automation and purge its frames |
| `clear_board` | — | Clear all frames and blank the board |
| `set_automation_config` | `automation_id`, `config` (dict) | Update persisted config for an automation |
| `generate_random_message` | `override_ttl` | On-demand frame from `messages_from_library` automation |
| `generate_random_art` | `override_ttl` | On-demand frame from `art_from_library` automation |
| `generate_ai_art` | `subject`, `override_ttl` | Generate and push AI pixel art |
| `generate_ai_art_preview` | `subject` | Generate AI art and store as preview without pushing to board |
| `clear_ai_art_preview` | — | Clear the AI art preview from status |
| `generate_ai_message` | `override_ttl` | On-demand AI-generated message |
| `preview_automation` | `automation_id` | Fire a generate event to any registered automation by ID for instant preview |

## Events fired by the controller

| Event | Data | Description |
|-------|------|-------------|
| `vestaboard_controller_ready` | — | Fired on startup; automations listen for this to re-register after a controller restart |
| `vb_auto_config (with automation_id in data)` | `config` (dict) | Config update pushed to a specific automation |
| `vb_auto_enabled (with automation_id in data)` | `enabled` (bool) | Enable/disable signal pushed to a specific automation |
| `vb_auto_generate (with automation_id in data)` | `generate_kwargs` (dict), `preview_only` (bool) | On-demand generate request to a specific automation |

## Template resolution

Frames can contain `{entity_id}` placeholders (e.g. `"UPS LOAD: {sensor.apc_load}W"`) that are resolved to live Home Assistant entity state at display time. Template resolution is handled by the controller using the shared `template_resolver` utility.

### How it works

1. When `push_frame` or `push_automation_frame` receives a payload with a `template` field, the controller calls `resolve_template()` to substitute all `{entity_id}` placeholders with current HA entity state values.
2. The resolved text is encoded to a 6×22 character grid via `text_to_grid()`, replacing the original `characters`.
3. If the payload also includes `refresh_interval_minutes`, the controller re-resolves the template on each tick (default 15s) once the interval has elapsed. If the resolved grid changes, the board is updated; if unchanged, the write is skipped.
4. Unavailable or unknown entities are substituted with `"N/A"`.
5. Overflow protection: if resolved text exceeds the 132-character grid capacity (6×22), entity values are proportionally truncated.

### Template refresh on tick

The tick loop checks the currently displayed frame for `template` + `refresh_interval_minutes`. When the interval elapses:
- Re-resolves all `{entity_id}` placeholders
- Compares the new grid to what's currently displayed
- Writes to the board only if the grid changed
- Logs at INFO when refreshing, DEBUG when unchanged

## Grid data encoding

All 6×22 character grids are JSON-stringified before being placed in event payloads to prevent Home Assistant from stripping leading/trailing zero cells. The controller and automation mixin both handle the JSON-string round-trip transparently.

## Config reference

| Key | Required | Default | Description |
|-----|----------|---------|-------------|
| `vestaboard_ip_env` | Yes | — | Env var name holding the Vestaboard local IP address |
| `vestaboard_api_key_env` | Yes | — | Env var name holding the Vestaboard local API key |
| `ha_url_env` | Yes | — | Env var name holding the HA base URL |
| `ha_token_env` | Yes | — | Env var name holding the HA long-lived access token |
| `tick_interval_s` | No | `15` | Seconds between queue tick evaluations |
| `automation_config_path` | No | `""` | Filesystem path for persistent automation config YAML |
| `frame_library_path` | No | `""` | Filesystem path for the frame library JSON (passed to automation apps that need it) |
| `sleep_window.enabled` | No | `true` | Whether to suppress board writes during the sleep window |
| `sleep_window.start` | No | `"01:00:00"` | Sleep window start time (HH:MM:SS) |
| `sleep_window.end` | No | `"07:00:00"` | Sleep window end time (HH:MM:SS) |

### YAML example

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
```

## Manual setup required

- Ensure the media directory (e.g. `/media/vestaboard/`) exists and is writable by the AppDaemon container.
- The Vestaboard device must be on the local network and have the local API enabled with a known IP and API key.
- Add the Lovelace resource for the configuration card JS after first deploy.

## Upstream/downstream dependencies

- **Upstream**: None — this is the root of the Vestaboard system.
- **Downstream**: All automation apps (`calendar_clock`, `messages_from_library`, `art_from_library`, `ai_art_generator`, `ai_message_generator`, `calendar_summary`, `weather_schedule`) register with this app via HA events at startup. No `dependencies:` YAML entry is needed.
- `vestaboard_configuration` reads `sensor.vestaboard_controller_status` and forwards commands to this app's event.
