# Vestaboard Controller App

Drives the Vestaboard flip-tile display with a library of board automations and a LIFO priority queue for managing what is shown on the board.

## Overview

`VestaboardControllerApp` is the central AppDaemon app that:

1. Connects to the Vestaboard local API (port 7000) to write 6×22 character grids.
2. Manages a **FrameQueue** with TTL, expiration, and fallback semantics.
3. Instantiates and registers **BoardAutomation** instances that generate frames.
4. Listens for `vestaboard_controller_command` events (fired by the relay script) to accept card/UI-driven commands.
5. Ticks every `tick_interval_s` seconds to advance queue state (promote pending frames when TTLs expire).
6. Publishes state to `sensor.vestaboard_controller_status` for dashboards.

## Architecture

```
Card / HA Automation
        │
        │ callService("script", "vestaboard_controller_relay", {command, payload})
        ▼
  HA Relay Script   → fires vestaboard_controller_command event
        │
        ▼
VestaboardControllerApp._on_command()
        │
        ├── push_frame  ──────────────────────► FrameQueue.push()
        ├── activate_automation  ────────────► register AppDaemon listeners
        ├── deactivate_automation ───────────► cancel AppDaemon listeners
        ├── clear_board  ────────────────────► FrameQueue.clear() + blank board
        ├── generate_random_message  ────────► RandomMessageAutomation.generate_frame()
        ├── generate_random_art  ────────────► RandomArtAutomation.generate_frame()
        └── generate_ai_art  ────────────────► AIArtGeneratorAutomation.generate_frame()

BoardAutomations (timer/state triggers) → _push_automation_frame() → FrameQueue
FrameQueue.tick() (every N seconds) → VestaboardClient.write_frame()
```

## Frame Queue Semantics

- **LIFO**: Most recently pushed frame from a given source takes priority in the pending queue.
- **TTL**: A displayed frame holds the board for `ttl_s` seconds. After expiration, the next pending frame is promoted.
- **Expiration**: A frame with `max_age_s` set is discarded if its absolute lifetime exceeds that window, even if it was never displayed.
- **Override TTL**: User-pushed frames default to `override_ttl=True` — they preempt the current frame immediately.
- **Fallback**: Previously displayed frames (if non-expired) are kept as a fallback stack. When the pending queue is empty, the most recently displayed frame is reshown.

## Board Automations

### `calendar_clock` — CalendarClockAutomation

Renders a 7-column calendar grid (left pane) and day/month/time (right pane) every 60 seconds.

- Left pane: S M T W T F S header + 5 weeks, colored by month (see source for month color map).
- Right pane: day-of-week, month+date, time (12-hour format with AM/PM).
- No TTL — holds indefinitely until another source overrides.

### `random_message` — RandomMessageAutomation

On-demand (user request only). Generates a witty bordered text message.

- With AI: calls `simple_text` provider with a personality prompt about a trapped AI in a flip board.
- Without AI: picks from a curated fallback list.
- Randomly colored border (red/orange/yellow/green/blue/violet).

### `random_art` — RandomArtAutomation

On-demand (user request only). Picks a pre-built pixel art frame from `automations/art_library.json`.

Included art: Heart, Smiley, House, Rocket, Rainbow, Checkerboard.

### `ai_art_generator` — AIArtGeneratorAutomation

On-demand, requires AI provider. Generates pixel art for a user-specified subject.

- Subject provided in command payload as `{"subject": "cat"}`.
- LLM produces a 6×22 grid of Vestaboard color codes.
- Validates output dimensions and code validity; retries once on failure.
- Falls back to blank grid after two failures.

### `calendar_summary` — CalendarSummaryAutomation

Triggered by state changes on configured HA calendar entities and on a periodic interval.

- Shows event name, time, and countdown (e.g. "14 MIN") on the board.
- TTL and expiration set dynamically: holds through event duration, expires 30 minutes after event end.
- Disabled by default in prod config — configure `calendar_entities` list to enable.

## Configuration

