# Basic Device Checker

Generic, config-driven health checker for any device that needs entity state monitoring and an optional IP ping. No repair support — designed for devices that cannot be auto-repaired from AppDaemon.

A single class that can be instantiated multiple times with different configuration, similar to `NetworkProtocolChecker` but for arbitrary entity checks rather than network protocol stacks.

## Checks

| Check | Config Key | Optional |
|-------|-----------|----------|
| IP Ping | `ping_host` | Yes — omit to skip |
| Entity State (1..N) | `entities[]` | At least one recommended |

## Configuration Reference

```yaml
vestaboard_health_checker:
  module: health_checks.checker_apps.device_checker.device_checker
  class: BasicDeviceChecker
  checker_id: vestaboard                  # Unique ID
  checker_name: Vestaboard                # Display name on cards
  ping_host: "192.168.50.159"             # IP to ping (optional, omit to skip)
  ping_check_name: Ping                   # Display name for ping check
  check_interval_s: 180                   # Check frequency (seconds)
  entities:                               # List of entity checks
    - entity_id: sensor.vestaboard_controller_status
      healthy_state: active               # Expected state value
      name: Controller Status             # Display name for this check
    - entity_id: sensor.vestaboard_configuration_status
      healthy_state: ok
      name: Configuration Status
```

Any check can be disabled by omitting its config key. YAML bool coercion is handled (`"on"` → `True` → reversed back to `"on"`).

## RepairableDeviceChecker

`RepairableDeviceChecker` extends `BasicDeviceChecker` with smart-switch power-cycle repair. Same check config, plus:

```yaml
printer_health_checker:
  module: health_checks.checker_apps.device_checker.repairable_device_checker
  class: RepairableDeviceChecker
  ha_url: !secret ha_url
  ha_token_env: TOKEN
  checker_id: printer
  checker_name: Printer
  ping_host: "192.168.0.211"
  ping_check_name: Ping
  check_interval_s: 180
  repair_switch: switch.downstairs_study_printer_switch  # Smart switch to toggle
  repair_recovery_wait_s: 300                            # Max wait for recovery
  repair_off_duration_s: 10                              # Seconds to keep switch off
  auto_repair_enabled_default: false
  auto_repair_delay_min_default: 5
  entities:
    - entity_id: sensor.brother_mfc_l3780cdw_series
      name: Status
```

Self-provisions `input_boolean.{checker_id}_health_auto_repair` and `input_number.{checker_id}_health_auto_repair_delay`.

## Dependencies

- `shared/check_utils` — `ping_check()` for IP pings
- `providers/ha_provisioner` — creates HA helpers (RepairableDeviceChecker only)
