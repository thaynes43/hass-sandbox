# Health Checks

System health monitoring for the Home Assistant dashboard. Provides visibility into AppDaemon backend status, network protocol stack health (Zigbee, Z-Wave), MQTT broker and device health, environmental sensor monitoring, and device health (Spa, fans, printers) with optional auto-repair capability.

## Architecture

```
┌──────────────────────────┐      HA Events       ┌─────────────────────────┐
│  NetworkProtocolChecker  │ ──────────────────▶   │  HealthCheckController  │
│  (Zigbee instance)       │  register_checker     │                         │
│                          │  report_status        │  Provisions:            │
├──────────────────────────┤                       │  - input_datetime       │
│  NetworkProtocolChecker  │ ──────────────────▶   │    .appdaemon_heartbeat │
│  (Z-Wave instance)       │                       │  - script               │
│                          │                       │    .health_check_relay  │
├──────────────────────────┤                       │                         │
│  MqttBrokerChecker       │ ──────────────────▶   │  Resolves dependencies: │
│                          │                       │  (published view only)  │
├──────────────────────────┤                       │                         │
│  MqttDeviceChecker       │ ──────────────────▶   │  Routes repair cmds:    │
│  (depends: mqtt_broker)  │  + dependencies       │  start_repair           │
├──────────────────────────┤                       │  update_repair_config   │
│  TempHumidityChecker     │ ──────────────────▶   │                         │
│                          │  + dependencies       │                         │
├──────────────────────────┤                       │                         │
│  SpaHealthChecker        │ ──────────────────▶   │  Publishes:             │
│  (supports_repair: true) │  + repair_state       │  - sensor               │
│                          │                       │    .health_check_status │
│  ◀── health_check_repair_spa ──                  │                         │
└──────────────────────────┘                       │                         │
                                                   │                         │
     ◀── health_check_controller_ready ──          │                         │
     ◀── health_check_recheck ───────────          └─────────────────────────┘
                                                              │
                                                              ▼
                                                   ┌─────────────────────────┐
                                                   │  Custom Lovelace Cards  │
                                                   │  health-check-card.js   │
                                                   │  health-check-detail    │
                                                   │    -card.js             │
                                                   └─────────────────────────┘
```

## How It Works

### Event-Based Decoupling

Checker apps communicate with the controller **exclusively via HA events** — never `get_app()`. This allows:

- The controller to run in production Kubernetes while new checkers are developed on a laptop
- Independent restart and lifecycle management
- Easy addition of new checker types without modifying the controller

### Heartbeat Mechanism

The controller updates `input_datetime.appdaemon_heartbeat` every 60 seconds. The custom card computes staleness client-side by comparing the timestamp against `Date.now()`. If stale > 180s, the card shows AppDaemon as offline — **no HA automation or template sensor needed**.

### Generic Protocol Checker

`NetworkProtocolChecker` is a single class instantiated per-protocol via `apps.yaml` config. Each instance performs three checks:

1. **Entity state** — verify an HA entity matches an expected healthy state
2. **Radio ping** — ICMP ping a PoE radio/coordinator hostname
3. **Web UI** — HTTP GET a management web interface URL

Adding a new protocol (e.g. Thread) requires only a new `apps.yaml` entry — no code changes.

### Spa Health Checker

`SpaHealthChecker` monitors a Gecko-integrated hot tub with four checks and optional auto-repair via power cycling. See `spa_health_checker/README.md` for details.

### Fan Health Checker

`FanHealthChecker` monitors all Modern Forms ceiling fans as a single checker (2 checks per fan). Supports per-fan repair via `script.zen32_hard_reset`. See `fan_health_checker/README.md` for details.

### Basic Device Checker

`BasicDeviceChecker` is a generic, config-driven checker for any device needing entity state monitoring and an optional IP ping. No repair support. See `device_checker/README.md` for details.

### Device Group Checker

`DeviceGroupChecker` monitors multiple devices as a single checker. Each device can have one or more entity checks and an optional IP ping. Check names are prefixed with the device name (e.g. "Movie Room Status", "Movie Room Ping"). No repair support.

`RepairableDeviceGroupChecker` extends `DeviceGroupChecker` with per-device repair via smart switch power cycling. Each device can have its own `repair_switch`, or all devices can share a top-level switch. Per-device repair state is tracked and reported via `device_repairs` in the repair_state payload. See `device_group_checker/README.md` for details.

