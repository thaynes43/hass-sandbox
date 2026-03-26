# Network Protocol Checker

Generic, config-driven health checker for network protocol stacks (Zigbee, Z-Wave, Thread, etc.). A single class instantiated multiple times with different configuration -- adding a new protocol requires only a new `apps.yaml` entry, no code changes.

## How It Works

Each instance performs up to three checks on a configurable interval. All checks are optional -- omit a config key to skip that check:

1. **Entity state** -- verify an HA entity matches an expected healthy state (e.g., Zigbee bridge connection is `"on"`)
2. **Radio ping** -- ICMP ping a PoE radio or coordinator hostname
3. **Web UI** -- HTTP GET a management web interface URL and verify it responds

Results are reported to the Health Check Controller via HA events. This app never calls `get_app()` -- communication is event-only.

## Checks

| Check | Config Key | Optional | Healthy When |
|-------|-----------|----------|-------------|
| Entity State | `entity_id` + `entity_healthy_state` | Yes | Entity state matches `entity_healthy_state` |
| Radio Ping | `radio_host` | Yes | Host responds to ICMP ping |
| Web UI | `web_ui_url` | Yes | HTTP GET returns a success status |

## Dependencies

- `shared/check_utils` -- `ping_check()` for ICMP pings, `http_check()` for HTTP GET checks

## Self-Provisioned Entities

None -- this checker has no HA entity requirements.

## Configuration Reference

```yaml
zigbee_health_checker:
  module: health_checks.checker_apps.network_protocol_checker.network_protocol_checker
  class: NetworkProtocolChecker
  disable: true
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

| Key | Required | Default | Description |
|-----|----------|---------|-------------|
| `checker_id` | No | `unknown` | Unique ID for this checker instance |
| `checker_name` | No | Same as `checker_id` | Display name on dashboard cards |
| `entity_id` | No | -- | HA entity to monitor (omit to skip entity check) |
| `entity_healthy_state` | No | `""` | Expected state value for the entity |
| `entity_check_name` | No | `Entity State` | Display name for the entity check |
| `radio_host` | No | -- | Hostname or IP to ICMP ping (omit to skip ping check) |
| `radio_check_name` | No | `Radio Ping` | Display name for the ping check |
| `web_ui_url` | No | -- | URL to HTTP GET (omit to skip web UI check) |
| `web_ui_check_name` | No | `Web UI` | Display name for the web UI check |
| `check_interval_s` | No | `180` | Check frequency in seconds |

YAML bool coercion is handled: if `entity_healthy_state` is coerced from `"on"` to `True`, it is reversed back to `"on"`.
