# Smart-Home Shepherd — per-checker triage runbooks

Agent-facing runbooks for the **smart-home Shepherd** (design:
`.agents/plans/smart-home-shepherd.md`). The Shepherd is a headless Claude
Code agent (cluster tooling, not an AppDaemon app) that wakes on a firing
critical health-check alert, tries the documented remediation, waits for
recovery, and only pages a human — *with a diagnosis* — if the runbook fails.

These are **not** part of the published mkdocs site. They are operational
scripts for an autonomous agent.

## When the Shepherd runs

A CronJob (every ~10 min) queries Alertmanager for firing criticals with
`source="appdaemon-health-check"`. Each firing alert carries a
`checker=<checker_id>` label — that label is authoritative for picking the
runbook (`<checker_id>.md`). `alertname` is secondary (default
`<CamelCaseCheckerName>Unhealthy`, e.g. `SpaUnhealthy`,
`ShadeGatewayUnhealthy`, `CeilingFansUnhealthy`; overridden for protect →
`ProtectEventStreamFrozen`).

Because of the controller's **for-gate**, a *firing* critical means the
checker stayed **non-ok** for ≥300s before promotion, ending on a critical
cycle — the clock starts at the first non-ok cycle of any severity, so
`warning → warning → critical` promotes on that one critical cycle. Except
`shade_gateway` and `ups`, which have a `critical: 0` override and page the
instant they go critical. For every other checker a single bad sample cannot reach the
phone, so the fault is *sustained* by the time the Shepherd sees it — but for `ups` and
`shade_gateway` it can be one cycle old, so confirm those are still failing before acting.

Note "sustained" is measured on the **checker**, not a device: a checker whose
devices take turns failing stays non-ok throughout and can page without any one
device being down for the whole window. And if a *warning* alert is already
active, a critical must clear a **fresh** 300s escalation gate — a single cycle
back at warning logs `Escalation dropped …` and restarts it. `fans.md` step 2
works through both cases.

## Sanctioned-action ladder (do these in order)

1. **Context** — read state before touching anything:
   - HA REST: `GET /api/states/sensor.health_check_status` →
     `attributes.checkers.<checker_id>`. Key fields: `status`, `checks[]`
     (each `{name, status, detail}`), `repair_state` (`{status, detail}` —
     `idle|pending|in_progress|success|failed`), `muted`, `muted_until`,
     `last_check`, `alert_history[]`, `is_dependency`.
   - Loki: `{namespace="home-automation", app="appdaemon"}` filtered to the
     checker (e.g. `|= "shade_gateway"` / `|= "Shade Gateway"`), last ~1h.
   - The checker's `README.md` in the baked repo copy
     (`appdaemon/apps/health_checks/checker_apps/<pkg>/README.md`).
   - HA state history (`GET /api/history/period/...` for a device entity) when a
     runbook needs *when* and *how long*, rather than the checker's 180 s-sampled
     view — e.g. true blip duration for a flapping Wi-Fi device.
   - HA entity state for infrastructure a runbook names (`GET /api/states/<entity_id>`)
     — e.g. the UniFi AP state sensors behind the fan checker's AP verdict, which only
     appears in a `State` detail while that check is `critical`, so on a recovered-looking
     snapshot this read is the only way to get it.
   - Prometheus (read-only) at
     `http://kube-prometheus-stack-prometheus.observability.svc.cluster.local:9090`
     (`/api/v1/query`, same cluster/namespace as the Alertmanager the bridge posts to),
     when a runbook's Diagnosis calls for it — the
     `unpoller` job carries UniFi device/client telemetry (AP airtime, per-client
     byte rates, RSSI) that HA does not expose. `fans.md` step 2 uses it to tell a
     2.4 GHz airtime problem from a fan fault, and that verdict can **halt** the
     remediation ladder, so it is a first-class source, not just an escalation link.
2. **Runbook match** — load `agent-docs/shepherd-runbooks/<checker_id>.md` and
   follow its **Diagnosis** section.
3. **Bounded remediation** — *sanctioned paths only* (below). The repair logic
   lives inside the checker apps; the Shepherd only **triggers** it.
4. **Verify** — wait one `check_interval_s` (+ any settle window the runbook
   names), re-read `sensor.health_check_status`. On recovery the
   AlertmanagerBridge posts `endsAt=now` and the page resolves itself.
5. **Escalate or stand down**:
   - Recovered → `record_note` the outcome, add a Grafana annotation, exit.
     Human never paged.
   - Not recovered within budget → let the page through to Pushover *with the
     diagnosis*: which check failed, what was tried, and the
     Alertmanager/Grafana links. `record_note` the same summary.

### The only sanctioned write actions

