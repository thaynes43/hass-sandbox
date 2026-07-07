# Runbook: `spa` — Spa Health Checker

Monitors the Gecko `in.touch` hot-tub gateway (Westford Spa). Checks: gateway
ICMP ping (`192.168.50.122`), the overall-connection binary sensor, and a
staleness/"zombie" detector (all tracked entities stale → data path dead even
if connectivity reads `on`). `check_interval_s: 120`. `supports_repair: yes`
(power-cycle `switch.spa_intouch3_switch`). Depends on `cloud`.

> ⚠️ **STOP — this checker is muted indefinitely.** The spa hardware is
> physically broken. If `checkers.spa.muted == true` (expected), **skip triage
> entirely**: no remediation, no page, no note. The mute is a deliberate human
> decision, not a transient. The rest of this runbook only applies if the mute
> has been lifted and the hardware repaired.

## Symptoms

- Alert `checker=spa` (default alertname `SpaUnhealthy`), severity critical.
- Description names one or more of: `Gateway Ping`, `Overall Connection`, or a
  staleness check (e.g. `Thermostat` / `Pump` entities) as failing.

## Diagnosis

1. **Precondition:** confirm `checkers.spa.muted`. If true → exit (see above).
2. Read `checkers.spa.checks[]` — which check is red?
   - `Gateway Ping` red → the `in.touch` gateway is off the network (power/RF).
   - `Overall Connection` red only → cloud/link handshake dropped.
   - Only staleness red (connectivity `on`) → the **zombie** state: entities
     report values but the coordinator has stopped updating.
3. Check the `cloud` dependency: read `checkers.cloud.status`. If `cloud` is
   critical, this is downstream — triage `cloud`, not `spa`.
4. Loki: `{namespace="home-automation", app="appdaemon"} |= "spa"` last 1h —
   look for ping failures, repair state transitions, coordinator errors.

## Remediation ladder

1. `record_note` — start the audit trail: `{"checker_id":"spa","note":"triage
   start: <which check failed>","source":"shepherd"}`.
2. `force_recheck` (payload `{}`) — re-run all checks now; a one-poll network
   blip that already cleared shows green on the next report. Wait ~120s, re-read.
3. If still critical and `repair_state.status == idle`: `start_repair`
   `{"checker_id":"spa"}` — power-cycles `switch.spa_intouch3_switch` (off,
   10s, on) and polls health for up to `repair_recovery_wait_s` (300s). Do
   **not** toggle the switch directly — `start_repair` carries the recovery
   verification.
4. If `repair_state.status` is already `pending`/`in_progress`, wait — do not
   stack a second repair. Max 2 `start_repair` attempts / 6h.

## Verify

- After `start_repair`, allow up to `repair_recovery_wait_s` (300s) plus one
  `check_interval_s` (120s) ≈ **7 min** budget.
- Recovery = all `checks[]` back to `ok` and `repair_state.status == success`;
  the bridge posts `[RESOLVED]` automatically.

## Escalate

If not recovered within budget, or `repair_state.status == failed`, let the
page through and `record_note` a summary:
- which checks are still red (`Gateway Ping` / staleness / etc.);
- that a power-cycle via `start_repair` was attempted and its outcome;
- likely cause (gateway offline vs. zombie/coordinator-stall vs. cloud
  dependency), and that physical inspection of the `in.touch` unit is likely
  needed. Attach the Alertmanager link and the Loki query above.
