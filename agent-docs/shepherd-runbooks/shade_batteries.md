# Runbook: `shade_batteries` — PowerView Shade Batteries

`BatteryChecker` over the 22 Hunter Douglas PowerView shade batteries
(`sensor.*shade*_battery`). Warning ≤25%, critical ≤5%. Runs with
**`disconnect_aware: true`** (`disconnect_healthy_floor: 40`).
`check_interval_s: 300`. **`supports_repair: no`** — `start_repair` is
rejected. The dedicated `shade_gateway` checker owns RF-disconnect paging and
the gateway power-cycle; this checker only pages for a *genuine* dying battery.

## Domain fact (read this first)

PowerView G3 shades report **0% on RF disconnect**, not real drain. The
`disconnect_aware` guard **downgrades an implausible drop** (last healthy
reading ≥40%, now ≤5%) to **warning (UI-only, no page)** with a `suspected
gateway disconnect … — see Shade Gateway` detail. Therefore a **critical page
from `shade_batteries` is, by construction, NOT a disconnect** — it's a
gradual decline where the last-good reading was already low, i.e. a battery
that is genuinely running down. Do not power-cycle the gateway for this.

## Symptoms

- Alert `checker=shade_batteries` (default alertname `ShadeBatteriesUnhealthy`),
  severity critical. Description names a shade and a very low level, e.g.
  `Dining Room 1: 3%`.
- (A `warning`, not critical, with a "suspected gateway disconnect" detail is
  the disconnect path — that never pages and is not a Shepherd concern.)

## Diagnosis

1. Read `checkers.shade_batteries.checks[]` — which shade(s), what level.
2. **Cross-check `shade_gateway`.** Read `checkers.shade_gateway.status` and
   `repair_state`. If `shade_gateway` is in an active episode (`critical` or
   repair `in_progress`), the shade readings are gateway-related — let
   `shade_gateway.md` drive; this leaf is a side effect. A `shade_batteries`
   critical that *survived* the disconnect-aware downgrade while
   `shade_gateway` is healthy is a **real** low battery.
3. Confirm the decline is gradual (HA history: a slow slope down through
   25%→10%→5%, not a cliff from 100%). A cliff should have been caught as a
   disconnect warning — if it paged critical instead, note the anomaly.
4. Loki: `{namespace="home-automation", app="appdaemon"} |= "shade_batteries"`.

## Remediation ladder

No bounded software remediation — the Shepherd cannot recharge/replace a shade
battery. Sanctioned actions are diagnostic only:

1. `force_recheck` (payload `{}`) — re-read to rule out a stale sample; wait
   ~300s, re-read.
2. `record_note` `{"checker_id":"shade_batteries","note":"genuine low battery
   on <shade> at <level>%; gateway healthy; needs recharge/replacement",
   "source":"shepherd"}`.
3. Do **not** `start_repair` (rejected). Do **not** trigger a `shade_gateway`
   power-cycle for a real low battery. Do **not** mute.

## Verify

Only a transient clears on `force_recheck`. A real ≤5% reading will not
self-recover — proceed to Escalate.

## Escalate

Let the page through with a `record_note` summary:
- the shade friendly name and level;
- that `shade_gateway` is healthy, so this is a **genuine dying shade battery**
  needing a recharge/replacement (not a disconnect);
- if `shade_gateway` was actually mid-episode, redirect: this is downstream of
  a gateway disconnect — see `shade_gateway.md`. Attach Alertmanager + Loki
  links.
