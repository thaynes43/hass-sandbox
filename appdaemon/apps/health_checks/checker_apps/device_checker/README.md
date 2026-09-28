# Basic Device Checker

Generic, config-driven health checker for any device that needs entity state monitoring and an optional IP ping. No repair support — designed for devices that cannot be auto-repaired from AppDaemon.

A single class that can be instantiated multiple times with different configuration, similar to `NetworkProtocolChecker` but for arbitrary entity checks rather than network protocol stacks.

## Checks

| Check | Config Key | Optional |
|-------|-----------|----------|
| IP Ping | `ping_host` | Yes — omit to skip |
| Entity State (1..N) | `entities[]` | At least one recommended |

### Ping config keys

| Key | Default | Meaning |
|-----|---------|---------|
| `ping_host` | — | IP or host name to ping; omit to skip the ping check |
| `ping_check_name` | `Ping` | Display name of the check |
| `ping_attempts` | `1` | Pings per cycle, ok on the first reply |
| `ping_fallback_host` | — | For a `ping_host` given as a name: an IP to ping when the name does not resolve. With it set, `ping_host` is resolved first (`getaddrinfo`, IPv4, `PING_RESOLVE_TIMEOUT_S` = 3 s). **Resolves** → the ping by name is authoritative and its result stands; the fallback is never pinged, so a device that merely drops ICMP keeps the `ping_attempts` miss tolerance and is not blamed on DNS. **Does not resolve** (NXDOMAIN, or a resolver that hangs past the timeout — or `ping` itself reporting `cannot resolve`, a race) → the fallback is pinged with the same attempts: answers → `warning`, `"<ms> via <fallback> — cannot resolve <host>"` (UI-only: never pages, never arms a repair); silent → its own `critical`, `"<detail> via <fallback> — cannot resolve <host>"`, so a dead device still repairs and pages. Without it: no pre-resolve, and an unresolvable name is `critical`, `cannot resolve <host>` (as for every `ping_check` caller), so on a repairable checker a DNS outage pages and power-cycles a healthy device. WARNING logged when the name stops resolving, INFO when it resolves again |

## Configuration Reference

```yaml
vestaboard_health_checker:
  module: health_checks.checker_apps.device_checker.device_checker
  class: BasicDeviceChecker
  checker_id: vestaboard                  # Unique ID
  checker_name: Vestaboard                # Display name on cards
  ping_host: "192.168.50.159"             # IP to ping (optional, omit to skip)
  ping_check_name: Ping                   # Display name for ping check
  ping_attempts: 3                        # Pings per cycle, ok on first success (default 1)
  check_interval_s: 180                   # Check frequency (seconds)
  entities:                               # List of entity checks
    - entity_id: sensor.vestaboard_controller_status
      healthy_state: active               # Expected state value
      name: Controller Status             # Display name for this check
    - entity_id: sensor.vestaboard_configuration_status
      healthy_state: ok
      name: Configuration Status
```

Any check can be disabled by omitting its config key. YAML bool coercion is handled (`"on"` → `True` → reversed back to `"on"`).

## RepairableDeviceChecker

`RepairableDeviceChecker` extends `BasicDeviceChecker` with smart-switch power-cycle repair. Same check config, plus:

```yaml
printer_health_checker:
  module: health_checks.checker_apps.device_checker.repairable_device_checker
  class: RepairableDeviceChecker
  ha_url: !secret ha_url
  ha_token_env: TOKEN
  checker_id: printer
  checker_name: Printer
  ping_host: "192.168.0.211"
  ping_check_name: Ping
  check_interval_s: 180
  repair_switch: switch.downstairs_study_printer_switch  # Smart switch to toggle
  repair_recovery_wait_s: 300                            # Max wait for recovery
  repair_off_duration_s: 10                              # Seconds to keep switch off
  auto_repair_enabled_default: false                     # Seeds the toggle AND is the fallback while the helper is unreadable
  auto_repair_delay_min_default: 5                       # Seeded the same way; clamped to 1-60
  entities:
    - entity_id: sensor.brother_mfc_l3780cdw_series
      name: Status
```

Self-provisions `input_boolean.{checker_id}_health_auto_repair` and `input_number.{checker_id}_health_auto_repair_delay`.

`entities:` is optional here too. A checker with only `ping_host` has a single check, `Ping`, and a sustained ping failure alone arms the repair — the cross-check downgrade needs at least two checks to act on.

### The repair

