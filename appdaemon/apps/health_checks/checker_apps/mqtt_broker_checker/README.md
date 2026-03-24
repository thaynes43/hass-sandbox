# MQTT Broker Checker

Verifies AppDaemon can communicate with the MQTT broker by performing a publish/subscribe ping test. Publishes a message with a unique nonce to a test topic and confirms the round-trip within a configurable timeout.

## How It Works

1. On startup, subscribes to the ping topic via AppDaemon's MQTT plugin
2. Periodically publishes a JSON ping message containing a nonce and timestamp
3. Listens for the ping to come back on the same topic
4. If received within timeout, reports **ok** with round-trip latency
5. If not received, reports **critical** with timeout detail

## Checks

| Check | Method | Healthy When |
|-------|--------|-------------|
| Broker Connectivity | MQTT publish/subscribe ping | Ping round-trip completes within `ping_timeout_s` |

## Dependencies

- AppDaemon MQTT plugin must be configured (see Manual Setup below)

## Self-Provisioned Entities

None -- this checker has no HA entity requirements.

## Configuration Reference

```yaml
mqtt_broker_checker:
  module: health_checks.checker_apps.mqtt_broker_checker.mqtt_broker_checker
  class: MqttBrokerChecker
  disable: true
  checker_id: mqtt_broker
  checker_name: MQTT Broker
  check_interval_s: 120
  ping_timeout_s: 10
  mqtt_namespace: mqtt
  ping_topic: appdaemon/health_check/ping
```

| Key | Required | Default | Description |
|-----|----------|---------|-------------|
| `checker_id` | No | `mqtt_broker` | Unique ID for this checker |
| `checker_name` | No | `MQTT Broker` | Display name on dashboard |
| `check_interval_s` | No | `120` | How often to run the ping test (seconds) |
| `ping_timeout_s` | No | `10` | Max time to wait for ping response (seconds) |
| `mqtt_namespace` | No | `mqtt` | AppDaemon MQTT plugin namespace |
| `ping_topic` | No | `appdaemon/health_check/ping` | MQTT topic for ping/pong messages |

## Manual Setup

The AppDaemon MQTT plugin must be configured in `appdaemon.yaml`:

```yaml
plugins:
  MQTT:
    type: mqtt
    namespace: mqtt
    client_host: !secret mqtt_host
    client_port: 1883
    client_user: !secret mqtt_user
    client_password: !secret mqtt_password
```

Add the corresponding secrets to `secrets.yaml` (gitignored).
