# Runbook: `protect_batteries` — UniFi Protect USL Entry Batteries

`BatteryChecker` instance over the UniFi Protect USL entry-sensor batteries
(front door, garage side door, kitchen slider, basement bulkhead —
`sensor.*usl_entry_battery*`). Warning ≤20%, critical ≤10%; unavailable/unknown
= critical. `check_interval_s: 300`. **`supports_repair: no`** — there is no
software fix for a dead battery. `start_repair` on this checker is *rejected*.

## Symptoms

- Alert `checker=protect_batteries` (default alertname
  `UniFiBatteriesUnhealthy`), severity critical.
- Description names a specific entry sensor and its level, e.g. `Front Door:
  4%`, or `Garage Side Door: unavailable`.

## Diagnosis

1. Read `checkers.protect_batteries.checks[]` — identify **which** sensor(s)
   are critical and whether it's a low **percentage** vs. **unavailable**.
2. A low percentage that has been declining over days = a genuinely dying
   battery. Cross-check trend via HA history for the named
   `sensor.*_usl_entry_battery*` if available.
3. `unavailable`/`unknown` instead of a number can mean the sensor dropped off
   (dead battery, or a Protect integration hiccup). Check whether the parent
   `protect` checker is also critical — if the whole integration is down, the
   `protect` runbook owns it and these unavailables are a symptom, not a
   battery.
4. Loki: `{namespace="home-automation", app="appdaemon"} |= "protect_batteries"`
   — mostly confirms the reading; there is no repair activity to find.

## Remediation ladder

There is **no bounded software remediation** — the Shepherd cannot swap a
battery. The only sanctioned actions here are diagnostic:

1. `force_recheck` (payload `{}`) — re-read now to rule out a single stale
   sample (especially for a transient `unavailable`). Wait ~300s, re-read.
2. `record_note` `{"checker_id":"protect_batteries","note":"low battery on
   <sensor> at <level>%; needs physical replacement","source":"shepherd"}`.
3. Do **not** call `start_repair` (it will be rejected). Do **not** mute.

## Verify

Only a `force_recheck` outcome is verifiable: if the reading was a transient
`unavailable` and the sensor is actually fine, it returns to `ok` on the next
poll and the page resolves. A genuine low percentage will **not** self-recover
— proceed to Escalate.

## Escalate

A real low/critical battery always pages a human (that is the intended
behavior — it's an actionable maintenance task, not a false alarm). Let the
page through with a `record_note` summary:
- the exact sensor friendly name and its level;
- that it needs a **physical battery replacement** (USL entry sensor);
- if `unavailable`, note whether the parent `protect` integration is healthy
  (battery-dead) or also down (integration issue → see `protect.md`).
Attach the Alertmanager link. No retry, no repair loop.