### MQTT Broker Checker

`MqttBrokerChecker` verifies AppDaemon can communicate with the MQTT broker by performing a publish/subscribe round-trip ping test. Publishes a JSON message with a unique nonce to a configurable topic, then listens for the message to come back. Reports **ok** with round-trip latency on success, or **critical** on timeout. See `mqtt_broker_checker/README.md` for details.

### MQTT Device Checker

`MqttDeviceChecker` monitors devices via both HA entity state and MQTT message timestamps. Discovers entities using configurable regex patterns and creates two checks per device: a State check and an MQTT check. Cross-check logic is symmetric: if only one check fails it is downgraded to **warning**; both must fail for **critical**. MQTT checks can declare a dependency on a protocol checker (e.g. Zigbee) so they show as **unknown** when the protocol itself is down. See `mqtt_device_checker/README.md` for details.

### Temp/Humidity Checker

`TempHumidityChecker` monitors environmental sensors with configurable warning and critical thresholds. Supports temperature, humidity, or both sensor types. Each sensor can have per-sensor threshold overrides and can declare a dependency on another checker. See `temp_humidity_checker/README.md` for details.

### Dependency System

Checkers can declare `dependencies` during registration to express that some of their checks depend on another checker being healthy. At publish time, the controller resolves these dependencies: if a dependency checker's status is not `ok` or `warning`, the affected checks are overridden to `unknown` with detail `"dependency unavailable"` in the **published view only** -- the internal state is never modified. This prevents misleading alerts when a shared dependency (e.g., the Zigbee protocol stack) is down.

Dependencies are declared per-check via `affects_checks`, or if omitted, all checks in the registering checker are affected:

```json
{
  "dependencies": [
    {
      "checker_id": "zigbee",
      "affects_checks": ["Device1 MQTT", "Device2 MQTT"]
    }
  ]
}
```

The checker-level status is recomputed from the modified checks in the published view using the standard severity precedence: critical > degraded > warning > unknown > ok.

### Repair Feature

Checkers can declare `supports_repair: true` during registration. The controller routes repair commands to the specific checker without knowing how to repair — all repair logic lives in the checker app. The detail card shows repair controls (manual button, auto-repair toggle, delay config) for repair-capable checkers.

## Dependencies

- `providers/ha_provisioner` — creates HA helpers and scripts on startup
- `aiohttp` — HTTP health checks (in `shared/check_utils.py`)

## Self-Provisioned Entities

| Entity | Type | Purpose |
|--------|------|---------|
| `input_datetime.appdaemon_heartbeat` | Helper | Controller heartbeat timestamp |
| `script.health_check_relay` | Script | Card → AppDaemon command relay |
| `sensor.health_check_status` | Virtual sensor | Aggregated health status (via `set_state`) |
| `input_boolean.spa_health_auto_repair` | Helper | Auto-repair toggle (provisioned by SpaHealthChecker) |
| `input_number.spa_health_auto_repair_delay` | Helper | Auto-repair delay in minutes (provisioned by SpaHealthChecker) |

## Associated Cards

| Card | File | Purpose |
|------|------|---------|
| `health-check-card` | `cards/health-check-card.js` | Compact summary bar for wall-display |
| `health-check-detail-card` | `cards/health-check-detail-card.js` | Full detail popup with alert history |

## Configuration Reference

### HealthCheckController

```yaml
health_check_controller:
  module: health_checks.controller.health_check_controller
  class: HealthCheckController
  ha_url: !secret ha_url
  ha_token_env: TOKEN
  heartbeat_interval_s: 60       # Heartbeat update frequency (seconds)
  alert_history_max: 50          # Max alerts retained per checker
  alert_retention: "1:12:00:00"  # TTL for alert history entries (DD:HH:MM:SS or HH:MM:SS)
```

### NetworkProtocolChecker

