# Temperature/Humidity Checker

Config-driven health checker for environmental sensors. Monitors temperature and/or humidity readings with configurable warning and critical thresholds. A single class can be instantiated multiple times for different rooms or sensor groups.

## How It Works

Each configured sensor is evaluated on a periodic interval:

1. Read the sensor's current state from Home Assistant
2. If the state is `unavailable`, `unknown`, or non-numeric, report **critical**
3. Compare the numeric value against warning and critical thresholds:
   - **ok** — value is within the warning range (inclusive)
   - **warning** — value is outside the warning range but within the critical range
   - **critical** — value is outside the critical range

Sensors can declare a **dependency** on a protocol checker (e.g., `zwave`, `zigbee`). When that protocol checker is not ok, the controller marks the dependent sensor checks as `unknown` automatically.

## Checks

| Check | Method | Healthy When |
|-------|--------|-------------|
| Per-sensor humidity | State value comparison | Within warning thresholds |
| Per-sensor temperature | State value comparison | Within warning thresholds |

## Threshold Evaluation

Thresholds are evaluated from outermost to innermost:

```
critical_low ... warning_low ... OK ... warning_high ... critical_high
```

For example, with humidity thresholds `60/62/68/70`:
- Below 60 or above 70: **critical**
- 60-62 or 68-70: **warning**
- 62-68: **ok**

## Dependencies

Sensors can depend on protocol checkers:
- Z-Wave sensors (`dependency: zwave`) — depend on the Z-Wave protocol checker
- Zigbee sensors (`dependency: zigbee`) — depend on the Zigbee protocol checker

When a dependency checker is degraded or critical, affected sensor checks show as `unknown` (handled by the controller's dependency system).

## Configuration Reference

```yaml
cigar_humidity_checker:
  module: health_checks.checker_apps.temp_humidity_checker.temp_humidity_checker
  class: TempHumidityChecker
  checker_id: cigar_humidity         # Unique checker ID
  checker_name: Cigars               # Display name on cards
  check_interval_s: 120             # Check frequency (seconds)

  # Default thresholds (applied to all sensors unless overridden)
  temp_low_warning: 60
  temp_high_warning: 70
  temp_low_critical: 58
  temp_high_critical: 72
  humidity_low_warning: 62
  humidity_high_warning: 68
  humidity_low_critical: 60
  humidity_high_critical: 70

  sensors:
    - entity_id: sensor.cigar_humidity_sensor_01_humidity
      name: Cigar Sensor 01          # Display name for this check
      type: humidity                  # "temperature", "humidity", or "both"
      dependency: zwave               # Optional protocol dependency
      # Per-sensor threshold overrides (optional)
      humidity_low_warning: 63
      humidity_high_warning: 67
```

### Sensor Types

| Type | Evaluates |
|------|-----------|
| `humidity` | Humidity thresholds only |
| `temperature` | Temperature thresholds only |
| `both` | Both sets of thresholds; worst result wins |

## Dependencies

- No external providers required
- Reports to Health Check Controller via `health_check_command` events
