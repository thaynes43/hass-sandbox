# System Health Monitoring

Proactive infrastructure monitoring for every layer of the smart home — from network radios to ceiling fans to the hot tub.

<!-- TODO: Add screenshot of wall display showing health check compact bar and detail popup -->

## Overview

A smart home with dozens of devices across multiple protocols will inevitably have things go wrong: a Zigbee coordinator loses its connection, a ceiling fan drops off WiFi, or the hot tub controller enters a zombie state where it *looks* connected but ignores commands. The health monitoring system catches these problems early and, where possible, fixes them automatically.

The system is built around two ideas:

1. **Decoupled checkers** — each checker app monitors one slice of infrastructure and reports status via Home Assistant events. The controller never needs to know *how* a check works.
2. **A single aggregated sensor** — the controller combines all checker reports into `sensor.health_check_status`, which custom Lovelace cards read to render the dashboard.

This architecture means adding a new health check is often just a YAML config change — no code required.

## What Gets Monitored

| Category | Examples | Checker Type |
|----------|----------|-------------|
| Network protocols | Zigbee bridge, Z-Wave controller, coordinator ping, web UI | `NetworkProtocolChecker` |
| MQTT infrastructure | Broker publish/subscribe round-trip | `MqttBrokerChecker` |
| MQTT devices | Zigbee2MQTT device availability + linkquality | `MqttDeviceChecker` |
| Environmental sensors | Temperature and humidity with threshold alerts | `TempHumidityChecker` |
| Smart devices | Printers, Vestaboards, any entity + optional ping | `BasicDeviceChecker` |
| Device groups | Cielo AC controllers, TP-Link plugs — related devices as one unit | `DeviceGroupChecker` |
| Ceiling fans | Modern Forms fans with per-fan repair | `FanHealthChecker` |
| Hot tub / spa | Gecko integration health, staleness detection, power-cycle repair | `SpaHealthChecker` |
| AppDaemon itself | Heartbeat timestamp — the card detects staleness client-side | Controller heartbeat |

## How It Works

### Event-Driven Architecture

Checker apps and the controller communicate exclusively through Home Assistant events — never through direct Python references. This means:

- The controller can run in production Kubernetes while a new checker is being developed on a laptop
- Checkers can restart independently without affecting others
- Adding a new checker type requires no changes to the controller

```
Checker Apps (Zigbee, Z-Wave, MQTT, Spa, Fans, ...)
  │
  │  register_checker / report_status events
  ▼
HealthCheckController
  │
  │  set_state()
  ▼
sensor.health_check_status
  │
  ├──▶ health-check-card (compact bar on wall displays)
  │         │
  │         │ tap to expand
  │         ▼
  └──▶ health-check-detail-card (full breakdown + repair controls)
              │
              │ callService("script", "health_check_relay", ...)
              ▼
         HA event bus → Controller routes commands back to checkers
```

### Status Levels

Each individual check reports one of five statuses. The controller rolls these up to a per-checker status (worst wins) and then to an overall system status.

| Status | Meaning | Card Color |
|--------|---------|------------|
| **ok** | Everything healthy | Green |
| **warning** | Degraded but functional | Yellow |
| **degraded** | Significant issues | Orange |
| **critical** | Service down or unreachable | Red |
| **unknown** | Not yet checked or dependency unavailable | Grey |

### Dependency System

Checkers can declare dependencies on other checkers. When a dependency is unhealthy, the controller automatically marks affected checks as `unknown` instead of reporting misleading failures.

For example, MQTT device checks depend on the MQTT broker checker. If the broker itself is down, individual device checks show as `unknown` rather than `critical` — because the real problem is the broker, not the devices.

```yaml
# MQTT device checker declares broker dependency
broker_dependency_id: mqtt_broker
```

The controller resolves these dependencies at publish time without modifying the underlying check data, so when the broker recovers, device checks immediately resume reporting their true status.

### Heartbeat

The controller updates an `input_datetime` helper every 60 seconds. The dashboard card compares this timestamp against the current time client-side. If the heartbeat is stale by more than 3 minutes, the card shows AppDaemon as offline — no HA automation or template sensor needed.

### Alert History

Every status transition is recorded with timestamps and duration tracking. The detail card shows a scrollable history of recent alerts, making it easy to see patterns like "the Zigbee bridge drops every night at 2 AM" without digging through logs.

Alerts are pruned automatically — both by age (default 36 hours) and by count (default 50 per checker).

## Auto-Repair

Some checkers support automatic repair, typically via smart switch power cycling. The repair system follows strict safety rules:

- **Only sustained failures trigger repair** — a brief blip does not cause a power cycle
- **Configurable delay** — the problem must persist for a configurable number of minutes before repair begins
- **Auto-clear on recovery** — after a failed repair, the `failed` state automatically resets to `idle` when all checks recover. No auto-retry while checks are still unhealthy.
- **Unknown does not trigger repair** — if AppDaemon itself is restarting, repair actions are suppressed

