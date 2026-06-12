# Health Checks

System health monitoring for the Home Assistant dashboard. Provides visibility into AppDaemon backend status, network protocol stack health (Zigbee, Z-Wave), MQTT broker and device health, environmental sensor monitoring, device health (Spa, fans, printers) with optional auto-repair capability, the UniFi Protect camera event stream (with config-entry-reload auto-heal), and the ComfyUI image-generation service. Critical checker failures can page the phone via the cluster's Alertmanager (see [Alertmanager Bridge](#alertmanager-bridge)).

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
├──────────────────────────┤                       │                         │
│  ProtectHealthChecker    │ ──────────────────▶   │  Mirrors to             │
│  (supports_repair: true) │  + alerting           │  Alertmanager (when     │
│                          │                       │  alertmanager_url set): │
│  ◀── health_check_repair_protect ──              │  critical → page        │
├──────────────────────────┤                       │  warning  → UI only     │
│  ImageGenHealthChecker   │ ──────────────────▶   │  ok       → resolve     │
│  (page-only, no repair)  │  + alerting           │                         │
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

### Protect Health Checker

`ProtectHealthChecker` detects both UniFi Protect failure modes: the silent websocket freeze (sensors stay available but stop changing — zero log errors) and the hard integration outage (UNVR down/auth failure — everything flips `unavailable`). Four checks: **Sensor Discovery** finds all Protect event sensors via the entity registry (`integration_entities` template, with a `motion_entities` config override) and classifies devices into cameras vs USL entry sensors; **Sensor Availability** pages within `availability_grace_s` (15m) when essentially all sensors are unavailable — the fast path a staleness clock can't provide, since the unavailable transition itself refreshes `last_changed` — while warnings fire only for fully-dark devices witnessed alive this app-lifetime within a 24h window (disabled channels and intentionally-unplugged cameras stay quiet); **Camera Events** requires the newest `last_changed` across available camera sensors to be younger than `stale_after_s`, measured in *active-hours* seconds so overnight quiet never accumulates staleness; **Entry Sensors** tracks the USL group's availability without a freshness threshold (quiet doors never page). Once frozen, the check stays critical until a *genuine* event arrives — one strictly newer than the detection-time baseline — so the post-reload re-registration timestamps cannot fake a recovery. Auto-repair reloads the loaded Protect config entry (discovered at runtime via `HaAdminClient`, never hardcoded; max one reload per `reload_cooldown_s`), then waits for a real event past the settle window before declaring success.

### ImageGen Health Checker

`ImageGenHealthChecker` watches the ComfyUI image-generation service by polling `GET /prompt` (`exec_info.queue_remaining`) via `ComfyUIStatusClient`. Two checks: **API Reachable** is a warning while the endpoint is unreachable, escalating to critical after `unreachable_after_s`; **Queue Progress** goes critical when the queue counter stays > 0 without any movement for `queue_stuck_after_s`. A wedged ComfyUI is the canonical symptom of the GPU falling off the PCI bus on the Proxmox host — only a host reboot fixes that — so this checker is **page-only**: no repair support. The 30-minute stuck threshold sits safely above the ~8.5-minute cold-start generation, and ComfyUI's in-memory queue resets to 0 on restart, which simply reads as healthy.

### Dependency System

Checkers can declare `dependencies` during registration to express that some of their checks depend on another checker being healthy. At publish time, the controller resolves these dependencies: if a dependency checker is unhealthy (`critical`/`degraded`) or missing entirely, the affected checks are overridden to `unknown` with detail `"dependency unavailable"` in the **published view only** -- the internal state is never modified. This prevents misleading alerts when a shared dependency (e.g., the Zigbee protocol stack) is down. A dependency that is merely `unknown` (registered but not reporting) does **not** mask its dependents: an unknown dependency raises no Alertmanager alert of its own, so masking would let a genuine dependent failure go completely silent.

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

### Alertmanager Bridge

When `alertmanager_url` is configured on the controller, checker health is mirrored into the cluster's Prometheus Alertmanager — one alert per unhealthy checker. The decision logic lives in `shared/alertmanager_bridge.py` (pure, no HTTP); `providers/alertmanager` does the actual `POST /api/v2/alerts`.

