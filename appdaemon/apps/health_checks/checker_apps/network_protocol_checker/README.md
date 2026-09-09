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

## Repair (`RepairableNetworkProtocolChecker`)

`RepairableNetworkProtocolChecker` (same package, `repairable_network_protocol_checker.py`) subclasses `NetworkProtocolChecker` and adds rate-limited auto-repair for radios reached over a **TCP serial bridge** -- the TubesZB ESP32 boards running ESPHome. Used by the `zwave` instance; the Zigbee board can adopt it later by pointing `repair_button` at its own ESPHome restart button.

### The incident this fixes

Z-Wave runs as `zwave-js-ui -> tcp://tubeszb-zwave01:6638`. The ESP32 stream server accepts exactly one client and never notices when that client dies, so a dropped TCP session leaves it holding a dead socket and refusing every reconnect. Nothing looks broken -- the radio answers ICMP, the ESPHome "serial connected" sensor stays `on`, the web UI loads -- while every Z-Wave entity in HA sits `unavailable`. That ran for 4h20m on 2026-09-09 and never paged: with two of the three checks passing, `apply_cross_check` downgraded the lone integration failure to `warning`. Restarting the zwave-js **pod** does not help (our side of the old socket sits in FIN_WAIT2). The verified remedy is an ESPHome **software** restart of the board, which drops the stale client so the waiting reconnect loop succeeds within seconds.

### Safety

The board must **never** be power-cycled -- PoE port cycling or any other hard power path. A previous TubesZB dongle was destroyed that way. The only action this class ever takes is a single `button/press` on the ESPHome software-restart button, and every press passes these gates, all enforced in code:

1. **Stale-client signature only** -- the integration entity must be unhealthy *while* the radio still answers ping (`repair_requires_radio_ping`). If ping is down the board is offline and a software restart cannot help, so no restart is attempted -- but the integration check is forced to `critical` so it pages, because the same cross-check downgrade masks that case too. `repair_serial_connected_entity` adds a second confirmation that the board still believes it has a client.
2. **Dwell** -- unhealthy for the full auto-repair delay (default 5 min) before the first action. The dwell clock restarts from scratch on every AppDaemon start, so a deploy landing mid-outage can never trigger an immediate restart.
3. **Minimum interval** between restarts (`repair_min_interval_s`, default 15 min).
4. **Rolling 24h cap** (`repair_max_per_24h`, default 3). Attempt timestamps are persisted to `input_text.<checker_id>_health_repair_attempts` and re-seeded on startup, so the ladder survives an AppDaemon restart instead of resetting. They are *not* read back from `sensor.health_check_status`: the controller publishes that sensor once at its own startup with `checkers: {}`, and `set_state` replaces the attribute wholesale, so on a whole-pod restart the controller usually wins the race and the persisted attempts are gone before any checker can read them. The sensor is still consulted as a fallback, which only matters before the helper exists.
5. **Quiet period** after every action (`repair_quiet_period_s`, default 3 min).
6. **Exactly one action per evaluation** -- the state machine moves to `in_progress` before anything is pressed, and the attempt is recorded *before* the press, so a crash mid-repair still counts against the caps.

Manual repair from the detail card skips the dwell but **not** the rate limits.

The partial-failure cross-check is reversed -- integration forced back to `critical` so Alertmanager pages -- in exactly two cases: the 24h cap is spent (auto-repair has given up), or the integration is down while the radio is unreachable (the board is off the network). Both are states no automation can fix. Everything else keeps the normal `warning` downgrade, and Alertmanager's 5-minute for-duration still applies, so a transient miss cannot page.

Both escalations sit **above** the serial-sensor guard and the auto-repair toggle in the guard order, deliberately. The toggle governs whether we press a button, not whether a total outage is allowed to page; and after three restarts that didn't take, the ESPHome serial sensor may well read `off` -- which is not evidence the outage got smaller. Letting either one short-circuit the escalation would put Z-Wave back on a silent `warning` while it is still fully down.

## Dependencies

- `shared/check_utils` -- `ping_check()` for ICMP pings, `http_check()` for HTTP GET checks; `apply_cross_check()` for the partial-failure downgrade
- `providers/ha_provisioner` -- auto-repair helper provisioning (`RepairableNetworkProtocolChecker` only)

## Self-Provisioned Entities

`NetworkProtocolChecker` provisions nothing. `RepairableNetworkProtocolChecker` provisions the three helpers below, named from `checker_id` (`zwave` today), and needs `ha_url` / `ha_token_env` to do so:

| Entity | Type | Purpose |
|--------|------|---------|
| `input_boolean.zwave_health_auto_repair` | Helper | Auto-repair toggle (default ON via `auto_repair_enabled_default`, applied on creation only) |
| `input_number.zwave_health_auto_repair_delay` | Helper | Dwell before the first restart, in minutes (1-60, step 1, default 5) |
| `input_text.zwave_health_repair_attempts` | Helper | Rolling 24h restart log (compact JSON, max 255 chars) -- this is what makes the cap survive a restart |

## Relay Commands

`RepairableNetworkProtocolChecker` only, routed via `script.health_check_relay` -> `health_check_repair_zwave`:

| Command | Payload | Description |
|---------|---------|-------------|
| `start_repair` | `{"checker_id": "zwave"}` | Press the restart button now -- skips the dwell, still refused by the rate limits |
| `cancel_repair` | `{"checker_id": "zwave"}` | Stand down a pending restart. Unlike the shade gateway this checker re-arms every cycle, so cancelling restarts the dwell clock -- a real deferral of one full delay, not a one-shot dismissal. Does not refund rate-limit budget |
| `update_repair_config` | `{"checker_id": "zwave", "auto_repair_enabled": true, "auto_repair_delay_min": 5}` | Update auto-repair settings |

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

### Repair keys (`RepairableNetworkProtocolChecker` only)

Set `module` to `...network_protocol_checker.repairable_network_protocol_checker` and `class` to `RepairableNetworkProtocolChecker`, keep every key above, and add `ha_url` / `ha_token_env` plus the keys below. Live values: `apps-prod.yaml` -> `zwave_health_checker`.

| Key | Required | Default | Description |
|-----|----------|---------|-------------|
| `repair_button` | No | -- | ESPHome **software** restart button. Never a power/PoE switch. Omit to disable repair |
| `repair_requires_radio_ping` | No | `true` | Only repair while the radio still answers ping |
| `repair_serial_connected_entity` | No | -- | Optional ESPHome "serial connected" sensor for a second signature check |
| `repair_serial_connected_state` | No | `on` | State of that sensor meaning "the board still has a client" |
| `repair_min_interval_s` | No | `900` | Minimum gap between restarts |
| `repair_max_per_24h` | No | `3` | Rolling 24h restart cap; once spent, stop repairing and force `critical` |
| `repair_quiet_period_s` | No | `180` | Settle time after an action before re-evaluating |
| `repair_recovery_wait_s` | No | `300` | How long to poll for recovery after a press |
| `auto_repair_enabled_default` | No | `false` | Initial toggle value, applied on helper creation only |
| `auto_repair_delay_min_default` | No | `5` | Initial dwell in minutes before the first restart |
