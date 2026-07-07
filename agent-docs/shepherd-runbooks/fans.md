# Runbook: `fans` — Ceiling Fan Health Checker

Monitors Modern Forms ceiling fans (Pink/Blue/White/Primary/Living Room/Study,
etc.) as one checker. **2 checks per fan**: `{name} State`
(`get_state` not `unavailable`/`unknown`) and `{name} Ping` (ICMP to the fan's
IP). `check_interval_s: 180`. **`supports_repair: yes`** — per-fan repair via
`script.zen32_hard_reset`, which power-cycles that fan's zen32 scene
controller. Auto-repair default **OFF**, delay 5 min, `repair_recovery_wait_s:
300`. Each fan gets **one** auto-repair attempt per failure.

## Symptoms

- Alert `checker=fans` (default alertname `CeilingFansUnhealthy`), severity
  critical.
- Description names the failing fan + check, e.g. `Living Room State:
  unavailable` or `Study Ping: no response`.
- Note: a fan that is simply **off is healthy** — only `unavailable`/`unknown`
  state (or ping failure) is a fault.

## Diagnosis

1. Read `checkers.fans.checks[]` — list **which** fans and **which** check
   (State vs. Ping) are red. Multiple fans failing at once points at the
   network/AP feeding them, not individual controllers.
2. Read `repair_state` — the fan checker reports per-fan repair status. If a
   fan is already `pending`/`in_progress`, its scene-controller cycle is
   running (budget 300s) — wait for it.
3. `State: unavailable` = the Modern Forms integration lost the fan (Wi-Fi
   drop / controller wedged). `Ping: no response` = the fan is off the network
   entirely (power/Wi-Fi). Both are addressed by the zen32 hard reset.
4. Loki: `{namespace="home-automation", app="appdaemon"} |= "fans"` (or
   `|= "Ceiling Fans"`) last 1h — repair-script calls, recovery polls,
   per-fan `failed` transitions.

## Remediation ladder

1. `record_note` the triage start (which fans/checks failed).
2. `force_recheck` (payload `{}`) — clears a one-poll blip (fan mid-reboot).
   Wait ~180s, re-read.
3. If still critical and the failing fan isn't already repairing: `start_repair`
   `{"checker_id":"fans"}` — repairs **all** currently-failing fans
   sequentially (resets any `failed` fan states first), power-cycling each
   fan's zen32 scene controller via `script.zen32_hard_reset`. Prefer this
   over toggling the `power_switch` entities directly — the script sequences
   the relay/scene controls and re-checks the fan.
4. Max 2 `start_repair` attempts / 6h across the checker. A fan already in
   `failed` after a repair means the hard reset didn't recover it → Escalate.

## Verify

- After `start_repair`, budget = `repair_recovery_wait_s` (300s) per fan plus
  one `check_interval_s` (180s). For a single failing fan ≈ **~8 min**; more
  fans repair sequentially, so extend the wait accordingly.
- Recovery = the fan's `State` and `Ping` both back to `ok`; the bridge
  resolves the page once **all** fans are healthy.

## Escalate

If a fan stays `failed` after its repair, or fans don't recover within budget,
let the page through with a `record_note` summary:
- which fans/checks are still red and whether it's State vs. Ping;
- that a zen32 hard-reset (`script.zen32_hard_reset`) was attempted per fan and
  the outcome;
- likely cause — a single stuck fan (reseat/replace the zen32 controller or
  the fan's Wi-Fi module) vs. many fans down together (check the AP / VLAN /
  power feeding `192.168.50.x` fan IPs). Attach Alertmanager + Loki links.