Severity mapping (the cluster's Alertmanager routes only `severity=critical` to Pushover):

| Checker status | Alert severity | Effect |
|----------------|----------------|--------|
| `critical` | `critical` | Pages the phone |
| `warning` / `degraded` | `warning` | Visible in Alertmanager/Grafana UI only |
| `ok` / `unknown` | — | Alert resolved (`unknown` is typically a dependency outage already alerted by the dependency's own checker) |

Mechanics:

- **Label-set identity** — `alertname` + `severity` + `source=appdaemon-health-check` + `checker=<id>` identify the alert. If the labels change (e.g. a warning escalates to critical), the old alert is resolved and a new one raised; if only the failing-check details change, annotations are refreshed in place.
- **Re-post keep-alive** — Alertmanager auto-resolves silent alerts after its `resolve_timeout` (5m in this cluster), so the controller re-posts all firing alerts every `alertmanager_repost_interval_s` (default 120s — must stay below the resolve_timeout).
- **Immediate resolve** — on recovery the bridge sends one final post with `endsAt=now`, producing an immediate `[RESOLVED]` notification instead of waiting out the resolve_timeout.
- **Fail-open** — Alertmanager being down never breaks health checking: failed posts are logged and retried by the next sync or re-post tick. An unsent raise stays in the active set so the re-post loop delivers it; an unsent resolve falls back to the resolve_timeout.
- **Repair context** — the alert description includes auto-repair progress ("auto-repair in progress" / "auto-repair FAILED: ...") alongside the failing checks.

Checkers opt in/out and name their alert via an `alerting` block in the registration payload (forwarded from their `apps.yaml` config):

```yaml
alerting:
  enabled: true                        # default true
  alertname: ProtectEventStreamFrozen  # default <CheckerName>Unhealthy (e.g. ImageGenUnhealthy)
```

## Dependencies

- `providers/ha_provisioner` — creates HA helpers and scripts on startup; `HaAdminClient` gives ProtectHealthChecker entity discovery and config-entry reload
- `providers/alertmanager` — posts/resolves alerts in the cluster Alertmanager (controller, when `alertmanager_url` is set)
- `providers/ai_providers/comfyui` — `ComfyUIStatusClient` queue polling (ImageGenHealthChecker)
- `aiohttp` — HTTP health checks (in `shared/check_utils.py`)

## Self-Provisioned Entities

| Entity | Type | Purpose |
|--------|------|---------|
| `input_datetime.appdaemon_heartbeat` | Helper | Controller heartbeat timestamp |
| `script.health_check_relay` | Script | Card → AppDaemon command relay |
| `sensor.health_check_status` | Virtual sensor | Aggregated health status (via `set_state`) |
| `input_boolean.spa_health_auto_repair` | Helper | Auto-repair toggle (provisioned by SpaHealthChecker) |
| `input_number.spa_health_auto_repair_delay` | Helper | Auto-repair delay in minutes (provisioned by SpaHealthChecker) |
| `input_boolean.protect_health_auto_repair` | Helper | Auto-repair toggle (provisioned by ProtectHealthChecker) |
| `input_number.protect_health_auto_repair_delay` | Helper | Auto-repair delay in minutes (provisioned by ProtectHealthChecker) |

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
  # Optional Alertmanager mirroring — omit alertmanager_url to disable
  alertmanager_url: http://kube-prometheus-stack-alertmanager.observability.svc.cluster.local:9093
  alertmanager_repost_interval_s: 120  # Firing-alert re-post period; must stay < Alertmanager resolve_timeout (5m)
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
│   ├── device_group_checker/
│   │   ├── __init__.py
│   │   ├── device_group_checker.py
│   │   ├── repairable_device_group_checker.py
│   │   └── README.md
│   ├── protect_health_checker/
│   │   ├── __init__.py
│   │   └── protect_health_checker.py
│   └── imagegen_health_checker/
│       ├── __init__.py
│       └── imagegen_health_checker.py
├── shared/
│   ├── __init__.py
│   ├── check_utils.py
│   └── alertmanager_bridge.py
├── cards/
│   ├── health-check-card.js
│   └── health-check-detail-card.js
└── README.md
```
