# Weather Schedule Automation

Displays weather conditions from a Home Assistant weather entity at configured daily times.

## How it works

1. On `initialize()`, registers with the controller by firing a `vestaboard_controller_command` event with `command="register_automation"` — no direct `get_app()` call is needed.
2. Listens for the `vestaboard_controller_ready` event so it automatically re-registers if the controller restarts.
3. Schedules `run_daily` timers for each time in `time_list`.
4. When a scheduled time fires, reads the weather entity state from HA and calls `generate_frame()` to build a 6×22 grid.
5. The frame is pushed to the controller by firing a `vestaboard_controller_command` event with `command="push_automation_frame"`.

## Architecture

- **Type**: `weather_schedule`
- **Base**: `hass.Hass` + `VestaboardAutomation` mixin
- **Trigger**: `run_daily` at each time in `time_list`
- **Entity**: Reads from a HA `weather.*` entity

```
WeatherScheduleApp
  → fire_event("vestaboard_controller_command", command="register_automation")
  → run_daily(time) → generate_frame()
  → fire_event("vestaboard_controller_command", command="push_automation_frame")
  → VestaboardControllerApp handles push → FrameQueue → VestaboardClient
```

## Configuration

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `weather_entity` | string | required | HA weather entity ID (e.g. `weather.first_floor_ecobee`) |
| `time_list` | list[str] | `["07:30:00", "15:00:00"]` | Daily times (HH:MM:SS) to display weather |
| `ttl_minutes` | int | 60 | How long the frame holds the board |
| `should_expire` | bool | true | Drop frame after TTL (don't move to fallback) |
| `force_push` | bool | false | Override active TTL to display immediately |

## Frame Layout

```
Row 0: [color bar based on weather condition]
Row 1: SUNNY / CLOUDY / RAINY etc. (centered)
Row 2: 72 F (temperature, centered)
Row 3: FEELS LIKE 75 / HUMIDITY 45% (centered)
Row 4: (blank)
Row 5: 7:30 AM (time of reading, centered)
```

## Weather Condition Colors

| Condition | Color |
|-----------|-------|
| sunny/clear | Yellow |
| cloudy/partly cloudy | White |
| rainy/pouring | Blue |
| snowy | White |
| thunderstorm | Yellow |
| windy | Green |
| severe | Red |

## YAML Example

```yaml
weather_schedule:
  module: vestaboard_apps.automations.weather_schedule.weather_schedule_app
  class: WeatherScheduleApp
  disable: true
  weather_entity: weather.first_floor_ecobee
  time_list:
    - "07:30:00"
    - "15:00:00"
```

## Dependencies

- `providers/vestaboard/character_encoding.py` — grid encoding utilities
- `vestaboard_apps._shared.base.VestaboardAutomation` — controller registration and frame push API

## Upstream/downstream dependencies

- **Upstream**: `vestaboard_controller` — must be running and listening for events before this app starts. Registration happens via HA events; no AppDaemon `dependencies:` entry is needed. The app also listens for `vestaboard_controller_ready` and re-registers automatically if the controller restarts.
- **Downstream**: None.
