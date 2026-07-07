# Runbook: `shade_gateway` — Shade Gateway Checker

Owns **gateway-wide RF-disconnect detection** for all Hunter Douglas PowerView
G3 shade batteries. Single aggregate check `Gateway Link`. `check_interval_s:
300`. `supports_repair: yes` — auto power-cycles PoE port 32
(`button.switch_pro_max_48_poe_port_32_power_cycle`) that feeds the primary
PowerView gateway. **for-override `critical: 0`** — this checker pages the
instant it goes critical because it enforces its own auto-restart grace period
internally.

## Domain fact (read this first)

PowerView G3 shades report **0% battery** (or flap `100% ↔ 0%` for hours) when
they lose the RF link to the gateway — **not** because the battery is dying.
This checker detects the *implausible-drop* signature (a healthy reading
collapsing straight to ~0%) and models it as one gateway **episode**. The fix
is **restarting the gateway**, not charging shades. It already automates the
port-32 PoE cycle with a grace period; a critical here usually means the
grace/auto-restart path is running or has already failed once.

## Symptoms

- Alert `checker=shade_gateway` (default alertname `ShadeGatewayUnhealthy`),
  severity critical. Fires immediately (for=0).
- `Gateway Link` detail reads e.g. `Disconnected {m}m (past {delay}m
  auto-restart deadline); affected: <shades>` or `Gateway power-cycle did not
  restore shades after {N}s — manual intervention needed`.

## Diagnosis

1. Read `checkers.shade_gateway.repair_state.status`:
   - `pending` → auto-restart is counting down inside its grace; **wait**.
   - `in_progress` → the PoE cycle is running (settle 180s, budget 900s);
     **wait**, do not stack another.
   - `failed` → the one auto-restart for this episode already ran and the
     shades did **not** come back → this is the human-escalation page.
   - `idle`/`success` while still critical → auto-repair may be disabled or
     the deadline not yet reached.
2. Read the `Gateway Link` detail for the affected-shade list and elapsed
   disconnect minutes.
3. Loki: `{namespace="home-automation", app="appdaemon"} |= "shade_gateway"`
   last 2h — episode start, implausible-drop logs, button press, recovery poll.

## Remediation ladder

1. `record_note` the triage start (affected shades + repair_state).
2. If `repair_state.status in (pending, in_progress)` → **do nothing but
   wait** — the checker is already handling it and the controller withholds
   nothing here (for=0), so the page you see may simply be ahead of the
   in-flight restart. Verify (below).
3. If `repair_state.status == idle` and it's still critical (auto-repair off,
   or the internal deadline reached without a trigger): `start_repair`
   `{"checker_id":"shade_gateway"}` — presses the port-32 PoE cycle button
   immediately (manual repair bypasses the one-per-episode auto gate). Prefer
   this over pressing the button entity directly.
4. Never more than 2 `start_repair` attempts / 6h. A `failed` repair_state
   means the one restart didn't work — go to **Escalate**, do not retry-spam.

## Verify

- After a `start_repair`, budget = `repair_settle_s` (180s) +
  `repair_recovery_wait_s` (900s) ≈ **~15 min** for provisional recovery
  (every affected shade healthy and flap-free for ≥180s).
- Recovery clears the episode; `Gateway Link` returns to `ok` (`"<n> shade
  batteries reporting normally"`) and the page resolves automatically.

## Escalate

If `repair_state.status == failed`, or shades don't recover within budget,
page with a `record_note` summary:
- the episode duration and the affected-shade list;
- that a port-32 PoE power-cycle was performed (auto and/or via `start_repair`)
  and did not restore RF link;
- that manual gateway intervention is needed (reseat/replace the primary
  Upstairs-PowerView-G3 gateway; check its PoE port). Attach Alertmanager +
  Loki links. Do **not** touch the shade batteries — this is a gateway fault.
