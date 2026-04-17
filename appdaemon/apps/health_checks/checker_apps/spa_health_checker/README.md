# Spa Health Checker

Monitors a Gecko-integrated hot tub (Westford Spa) connected via the [ha-gecko-integration](https://github.com/geckoal/ha-gecko-integration). Detects network failures, cloud connectivity issues, and the "zombie" state where entities report normal values but commands time out.

## Checks

| Check | Method | Healthy When |
|-------|--------|-------------|
| Gateway Ping | ICMP ping to in.touch gateway IP (`gateway_host`) | Responds within timeout |
| Connection entity checks | Entity state for each entry in `connection_entities` | `"on"` |
| Staleness | Age of `last_updated` across all entities in `staleness_entities` | ANY entity is fresh (< `staleness_threshold_s`); fails only if ALL are stale |

The checks are config-driven: `connection_entities` accepts a list of binary sensor entity IDs. Check names are derived from the entity ID (e.g. `binary_sensor.westford_spa_overall_connection` becomes "Overall Connection"). Any check can be omitted by removing its config key.

The staleness check is the key zombie detector — the Gecko integration's coordinator polls every 30 seconds, so if no tracked entity has been updated recently, the data path is stale even if connectivity sensors still report "on". Using multiple entities (thermostat, lights, pumps) with OR logic reduces false positives: any one fresh entity keeps the check healthy.

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
pending → idle    (checks recover before deadline, OR cancel_repair command received)
pending → in_progress  (deadline reached, executing power cycle)
in_progress → success  (checks green during recovery polling)
in_progress → failed   (timeout without recovery)
failed → idle    (checks recover naturally — state clears automatically)
```

### Safety Rules

- **Unknown** status does NOT trigger auto-repair (handles AppDaemon-down case)
- Only sustained **critical** or **degraded** triggers auto-repair
- After failure, resets to **idle** automatically when all checks pass, or via the manual "Repair" button
- No auto-retry from **failed** state while checks are still unhealthy

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
  staleness_entities:                          # Entities for staleness detection (OR logic — any fresh entity passes)
    - climate.westford_spa_thermostat_1
  staleness_threshold_s: 10800                 # Seconds before all entities are considered stale (3 hours)
  repair_switch: switch.spa_intouch3_switch    # Z-Wave switch controlling spa power
  repair_recovery_wait_s: 300                  # Max seconds to wait for recovery after repair
  check_interval_s: 120                        # Check frequency (seconds)
  auto_repair_enabled_default: false           # Default auto-repair toggle state
  auto_repair_delay_min_default: 15            # Default minutes before auto-repair triggers
```

> **Backward compatibility**: the legacy `staleness_entity` (single string) is still accepted and is treated as a one-element list. Prefer `staleness_entities` for new configs.

## Commands

The checker listens for relay commands routed by the controller:

| Command | Payload | Description |
|---------|---------|-------------|
| `trigger_repair` | `{"checker_id": "spa"}` | Manually trigger a power-cycle repair |
| `cancel_repair` | `{"checker_id": "spa"}` | Cancel a pending auto-repair (returns to idle) |

## Dependencies

- `providers/ha_provisioner` — creates HA helpers on startup
- `shared/check_utils` — `ping_check()` for gateway ICMP ping