| Key | Required | Default | Description |
|-----|----------|---------|-------------|
| `vestaboard_ip_env` | Yes | — | Env var name holding the Vestaboard LAN IP |
| `vestaboard_api_key_env` | Yes | — | Env var name holding the Vestaboard local API key |
| `ha_url_env` | Yes | — | Env var name holding the HA base URL for provisioning |
| `ha_token_env` | Yes | — | Env var name for the HA long-lived access token |
| `tick_interval_s` | No | `15` | How often (seconds) to advance the frame queue |
| `ai_provider_conf` | No | — | AI provider bundle (used by RandomMessage, AIArtGenerator) |
| `automations` | No | `{}` | Per-automation config dict (see Automation Config below) |

### Automation Config

Each key under `automations` is an automation ID:

```yaml
automations:
  calendar_clock:
    enabled: true
  random_message:
    enabled: true
  calendar_summary:
    enabled: true
    calendar_entities:
      - calendar.family
      - calendar.hot_tub_maintenance
    reminder_minutes: 15
    check_interval_s: 300
```

### Env Vars

| Variable | Description |
|----------|-------------|
| `VESTABOARD_IP` | LAN IP address of the Vestaboard (e.g. `192.168.1.50`) |
| `VESTABOARD_API_KEY` | Local API key from the Vestaboard companion app |
| `HA_URL` | Home Assistant base URL (e.g. `http://homeassistant.local:8123`) |
| `TOKEN` | HA long-lived access token |

## Commands (via relay script)

Call `script.vestaboard_controller_relay` with `command` and `payload` fields:

| Command | Payload | Description |
|---------|---------|-------------|
| `push_frame` | `{characters, source, source_label, ttl_s, max_age_s, override_ttl}` | Push a pre-built 6×22 grid |
| `activate_automation` | `{automation_id}` | Register triggers for an automation |
| `deactivate_automation` | `{automation_id}` | Cancel triggers for an automation |
| `clear_board` | `{}` | Clear queue and blank the board |
| `generate_random_message` | `{override_ttl}` | Generate and push a random message |
| `generate_random_art` | `{override_ttl}` | Generate and push a random art frame |
| `generate_ai_art` | `{subject, override_ttl}` | Generate AI pixel art for a subject |
| `set_automation_config` | `{automation_id, config}` | Update live automation config |

## Status Sensor

`sensor.vestaboard_controller_status` — state is `"active"` or `"idle"`.

Attributes:
- `displayed_frame`: `{frame_id, source, source_label, characters}` or `null`
- `displayed_source`: source string of currently displayed frame
- `displayed_ttl_remaining_s`: seconds until TTL expires (null if no TTL)
- `pending_count`: number of frames waiting to be shown
- `fallback_count`: number of non-expired fallback frames
- `active_automations`: list of `{id, name, enabled}` dicts
- `last_write_ok`: boolean result of last board write
- `queue_state`: serialized pending and fallback frame lists

## Self-Provisioned Entities

The app creates these automatically on startup (idempotent):

| Entity | Description |
|--------|-------------|
| `script.vestaboard_controller_relay` | Relay script for card/UI → AppDaemon communication |

## Manual Prerequisites

These require manual setup and cannot be provisioned automatically:

1. **Lovelace resource**: Add the vestaboard configuration card JS as a Lovelace resource.
2. **Env vars**: `VESTABOARD_IP` and `VESTABOARD_API_KEY` must be set in `.env` (dev) or Kubernetes ExternalSecret (prod).

## Adding New Art

Add entries to `automations/art_library.json`. Each entry must be:

```json
{
  "name": "MyArt",
  "description": "Brief description",
  "characters": [
    [0, 63, 0, ...],   // row 1, 22 ints
    ...                // 6 rows total
  ]
}
```

Valid character codes: `0` (blank), `63-70` (color tiles: red, orange, yellow, green, blue, violet, white, black).

## Extending with New Automations

1. Create `automations/my_automation.py` extending `BoardAutomation`.
2. Implement `get_triggers()` and `generate_frame()`.
3. Register the class in `vestaboard_controller_app.py` `_register_automations()`.
4. Add config entry under `automations:` in `apps-prod.yaml` / `apps-dev.yaml`.
