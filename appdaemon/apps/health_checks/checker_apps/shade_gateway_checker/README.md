# Shade Gateway Checker

## Overview

`ShadeGatewayChecker` owns **gateway-wide RF disconnect detection** for all
Hunter Douglas PowerView shade batteries, and auto-repairs by power-cycling
the PoE port that feeds the primary gateway.

### The incident this fixes

PowerView G3 shades report `0%` battery when they lose their RF link to the
gateway — not only when the battery is genuinely dead. When the gateway
hiccups, every shade on it can flap `100% <-> 0%` many times over several
hours as it repeatedly loses and regains its link. `sensor.cover` entities
use `assumed_state: true` and don't reflect this, so the battery sensor is
the only usable signal.

The generic `BatteryChecker` has no way to tell an RF disconnect apart from
a genuinely dying battery — it saw `0%`, treated it as critical-low, and
paged Pushover repeatedly overnight even though the shades self-healed with
no intervention. `ShadeGatewayChecker` fixes that by:

1. Detecting the **implausible-drop signature** (a healthy reading collapsing
   straight to ~0%, which a real battery physically cannot do in one step)
   instead of just thresholding on the raw percentage.
2. Modeling disconnects as a single **gateway-level episode** that survives
   mid-episode flap-backs to 100% — only sustained flap-free health clears it.
3. Auto-repairing by pressing a UniFi PoE power-cycle button, with **one
   restart attempt per episode** — if that doesn't restore the shades, it
   escalates to a critical page instead of retrying forever.
4. Cooperating with `BatteryChecker` (via `disconnect_aware` on the
   `shade_battery_checker` instance) so a real gradual low-battery decline
   still pages through the normal battery check.

## Detection Heuristic

Every shade battery sensor is tracked with:

- `last_good_value` — the last reading seen **above** `disconnect_low_threshold`.
- `current_value` — the most recent reading, good or bad (updated live via
  `listen_state`, never a polled snapshot — this is what lets the checker
  see a flap that a 300s poll could otherwise miss entirely).

A reading is an **implausible drop** (see `shared/check_utils.is_implausible_battery_drop`)
when:

```
current_value <= disconnect_low_threshold AND last_good_value >= healthy_floor
```

i.e. a real battery never loses 35+ percentage points between two
consecutive readings — that pattern is the RF-disconnect signature, not a
discharge curve. A **gradual decline** (e.g. `8% -> 0%`, where the last good
value was already below `healthy_floor`) is deliberately *not* flagged here
— that's a real dying battery, still the `BatteryChecker`'s job to page.

### Episode lifecycle

- **Start**: the first implausible drop while no episode is active sets
  `disconnect_since = now`.
- **Continues**: any further implausible drop (including a shade that
  bounced back to 100% and then dropped to 0% again) refreshes the global
  `last_zero_time` but does **not** reset `disconnect_since` — a mid-episode
  100% reading must never clear the episode by itself.
- **Recovers**: once `now - last_zero_time >= recovery_settle_s` (default
  15 min) **and** every *affected* shade (an entry in `_episode_affected` —
  a shade that actually contributed a qualifying drop this episode) has a
  known, current reading above `disconnect_low_threshold`, the episode
  clears, the affected-shade list resets, and the one-restart-per-episode
  guard resets. See `_affected_shades_healthy()`.
- Recovery is judged **only against the affected shades**, not every
  discovered shade. A shade that never flapped this episode but happens to
  be `unavailable`/`unknown` right now (a battery change, an unrelated
  integration hiccup, anything with nothing to do with the gateway) can
  never block recovery of the shades that actually went down — it was
  never part of the episode in the first place. An affected shade that is
  itself still `unavailable`/`unknown` (recorded as `None` in
  `current_value`, never a stale cached number) or still at/below the low
  threshold **does** block recovery — we can't confirm it actually came
  back.
- A non-numeric reading (`unavailable`/`unknown`/`None`, e.g. during an HA
  restart) is recorded explicitly as unknown but never starts or feeds an
  episode by itself — only the numeric drop-to-low-from-healthy signature
  does that.
- Recomputed once per `check_interval_s` tick (`_recompute_recovery`), not
  just on state-change events, so an episode can clear even if nothing has
  changed recently.

### Cold start

