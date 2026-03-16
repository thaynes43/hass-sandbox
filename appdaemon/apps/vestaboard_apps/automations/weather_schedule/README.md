# Weather Schedule Automation

Displays weather conditions from a Home Assistant weather entity at configured daily times.

## Architecture

- **Type**: `weather_schedule`
- **Base**: `hass.Hass` + `VestaboardAutomation` mixin
- **Trigger**: `run_daily` at each time in `time_list`
- **Entity**: Reads from a HA `weather.*` entity

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
  dependencies:
    - vestaboard_controller
  weather_entity: weather.first_floor_ecobee
  time_list:
    - "07:30:00"
    - "15:00:00"
```

## Dependencies

- `vestaboard_controller` — registers with the controller on startup
- `providers/vestaboard/character_encoding.py` — grid encoding utilities
