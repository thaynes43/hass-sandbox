# Runbook: `protect` — UniFi Protect Health Checker

Detects the **silent Protect websocket freeze**: the integration's websocket
dies with *zero log errors* — camera attributes keep updating but
motion/smart-detect binary sensors stop changing state entirely. Also catches
hard outages (UNVR down / auth 401s → all sensors `unavailable`).
`check_interval_s: 300`. `supports_repair: yes` — auto-reloads the
`unifiprotect` config entry (max 1/hour). Alertname overridden to
`ProtectEventStreamFrozen`. Auto-repair default ON, delay 1 min.

## Symptoms

- Alert `checker=protect`, alertname `ProtectEventStreamFrozen`, severity
  critical (this is the only checker whose critical routes to Pushover under a
  custom alertname).
- Description names a failing check:
  - `Camera Events` stale → the **freeze** (websocket dead, no new events).
  - `Sensor Availability` critical → **hard outage** (≥90% of sensors
    unavailable past the 15-min grace — UNVR down or auth failure).
  - `Sensor Discovery` critical → config entry not loaded / no admin access.

## Diagnosis

1. Which check is red (`checkers.protect.checks[]`)? Freeze vs. hard-outage vs.
   discovery drives everything below.
2. Read `repair_state.status`:
   - `in_progress` → a config-entry reload is running (settle 60s, budget
     600s) — **wait**.
   - `failed` → the last auto-reload did not produce a genuine post-reload
     event; it will retry once the 1-hour `reload_cooldown_s` allows.
   - `pending`/`idle` → reload is scheduled or the cooldown is blocking it.
3. Note the **reload cooldown**: auto-repair reloads at most once/hour. If a
   reload just happened, a fresh page can appear before the retry is allowed —
   manual `start_repair` bypasses the cooldown.
4. Loki: `{namespace="home-automation", app="appdaemon"} |= "protect"` last 2h
   — reload attempts, baseline moves, "auto-repair FAILED" lines. Note: the
   freeze is *silent by definition*, so absence of Protect errors is expected.

## Remediation ladder

1. `record_note` the triage start (which check failed + repair_state).
2. If `repair_state.status == in_progress` → wait; do not stack a reload.
3. If frozen/hard-outage and no reload in flight: `start_repair`
   `{"checker_id":"protect"}` — reloads the loaded `unifiprotect` config entry
   immediately, **bypassing the 1-hour cooldown** (manual = now). This is the
   proven fix for the websocket freeze.
4. If `Sensor Discovery` is the red check (config entry not loaded / no admin
   token), a reload can't help → go straight to **Escalate**.
5. Max 2 `start_repair` attempts / 6h. A reload that lands in `failed` twice
   means the stream is genuinely dead → escalate.

## Verify

- After `start_repair`, the checker moves its freeze baseline to
  reload-completion + `repair_settle_s` (60s) and polls every 30s up to
  `repair_recovery_wait_s` (600s) for a **genuine** post-reload event. Budget
  ≈ **~10 min** — but recovery needs real motion, so it can lag until someone/
  something trips a camera. During active hours (08:00–23:00) that's usually
  quick.
- Recovery unfreezes the checker; the bridge posts `[RESOLVED]`.

## Escalate

If not recovered within budget, `repair_state.status == failed` after a reload,
or `Sensor Discovery` is red, let the page through with a `record_note`:
- which check is red and whether it's a freeze vs. hard outage vs. discovery
  failure;
- that a config-entry reload was triggered (auto and/or manual) and its result;
- next human steps: for a hard outage check the UNVR/UDM and Protect auth; for
  a persistent freeze that survives reload, restart the Protect app or the
  UNVR. Attach Alertmanager + Loki links.
