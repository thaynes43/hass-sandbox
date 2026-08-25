# Fan Health Checker

Monitors Modern Forms ceiling fans connected via the Modern Forms integration. All fans are reported as a single checker to keep the dashboard compact. Each fan is checked for entity availability and network reachability.

## Checks (2 per fan)

| Check | Method | Healthy When |
|-------|--------|-------------|
| `{name} State` | `get_state(entity_id)` | Not `unavailable`/`unknown`/`None` |
| `{name} Ping` | ICMP ping to fan IP (3 attempts) | Any attempt responds within timeout |

The ping check retries up to 3 times per cycle — Modern Forms fans are ESP
devices in Wi-Fi power-save and routinely drop a single ping, so one miss never
counts as a failure.

## Repair

Supports per-fan repair via a configurable HA script (default: `script.zen32_hard_reset`). The script power-cycles the fan's zen32 scene controller and optionally toggles the fan entity if still unavailable.

### Auto-Repair Trigger (per-fan grace)

Auto-repair only ever fires for a fan whose **entity is down** (State check
critical — `unavailable`/`unknown`). A ping-only miss while HA can still reach
the fan is a transient warning and never justifies power-cycling a possibly
running fan.

Each fan accrues its **own** unhealthy timer toward the auto-repair delay. One
long-failed fan can never fast-track an immediate repair of another fan that
only just went down — every fan serves the full configured delay from the
moment *it* went unhealthy. Timers update on every check cycle, including
while a repair is running, so a fan that recovers mid-repair never keeps a
stale timer.

**Systemic outage guard**: if *every* fan is entity-down at once, that points
at HA, the integration, or the Wi-Fi network — not at individual fans.
Auto-repair is suspended (timers cleared, WARNING logged) until the signature
clears; the fans that are still down afterwards then serve a fresh grace
period.

**Busy repair script**: the repair script is `mode: single` with a long
cooldown tail — a `turn_on` while it runs is silently dropped by HA. The
checker waits for the script to be free (up to ~11 min) before invoking it;
if it stays busy the attempt is marked failed with detail "Repair script
busy" instead of pretending a power-cycle happened.

### Per-Fan Repair Tracking

Each fan independently tracks its own repair state. When auto-repair triggers:

1. Find the entity-down fan with repair status `idle` that has been unhealthy
   the longest past its delay
2. Call the repair script with that fan's zen32 entities
3. Poll for recovery every ~5s for up to `repair_recovery_wait_s`
4. On success, move to the next failing fan on the next check cycle
5. On timeout, mark that fan `failed` and move to the next
6. Each fan gets ONE auto-repair attempt — no auto-retry after failure
7. A fan's `failed` state resets to `idle` if its checks go green naturally

### Manual Repair

The "Repair" button resets all `failed` fan states and repairs all currently **entity-down** fans sequentially. The same repair-worthiness rule as auto-repair applies: a fan that is reachable by HA but missing pings never gets power-cycled, even manually.

### State Restore After Repair

The repair script cuts mains power to the fan, which **reboots the Modern Forms controller back to its hardware default** — the physical fan can come back off or at the wrong speed. Because the Modern Forms integration keeps serving stale last-known state while the fan's Wi-Fi is down, HA never sends a corrective command, so the physical fan ends up out of sync with what the user wanted.

To fix this, the checker keeps a **last-known-good state cache** and replays it after every successful repair:

1. **Capture (event-based)** — a `listen_state(..., attribute="all")` listener caches each fan's `state` (on/off), `percentage` (speed), and `direction` (forward/reverse) whenever it reports a good state. Caching is **frozen while that fan is being repaired** so the power-cycle's transient states never overwrite the value we need.
2. **Seed on startup** — the cache is seeded fresh from current HA state on startup, so state a fan changed to while AppDaemon was down is picked up. A fan that is `unavailable` at startup is left unseeded until the listener sees it report a good state.
3. **Restore** — once a repair recovers the fan, the cached values are pushed back to the device (`fan.turn_on` → `fan.set_percentage` → `fan.set_direction`, or `fan.turn_off`), forcing the physical fan to match the intended state.

Restore is on by default; set `restore_state_enabled: false` to disable it. If no good state has ever been cached for a fan (e.g. it was unavailable across an AppDaemon restart and then repaired before reporting), restore is skipped and logged.

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
  checker_name: Ceiling Fans                          # Display name on cards
  check_interval_s: 180                              # Check frequency (seconds)
  repair_recovery_wait_s: 300                        # Max wait for recovery after repair
  auto_repair_enabled_default: false                 # Default auto-repair toggle
  auto_repair_delay_min_default: 5                   # Default minutes before auto-repair
  restore_state_enabled: true                        # Re-apply on/off + speed + direction after repair
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
