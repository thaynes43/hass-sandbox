# MQTT Device Checker

Monitors Zigbee2MQTT devices via dual HA entity state and MQTT message tracking. Discovers entities dynamically using configurable regex patterns, then cross-references HA availability with MQTT message timestamps to distinguish device failures from integration/bridge issues.

## How It Works

1. On startup, discovers HA entities matching configured include/exclude regex patterns
2. Subscribes to all MQTT messages and tracks the timestamp of the last message received per `zigbee2mqtt/<device>` topic (any message, not just linkquality)
3. Skips retained messages delivered during initial subscribe (5-second grace period)
4. Periodically checks each discovered entity for both HA state and MQTT freshness
5. Cross-check logic is symmetric: if only one check fails, it is downgraded to **warning**; if both fail, both stay **critical**
6. MQTT checks declare a dependency on a protocol checker (e.g. Zigbee) so they show as **unknown** when the protocol itself is down

## Checks

For each discovered entity, two checks are registered:

| Check | Method | Healthy When |
|-------|--------|-------------|
| `{name} State` | `get_state(entity_id)` | State is not `None`, `unavailable`, or `unknown` |
| `{name} MQTT` | MQTT message timestamp tracking | Any MQTT message received from device within `mqtt_stale_s` |

### Cross-Check Warning Logic

| State Check | MQTT Check | Result |
|-------------|------------|--------|
| ok | ok | Both ok |
| ok | critical | State ok, MQTT **warning** (HA state ok) |
| critical | ok | State **warning** (MQTT ok), MQTT ok |
| critical | critical | Both critical |

## Entity Discovery

Entities are discovered at startup by matching `entity_id` values against regex patterns:

```yaml
entity_patterns:
  - include: ".*basement.*inovelli.*"       # Match Inovelli switches in basement
  - include: "light\\.basement.*hue.*\\d+$"  # Match Hue bulbs in basement
  - exclude: ".*night_light.*"               # Skip night light entities
```

All matched entities are logged on startup for validation.

## Dependencies

- Protocol checker (e.g. `zigbee`) -- MQTT checks depend on protocol health via `protocol_dependency_id`
- AppDaemon MQTT plugin must be configured with `client_topics` including `zigbee2mqtt/#`

## Self-Provisioned Entities

None -- this checker has no HA entity requirements.

## Configuration Reference

```yaml
basement_lights_checker:
  module: health_checks.checker_apps.mqtt_device_checker.mqtt_device_checker
  class: MqttDeviceChecker
  disable: true
  checker_id: basement_lights
  checker_name: Basement Lights
  check_interval_s: 300
  mqtt_stale_s: 21600
  mqtt_namespace: mqtt
  protocol_dependency_id: zigbee
  entity_patterns:
    - include: "(light|switch)\\.basement.*inovelli.*"
    - include: "light\\.basement.*hue.*\\d+$"
    - exclude: ".*night_light.*"
```

| Key | Required | Default | Description |
|-----|----------|---------|-------------|
| `checker_id` | No | `mqtt_devices` | Unique ID for this checker instance |
| `checker_name` | No | `checker_id` | Display name on dashboard |
| `check_interval_s` | No | `300` | How often to run checks (seconds) |
| `mqtt_namespace` | No | `mqtt` | AppDaemon MQTT plugin namespace |
| `mqtt_topic_prefix` | No | `zigbee2mqtt` | MQTT topic prefix for device messages |
| `mqtt_stale_s` | No | `21600` | Seconds before MQTT data is considered stale (default 6 hours) |
| `protocol_dependency_id` | No | `""` (none) | Checker ID of a protocol checker (e.g. `zigbee`) — MQTT checks depend on this |
| `entity_patterns` | Yes | `[]` | List of include/exclude regex patterns for entity discovery |

### Entity Pattern Config

Each entry in `entity_patterns` has exactly one of:

| Key | Description |
|-----|-------------|
| `include` | Regex pattern -- entities matching this are included |
| `exclude` | Regex pattern -- entities matching this are excluded (applied after includes) |
