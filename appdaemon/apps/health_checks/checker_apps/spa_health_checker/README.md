# Spa Health Checker

Monitors a Gecko-integrated hot tub (Westford Spa) connected via the [ha-gecko-integration](https://github.com/geckoal/ha-gecko-integration). Detects network failures, cloud connectivity issues, and the "zombie" state where entities report normal values but commands time out.

## Checks

| Check | Method | Healthy When |
|-------|--------|-------------|
| Gateway Ping | ICMP ping to in.touch gateway IP (`gateway_host`) | Responds within timeout |
| Connection entity checks | Entity state for each entry in `connection_entities` | `"on"` |
| Thermostat Staleness | Age of `last_updated` on `staleness_entity` | < `staleness_threshold_s` |

The checks are config-driven: `connection_entities` accepts a list of binary sensor entity IDs. Check names are derived from the entity ID (e.g. `binary_sensor.westford_spa_overall_connection` becomes "Overall Connection"). Any check can be omitted by removing its config key.

The staleness check is the key zombie detector — the Gecko integration's coordinator polls every 30 seconds, so if `last_updated` hasn't changed in 5+ minutes, the data path is stale even if connectivity sensors still report "on".

## Repair

Supports auto-repair via power cycling a smart switch. The repair action:

1. Turns off `repair_switch` (cuts power to the hot tub controller)
2. Waits 10 seconds
3. Turns on `repair_switch`
4. Polls health checks every ~5 seconds for up to `repair_recovery_wait_s`
5. Reports success immediately when all checks go green, or failure after timeout

### Repair State Machine

```
idle → pending    (unhealthy for configured duration, auto-repair enabled)
pending → idle    (checks recover before deadline)
pending → in_progress  (deadline reached, executing power cycle)
in_progress → success  (checks green during recovery polling)
in_progress → failed   (timeout without recovery)
failed → (stays failed, no auto-retry — manual intervention required)
```

### Safety Rules

- **Unknown** status does NOT trigger auto-repair (handles AppDaemon-down case)
- Only sustained **critical** or **degraded** triggers auto-repair
- After failure, stays in **failed** — no auto-retry
- Manual "Repair" button resets from failed to allow retry

## Self-Provisioned Entities

| Entity | Type | Purpose |
|--------|------|---------|
| `input_boolean.spa_health_auto_repair` | Helper | Toggle auto-repair on/off |
| `input_number.spa_health_auto_repair_delay` | Helper | Minutes before auto-repair triggers (1-60) |

## Configuration Reference

```yaml
spa_health_checker:
  module: health_checks.checker_apps.spa_health_checker.spa_health_checker
  class: SpaHealthChecker
  ha_url: !secret ha_url
  ha_token_env: TOKEN
  checker_id: spa                              # Unique ID
  checker_name: Spa                            # Display name on cards
  gateway_host: "192.168.50.122"                # in.touch gateway IP to ping
  connection_entities:                         # Binary sensors to monitor
    - binary_sensor.westford_spa_overall_connection
  staleness_entity: climate.westford_spa_thermostat_1  # Entity for staleness detection
  staleness_threshold_s: 300                   # Seconds before entity is considered stale
  repair_switch: switch.power_distribution_hi_density_hot_tub  # Smart switch for power cycle
  repair_recovery_wait_s: 300                  # Max seconds to wait for recovery after repair
  check_interval_s: 120                        # Check frequency (seconds)
  auto_repair_enabled_default: false           # Default auto-repair toggle state
  auto_repair_delay_min_default: 15            # Default minutes before auto-repair triggers
```

## Dependencies

- `providers/ha_provisioner` — creates HA helpers on startup
- `shared/check_utils` — `ping_check()` for gateway ICMP ping