1. Turn `repair_switch` off, wait `repair_off_duration_s`.
2. Turn it on and **confirm it reports `on`** — read every 5 s for up to 60 s (`SWITCH_CONFIRM_POLL_S` / `SWITCH_CONFIRM_TIMEOUT_S` in `shared/switch_power_cycle.py`).
3. Not on → log a WARNING, `turn_on` once more, confirm again for up to 60 s.
4. Still not on (the entity missing included) → the repair ends `failed` straight away with detail `"<switch> did not turn back on — check the outlet"` (or, when the entity is missing, `"<switch> did not turn back on — the entity is missing from Home Assistant — reload the integration that owns it"`; or, when reading it raised — most likely AppDaemon's Home Assistant connection dropped — `"<switch> did not turn back on — the switch could not be read — see the AppDaemon log"`, with each failed read logged at WARNING), an ERROR log and a failed `repair_event` (no `duration_s`). There is no recovery wait: it would only burn five minutes on an unpowered device.
5. Confirmed on → poll the checks every 5 s for up to `repair_recovery_wait_s` of **wall-clock** time; the recovery clock starts here, not at `turn_on`. Wall-clock, checks included: a dead device's pings take seconds each, and counting only the 5 s sleeps let a "300 s" wait run for many minutes. The last check starts at the deadline.

Why confirm: on a UniFi USP PDU a toggle re-provisions the whole PDU for ~40 s, Home Assistant took more than 10 s to report the outlet back on, and once the unifi integration dropped every outlet entity of the PDU until it was reloaded — a fire-and-forget `turn_on` could leave the device off. Because HA reports late, an `on` read shortly after `turn_on` can still be the state from *before* the cycle, so an `on` confirms early only if its `last_changed` is after the cycle started (a `datetime` or a naive UTC timestamp counts too, as in the protect checker), or if an `off` was read earlier in the same cycle — either proves HA registered it. An `on` that never changed is accepted at the end of the 60 s window (logged at WARNING — on a successful repair that line is the only record): HA never saw the switch go off, so the device is powered but may not have been cycled. That is not a failure, but the result carries a note, and if the recovery wait then fails the detail becomes `"Did not recover after 300s (the outlet never reported off — it may not have been power cycled)"`, so the card and the Alertmanager description say it. On success the note is only logged.

A `warning` counts as healthy as well as `ok`, both during the recovery wait and when auto-repair evaluates a cycle: this checker only produces one when `ping_host` does not resolve and `ping_fallback_host` answers, i.e. the device is up and DNS is not. So a warning-only cycle stands a `pending` repair down (its countdown cannot outlive a recovery that happened during a DNS outage) and clears `success`/`failed` to `idle`.

One repair per outage: after `failed` the checker stays failed until a fully healthy cycle, so a device that does not come back is power-cycled once, not in a loop.

A relapse after `success` — the device bad again (`critical`) before a fully healthy cycle — moves the repair to `failed` with detail `"Relapsed after a successful repair — recovery did not stick"` (a WARNING in the log). `success` is judged moments after the power cycle, and left standing it is one of the Alertmanager bridge's repair-hold states, so it would keep withholding the page for a device that is down again; `failed` releases it. It is `failed` rather than `idle` so the outage does not earn a second auto-repair, and it clears on the next all-ok cycle. An `unknown` is not a relapse (it says nothing about the device: `success` stands, nothing is armed), and a `warning` (the DNS-fallback case: the device answered on its IP) is a healthy cycle, so `success` clears to `idle`. Without a `ping_fallback_host`, an unresolvable name is `critical` and does count. (`RepairableDeviceGroupChecker` does the same per device.) With auto-repair switched off, a stale `success` goes to `idle` instead (`_stand_down_pending_repair`).

```
idle → pending → in_progress → success → idle     (checks stay healthy)
                             │         → failed   (relapse before a healthy cycle)
                             → failed  → idle     (checks recover)
```

### Second example — the Movie Room Sonos Port

```yaml
movie_room_sonos_health_checker:
  module: health_checks.checker_apps.device_checker.repairable_device_checker
  class: RepairableDeviceChecker
  ha_url: !secret ha_url
  ha_token_env: TOKEN
  checker_id: movie_room_sonos
  checker_name: Movie Room Sonos
  ping_host: movieroomsonos.haynesnetwork                  # the FQDN, by Tom's choice
  ping_fallback_host: "192.168.0.70"                       # its DHCP reservation; pinged only when the name does not resolve
  ping_check_name: Ping
  ping_attempts: 3
  check_interval_s: 180
  repair_switch: switch.power_distribution_hi_density_outlet_21   # UniFi PDU outlet 21, labelled "Sonos Port"
  repair_recovery_wait_s: 300
  repair_off_duration_s: 10
  auto_repair_enabled_default: true
  auto_repair_delay_min_default: 10
```

Ping is its only signal on purpose. The Port wedged on 2026-09-22 after a network-switch blip (link up, no ARP, ping or TCP 1400) and `media_player.movie_room` stayed unavailable for six days until a 12 s power cycle of its outlet fixed it. The Music Assistant player cannot be a check, because it also goes unavailable on every MA restart and that must never power-cycle the Port; the outlet's power sensor reads 0 W at idle, so power draw is no signal either. Triage: `agent-docs/shepherd-runbooks/movie_room_sonos.md`.

## Dependencies

- `shared/check_utils` — `ping_check()` for IP pings
- `providers/ha_provisioner` — creates HA helpers (RepairableDeviceChecker only)
- `shared/auto_repair_config` — `AutoRepairConfigMixin`: the auto-repair toggle/delay helpers (RepairableDeviceChecker only)
- `shared/switch_power_cycle` — `power_cycle_switch()`: the off / wait / on + confirm sequence (RepairableDeviceChecker only)