All Shepherd HA writes go through `script.health_check_relay`. Call it via HA
REST: `POST /api/services/script/health_check_relay` with body
`{"command": "<cmd>", "payload": "<json-string>"}` — note `payload` is a
**JSON-encoded string**, not a nested object.

| Command | Payload (JSON string) | Effect | Notes |
|---------|----------------------|--------|-------|
| `force_recheck` | `"{}"` | Broadcasts `health_check_recheck` to **all** checkers — re-runs every check immediately | Global, not per-checker, and **not a passive read**: each checker's cycle evaluates auto-repair, so this can fire a repair on any checker whose toggle is on and whose grace/backoff deadline has passed — including one you are not triaging (`shade_gateway` and `protect` default to auto-repair **on**). Nothing in the code counts those firings against the max-2-attempts-per-6h guardrail — **you** must: count a `force_recheck` that could fire a repair against the same budget, and never reach for it to get another power-cycle once the two `start_repair` attempts are spent. Use it to confirm a fault is still live, not as a free look. |
| `start_repair` | `"{\"checker_id\": \"<id>\"}"` | Triggers that checker's built-in repair (power-cycle / port-cycle / config reload) | Rejected unless the checker's `supports_repair` is true. Battery checkers reject it. |
| `record_note` | `"{\"checker_id\": \"<id>\", \"note\": \"...\", \"source\": \"shepherd\"}"` | Inserts a note into the checker's alert history (visible on the detail card) | Note capped at 280 chars. Leaves the audit trail — always record what you tried. |

**Explicitly whitelisted repair switches**: the design authorizes documented
repair switches (e.g. `switch.spa_intouch3_switch`) as a fallback. In
practice **every repair-capable checker here already exposes a built-in repair
via `start_repair`**, so prefer `start_repair` and do **not** toggle switches
directly — the built-in path carries the grace periods, one-attempt-per-episode
guards, and recovery verification that a raw switch toggle would bypass. Only
reach for a whitelisted switch if a runbook explicitly tells you to.

## Guardrails (never violate)

- **Max 2 remediation attempts per checker per 6h.** Persist the counter
  (ConfigMap or HA helper). No repair loops fighting hardware failures — a
  third would-be attempt goes straight to **Escalate**.
- **Never mute.** The Shepherd never issues `mute_checker` / `unmute_checker`.
  If something is too noisy, escalate to a human; do not silence it.
- **Never kubectl writes.** No cluster mutations, no `call_service` beyond
  `script.health_check_relay`, no HA config-entry / helper edits.
- **Idempotency.** Skip an alert already carrying an in-flight/exhausted
  triage marker (Alertmanager annotation or the checker's `record_note`
  history). Don't double-triage the same episode.

## Universal preconditions (check before *any* remediation)

Run these gates first, in order — several send you straight to skip/escalate:

1. **Muted → SKIP entirely.** If `checkers.<id>.muted == true`, do nothing:
   no remediation, no page. A human silenced it deliberately (the spa, for
   example, is muted indefinitely because the hardware is physically broken).
2. **Repair already running → wait, don't stack.** If `repair_state.status`
   is `pending` or `in_progress`, the checker is self-healing and the
   controller is withholding the page under the repair-hold cap (1800s). Let
   it run; do not fire a second `start_repair`.
3. **Attempt budget exhausted → escalate.** ≥2 Shepherd remediation attempts
   for this checker in the last 6h → skip to **Escalate**.
4. **Dependency first.** If the checker declares a `health_dependencies` entry
   (e.g. spa/locks depend on `cloud`, zigbee batteries depend on `zigbee`)
   and that dependency is itself critical, triage the dependency's runbook —
   the leaf alert is a symptom.

## Runbook index

| Runbook | checker_id | Repairable? | Built-in remediation |
|---------|-----------|-------------|----------------------|
| [spa.md](spa.md) | `spa` | yes | power-cycle `switch.spa_intouch3_switch` — **but muted indefinitely, skip** |
| [shade_gateway.md](shade_gateway.md) | `shade_gateway` | yes | PoE port-32 cycle of the PowerView gateway (auto, one/episode) |
| [protect.md](protect.md) | `protect` | yes | reload the `unifiprotect` config entry (auto, 1/hour) |
| [protect_batteries.md](protect_batteries.md) | `protect_batteries` | **no** | none — physical battery replacement |
| [shade_batteries.md](shade_batteries.md) | `shade_batteries` | **no** | none — disconnects owned by `shade_gateway`; real decline = replace |
| [fans.md](fans.md) | `fans` | yes | per-fan `script.zen32_hard_reset` scene-controller cycle |

These six caused ~43 critical episodes/week before the v1.4.0 paging fixes and
the auto-repair work — they are the highest-value triage targets.