```yaml
zigbee_health_checker:
  module: health_checks.checker_apps.network_protocol_checker.network_protocol_checker
  class: NetworkProtocolChecker
  checker_id: zigbee                                          # Unique ID
  checker_name: Zigbee                                        # Display name
  entity_id: binary_sensor.zigbee2mqtt_bridge_connection_state  # HA entity to monitor
  entity_healthy_state: "on"                                  # Expected healthy state
  entity_check_name: Bridge Connection                        # Check display name
  radio_host: tubeszb-zigbee01.haynesnetwork                  # Hostname to ping
  radio_check_name: Coordinator Ping                          # Check display name
  web_ui_url: https://zigbee.haynesops.com                    # URL to GET
  web_ui_check_name: Web UI                                   # Check display name
  check_interval_s: 180                                       # Check frequency (seconds)
```

Any check can be disabled by omitting its config key (e.g., remove `radio_host` to skip the ping check).

## Relay Commands

| Command | Payload | Description |
|---------|---------|-------------|
| `force_recheck` | `{}` | Triggers all checkers to run immediately |
| `start_repair` | `{"checker_id": "spa"}` | Trigger manual repair for a specific checker |
| `update_repair_config` | `{"checker_id": "spa", "auto_repair_enabled": true, "auto_repair_delay_min": 5}` | Update auto-repair settings |
| `clear_alert_history` | `{"checker_id": "optional"}` | Clear alert history for one or all checkers |

## Sensor Attributes Schema

`sensor.health_check_status` attributes:

```json
{
  "checkers": {
    "<checker_id>": {
      "name": "Zigbee",
      "status": "ok|warning|degraded|critical|unknown",
      "last_check": "2026-03-19T20:00:00",
      "checks": [
        {
          "name": "Bridge Connection",
          "status": "ok",
          "detail": "on",
          "last_changed": "2026-03-19T20:00:00"
        }
      ],
      "checks_summary": {
        "total": 3,
        "ok": 3,
        "non_ok": 0
      },
      "alert_history": [
        {
          "timestamp": "2026-03-19T19:55:00",
          "check": "Bridge Connection",
          "from_status": "ok",
          "to_status": "critical",
          "detail": "Expected 'on', got 'off'",
          "previous_state_entered": "2026-03-19T18:30:00",
          "previous_state_duration_s": 5100.0
        }
      ],
      "supports_repair": false,
      "repair_state": null
    }
  },
  "last_updated": "2026-03-19T20:00:00",
  "friendly_name": "Health Check Status",
  "icon": "mdi:heart-pulse"
}
```

**Note:** For checkers with more than 20 checks, only non-ok checks are included in the `checks` array to stay within HA's WebSocket attribute size limit. The `checks_summary` object always contains the full counts.

## Manual Setup Required

1. **Lovelace resources** — register the JS card files:
   - `/local/health-checks/health-check-card.js`
   - `/local/health-checks/health-check-detail-card.js`
2. **Copy card JS** to `/config/www/health-checks/` on the HA instance
3. **Add cards** to the Wall-Display dashboard (see `home-assistant/cards/wall-display/`)

## Folder Structure

```
health_checks/
├── controller/
│   ├── __init__.py
│   └── health_check_controller.py
├── checker_apps/
│   ├── __init__.py
│   ├── network_protocol_checker/
│   │   ├── __init__.py
│   │   ├── network_protocol_checker.py
│   │   └── README.md
│   ├── mqtt_broker_checker/
│   │   ├── __init__.py
│   │   ├── mqtt_broker_checker.py
│   │   └── README.md
│   ├── mqtt_device_checker/
│   │   ├── __init__.py
│   │   ├── mqtt_device_checker.py
│   │   └── README.md
│   ├── temp_humidity_checker/
│   │   ├── __init__.py
│   │   ├── temp_humidity_checker.py
│   │   └── README.md
│   ├── spa_health_checker/
│   │   ├── __init__.py
│   │   ├── spa_health_checker.py
│   │   └── README.md
│   ├── fan_health_checker/
│   │   ├── __init__.py
│   │   ├── fan_health_checker.py
│   │   └── README.md
│   ├── device_checker/
│   │   ├── __init__.py
│   │   ├── device_checker.py
│   │   ├── repairable_device_checker.py
│   │   └── README.md
│   └── device_group_checker/
│       ├── __init__.py
│       ├── device_group_checker.py
│       ├── repairable_device_group_checker.py
│       └── README.md
├── shared/
│   ├── __init__.py
│   └── check_utils.py
├── cards/
│   ├── health-check-card.js
│   └── health-check-detail-card.js
└── README.md
```
