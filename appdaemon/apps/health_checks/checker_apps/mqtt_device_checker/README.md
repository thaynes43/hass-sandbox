# MQTT Device Checker

Monitors Zigbee2MQTT devices via dual HA entity state and MQTT linkquality checks. Discovers entities dynamically using configurable regex patterns, then cross-references HA availability with MQTT linkquality messages to distinguish device failures from integration/bridge issues.

## How It Works

1. On startup, discovers HA entities matching configured include/exclude regex patterns
2. Subscribes to all MQTT messages and tracks `linkquality` per Zigbee2MQTT device
3. Periodically checks each discovered entity for both HA state and MQTT freshness
4. Cross-check logic: if HA entity fails but MQTT linkquality is fresh, the HA check is downgraded to **warning** (likely an integration issue, not a device failure)
5. MQTT checks declare a dependency on the MQTT Broker checker so they show as **unknown** when the broker itself is down

## Checks

For each discovered entity, two checks are registered:

| Check | Method | Healthy When |
|-------|--------|-------------|
| `{name} HA State` | `get_state(entity_id)` | State is not `None`, `unavailable`, or `unknown` |
| `{name} MQTT` | Zigbee2MQTT linkquality tracking | `linkquality` message received within `mqtt_stale_s` |

### Cross-Check Warning Logic

| HA State | MQTT Status | Result |
|----------|-------------|--------|
| ok | ok | Both ok |
| ok | unknown/critical | HA ok, MQTT as reported |
| critical | ok | HA **warning** (not critical), MQTT ok |
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

- MQTT Broker Checker (`mqtt_broker`) -- MQTT checks depend on broker health
- AppDaemon MQTT plugin must be configured

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
  mqtt_namespace: mqtt
  mqtt_stale_s: 600
  broker_dependency_id: mqtt_broker
  entity_patterns:
    - include: ".*basement.*inovelli.*"
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
| `mqtt_stale_s` | No | `600` | Seconds before MQTT data is considered stale |
| `broker_dependency_id` | No | `mqtt_broker` | Checker ID of the MQTT broker checker (for dependency) |
| `entity_patterns` | Yes | `[]` | List of include/exclude regex patterns for entity discovery |

### Entity Pattern Config

Each entry in `entity_patterns` has exactly one of:

| Key | Description |
|-----|-------------|
| `include` | Regex pattern -- entities matching this are included |
| `exclude` | Regex pattern -- entities matching this are excluded (applied after includes) |
