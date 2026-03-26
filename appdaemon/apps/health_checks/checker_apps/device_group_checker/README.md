# Device Group Checker

Generic, config-driven health checker for monitoring multiple devices as a single checker unit. Useful when a set of related devices (e.g. Cielo smart AC controllers, TP-Link plugs) should appear together in the health dashboard.

Two classes are provided:

- **`DeviceGroupChecker`** — monitors multiple devices with entity state checks and optional IP pings. No repair support.
- **`RepairableDeviceGroupChecker`** — extends `DeviceGroupChecker` with per-device repair via smart switch power cycling.

## Check Naming

For each device, checks are named as `"{device_name} {entity_check_name}"` and (if `ip` is provided) `"{device_name} Ping"`.

Example with `name: Movie Room` and entity `name: Status`:
- `Movie Room Status`
- `Movie Room Ping`

## DeviceGroupChecker

### Config

```yaml
cielo_health_checker:
  module: health_checks.checker_apps.device_group_checker.device_group_checker
  class: DeviceGroupChecker
  disable: true
  checker_id: cielo
  checker_name: Cielo Home
  check_interval_s: 180
  devices:
    - name: Movie Room
      ip: "192.168.50.102"          # Optional — omit to skip ping
      entities:
        - entity_id: binary_sensor.movie_room_breeze_status
          healthy_state: "on"       # Exact state match. Omit for "not unavailable/unknown"
          name: Status              # Check display name
    - name: Rumpus Room
      ip: "192.168.50.192"
      entities:
        - entity_id: binary_sensor.rumpus_room_breeze_status
          healthy_state: "on"
          name: Status
```

### Config reference

| Key | Required | Default | Description |
|-----|----------|---------|-------------|
| `checker_id` | Yes | `device_group` | Unique ID for this checker |
| `checker_name` | No | `checker_id` | Display name |
| `check_interval_s` | No | `180` | How often to run checks (seconds) |
| `devices` | Yes | `[]` | List of device configs (see below) |

### Device config

| Key | Required | Description |
|-----|----------|-------------|
| `name` | Yes | Display name — used to prefix all check names for this device |
| `ip` | No | IP address to ping. Omit to skip ping check |
| `entities` | No | List of entity checks (see below) |

### Entity check config

| Key | Required | Description |
|-----|----------|-------------|
| `entity_id` | Yes | HA entity ID to check |
| `name` | Yes | Check display name (prefixed with device name) |
| `healthy_state` | No | Expected state value. Omit to accept any state except `unavailable`/`unknown` |

**Note on YAML bool coercion:** YAML parses `"on"` and `"off"` as booleans (`True`/`False`). The checker reverses this coercion automatically, so `healthy_state: "on"` works correctly.

## RepairableDeviceGroupChecker

Extends `DeviceGroupChecker` with per-device repair via smart switch power cycling.

### Repair logic

- Each device gets one auto-repair attempt (marked `failed` if unsuccessful, never retried automatically)
- Devices are repaired sequentially — one at a time
- A device that recovers naturally (without repair) resets to `idle`
- Manual repair resets all `failed` states and repairs all currently-failing devices
- Auto-repair is controlled via provisioned HA helpers (see Self-Provisioned Entities below)

### Repair switch selection

Each device can specify its own `repair_switch`. If absent, the top-level `repair_switch` is used as a shared switch for all devices.

### Config

```yaml
cielo_health_checker:
  module: health_checks.checker_apps.device_group_checker.repairable_device_group_checker
  class: RepairableDeviceGroupChecker
  disable: true
  ha_url: !secret ha_url
  ha_token_env: TOKEN
  checker_id: cielo
  checker_name: Cielo Home
  check_interval_s: 180
  repair_switch: switch.shared_cielo_power    # Shared fallback switch (optional)
  repair_recovery_wait_s: 300
  repair_off_duration_s: 10
  auto_repair_enabled_default: false
  auto_repair_delay_min_default: 5
  devices:
    - name: Movie Room
      ip: "192.168.50.102"
      repair_switch: switch.movie_room_power   # Per-device switch (overrides shared)
      entities:
        - entity_id: binary_sensor.movie_room_breeze_status
          healthy_state: "on"
          name: Status
    - name: Rumpus Room
      ip: "192.168.50.192"
      repair_switch: switch.rumpus_room_power
      entities:
        - entity_id: binary_sensor.rumpus_room_breeze_status
          healthy_state: "on"
          name: Status
```

### Additional config keys

| Key | Required | Default | Description |
|-----|----------|---------|-------------|
| `ha_url` | Yes | — | HA URL for provisioning |
| `ha_token_env` | Yes | — | Env var name for HA token |
| `repair_switch` | No | `""` | Shared repair switch entity ID (fallback when device has no per-device switch) |
| `repair_recovery_wait_s` | No | `300` | Max time to wait for recovery after repair (seconds) |
| `repair_off_duration_s` | No | `10` | How long to hold the switch off during power cycle |
| `auto_repair_enabled_default` | No | `false` | Initial state of auto-repair toggle |
| `auto_repair_delay_min_default` | No | `5` | Initial auto-repair delay in minutes |

### Device-level repair config

| Key | Required | Description |
|-----|----------|-------------|
| `repair_switch` | No | Per-device repair switch entity ID. Takes precedence over top-level `repair_switch` |

### Self-Provisioned Entities

| Entity | Type | Purpose |
|--------|------|---------|
| `input_boolean.{checker_id}_health_auto_repair` | Helper | Auto-repair enable toggle |
| `input_number.{checker_id}_health_auto_repair_delay` | Helper | Auto-repair delay in minutes |

For `checker_id: cielo`:
- `input_boolean.cielo_health_auto_repair`
- `input_number.cielo_health_auto_repair_delay`
