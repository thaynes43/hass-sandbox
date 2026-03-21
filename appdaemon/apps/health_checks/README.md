# Health Checks

System health monitoring for the Home Assistant dashboard. Provides visibility into AppDaemon backend status, network protocol stack health (Zigbee, Z-Wave), and device health (Spa) with optional auto-repair capability.

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
│  SpaHealthChecker        │ ──────────────────▶   │  Routes repair cmds:    │
│  (supports_repair: true) │  + repair_state       │  start_repair           │
│                          │                       │  update_repair_config   │
│  ◀── health_check_repair_spa ──                  │                         │
└──────────────────────────┘                       │  Publishes:             │
                                                   │  - sensor               │
     ◀── health_check_controller_ready ──          │    .health_check_status │
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

`BasicDeviceChecker` is a generic, config-driven checker for any device needing entity state monitoring and an optional IP ping. No repair support. See `basic_device_checker/README.md` for details.

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
  alert_history_max: 20          # Max alerts retained per checker
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

## Sensor Attributes Schema

`sensor.health_check_status` attributes:

```json
{
  "checkers": {
    "<checker_id>": {
      "name": "Zigbee",
      "status": "ok|degraded|critical|unknown",
      "last_check": "2026-03-19T20:00:00",
      "checks": [
        {
          "name": "Bridge Connection",
          "status": "ok",
          "detail": "on",
          "last_changed": "2026-03-19T20:00:00"
        }
      ],
      "alert_history": [
        {
          "timestamp": "2026-03-19T19:55:00",
          "check": "Bridge Connection",
          "from_status": "ok",
          "to_status": "critical",
          "detail": "Expected 'on', got 'off'"
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
│   │   └── network_protocol_checker.py
│   ├── spa_health_checker/
│   │   ├── __init__.py
│   │   ├── spa_health_checker.py
│   │   └── README.md
│   ├── fan_health_checker/
│   │   ├── __init__.py
│   │   ├── fan_health_checker.py
│   │   └── README.md
│   └── basic_device_checker/
│       ├── __init__.py
│       ├── basic_device_checker.py
│       └── README.md
├── shared/
│   ├── __init__.py
│   └── check_utils.py
├── cards/
│   ├── health-check-card.js
│   └── health-check-detail-card.js
└── README.md
```
