# Calendar Summary

Vestaboard automation that watches a Home Assistant calendar entity and displays upcoming events — including event name, start time, and a countdown ("14 MIN") — on the board.

Supports multiple instances: configure one YAML entry per calendar. The display name is derived from the app key (e.g. `calendar_summary_family` → "Calendar: Family").

## How it works

1. On `initialize()`, registers with the controller via `VestaboardAutomation.register_with_controller()`.
2. Starts a periodic interval check (default 300 s) and optionally a state listener on the configured `calendar_entity`.
3. Each check calls `_fire_frame_if_event()`:
   - Reads the calendar entity state from HA.
   - If the entity is active (`state == "on"`), the event is currently happening.
   - If inactive, checks the next upcoming event's start time against the `time_before_event_hours` window.
   - If within the window, builds a 6×22 grid with the event name (rows 1–2), start time (row 4), and countdown (row 5), all center-aligned.
4. A **rotation throttle** prevents the same event from being pushed again until `rotation_interval_hours` have passed.
5. TTL is derived from event duration (time until end of event + 30-minute buffer). If `ttl_minutes` is configured in the UI, that overrides the dynamic TTL.
6. `max_age_s` is also derived from event end time to prevent stale frames from lingering.

## Architecture

```
CalendarSummaryApp
  → VestaboardAutomation.register_with_controller()
  → listen_state(calendar_entity) + run_every(check_interval_s)
  → _fire_frame_if_event()
  → push_frame() → VestaboardControllerApp.push_automation_frame()
```

## Dependencies

- `providers.vestaboard.character_encoding` — character encoding and blank grid
- `vestaboard_apps._shared.base.VestaboardAutomation` — controller registration and frame push API

## Self-provisioned entities

None. The controller provisions all shared entities.

## Config reference

### YAML config keys

| Key | Required | Default | Description |
|-----|----------|---------|-------------|
| `module` | Yes | — | `vestaboard_apps.automations.calendar_summary.calendar_summary_app` |
| `class` | Yes | — | `CalendarSummaryApp` |
| `dependencies` | Yes | — | Must include `vestaboard_controller` |
| `calendar_entity` | Yes | — | HA entity ID of the calendar to watch (e.g. `calendar.family`) |
| `check_interval_s` | No | `300` | How often (seconds) to poll the calendar entity regardless of state changes |
| `controller_app` | No | `vestaboard_controller` | AppDaemon app key of the controller instance |

### UI-editable config (stored in controller's `automation_config_path`)

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | bool | `false` | Whether the automation is active |
| `ttl_minutes` | int or null | `30` | TTL for displayed frames. Overrides dynamic duration-based TTL when set |
| `should_expire` | bool | `false` | If `true`, frame is dropped after TTL rather than moved to fallback |
| `time_before_event_hours` | int | `240` | Hours before an event to start showing it on the board (240 = 10 days) |
| `rotation_interval_hours` | int | `12` | Minimum hours between pushes of the same event to prevent repetition |

### YAML example (single calendar)

```yaml
calendar_summary_family:
  module: vestaboard_apps.automations.calendar_summary.calendar_summary_app
  class: CalendarSummaryApp
  disable: true
  dependencies:
    - vestaboard_controller
  calendar_entity: calendar.family
```

### YAML example (multiple calendars)

```yaml
calendar_summary_family:
  module: vestaboard_apps.automations.calendar_summary.calendar_summary_app
  class: CalendarSummaryApp
  disable: true
  dependencies:
    - vestaboard_controller
  calendar_entity: calendar.family

calendar_summary_work:
  module: vestaboard_apps.automations.calendar_summary.calendar_summary_app
  class: CalendarSummaryApp
  disable: true
  dependencies:
    - vestaboard_controller
  calendar_entity: calendar.work
```

Each instance registers with the controller under its own app key and appears separately in the configuration card's automation list with the display name derived from the key suffix.

## Manual setup required

- The HA calendar integration must be configured and the `calendar_entity` must exist before the app starts.

## Upstream/downstream dependencies

- **Upstream**: `vestaboard_controller` — must be running and registered before this app starts.
- **Downstream**: None.
