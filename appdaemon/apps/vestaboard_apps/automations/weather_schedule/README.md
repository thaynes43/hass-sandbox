# Weather Schedule Automation

Displays weather conditions from a Home Assistant weather entity at configured daily times. Shows current conditions with color-coded temperature tiles, humidity, and a wind speed meter. Re-fetches and updates the board every 15 minutes during the TTL window.

## How it works

1. On `initialize()`, registers with the controller by firing a `vestaboard_controller_command` event with `command="register_automation"` — no direct `get_app()` call is needed.
2. Listens for the `vestaboard_controller_ready` event so it automatically re-registers if the controller restarts.
3. Schedules `run_daily` timers for each time in `time_list`.
4. When a scheduled time fires:
   - Reads current weather state (condition, temperature, humidity, wind speed) from the configured HA weather entity.
   - Fetches today's daily forecast (high/low) via the HA REST API (`weather.get_forecasts` service).
   - Builds a 6×22 grid with color-coded temperature tiles and wind meter.
   - Pushes the frame and starts a 15-minute periodic update timer.
5. Every 15 minutes during the TTL window, re-fetches current weather and pushes an updated frame (same-source push replaces the displayed frame without TTL override).

## Architecture

- **Type**: `weather_schedule`
- **Base**: `hass.Hass` + `VestaboardAutomation` mixin
- **Trigger**: `run_daily` at each time in `time_list`
- **Entity**: Reads from a HA `weather.*` entity (recommended: Met.no `weather.forecast_home`)
- **Periodic updates**: Every 15 minutes via `run_in` timer

```
WeatherScheduleApp
  → fire_event("vestaboard_controller_command", command="register_automation")
  → run_daily(time)
    → generate_frame() → push_frame()
    → schedule 15-min update timer
      → re-fetch weather → push_frame() (same source replaces)
      → schedule next 15-min update
  → VestaboardControllerApp handles push → FrameQueue → VestaboardClient
```

## Frame layout

```
Row 0: [color bar based on weather condition]
Row 1: CLOUDY (condition label, centered)
Row 2: HI[Y]55  LO[B]28  [B]33 (high, low, current with color tiles)
Row 3: RH 76%   WIND [W] (humidity + wind speed meter)
Row 4: (blank)
Row 5: 7:30 AM (time of reading, centered)
```

### Temperature color tiles

Each temperature value is preceded by a colored tile indicating the range:

| Range | Color | Meaning |
|-------|-------|---------|
| ≤ 40°F | Blue | Cold |
| 41–60°F | Yellow | Mild |
| 61–80°F | Orange | Warm |
| 81°F+ | Red | Hot |

### Wind speed meter

A bar of 1–4 colored tiles after the "WIND" label:

| Wind speed | Tiles | Color | Meaning |
|------------|-------|-------|---------|
| 0–7 mph | 1 tile | White | Calm |
| 8–15 mph | 2 tiles | Yellow | Breeze |
| 16–25 mph | 3 tiles | Orange | Moderate |
| 26+ mph | 4 tiles | Red | High wind |

### Condition bar colors (row 0)

| Condition | Color |
|-----------|-------|
| sunny / clear | Yellow |
| clear-night | Blue |
| cloudy / partly cloudy | White |
| rainy / pouring | Blue |
| snowy | White |
| snowy-rainy | Violet |
| thunderstorm | Yellow |
| windy | Green |
| severe weather | Red |

## Config reference

### YAML config keys

| Key | Required | Default | Description |
|-----|----------|---------|-------------|
| `module` | Yes | — | `vestaboard_apps.automations.weather_schedule.weather_schedule_app` |
| `class` | Yes | — | `WeatherScheduleApp` |
| `weather_entity` | Yes | — | HA weather entity ID (e.g. `weather.forecast_home`) |
| `ha_url_env` | Yes | — | Env var name holding the HA base URL (needed for forecast REST API) |
| `ha_token_env` | Yes | — | Env var name holding the HA long-lived access token |

### UI-editable config (stored in controller's `automation_config_path`)

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | bool | `true` | Whether the automation is active |
| `ttl_minutes` | int | `60` | How long the frame holds the board |
| `should_expire` | bool | `true` | Drop frame after TTL (don't move to fallback) |
| `force_push` | bool | `false` | Override active TTL to display immediately |
| `time_list` | time_list | `["07:30:00", "15:00:00"]` | Daily times (HH:MM:SS) to display weather |

### YAML example

```yaml
weather_schedule:
  module: vestaboard_apps.automations.weather_schedule.weather_schedule_app
  class: WeatherScheduleApp
  disable: true
  weather_entity: weather.forecast_home
  ha_url_env: HA_URL
  ha_token_env: TOKEN
  time_list:
    - "07:30:00"
    - "15:00:00"
```

## Dependencies

- `providers/vestaboard/character_encoding.py` — grid encoding utilities and color codes
- `providers/ha_provisioner/ha_rest_client.py` — HA REST API client for daily forecast
- `providers/secrets.py` — env var secret resolution
- `vestaboard_apps._shared.base.VestaboardAutomation` — controller registration and frame push API

## Manual setup required

- The HA weather integration must be configured and the `weather_entity` must exist before the app starts.
- Recommended: use Met.no (`weather.forecast_home`) for accurate current conditions and hourly/daily forecasts. Ecobee weather entities lag on condition updates (e.g. still report "sunny" at midnight).

## Upstream/downstream dependencies

- **Upstream**: `vestaboard_controller` — must be running and listening for events before this app starts. Registration happens via HA events; no AppDaemon `dependencies:` entry is needed. The app also listens for `vestaboard_controller_ready` and re-registers automatically if the controller restarts.
- **Downstream**: None.
