# Fan Health Checker

Monitors Modern Forms ceiling fans connected via the Modern Forms integration. All fans are reported as a single checker to keep the dashboard compact. Each fan is checked for entity availability and network reachability.

## Checks (2 per fan)

| Check | Method | Healthy When |
|-------|--------|-------------|
| `{name} State` | `get_state(entity_id)` | Not `unavailable`/`unknown`/`None` |
| `{name} Ping` | ICMP ping to fan IP | Responds within timeout |

## Repair

Supports per-fan repair via a configurable HA script (default: `script.zen32_hard_reset`). The script power-cycles the fan's zen32 scene controller and optionally toggles the fan entity if still unavailable.

### Per-Fan Repair Tracking

Each fan independently tracks its own repair state. When auto-repair triggers:

1. Find the first failing fan with repair status `idle`
2. Call the repair script with that fan's zen32 entities
3. Poll for recovery every ~5s for up to `repair_recovery_wait_s`
4. On success, move to the next failing fan on the next check cycle
5. On timeout, mark that fan `failed` and move to the next
6. Each fan gets ONE auto-repair attempt — no auto-retry after failure
7. A fan's `failed` state resets to `idle` if its checks go green naturally

### Manual Repair

The "Repair" button resets all `failed` fan states and repairs all currently-failing fans sequentially.

### Safety Rules

- Each fan uses a different zen32 controller — repairs run independently
- Only `unavailable`/`unknown` states trigger repair (a fan that is `off` is healthy)
- After failure, stays `failed` — no auto-retry

## Self-Provisioned Entities

| Entity | Purpose |
|--------|---------|
| `input_boolean.fans_health_auto_repair` | Auto-repair toggle |
| `input_number.fans_health_auto_repair_delay` | Minutes before auto-repair (1-60) |

## Configuration Reference

```yaml
fan_health_checker:
  module: health_checks.checker_apps.fan_health_checker.fan_health_checker
  class: FanHealthChecker
  ha_url: !secret ha_url
  ha_token_env: TOKEN
  checker_id: fans                                   # Unique ID
  checker_name: Fans                                 # Display name on cards
  check_interval_s: 180                              # Check frequency (seconds)
  repair_recovery_wait_s: 300                        # Max wait for recovery after repair
  auto_repair_enabled_default: false                 # Default auto-repair toggle
  auto_repair_delay_min_default: 5                   # Default minutes before auto-repair
  repair_script: script.zen32_hard_reset             # HA script entity for repair
  fans:
    - name: Pink Room                                # Display name
      entity_id: fan.pink_room_fan_fan               # Fan entity to monitor
      ip: "192.168.50.112"                           # IP to ping
      power_switch: switch.upstairs_pink_room_scene_controller
      relay_control: select.upstairs_pink_room_scene_controller_relay_control
      scene_control: select.upstairs_pink_room_scene_controller_scene_control_relay
```

## Dependencies

- `providers/ha_provisioner` — creates HA helpers on startup
- `shared/check_utils` — `ping_check()` for fan IP pings
- `script.zen32_hard_reset` — HA script for fan power cycle repair
