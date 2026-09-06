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

Fans roam between APs; the table (and `ap_status_entity` in `apps-prod.yaml`) is the AP each
fan usually holds. Confirm the live one with `unpoller_client_rssi_db{name=~"MF Fan.*"}` and
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
- The `State` detail always carries an AP verdict — read it, it is the
  triage's first branch:
  - `… (Wi-Fi fan; AP <name> is disconnected — fan offline expected,
    power-cycle held until the AP recovers)` → **AP fault, not a fan fault.**
  - `… (Wi-Fi fan; AP <name>: connected — fan itself unreachable)` → the fan
    is genuinely wedged/off the network; the power-cycle is the right tool.
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
2. **Check 2.4 GHz airtime before anything else when the red fan keeps moving.** The
   page you woke on can be pure flapping, so do not go looking for one continuously-down
   fan. The alert clocks (`_pending`/`_active`) are keyed by **checker, not fan**, and all
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
   - **Once the partials have themselves paged** (~4 partial cycles promote a *warning*
     alert to active, which clears that pending entry): a later critical cycle opens a
     **fresh** escalation clock that must sustain its own 300 s — at a 180 s cadence, the
     third consecutive critical cycle. **Two** things reset that clock, and they log
     differently: a cycle falling back to warning gives
     `Escalation dropped for checker 'fans' — returned to severity=warning before promotion`,
     while a **fully green** cycle deletes the pending escalation too but logs
     `Alert suppressed for checker 'fans' — recovered after Ns pending`. With six fans
     blipping the green cycle is the *usual* interrupter, so do not look only for
     `Escalation dropped`. The warning alert itself stays up throughout — it is the
     escalation to critical that keeps restarting.

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
   bytes *from* the client, B/s: ~500 kB/s ≈ 4 Mbit/s is already a hog next to a fan's ~280 B/s.
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
   `topk(5, max by (name, ap_name) (rate(unpoller_client_receive_bytes_total{wired="false"}[1h]))
   and on (name) (max by (name) (unpoller_client_rssi_db{radio="ng"})))`
   which returns only clients associated on 2.4 GHz, so it always yields a candidate when
   the gate has tripped. Run the unbanded form first (it is how the 2026-09-05 cameras were
   caught, before they moved to 5 GHz) and fall back to this when it comes back all-5 GHz.
   **Derive the co-channel set live** rather than trusting a remembered one — UniFi
   auto-channel moves APs: `unpoller_device_radio_channel{radio="ng"}` gives each AP's 2.4
   GHz channel, and the neighbours sharing the fan's AP's value are the ones that can hurt
   it (2026-09-05: Guest Room and Livingroom-Wall on 1; Kitchen Pantry, Server Room and
   Storage on 6; Garage-Wall and Primary Closet on 11). Check
   `unpoller_device_radio_channel_utilization_total_ratio` on those. Fix the hog (move it to
   5 GHz, lower its bitrate, lock it to its home AP) — do not power-cycle fans.
3. If any AP verdict says the AP is down, triage **the AP** (UniFi: is it
   adopted/powered/uplinked?). The checker has already held the power-cycles;
   there is nothing to repair on the fan side and no page-worthy fan fault.
4. Read `repair_state.device_repairs[<fan>]` — per-fan status:
   - `pending`/`in_progress` → a ZEN32 cycle is running (budget 300s) — wait.
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
2. If the AP is down — **or** Diagnosis step 2 confirmed 2.4 GHz airtime
   saturation → **stop**. Neither is a fan fault. The airtime half needs **both**
   band-filtered gates, not just a name from the `topk`:
   - `..._receive_ratio{radio="ng"}` above ~0.3 on the fan's own AP **or** on a
     co-channel neighbour (derive the neighbours from
     `unpoller_device_radio_channel{radio="ng"}`) — the fan's own AP reading clean
     does not by itself clear the branch; and
   - the client the `topk` named confirmed present on `ng` via
     `unpoller_client_rssi_db{radio="ng"}`.

   If every `ng` receive ratio on the channel is at baseline (0.02-0.05), this is
   **not** an airtime event however fat the `topk` looks — that list is unbanded and
   5 GHz talkers head it routinely. Carry on down the ladder; a genuinely wedged fan
   deserves its `start_repair`.

   The two stops exit differently — take the right one:
   - **AP down** → handle the access point (or escalate it) and let the checker resume
     on its own. The power-cycles are already held and the backoff clocks are not
     accruing, so there is nothing to do on the fan side and no page-worthy fan fault;
     recovery is automatic once the AP is back.
   - **Airtime confirmed** → the AP reads *connected*, so nothing below gates on it and
     `start_repair` would cycle **every** entity-down fan — up to three at once when Guest
     Room is the affected radio — and wipe all six backoff ladders, for a cause a
     power-cycle cannot touch. Nothing self-heals here either: the remedy is a UniFi
     console change (move the hog to 5 GHz, cap its bitrate, lock it to its home AP),
     outside the sanctioned write actions, and the Escalate triggers below (attempt
     counts, recovery budget) will never fire because no repair ran. So `record_note` the
     hog — client name, its `ap_name`, its byte rate, the saturated radio — and **let the
     page through** to a human with the Grafana/Alertmanager links.

   In both cases: do not silence the alert and do not fall through to steps 3-5.
3. `force_recheck` (payload `{}`) — clears a one-poll blip (fan mid-reboot).
   Wait ~180s, re-read.
4. If still critical, the fan's AP is up, and its `device_repairs` entry is
   `idle` (auto-repair off, or never reached its deadline): `start_repair`
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
