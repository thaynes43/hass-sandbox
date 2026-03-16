# Vestaboard Controller

Drives the physical Vestaboard flip-tile display. Manages a LIFO priority queue of frames, dispatches them to the board on a periodic tick, and exposes a dynamic automation registration API so each automation app independently publishes frames.

## How it works

1. On startup, provisions `script.vestaboard_controller_relay` in HA via `HAProvisioner`.
2. Reads the current frame from the physical board so the queue has a starting state.
3. Loads the persistent `AutomationConfigStore` from `automation_config_path` to restore previously saved UI settings.
4. Registers a periodic tick (default 15 s) that advances the `FrameQueue` — promoting pending frames when TTLs expire and publishing the updated status sensor.
5. Automation apps call `register_automation(self)` from their `initialize()` and receive their persisted config back immediately.
6. When an automation generates a frame it calls `push_automation_frame()`, which pushes it into the LIFO queue and may immediately display it.
7. Commands from the Lovelace card arrive via `script.vestaboard_controller_relay` → `vestaboard_controller_command` event → `_on_command()`.

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

```
Lovelace card
  → hass.callService("script", "vestaboard_controller_relay", {command, payload})
  → vestaboard_controller_command event
  → VestaboardControllerApp._on_command()

Automation apps (CalendarClockApp, AiArtGeneratorApp, ...)
  → VestaboardAutomation.push_frame()
  → VestaboardControllerApp.push_automation_frame()
  → FrameQueue.push()
  → VestaboardClient.write_frame()  [if frame is immediately displayed]

Periodic tick (every tick_interval_s)
  → FrameQueue.tick()
  → VestaboardClient.write_frame()  [if a frame is promoted]
  → VestaboardControllerApp._publish_status()
  → sensor.vestaboard_controller_status
```

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

## Public API (called by automation apps)

| Method | Signature | Description |
|--------|-----------|-------------|
| `register_automation` | `(automation: Any) -> None` | Register an automation app instance with the controller |
| `deregister_automation` | `(auto_id: str) -> None` | Deregister and purge all frames from an automation |
| `push_automation_frame` | `(automation_id, source_label, grid, ttl_s, max_age_s, override_ttl, should_expire) -> None` | Push a 6×22 frame to the queue |

## Supported commands (via relay script)

| Command | Payload fields | Description |
|---------|---------------|-------------|
| `push_frame` | `characters`, `ttl_s`/`ttl_minutes`, `max_age_s`, `override_ttl`, `should_expire` | Push a pre-built frame |
| `activate_automation` | `automation_id` | Enable an automation and push an immediate frame |
| `deactivate_automation` | `automation_id` | Disable an automation and purge its frames |
| `clear_board` | — | Clear all frames and blank the board |
| `set_automation_config` | `automation_id`, `config` (dict) | Update persisted config for an automation |
| `generate_random_message` | `override_ttl` | On-demand frame from `messages_from_library` automation |
| `generate_random_art` | `override_ttl` | On-demand frame from `art_from_library` automation |
| `generate_ai_art` | `subject`, `override_ttl` | Generate and push AI pixel art |
| `generate_ai_art_preview` | `subject` | Generate AI art and store as preview without pushing to board |
| `clear_ai_art_preview` | — | Clear the AI art preview from status |
| `generate_ai_message` | `override_ttl` | On-demand AI-generated message |

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
- **Downstream**: All automation apps (`calendar_clock`, `messages_from_library`, `art_from_library`, `ai_art_generator`, `ai_message_generator`, `calendar_summary`, `weather_schedule`) depend on this app via `dependencies: [vestaboard_controller]`.
- `vestaboard_configuration` reads `sensor.vestaboard_controller_status` and forwards commands to this app's event.
