# AC Mains Checker

## Overview

Monitors `ac_mains_disconnected` binary sensors so that **loss of wall power on
a battery-backed device pages immediately**, instead of surfacing days later as
a dead Z-Wave node.

Mains-powered Z-Wave devices with battery backup — Zooz ZAC38 range extenders
are the canonical example — expose
`binary_sensor.<device>_ac_mains_disconnected` via the Notification command
class. When wall power drops, the device transparently falls back to its
internal battery and keeps routing, so nothing *looks* broken. The failure only
becomes visible when the battery runs flat and the node stops responding.

### The incident that motivated this checker (2026-07-21 → 2026-07-26)

`shed_extender` (Zooz ZAC38, node 20) lost AC mains at **2026-07-21 14:07Z**
when a breaker blew. It ran on backup battery for ~38 hours, drained
100% → 16% between 07-22 22:21Z and 07-23 04:25Z, and went silent. Nothing
alerted. The failure was only discovered on **07-26** — five days later — and
then only by accident: an unrelated node reboot flushed a stale cached battery
reading, which finally flipped the entity to `unavailable` and tripped the
`zwave_batteries` checker.

`ac_mains_disconnected` had been `on` the entire time. This checker watches it.

## How It Works

1. On startup, discovers entities whose `entity_id` matches the configured
   `entity_patterns` include regexes (and not the excludes).
2. Registers one check per discovered entity with `health_check_controller`,
   named after the device's friendly name with the ` AC mains disconnected`
   suffix stripped.
3. Every `check_interval_s`, reads each entity and maps state to status.
4. Emits an `ac_mains_disconnected` gauge metric per device (`1` = on backup
   battery, `0` = mains present) for Grafana.

### Status logic

| Entity state | Status | Meaning |
|---|---|---|
| `off` | `ok` | AC mains present |
| `on` | `disconnected_status` (default `critical`) | Running on backup battery |
| `unavailable` / `unknown` / missing | `unknown` | No data — see below |
| anything else | `unknown` | Unexpected state, reported verbatim |

**Unavailable is `unknown`, never critical.** This mirrors the doctrine in
`battery_checker/README.md`: a missing reading is *no data*, not a power loss.
A device that drops off entirely is a Z-Wave connectivity failure owned by the
`zwave` checker — which this checker declares as a `health_dependency`, so the
controller masks these checks to `unknown` whenever Z-Wave itself is
critical/degraded. Without that, every Z-Wave driver restart would page the
entire mains fleet at once.

### Why the exclude list is load-bearing

Unlike battery sensors, these entities carry **no `device_class`**, so
include/exclude patterns are the *only* selector — there is no attribute filter
to fall back on.

Critically, **battery-only Z-Wave devices also expose this sensor and report
`on` permanently**, because they have no AC mains to lose. On this install
that's three devices:

| Device | Model | Why excluded |
|---|---|---|
| `basement_concessions_qsensor` | Zooz ZSE11 | Battery-powered Q Sensor (node 48) |
| `basement_hallway_qsensor` | Zooz ZSE11 | Battery-powered Q Sensor (node 45) |
| `shed_indoor_motion_sensor` | Zooz ZSE70 800LR | Battery-powered motion sensor (node 40) |

All three reported `ac_mains_disconnected=on` continuously across a 10-day
history window, never once reporting `off`, while holding 97–100% battery.
Left unexcluded they would fire three permanent false criticals, which is how
alerting gets ignored.

A quick way to tell a battery device from a mains device: check
`sensor.<device>_node_status`. Mains/USB-powered Z-Wave nodes are
always-listening and report `alive`; battery nodes report `asleep`. The ZAC38
extenders are `alive`; all three excluded sensors are `asleep`.

If you add a new battery-only device that exposes this sensor, it will alert
once — add an `exclude` pattern for it.

## Dependencies

- **Upstream**: `zwave` (`NetworkProtocolChecker`) — declared via
  `health_dependencies`, masks these checks when the Z-Wave driver is down.
- **Downstream**: none.

## Self-Provisioned Entities

None. This checker only reads existing Z-Wave entities.

## Associated Card

None of its own — surfaces through the shared health-check dashboard card.

## Configuration Reference

### Required

| Key | Description |
|---|---|
| `module` | `health_checks.checker_apps.ac_mains_checker.ac_mains_checker` |
| `class` | `AcMainsChecker` |
| `entity_patterns` | List of `{include: regex}` / `{exclude: regex}` rules matched against `entity_id` via `re.search` |

### Optional

| Key | Default | Description |
|---|---|---|
| `checker_id` | `ac_mains` | Controller-facing id |
| `checker_name` | `AC Mains` | Display name |
| `check_interval_s` | `300` | Seconds between check cycles |
| `disconnected_status` | `critical` | Status when mains is lost. One of `critical`, `degraded`, `warning`. Invalid values fall back to `critical` with a WARNING log |
| `health_dependencies` | `[]` | List of `{checker_id: <id>}` that mask these checks |

### Example

```yaml
ac_mains_checker:
  module: health_checks.checker_apps.ac_mains_checker.ac_mains_checker
  class: AcMainsChecker
  disable: true
  checker_id: ac_mains
  checker_name: AC Mains
  check_interval_s: 300
  disconnected_status: critical
  health_dependencies:
    - checker_id: zwave
  entity_patterns:
    - include: "binary_sensor\\..*_ac_mains_disconnected$"
    - exclude: ".*qsensor.*"
    - exclude: ".*shed_indoor_motion_sensor.*"
```

The include regex deliberately anchors on `_ac_mains_disconnected$` so the
paired `_ac_mains_re_connected` sensor is never picked up as its own check.

### Alert debounce

Power loss is time-sensitive, so this checker pages at `critical`. Unlike the
UPS checker (`critical: 0` — never debounce), Z-Wave notification reports can
flap, so the controller is configured with a short debounce in
`apps-prod.yaml`:

```yaml
  alert_for_overrides:
    ac_mains:
      critical: 120   # ~2 min — a real breaker trip still pages fast
```

## Manual Setup Required

None. Discovery is automatic; only the exclude list needs curating as new
battery-only devices are added.