At startup, `last_good_value` and `current_value` are seeded from each
entity's current state — but **only** a currently-healthy reading seeds
`last_good_value`. A shade that happens to be reading low at AppDaemon
startup never fabricates an episode; an episode requires an *observed*
healthy -> low transition via `listen_state` (or a seeded healthy baseline
followed by an observed drop).

## Gateway Attribution (2026-08-26)

The gateway alert and the PoE auto-restart require **gateway-level evidence**;
the battery-drop signature alone is not enough:

- **Direct probes**: every configured gateway (`gateways:` list) is ICMP-pinged
  each check cycle, and gateways flagged `api: true` also get a REST probe
  (`GET http://<host>/home/shades`). One failed probe reports `warning`;
  `probe_fail_confirm` (default 2) consecutive failures report `critical` and
  start the repair clock (`_probe_down_since`). The debounce matters because
  this checker carries a `for=0` alert override — an undebounced blip would
  page instantly.
- **Multi-shade corroboration**: a battery-drop episode counts as
  gateway-attributed only when `min_shades_for_gateway` (default 2) distinct
  shades contribute qualifying drops. This catches the RF-side-dead case where
  the API still answers.
- **Single-shade episode**: reported as `Gateway Link: ok` with a
  `"gateway healthy; N shade(s) RF-disconnected (...) — see Shade Batteries"`
  detail. The BatteryChecker's disconnect-aware guard owns surfacing that
  shade (`shade unreachable — dead battery or RF fault` warning). No page, and
  **never** a PoE power-cycle — rebooting the gateway for one dead shade blips
  every other shade for nothing (incident: First Floor Bathroom at -89 dBm,
  2026-08-26).

## Status Reporting

Aggregate check `Gateway Link`, plus one `"<name> Ping"` check per configured
gateway and `"<name> API"` for gateways with `api: true`:

| Condition | Status | Notes |
|---|---|---|
| No active episode | `ok` | `"<n> shade batteries reporting normally"` |
| Episode active, single shade (not gateway-attributed) | `ok` | `"gateway healthy; ... — see Shade Batteries"` |
| Episode active (≥2 shades), within grace | `warning` | UI-only, no page. `"Disconnected {m}m; auto-restart at {iso}; affected: ..."` |
| Episode active (≥2 shades), past grace, repair `in_progress` | `critical` | Pages `ShadeGatewayDisconnected`. |
| Episode active (≥2 shades), past grace, auto-repair disabled or not yet triggered | `critical` | `"Disconnected {m}m (past {delay}m auto-restart deadline); affected: ..."` |
| Repair `failed` (timed out) | `critical` | `"Gateway power-cycle did not restore shades after {N}s — manual intervention needed"` — the human escalation page. |
| Probe failing (1 consecutive) | `warning` | `"timeout (1/2 probes — confirming)"` |
| Probe failing (≥`probe_fail_confirm` consecutive) | `critical` | Pages — the gateway itself is unreachable. |

`repair_state` is included on every report (same shape as `SpaHealthChecker`).

## Repair Behavior

0. Auto-repair is **hard-gated on gateway attribution**: the repair clock
   (`_gateway_unhealthy_since()`) only runs for a confirmed probe outage or a
   ≥`min_shades_for_gateway` episode. A single-shade episode never schedules
   auto-repair, and a pending countdown is cancelled if attribution drops to
   single-shade. Manual repair via the card remains available regardless.
1. At the auto-repair deadline (`gateway_unhealthy_since + auto_repair_delay_min`),
   `button.press` is called on the configured `repair_button` — a UniFi PoE
   port power-cycle button that restarts the primary PowerView gateway.
   (Probe-attributed recovery additionally requires the probes to pass again
   before the repair is declared successful.)
2. Waits `repair_settle_s` (default 180s) for the gateway to reboot and
   shades to re-associate.
3. Polls every 5s, up to a total of `repair_recovery_wait_s` (default 900s)
   elapsed since the button press, for **provisional recovery**: every
   *affected* shade (same `_affected_shades_healthy()` check used for
   ambient recovery — unrelated unavailable shades never block this) is
   currently healthy AND flap-free (no new implausible drop) for at least
   `repair_settle_s`. This is a shorter bar than the ambient
   `recovery_settle_s` — a fresh reboot that's stayed clean for 3 minutes is
   good evidence the fix worked.
