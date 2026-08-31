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

Two consequences that shape everything below:

- **ESP Wi-Fi power-save drops single pings.** `Ping` alone is retried
  (`PING_ATTEMPTS = 3`) and a ping-only failure **never** power-cycles a fan —
  only `State: unavailable/unknown` justifies cutting power.
- **AP down ⇒ fan offline is expected.** Each fan declares its AP's state
  sensor (`ap_status_entity`), and while that AP reads `disconnected` /
  `not_home` / `off` the fan is **not repair-worthy**: the power-cycle is held
  and its grace/backoff clocks do not accrue. Power-cycling a fan cannot fix
  the AP it cannot reach.

| Fan | IP | Access point |
|-----|----|--------------|
| Pink Room | 192.168.50.112 | Kitchen Pantry U7 Pro — **weakest link (-63 dBm, a downstairs AP)** |
| Blue Room | 192.168.50.134 | Guest Room U7 Pro |
| White Room | 192.168.50.187 | Guest Room U7 Pro |
| Primary Bedroom | 192.168.50.146 | Primary Closet U7 Pro |
| Living Room | 192.168.50.148 | Livingroom U7-Pro-Wall |
| Study | 192.168.50.179 | Kitchen Pantry U7 Pro |

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
   Multiple fans failing together, especially two on the same AP (Pink+Study,
   Blue+White), points at that AP or the network — not at the fans.
2. If any AP verdict says the AP is down, triage **the AP** (UniFi: is it
   adopted/powered/uplinked?). The checker has already held the power-cycles;
   there is nothing to repair on the fan side and no page-worthy fan fault.
3. Read `repair_state.device_repairs[<fan>]` — per-fan status:
   - `pending`/`in_progress` → a ZEN32 cycle is running (budget 300s) — wait.
   - `failed` with `(attempt N; retry at HH:MM)` → the ladder is climbing;
     attempt N already ran and did not stick. **This is expected behaviour,
     not a stuck repair.** Note N — a high N means the fan is crashlooping and
     that is the escalation signal.
   - `success` → the last power-cycle brought the fan back; the ladder still
     holds its rung until 30 clean minutes pass.
4. `State: unavailable` = the Modern Forms integration lost the fan (Wi-Fi
   drop / ESP wedged). `Ping: no response` alone = a power-save miss or the
   fan is off the network; it never triggers a power-cycle on its own.
5. Loki: `{namespace="home-automation", app="appdaemon"} |= "fans"` (or
   `|= "Ceiling Fans"`) last 1h — repair-script calls, recovery polls,
   `relapsed after repair — resuming backoff ladder`, AP up/down transitions,
   and `Restored repair backoff ladder from input_text…` after a reload.

## Remediation ladder

1. `record_note` the triage start (which fans/checks failed + each AP verdict).
2. If the AP is down → **stop**. Nothing here is a fan fault; handle the AP (or
   escalate it) and let the checker resume on its own.
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
  relay: reseat/replace) vs. several fans on one AP (that access point: RSSI,
  channel utilization, uplink; Pink Room is the known-weak client at -63 dBm on
  a downstairs AP and is a candidate for re-pinning to a closer AP) vs. all
  fans at once (HA integration / VLAN / power feeding `192.168.50.x`). Attach
  Alertmanager + Loki links.
