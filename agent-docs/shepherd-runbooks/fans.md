# Runbook: `fans` — Ceiling Fan Health Checker

Monitors Modern Forms ceiling fans (Pink/Blue/White/Primary/Living Room/Study)
as one checker. **2 checks per fan**: `{name} State` (`get_state` not
`unavailable`/`unknown`) and `{name} Ping` (ICMP to the fan's IP).
`check_interval_s: 180`. **`supports_repair: yes`** — per-fan repair via
`script.zen32_hard_reset`, which power-cycles that fan through its ZEN32 scene
controller's relay. Auto-repair default **OFF**, delay 5 min,
`repair_recovery_wait_s: 300`. Failed repairs retry forever on a
**CrashLoopBackOff ladder** (below) — there is no "one attempt per failure".

## Domain fact (read this first)

**These fans are Wi-Fi devices, not Z-Wave.** Modern Forms fans are Espressif
(ESP) Wi-Fi clients on `192.168.50.x`, each associated with one UniFi access
point. The **ZEN32 is only the Z-Wave scene controller whose relay cuts mains
power to the fan** — it is the repair *actuator*, nothing more. A fan going
`unavailable` is almost always a Wi-Fi/AP event or a wedged ESP, never a Z-Wave
mesh problem. (A triage agent misdiagnosed this checker as "ZEN32/Z-Wave fans"
on 2026-08-31 and chased the wrong network entirely.)

Three consequences that shape everything below:

- **ESP Wi-Fi power-save drops single pings.** `Ping` alone is retried
  (`PING_ATTEMPTS = 3`) and a ping-only failure **never** power-cycles a fan —
  only `State: unavailable/unknown` justifies cutting power.
- **AP down ⇒ fan offline is expected.** Each fan declares its AP's state
  sensor (`ap_status_entity`), and while that AP reads `disconnected` /
  `not_home` / `off` the fan is **not repair-worthy**: the power-cycle is held
  and its grace/backoff clocks do not accrue. Power-cycling a fan cannot fix
  the AP it cannot reach.
- **Sub-minute flapping is 2.4 GHz airtime, not a fan fault.** Dozens to hundreds of
  20-40 s `unavailable` blips a day that self-recover mean the fan's 2.4 GHz radio is
  saturated. Judge that by blip **duration** and the airtime measurement — *not* by
  `device_repairs`: no blip lasts the ~6 min the 180 s poll / 5 min grace needs, so it
  would read `idle` even with auto-repair on, and with it off
  (`auto_repair_enabled_default: false`, the default here) `idle` says nothing at all.
  On 2026-09-05 the cause was two G6 Instant Wi-Fi cameras (`HNETCameras`) streaming
  4-8 Mbit/s on 2.4 GHz after roaming onto the fans' APs. `HNETCameras` is **5 GHz-only
  by design** since then; a camera showing `radio="ng"` means that WLAN setting
  regressed. Power-cycling the fan cannot fix airtime.

| Fan | Entity | IP | Access point |
|-----|--------|----|--------------|
| Pink Room | `fan.pink_room_fan_fan` | 192.168.50.112 | Guest Room U7 Pro (roamed off Kitchen Pantry 2026-08-31 ~16:47Z, held since) — **weakest link (-65 dBm)** |
| Blue Room | `fan.blue_room_fan_fan` | 192.168.50.134 | Guest Room U7 Pro |
| White Room | `fan.white_room_fan_fan` | 192.168.50.187 | Guest Room U7 Pro |
| Primary Bedroom | `fan.primary_bedroom_fan_fan` | 192.168.50.146 | Primary Closet U7 Pro |
| Living Room | `fan.livingroom_fan_fan` | 192.168.50.148 | Livingroom U7-Pro-Wall |
| Study | `fan.study_fan_fan` | 192.168.50.179 | Kitchen Pantry U7 Pro |

Fans roam between APs; the **Access point** column above is the AP each fan usually holds
(it mirrors `ap_status_entity`, which the checker reads from its own config — do not go
looking for `apps-prod.yaml`, the image does not carry it). Confirm the live one with `unpoller_client_rssi_db{name=~"MF Fan.*"}` and
read the `ap_name` label before trusting an AP verdict. Use the **regex**, not an exact name:
the UniFi client names do not track the checker's fan names — Living Room is
`MF Fan Livingroom` — so an exact match can return an empty vector that reads as "not
associated". (Observed 2026-09-05: `MF Fan Pink Room`, `MF Fan Blue Room`, `MF Fan White
Room`, `MF Fan Study`, `MF Fan Livingroom`, `MF Fan Primary Bedroom`.)

## Repair backoff ladder (CrashLoopBackOff)

Each fan carries its own ladder — one long-failed fan never fast-tracks a
power-cycle of another fan that merely blipped.

- **Retry delays double**: attempt *n* schedules attempt *n+1* after
  `delay × 2^(n-1)` minutes — **5 → 10 → 20 → 40 → 80 → 160 → 320**, capped at
  `repair_backoff_max_min: 360` (6h). The episode never ends on failure; it
  just slows down.
- **A "successful" repair does NOT reset the ladder.** Only a recovery
  *sustained* for `repair_backoff_reset_min: 30` minutes of fully-clean cycles
  (State **and** Ping ok) clears `attempts`. A fan that comes back for one poll
  and drops again **resumes** the ladder — the false success counts as a failed
  attempt and the next retry waits out the doubled backoff.
  (Before this, every false recovery reset the ladder to attempt 1: ~11
  power-cycles in 5h on 2026-08-31, each one a page.)
- **The ladder survives an app reload.** It is persisted to
  `input_text.fans_health_repair_ladder` as compact JSON
  `{fan: [attempts, next_retry]}` and re-seeded at startup — an HA restart or
  plugin reconnect re-initialises every AppDaemon app, and used to reset a
  climbing ladder back to instant power-cycles. A restored retry time is
  floored to `now + delay` (5 min), so a reload never fires a power-cycle
  immediately.
- **All fans entity-down at once** is a systemic-outage signature (HA, the
  integration, or the network) — auto-repair is suspended entirely and every
  timer cleared until it clears.
- **A manual `start_repair` wipes every fan's ladder** back to attempt 1. That
  is deliberate (a human declaring a fresh start) — but it means firing one to
  "hurry things along" throws away hours of accumulated backoff for **all six
  fans**. Use it once, on purpose.

## Symptoms

- Alert `checker=fans` (default alertname `CeilingFansUnhealthy`), severity
  critical.
- Description names the failing fan + check, e.g. `Living Room State:
  unavailable (Wi-Fi fan; AP Livingroom U7-Pro-Wall: connected — fan itself
  unreachable)` or `Study Ping: timeout (3 attempts)`.
- A **failing** `State` detail normally carries an AP verdict — read it, it is the
  triage's first branch. Two details lack one: a passing check (the `ok` detail is a
  bare `on`/`off`) and an `Error: <exc>` detail (the entity read itself threw). In
  either case get the verdict from `GET /api/states/sensor.<ap>_state` instead, per
  Diagnosis step 2.
  - `… (Wi-Fi fan; AP <name> is disconnected — fan offline expected,
    power-cycle held until the AP recovers)` → **AP fault, not a fan fault.**
  - `… (Wi-Fi fan; AP <name>: connected — fan itself unreachable)` → the fan is off
    the network and the AP is fine. Usually the power-cycle is the right tool — **but
    this is also exactly what a 2.4 GHz airtime event looks like**, so if the fan is
    *flapping* rather than steadily down, clear Diagnosis step 2 before repairing.
  - `… (Wi-Fi fan; AP <name>: state unknown)` → the UniFi integration itself
    is unreadable; repair is **not** gated (an unknown AP never disables
    repair), but weigh the network as a suspect.
- Note: a fan that is simply **off is healthy** — only `unavailable`/`unknown`
  state (or ping failure) is a fault.
- The `[RESOLVED]` page lags real recovery by ~15 min: the controller holds
  every resolve/de-escalation until it is sustained (`alert_improve_hold_s:
  900`). A firing alert that the dashboard already shows green is that hold,
  not a stuck alert — do not "fix" it.

## Diagnosis

1. Read `checkers.fans.checks[]` — list **which** fans and **which** check
   (State vs. Ping) are red, and read the AP verdict in each `State` detail.
   Multiple fans failing together, especially fans on the same AP
   (Pink+Blue+White on Guest Room), points at that AP or the network — not at
   the fans. Three of the six now hang off Guest Room, which is also where an
   airtime hog (step 2) does the most damage.
   **If nothing is red, do not stand down** — that is the *expected* snapshot for a
   flapping incident. `alert_improve_hold_s: 900` keeps a firing critical alive through
   fully-ok cycles, and any critical sighting restarts the window, so a page can outlive
   every visible fault by 15 minutes. Go to step 2 and read the cycle history rather than
   calling it a stale page.
2. **Check 2.4 GHz airtime when the red fans keep moving** — but read the AP verdicts
   from step 1 first.
   **Which fans are "in scope":** every fan **not `ok`** in `checks[]` — `critical`
   *and* `warning`. Do not read "red" as critical-only: the cross-check downgrade makes
   `warning` the status an airtime blip normally reports, so a critical-only filter comes
   back empty on exactly the all-partial snapshot this step is written for. And when
   *nothing* is currently failing (the improve-hold arrival, per step 1), fall back to the
   fans that appear failing across the recent cycle history below. Never conclude "nothing
   is red, nothing to check" — that is the case this step exists for.
   **Getting an AP verdict at all:** a *failing* `State` detail normally carries one,
   `warning` included — the downgrade appends `" (partial failure)"` rather than
   rebuilding the string, so the AP note survives it. Two details carry none: an `ok` one
   (a bare `on`/`off`, no AP state reaches the payload) and an `Error: <exc>` one (the
   entity read threw before the note was built). Whenever the verdict is missing — the
   all-green arrival above, or an error detail — fetch it yourself with `GET /api/states/sensor.<ap>_state` for the APs named in
   the fan table above (`sensor.guest_room_u7_pro_state`,
   `sensor.kitchen_pantry_u7_pro_state`, `sensor.livingroom_u7_pro_wall_state`,
   `sensor.primary_closet_u7_pro_state`); `disconnected` / `not_home` / `off` count as
   down.
   **Then do this per fan:** skip this step for any in-scope fan whose own AP is
   down (gate 1 spans co-channel neighbours whose radios are still reporting, so the
   workup can manufacture an "airtime, do not power-cycle" verdict on what is squarely an
   AP fault), and run it for the rest. Only when *every* in-scope fan sits behind a down
   AP does the whole step fall away — go to step 3. Everything below assumes the fan under
   test has its AP reading *connected*.
   With that settled: the page you woke on can be pure flapping, so do not go looking for
   one continuously-down fan. The alert clocks (`_pending`/`_active`) are keyed by **checker, not fan**, and all
   six fans report into one result list — so the checker stays non-ok while *any* fan is,
   and fans taking turns keep the clock running with no single fan down two polls in a row.
   That is the shape of an airtime event on the Guest Room cluster. Exactly *which* cycle
   promotes depends on the cross-check downgrade, below — read that before you try to match
   the page against the log.
   **How the clock actually runs** — this decides whether the Loki timeline explains the
   page. `apply_cross_check_per_device` downgrades a fan's `critical` to `warning` (detail
   gains `" (partial failure)"`) whenever its *other* check still passes, so an airtime
   event's cycles are **mostly partials** — and `warning` is itself alertable
   (`alert_for_seconds.warning: 600`, UI-only). Two regimes follow:
   - **Nothing firing yet:** `_pending["since"]` starts on the first **non-ok** cycle of
     any severity and survives warning cycles, so a run of partials punctuated by a
     both-checks-red cycle past 300 s promotes straight to critical. It resets on any
     **fully green** cycle — with six fans blipping those are common — so only an
     *unbroken* run counts and one all-ok cycle restarts the 300 s. Date the promoting
     window from the last green cycle, not from the start of the day's churn.
   - **Once the partials have themselves paged** — with `alert_for_seconds.warning: 600`
     the warning promotes on the **fifth** consecutive non-ok cycle (pending at t=0,
     `elapsed ≥ 600` first true at t=720), clearing that pending entry. Everything before
     that is still regime 1: a critical cycle at t=540 promotes *critical* with no warning
     alert ever firing, so do not switch rules early. Once the warning is active: a later critical cycle opens a
     **fresh** escalation clock that must sustain its own 300 s — at a 180 s cadence, the
     third consecutive critical cycle. **Two** things reset that clock, and they log
     differently: a cycle falling back to warning gives
     `Escalation dropped for checker 'fans' — returned to severity=warning before promotion`,
     while a **fully green** cycle deletes the pending escalation too but logs
     `Alert suppressed for checker 'fans' — recovered after Ns pending`. With six fans
     blipping the green cycle is the *usual* interrupter, so do not look only for
     `Escalation dropped`. The warning alert itself stays up throughout — it is the
     escalation to critical that keeps restarting.

   **One more delay before you do the arithmetic:** `alert_repair_hold_cap_s: 1800`
   withholds a *critical* promotion while `repair_state.status` is `pending`,
   `in_progress` **or `success`** — and the fan checker rolls the per-fan states up into
   one value (`in_progress` > `pending` > `failed` > `success`), so a lone `success` holds
   while a mixed success+failed outcome does not. The hold keys only on
   `severity == "critical"`, and the escalation branch runs through the same
   `_promotion_due` — so it inflates **both** promotion lines: regime 1's 300 s can read
   1080 s, and regime 2's third-consecutive-critical becomes the seventh. Since
   Remediation step 4 has you fire `start_repair` yourself, this is most likely exactly
   when you are reading. Look for
   `Repair hold for checker 'fans' — critical promotion withheld …` in the same stream
   before concluding an inflated elapsed figure means the other regime — or that the
   timeline fails to explain the page.
   **Tell the regimes apart from the promotion line itself** — the bridge names them:
   `Alert promoted for checker 'fans' after Ns unhealthy` is regime 1, and
   `Escalation promoted for checker 'fans' after Ns sustained` is regime 2. Read that line
   first, then match the cycles to the right rule: regime 1 promotes on the **first**
   critical cycle past 300 s of unbroken non-ok, so a single both-checks-red cycle *is* a
   complete explanation there; only regime 2 needs the sustained critical run. Either way,
   do not count criticals and conclude "not flapping" when you see few, and check **both**
   reset lines (`Escalation dropped` and `Alert suppressed`) for the runs that did not
   make it.
   The per-fan evidence is in **Loki**, logged unconditionally every cycle:
   `{namespace="home-automation", app="appdaemon"} |= "Check cycle complete for 'Ceiling Fans'"`
   over ~6h gives one line per 180 s naming every fan's `State`/`Ping`. Read down it: a
   *different* fan red each cycle (rather than the same one throughout) is the flapping
   signature, and it names which fans, which is what you need for the AP question below.
   Those statuses are **post-downgrade**, so a partial reads `State=warning`, not `critical`.
   (`|= "Alert suppressed"` marks blips too — it is written whenever a pending clock was
   live and the checker then went fully green, which covers both a pre-incident blip *and*
   an escalation dropped under a firing warning alert, so it is live-triage evidence in
   regime 2. It stops only once a *critical* is active with nothing pending. It is
   checker-scoped either way, so it never names a fan — pair it with the per-cycle line
   above for that.)
   `alert_history[]` corroborates coarsely: `State` is a point-in-time `get_state` on a
   180 s cadence, so a 20-40 s blip is shorter than one poll and most leave no entry; the
   controller records **both** directions, so a caught round-trip is two entries and a
   co-failing `<Fan> Ping` adds its own pair — the 50-entry ring (`alert_history_max`) holds
   only ~12-25 round-trips and can wrap inside an hour. For true blip duration use HA state
   history on the fan entity — the ids are **not** a uniform template
   (`fan.pink_room_fan_fan`, but `fan.livingroom_fan_fan`); take them from the table above —
   **not** from `apps-prod.yaml`, which the Docker build strips out of the image
   (`docker/Dockerfile`: it ships the processed `apps.yaml` instead).
   Do **not** judge from `device_repairs` (`idle` here regardless — see the domain fact).
   Then measure airtime. PromQL:
   `max by (name) (avg_over_time(unpoller_device_radio_channel_utilization_receive_ratio{radio="ng"}[1h]))`
   — receive airtime above ~0.3 on a fan's AP (baseline is 0.02-0.05) means an associated
   client is hogging uplink — on that AP *or* on a co-channel neighbour. Rank the offenders
   directly, **site-wide**, and do not guess from RSSI:
   `topk(5, max by (name, ap_name) (rate(unpoller_client_receive_bytes_total{wired="false"}[1h])))`
   Unscoped on purpose: on 2026-09-05 the fans flapped on Guest Room while both cameras sat on
   Livingroom-Wall and Kitchen Pantry, so an `ap_name="<that AP>"` filter would have come back
   clean. The `ap_name` in the result is what tells you where the hog actually is. Values are
   bytes *from* the client, B/s. **The hog bar is ~100 kB/s (≈0.8 Mbit/s)**: a fan pulls
   ~280 B/s and a quiet 2.4 GHz band tops out in the single-digit kB/s, so anything at
   100 kB/s or above is ~350x a fan and worth chasing — the 2026-09-05
   cameras measured 380-960 kB/s depending on the window.
   A streaming camera is the usual suspect — this query named both G6s outright — but confirm,
   don't assume.
   **`topk` alone is never the verdict.** It always returns five rows, and the client byte
   counters carry `ap_name` but **not** `radio` — so its top talkers are usually 5 GHz
   clients doing nothing wrong (`HNETCameras` lives on 5 GHz by design now, and the G6s
   still top this list). Before a name counts as a hog, confirm it is on 2.4 GHz:
   `unpoller_client_rssi_db{radio="ng"}` lists who is associated on `ng` and on which AP.
   The band-filtered `..._receive_ratio{radio="ng"}` reading is the gate; the `topk` only
   supplies the culprit's name. If the unbanded top five contains no `ng` client at all —
   likely, since 5 GHz talkers head it — rank *within* the band by joining the two:
   `topk(5, max by (name, ap_name) (rate(unpoller_client_receive_bytes_total{wired="false",
   name!~"MF Fan.*"}[1h])) and on (name) (max by (name) (unpoller_client_rssi_db{radio="ng"})))`
   which returns only non-fan clients associated on 2.4 GHz. **Exclude the fans** — they are
   `ng` clients themselves and will otherwise top a quiet band and get written up as their
   own hog. Run the unbanded form first (it is how the 2026-09-05 cameras were
   caught, before they moved to 5 GHz) and fall back to this when it comes back all-5 GHz.
   **Derive the co-channel set live** rather than trusting a remembered one — UniFi
   auto-channel moves APs: `unpoller_device_radio_channel{radio="ng"}` gives each AP's 2.4
   GHz channel, and the neighbours sharing the fan's AP's value are the ones that can hurt
   it (2026-09-05: Guest Room and Livingroom-Wall on 1; Kitchen Pantry, Server Room and
   Storage on 6; Garage-Wall and Primary Closet on 11). Read
   `..._receive_ratio{radio="ng"}` on those neighbours too — the **same** metric as the
   fan's own AP, because that is what Remediation gate 1 consumes and what the ~0.3 /
   0.02-0.05 figures are calibrated for. (`..._total_ratio` folds in transmit and beacon
   overhead and sits structurally higher; read it for colour, never against those
   thresholds.) Fix the hog (move it to 5 GHz, lower its bitrate, lock it to its home AP)
   — do not power-cycle fans.
3. For each in-scope fan whose AP verdict says the AP is down, triage **that AP**
   (UniFi: is it adopted/powered/uplinked?). The checker has already held those fans'
   power-cycles, so there is nothing to repair on them. This accounts for the **whole
   page** only if every in-scope fan is behind a down AP — otherwise the remaining fans still need step 2's
   airtime workup and steps 4-6.
4. Read `repair_state.device_repairs[<fan>]` — per-fan status. It takes only
   `idle`, `in_progress`, `success` and `failed`: **never `pending`**, because the
   pre-repair grace countdown is checker-wide. So the state meaning "a power-cycle is
   about to fire on its own" is not here — it is `repair_state.status: pending` with
   `repair_state.auto_repair_deadline`, which is what Remediation step 3 gates on.
   - `in_progress` → a ZEN32 cycle is running (budget 300s) — wait.
   - `failed` with `(attempt N; retry at HH:MM)` → the ladder is climbing;
     attempt N already ran and did not stick. **This is expected behaviour,
     not a stuck repair.** Note N — a high N means the fan is crashlooping and
     that is the escalation signal.
   - `success` → the last power-cycle brought the fan back; the ladder still
     holds its rung until 30 clean minutes pass.
5. `State: unavailable` = the Modern Forms integration lost the fan (Wi-Fi
   drop / ESP wedged). `Ping: no response` alone = a power-save miss or the
   fan is off the network; it never triggers a power-cycle on its own.
6. Loki: `{namespace="home-automation", app="appdaemon"} |= "fans"` (or
   `|= "Ceiling Fans"`) last 1h — repair-script calls, recovery polls,
   `relapsed after repair — resuming backoff ladder`, AP up/down transitions,
   and `Restored repair backoff ladder from input_text…` after a reload.

## Remediation ladder

1. `record_note` the triage start (which fans/checks failed + each AP verdict).
2. **Two stops live here — check them in this order.**

   **(a) For each in-scope fan (step 2 above) whose AP is down → stop *for that fan*.** Take the AP-down
   exit below for it and do not apply the airtime table to it: a down radio reports no
   airtime, so baseline ratios are exactly what you expect and prove nothing.
   **This is per fan, not per checker.** The six fans span four APs and the checker reads
   each fan's AP separately, so one down AP does **not** close the incident — if any other
   in-scope fan has its AP up, carry on to (b) for those. Only when *every* in-scope fan
   is behind a down AP is the whole page an AP fault.

   **(b) For in-scope fans whose AP is up → test the airtime branch** on two band-filtered
   readings:
   - **Gate 1 — airtime:** the highest `..._receive_ratio{radio="ng"}` across the fan's
     own AP *and* its co-channel neighbours (derive those from
     `unpoller_device_radio_channel{radio="ng"}`). The fan's own AP reading clean does
     not by itself clear the branch. Use the **receive** ratio for both — the ~0.3 bar
     and the 0.02-0.05 baseline are calibrated for it; `..._total_ratio` folds in
     transmit and beacon overhead and sits structurally higher, so read it for colour,
     never against these thresholds.
   - **Gate 2 — culprit:** a client that is *not* one of the fans, confirmed on `ng` via
     `unpoller_client_rssi_db{radio="ng"}`, at or above the ~100 kB/s hog bar from
     Diagnosis step 2 (the 2026-09-05 cameras measured 380-960 kB/s). A top-of-list
     reading in the single-digit kB/s is a quiet band, not a hog — gate 2 **failing**,
     not passing. (`topk` always returns rows, so without a threshold it cannot fail.)

   | Gate 1 (`ng` receive) | Gate 2 (culprit) | Do this |
   |---|---|---|
   | ≥ ~0.3 | named | **Stop** — airtime confirmed with a culprit. Airtime exit below. |
   | ≥ ~0.3 | none | **Stop anyway** — the band is saturated even if the `topk` cannot name who. `record_note` the AP, the ratio and the channel, say the hog was not identified, and let the page through. Do **not** power-cycle. |
   | ~0.05-0.3 | either | Ambiguous — do not stop on it alone. `record_note` the reading and continue to step 3; if a later `start_repair` fails to hold, re-read this as airtime rather than climbing the ladder. Same exception as the row below: if any other in-scope fan stopped on airtime, the mixed-incident rule takes steps 3-5 off the table and this fan waits with the rest. |
   | ≤ ~0.05 | either | **Not** an airtime event, however fat the unbanded `topk` looks. Carry on down the ladder for this fan — a genuinely wedged fan deserves its `start_repair`, *unless* any other in-scope fan stopped on airtime (see the mixed-incident rule below, which takes steps 3-5 off the table checker-wide). |

   The two stops exit differently — take the right one:
   - **AP down** → handle the access point (or escalate it) and let the checker resume
     on its own. Its fans' power-cycles are already held and their backoff clocks are not
     accruing, so there is nothing to do on those fans and no page-worthy fault in them;
     recovery is automatic once the AP is back. This closes the *page* only if every
     in-scope fan sits behind a down AP — otherwise finish (b) for the rest before exiting.
   - **Airtime confirmed** → the AP reads *connected*, so nothing below gates on it and
     `start_repair` would cycle **every** entity-down fan — up to three at once when Guest
     Room is the affected radio — and wipe all six backoff ladders, for a cause a
     power-cycle cannot touch. Nothing self-heals here either: the remedy is a UniFi
     console change (move the hog to 5 GHz, cap its bitrate, lock it to its home AP),
     outside the sanctioned write actions, and the Escalate triggers below (attempt
     counts, recovery budget) will never fire because no repair ran. So `record_note` the
     hog — client name, its `ap_name`, its byte rate, the saturated radio — and **let the
     page through** to a human with the Grafana/Alertmanager links.
     **The note is lossy: keep it short and do not let it be the only copy.** It is
     truncated at 280 chars and lands in the same 50-entry `alert_history` ring as check
     transitions, with no carve-out for notes — and Diagnosis step 2 says that ring can
     wrap inside an hour during exactly this incident. Lead with the client name and its
     AP so the first clause survives truncation, and put the full reasoning in the
     escalation summary, which is what the human actually reads.

   In both cases: do not silence the alert, and do not fall through to steps 3-5 for a
   fan this step stopped on. **`force_recheck` is not read-only** — it runs a full check
   cycle, which evaluates auto-repair and can fire `script.zen32_hard_reset` on the
   earliest-due fan whenever the live auto-repair toggle is on and a grace/backoff
   deadline has passed. In a mixed incident the wedged fan's timer is exactly the one
   that has accrued, so step 3 can power-cycle during an airtime stop. Treat 3-5 as
   equally off the table here.

   **A mixed incident still blocks step 4 for everyone.** `start_repair` is
   checker-wide — there is no per-fan variant. It rebuilds its own candidate list of
   every entity-down fan, and the only exclusion is a fan whose **AP is down**; an
   airtime-stopped fan is by definition not excluded — and the candidate list is rebuilt
   **at fire time**, not from the snapshot you read, so a fan that looks green right now
   can still be picked up. So if *any* in-scope fan stopped on
   airtime, firing `start_repair` for a different wedged fan would power-cycle the
   airtime fans too and wipe all six ladders — the thing the airtime exit above forbids.
   In that case: `record_note` both findings (which fans are airtime-blocked, which one
   looks genuinely wedged), skip steps 3-5, and let the page through so a human can
   power-cycle the wedged fan by hand after the hog is dealt with. Note `force_recheck`
   is **not** a safe read here either — see above.
3. `force_recheck` (payload `{}`) — clears a one-poll blip (fan mid-reboot).
   Wait ~180s, re-read. **Not a passive read:** it runs a full check cycle, which
   evaluates auto-repair and can fire `script.zen32_hard_reset` on the earliest-due fan.
   Gate it on `repair_state.auto_repair_enabled`: **true** means treat it as a
   power-cycle you are scheduling. **False clears only the fan side** — the command is
   global (one un-targeted `health_check_recheck` to every checker), and five prod
   checkers evaluate auto-repair on it: `fans`, `printer`, `spa`, `shade_gateway` and
   `protect`. The first three default off and the last two on, but every one re-reads its
   `input_boolean` live each cycle, so a default proves nothing — check each
   `auto_repair_enabled` before firing. See the command table in `README.md`.
   Do not try to clear it by checking whether a due time has *passed* — that test is
   nearly always false while auto-repair is on, because `auto_repair_deadline` is only
   published while it is still in the future and is nulled the moment it fires, and a past
   `next_retry_at` is visible only when a repair is already running or the toggle is off
   (both states where this command cannot start one). What matters is whether one is
   **queued**: a non-null `repair_state.auto_repair_deadline` (idle fans, checker-wide) or
   `device_repairs[<fan>].next_retry_at` (failed fans) says yes. Either value is up to one
   `check_interval_s` stale and may be seconds away, and this step's Loki reconstruction
   and PromQL queries take minutes — so "not due yet" is not a clearance. A fan reading
   `idle` is not one either; the checker-wide deadline governs it. Do not reach for this
   after a step-2 airtime stop.
4. **First re-read step 2 — these entry conditions are the airtime signature verbatim.**
   "Still critical, AP up, `device_repairs` idle" is exactly what a saturated 2.4 GHz
   radio produces, so if step 2's gates tripped for *any* fan, stop here: `start_repair`
   is checker-wide and would power-cycle the airtime fans too (see the mixed-incident
   rule in step 2). Otherwise, with the airtime branch cleared: if still critical and the
   fan's AP is up — **and no repair is already queued.** That takes **both** reads, for
   the two different queues: `repair_state.status: pending` + `auto_repair_deadline` for
   an *idle* fan's pre-attempt countdown (checker-wide — a fan's own `idle` says nothing
   about it, per Diagnosis step 4), and `device_repairs[<fan>].next_retry_at` for a
   *failed* fan's backoff retry. Neither substitutes for the other: on a backoff retry
   the evaluator explicitly clears `_repair_status` and `_auto_repair_deadline` ("don't
   advertise pending"), so the checker-level fields read clean while an attempt is
   scheduled. Stand down if either says a repair is coming —
   nothing downstream will refuse you (`_handle_start_repair` checks only
   `supports_repair`, and `_is_any_repair_active` tests `in_progress`, never `pending`),
   so firing into one resets all six ladders to attempt 1 and cycles every entity-down
   fan seconds before the targeted single-fan auto-repair would have run. That is the
   "don't stack repairs" case the README's universal preconditions forbid. With all of
   that clear: `start_repair`
   `{"checker_id":"fans"}` — repairs **all** currently entity-down fans
   sequentially via `script.zen32_hard_reset`. Prefer this over toggling the
   `power_switch` entities directly — the script sequences the relay/scene
   controls and re-checks the fan.
5. If the fan is already `failed` with a scheduled retry, **let the ladder
   run**. A manual repair only resets everyone's backoff; it does not have a
   better power-cycle than the one that already failed. Max **2**
   `start_repair` attempts / 6h across the checker.

## Verify

- After `start_repair`, budget = `repair_recovery_wait_s` (300s) per fan plus
  one `check_interval_s` (180s). For a single failing fan ≈ **~8 min**; more
  fans repair sequentially, so extend the wait accordingly.
- Recovery = the fan's `State` and `Ping` both back to `ok`. The page then
  resolves once **all** fans are healthy *and* that health has held for the
  controller's 15-minute improvement hold — budget ~**~23 min** end to end
  before the `[RESOLVED]` lands.
- A recovery is only banked once it survives **30 minutes**
  (`repair_backoff_reset_min`); until then `device_repairs[<fan>].attempts`
  still shows the rung. Do not report "fixed" off a single clean cycle — that is
  exactly the false-recovery signature that caused the page storm.

## Escalate

If a fan keeps climbing the ladder (attempt ≥ 3 with no sustained recovery), or
fans don't recover within budget, let the page through with a `record_note`
summary:
- which fans/checks are still red, State vs. Ping, and each fan's AP verdict;
- the fan's attempt count and next retry time — a crashlooping fan is a
  hardware/Wi-Fi story, and the ladder is the evidence;
- that ZEN32 hard-resets (`script.zen32_hard_reset`) were attempted, how many,
  and that none held;
- likely cause — a single crashlooping fan (its ESP Wi-Fi module or the ZEN32
  relay: reseat/replace) vs. several fans on one AP or channel (first a
  2.4 GHz airtime hog — see Diagnosis step 2 — then that access point: RSSI,
  channel utilization, uplink; Pink Room is the known-weak client at -65 dBm,
  and note Guest Room already carries three of the six, so moving another fan
  onto it is the wrong direction) vs. all fans at once
  (HA integration / VLAN / power feeding `192.168.50.x`). Attach
  Alertmanager + Loki links.