4. **Success** clears the episode immediately and reports `repair_state.status = success`.
5. **Timeout** sets `repair_state.status = failed` and keeps the `Gateway
   Link` check `critical` with a manual-intervention detail — this is the
   human escalation page. No automatic retry.

**One restart per episode**: `_repair_attempted_this_episode` is set the
moment a repair (auto or manual) starts, and is only cleared when the
episode fully clears (recovery or manual cancel does **not** clear it — see
below). Manual repair via the card always works regardless of this flag —
only the *automatic* trigger is gated.

`cancel_repair` only cancels a *pending* auto-repair countdown; it
deliberately leaves `disconnect_since` untouched, so the checker keeps
reporting the outage even if a human cancels the scheduled restart.

## Configuration

```yaml
shade_gateway_checker:
  module: health_checks.checker_apps.shade_gateway_checker.shade_gateway_checker
  class: ShadeGatewayChecker
  ha_url: !secret ha_url
  ha_token_env: TOKEN
  checker_id: shade_gateway
  checker_name: Shade Gateway
  check_interval_s: 300                # periodic re-evaluation tick
  entity_patterns:
    - include: "sensor\\..*shade.*_battery$"
  disconnect_low_threshold: 5          # % at/below which a reading is "zero-ish"
  healthy_floor: 40                    # % a baseline must be at/above to make a drop "implausible"
  recovery_settle_s: 900               # flap-free seconds required to clear an episode (ambient, no repair)
  gateways:                            # direct probes; every host is pinged, api: true adds GET /home/shades
    - name: Upstairs
      host: 192.168.0.153
      api: true                        # G3 REST API only answers on the primary
    - name: Downstairs
      host: 192.168.0.210
  min_shades_for_gateway: 2            # battery-drop episodes need >=N shades to count as gateway evidence
  probe_fail_confirm: 2                # consecutive probe failures before critical + repair clock
  repair_button: button.switch_pro_max_48_poe_port_32_power_cycle
  repair_settle_s: 180                 # wait after button press before polling; also the post-repair flap-free bar
  repair_recovery_wait_s: 900          # total elapsed budget (from button press) to confirm recovery
  auto_repair_enabled_default: true
  auto_repair_delay_min_default: 120   # 2h grace before auto-restart
```

`entity_patterns` uses the same include/exclude regex approach as
`BatteryChecker` (see `battery_checker/README.md`).

## Self-Provisioned Entities

| Entity | Type | Purpose |
|--------|------|---------|
| `input_boolean.shade_gateway_health_auto_repair` | Helper | Auto-repair toggle (default ON) |
| `input_number.shade_gateway_health_auto_repair_delay` | Helper | Auto-repair grace period in minutes (15-360, step 15, default 120) |

## Manual Setup Required

None beyond the standard health-check dashboard cards (already provisioned
by `health_check_controller`) — this app only self-provisions its two
auto-repair helpers above. The `repair_button` entity
(`button.switch_pro_max_48_poe_port_32_power_cycle`) and the
`shade_battery_checker`'s `disconnect_aware: true` flag are pre-existing
configuration, not something this app creates.

## Interaction With BatteryChecker

Enable `disconnect_aware: true` on the `shade_battery_checker` instance so
its "charge me" alert no longer false-fires during a gateway disconnect,
while a genuine gradual low battery still pages normally. See
`battery_checker/README.md` for details. Both checkers share the
`is_implausible_battery_drop` classification helper in
`shared/check_utils.py` so the two apps agree on what counts as a
disconnect.

## Relay Commands

Routed the same way as every other repair-capable checker, via
`script.health_check_relay` -> `health_check_repair_shade_gateway`:

| Command | Payload | Description |
|---------|---------|-------------|
| `start_repair` | `{"checker_id": "shade_gateway"}` | Trigger a manual power-cycle immediately |
| `cancel_repair` | `{"checker_id": "shade_gateway"}` | Cancel a pending auto-repair countdown (does not clear the episode) |
| `update_repair_config` | `{"checker_id": "shade_gateway", "auto_repair_enabled": true, "auto_repair_delay_min": 120}` | Update auto-repair settings |