The repair state machine:

```
idle → pending → in_progress → success → idle (checks stay healthy)
                             → failed  → idle (checks recover naturally)
```

Repair-capable checkers provision their own HA helpers for configuration:

- **Auto-repair toggle** — `input_boolean` to enable/disable
- **Delay setting** — `input_number` for minutes before repair triggers

These settings persist across AppDaemon restarts because they live in Home Assistant.

!!! info "Per-device repair"
    The fan checker and repairable device group checker support **per-device** repair. Each device tracks its own repair state independently, and devices are repaired sequentially — one at a time — to avoid overwhelming the electrical system.

## Dashboard Experience

### Compact Health Bar

The compact card is designed for wall-mounted displays where screen space is limited. It shows a single row of colored status indicators — one per checker — with the overall system status.

- Tap the bar to open the detail popup
- Green bar with no indicators means everything is healthy
- Warning and critical states are immediately visible through color

### Detail Popup

The detail card provides a full breakdown:

- **Per-checker sections** with individual check results and status icons
- **Last check timestamps** showing when each checker last ran
- **Alert history** with transition details (e.g., "Bridge Connection: ok -> critical")
- **Force Re-check** button to trigger all checkers immediately
- **Clear History** button to dismiss resolved alerts
- **Repair controls** for repair-capable checkers (manual trigger, auto-repair toggle, delay setting)

## Extending the System

### Adding a New Protocol Check (Config Only)

The `NetworkProtocolChecker` supports any combination of entity state, ICMP ping, and HTTP checks. Adding a new protocol is a single YAML entry:

```yaml
thread_health_checker:
  module: health_checks.checker_apps.network_protocol_checker.network_protocol_checker
  class: NetworkProtocolChecker
  checker_id: thread
  checker_name: Thread
  entity_id: binary_sensor.thread_border_router_state
  entity_healthy_state: "on"
  entity_check_name: Border Router
  radio_host: thread-br.local
  radio_check_name: Radio Ping
  check_interval_s: 180
```

### Adding a New Device Check (Config Only)

For any device that has an HA entity and optionally responds to ping:

```yaml
nas_health_checker:
  module: health_checks.checker_apps.device_checker.device_checker
  class: BasicDeviceChecker
  checker_id: nas
  checker_name: NAS
  ping_host: "192.168.0.10"
  ping_check_name: Ping
  check_interval_s: 300
  entities:
    - entity_id: sensor.synology_status
      healthy_state: normal
      name: Status
```

### Adding Environmental Monitoring (Config Only)

The `TempHumidityChecker` supports configurable warning and critical thresholds:

```yaml
server_room_temp_checker:
  module: health_checks.checker_apps.temp_humidity_checker.temp_humidity_checker
  class: TempHumidityChecker
  checker_id: server_room_temp
  checker_name: Server Room
  check_interval_s: 120
  temp_low_warning: 60
  temp_high_warning: 80
  temp_low_critical: 55
  temp_high_critical: 85
  sensors:
    - entity_id: sensor.server_room_temperature
      name: Rack Temperature
      type: temperature
```

### Writing a Custom Checker App

For monitoring that goes beyond entity state and ping — such as the MQTT round-trip test or the spa staleness detector — you can write a custom checker app. The minimum contract is:

1. Listen for `health_check_controller_ready` and register with check names
2. Periodically run checks and report results via `health_check_command` events
3. Listen for `health_check_recheck` to support on-demand re-checks

The shared `check_utils` module provides reusable building blocks like `ping_check()` and `http_check()`.

## Current Checkers

| Checker | Type | What It Monitors | Repair |
|---------|------|-----------------|--------|
| Zigbee | `NetworkProtocolChecker` | Bridge connection, coordinator ping, web UI | No |
| Z-Wave | `NetworkProtocolChecker` | Controller state, radio ping, web UI | No |
| MQTT Broker | `MqttBrokerChecker` | Publish/subscribe round-trip latency | No |
| Basement Lights | `MqttDeviceChecker` | Zigbee2MQTT device HA state + linkquality | No |
| Cigar Room Humidity | `TempHumidityChecker` | Humidity sensors with threshold alerts | No |
| Spa | `SpaHealthChecker` | Gateway ping, connections, thermostat staleness | Yes — power cycle |
| Fans | `FanHealthChecker` | Entity state + IP ping per fan | Yes — per-fan zen32 reset |
| Printer | `RepairableDeviceChecker` | Entity state + IP ping | Yes — power cycle |
| Vestaboard | `BasicDeviceChecker` | Controller + configuration status | No |
| Cielo Home | `DeviceGroupChecker` | AC controller status + IP per room | No |

## Related

- App README: `appdaemon/apps/health_checks/README.md`
- Architecture: [Overview](../architecture/overview.md)
- Custom cards: `appdaemon/apps/health_checks/cards/`
