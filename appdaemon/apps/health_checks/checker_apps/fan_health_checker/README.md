# Fan Health Checker

Monitors Modern Forms ceiling fans — Espressif **Wi-Fi** devices, reached over the LAN by the Modern Forms integration — as a single checker, to keep the dashboard compact. Each fan is checked for entity availability and network reachability.

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

### Access-Point Awareness (these are Wi-Fi fans)

Modern Forms fans are **Espressif Wi-Fi devices** — they associate with a
UniFi access point like any other wireless client. The ZEN32 scene
controller is only the *actuator* the repair script uses to cut mains power;
it is Z-Wave, the fan is not. Nobody (human or LLM) triaging a fan outage
should reach for the Z-Wave stack.

Each fan may therefore declare the AP it usually holds:

| Key | Meaning |
|-----|---------|
| `ap_status_entity` | The HA UniFi integration's state sensor for that AP (e.g. `sensor.guest_room_u7_pro_state`) |
| `ap_name` | Friendly AP name for log and alert text; derived from the entity id when omitted |

Fans **roam**, so this is the AP each fan usually holds, not a fixed binding —
confirm the live one with `unpoller_client_rssi_db{name="MF Fan <Room>"}` (label
`ap_name`) before trusting an AP verdict. See
`agent-docs/shepherd-runbooks/fans.md` for triage, including the 2.4 GHz airtime
signature behind sub-minute flapping.

The AP state is refreshed once per check cycle. States `disconnected`,
`not_home`, and `off` count as **AP down**; anything else — including
`unavailable`/`unknown`, which usually means the UniFi *integration* is
broken rather than the AP — is treated as AP-state-unknown and gates
nothing.

While a fan's AP is down, that fan is never repair-worthy: power-cycling a
fan cannot fix a network fault. The repair is held, and with it the fan's
grace timer and backoff retry (its stale retry keeps sliding forward), so
the ladder does not climb through an outage the fan was never responsible
for. The hold and its release are each logged once, on the transition.

The State check's alert detail carries a suffix naming the AP, so the page
itself says whose fault it is:

| AP state | Alert-detail suffix |
|----------|---------------------|
| Down | `(Wi-Fi fan; AP X is disconnected — fan offline expected, power-cycle held until the AP recovers)` |
| Unreadable | `(Wi-Fi fan; AP X: state unknown)` |
| Up | `(Wi-Fi fan; AP X: connected — fan itself unreachable)` |

A fan with no `ap_status_entity` configured still gets a bare `(Wi-Fi fan)`
suffix, and is repaired exactly as before.

### Per-Fan Repair Tracking

Each fan independently tracks its own repair state. When auto-repair triggers:

1. Find the entity-down fan whose repair is due soonest (first attempts are
   due at their grace deadline; failed fans at their scheduled backoff retry)
2. Call the repair script with that fan's zen32 entities
3. Poll for recovery every ~5s for up to `repair_recovery_wait_s`
4. On success, move to the next failing fan on the next check cycle
5. On timeout, mark that fan `failed` and schedule its next retry
6. **CrashLoopBackOff retries** — a failed repair never ends the episode:
   the n-th failure schedules retry n+1 after `delay × 2^(n-1)` minutes
   (5m → 10m → 20m → …), capped at `repair_backoff_max_min` (default 6h).
   While a failed fan is *not* entity-down, its scheduled retry keeps
   sliding to at least one delay out — a stale schedule can never fire the
   instant the entity blips down again.
7. **Sustained-recovery reset** — the attempt counter resets only after the
   fan has stayed healthy for `repair_backoff_reset_min` (default 30 min),
   or on a manual repair (a human-declared fresh start). A bare recovery no
   longer resets anything: a `success` or `failed` fan whose checks go green
   just starts a recovery clock, keeping its state and attempt count, and
   only when that clock runs out does it drop to `idle` with `attempts=0`.
   **A relapse inside the window resumes the ladder**, counting the false
   success as a failed attempt — so the next power-cycle waits out the
   corresponding backoff instead of firing on the next check cycle. This is
   exactly the 2026-08-31 storm: every false recovery reset the ladder to
   attempt 1, which bought ~11 power-cycles of one flapping fan in five
   hours.
8. **The ladder is persisted** to `input_text.<checker_id>_health_repair_ladder`
   as compact JSON — `{fan: [attempts, next_retry_iso|null, "failed"|"success"]}`,
   lowest ladders dropped first if it would exceed the helper's 255-char
   limit — and seeded back on startup, because an HA restart or plugin
   reconnect re-initialises every AppDaemon app mid-incident. Restored
   `failed` entries get a `now + delay` floor on their retry so a stale past
   retry time cannot fire the instant the app comes back; restored `success`
   entries come back as `success` awaiting sustained recovery, so a
   currently-healthy fan is never misreported as failed.

### Manual Repair

The "Repair" button clears every fan's backoff ladder — attempt count, `failed`/`success` state, and pending retry alike, since a manual repair is a human-declared fresh start — and repairs all currently **entity-down** fans sequentially. The same repair-worthiness rule as auto-repair applies: a fan that is reachable by HA but missing pings never gets power-cycled, even manually.

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
- A fan whose access point is down is never power-cycled — the repair and its
  clocks hold until the AP recovers
- After failure, retries continue on the capped exponential backoff schedule —
  never more often than the schedule allows, but never stopping entirely

## Self-Provisioned Entities

| Entity | Purpose |
|--------|---------|
| `input_boolean.fans_health_auto_repair` | Auto-repair toggle |
| `input_number.fans_health_auto_repair_delay` | Minutes before auto-repair (1-60) |
| `input_text.fans_health_repair_ladder` | Per-fan backoff ladder as JSON (`input_text.<checker_id>_health_repair_ladder`), so an app reload cannot reset a climbing ladder |

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
  repair_backoff_max_min: 360                        # Backoff cap for repair retries (minutes)
  repair_backoff_reset_min: 30                       # Recovery must hold this long to reset the ladder
  restore_state_enabled: true                        # Re-apply on/off + speed + direction after repair
  repair_script: script.zen32_hard_reset             # HA script entity for repair
  # ap_status_entity: the HA UniFi integration's state sensor for the access
  # point each Wi-Fi fan associates with. AP down => fan offline is expected:
  # power-cycles are held and the alert names the AP instead of the fan.
  fans:
    - name: Pink Room                                # Display name
      entity_id: fan.pink_room_fan_fan               # Fan entity to monitor
      ip: "192.168.50.112"                           # IP to ping
      power_switch: switch.upstairs_pink_room_scene_controller
      relay_control: select.upstairs_pink_room_scene_controller_relay_control
      scene_control: select.upstairs_pink_room_scene_controller_scene_control_relay
      ap_status_entity: sensor.guest_room_u7_pro_state       # AP this fan usually holds
      ap_name: Guest Room U7 Pro                     # Friendly AP name for logs/alerts
```

## Dependencies

- `providers/ha_provisioner` — creates HA helpers on startup
- `shared/check_utils` — `ping_check()` for fan IP pings
- `script.zen32_hard_reset` — HA script for fan power cycle repair
