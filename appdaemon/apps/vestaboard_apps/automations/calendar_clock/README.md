# Calendar Clock

Vestaboard automation that renders a 7-column calendar grid on the left pane and the current day, date, and time on the right pane, updated every 60 seconds.

## How it works

1. On `initialize()`, registers with the controller via `VestaboardAutomation.register_with_controller()`.
2. Starts a 60-second `run_every` timer.
3. Each tick calls `generate_frame()` which builds the 6×22 grid:
   - Left 7 columns: S M T W T F S day-of-week header (row 0) + calendar tiles for the current month's weeks (rows 1–5). Today's tile uses the "today" color; all other days use the "day" color; out-of-month cells are black.
   - 2-column separator gap.
   - Right 13 columns: day-of-week name (row 1), month + day (row 2), blank (row 3), time in 12-hour format (row 4), blank (row 5).
4. Month-specific color pairs are defined for each month (e.g. January = blue/white, December = red/green).
5. The frame is pushed to the controller with the configured TTL and `should_expire` value via `push_frame()`.
6. When disabled via the UI, the timer is cancelled. When re-enabled, the timer restarts and an immediate frame is pushed.

## Architecture

```
CalendarClockApp
  → VestaboardAutomation.register_with_controller()
  → run_every(60s) → generate_frame() → push_frame()
  → VestaboardControllerApp.push_automation_frame()
```

## Dependencies

- `providers.vestaboard.character_encoding` — character/color code constants and encoding helpers
- `vestaboard_apps._shared.base.VestaboardAutomation` — controller registration and frame push API

## Self-provisioned entities

None. The controller provisions all shared entities.

## Config reference

### YAML config keys

| Key | Required | Default | Description |
|-----|----------|---------|-------------|
| `module` | Yes | — | `vestaboard_apps.automations.calendar_clock.calendar_clock_app` |
| `class` | Yes | — | `CalendarClockApp` |
| `dependencies` | Yes | — | Must include `vestaboard_controller` |
| `controller_app` | No | `vestaboard_controller` | AppDaemon app key of the controller instance |

### UI-editable config (stored in controller's `automation_config_path`)

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | bool | `true` | Whether the automation is active |
| `ttl_minutes` | int or null | `null` | TTL for displayed frames in minutes. `null` = no TTL protection |
| `should_expire` | bool | `false` | If `true`, frame is dropped after TTL rather than moved to fallback |

### YAML example

```yaml
calendar_clock:
  module: vestaboard_apps.automations.calendar_clock.calendar_clock_app
  class: CalendarClockApp
  disable: true
  dependencies:
    - vestaboard_controller
```

## Manual setup required

None beyond the controller's prerequisites.

## Upstream/downstream dependencies

- **Upstream**: `vestaboard_controller` — must be running and registered before this app starts.
- **Downstream**: None.
