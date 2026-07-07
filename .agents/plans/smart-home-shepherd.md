# Smart-Home Shepherd — agentic triage for health-check pages

**Status: DESIGN (not yet approved for build)**
**Author: Claude, 2026-07-06 overnight session (health-check paging overhaul)**

## Problem

Even after the v1.4.0 paging fixes (escalation gate, repair hold, per-checker
mute), a critical page still means "a human must look at this". Many of those
looks follow a known script: check the checker's detail, glance at Loki, try
the documented remediation (power-cycle switch, port-cycle, force recheck),
wait, confirm recovery. That script is automatable — the same insight behind
the Tier-4 **upgrade-shepherd** in `../haynes-ops` (see
`.agents/runbooks/upgrade-shepherd.md` there).

Goal: a page should reach the phone only after an agent has already tried the
runbook and failed — and the page should carry the diagnosis and what was
attempted.

## Shape

Follow the upgrade-shepherd precedent: a headless Claude Code agent run as a
Kubernetes CronJob/Job in haynes-ops, **not** an AppDaemon app (the repo's
"AI runs in AppDaemon" rule is about HA-vs-AppDaemon placement of LLM
workloads inside the smart-home stack; the shepherd is cluster tooling like
the upgrade agent).

Two candidate integration points, pick one at build time:

1. **New mode on the existing upgrade-shepherd** ("one agent, N modes" is its
   stated design). Pro: reuses image, secrets plumbing, safety prompt,
   paging. Con: needs an HA token the upgrade agent deliberately doesn't
   have; blast-radius coupling.
2. **Sibling deployment** `kubernetes/main/apps/home-automation/shepherd/`
   with its own ExternalSecret (HA token + Anthropic key) and a
   smart-home-scoped safety prompt. Pro: clean separation of credentials and
   scope. **Recommended.**

## Trigger

Polling, not webhook (matches the cluster's poll-only Flux philosophy and the
upgrade-shepherd's scheduled mode): a CronJob every 10 min queries
Alertmanager (`/api/v2/alerts?filter=source="appdaemon-health-check"`) for
firing critical alerts. No firing alerts → exit 0 (cheap). Firing alert →
run triage.

Alertmanager routing change that makes this "triage before page": add a
`triage` route for `source="appdaemon-health-check", severity="critical"`
with `group_wait` long enough for the shepherd to act (e.g. 15m) OR keep
paging as-is initially and let the shepherd race the page (phase 1: shepherd
annotates + remediates; phase 2: once trusted, delay the Pushover route).

## Triage flow (per alert)

1. **Context**: read `sensor.health_check_status` attrs for the checker (HA
   REST), pull last 1h of that checker's log lines from Loki, read the
   checker's README from the baked repo copy.
2. **Runbook match**: per-checker playbooks live in
   `agent-docs/shepherd-runbooks/<checker_id>.md` (to be authored — spa,
   shade_gateway, zigbee, protect each have known remediations; PowerView
   port-32 cycle is already documented in memory/README).
3. **Bounded remediation** via *sanctioned paths only*:
   - `script.health_check_relay` commands: `force_recheck`, `start_repair`
     (repair logic stays in the checker apps — the shepherd only triggers it)
   - documented repair switches (e.g. `switch.spa_intouch3_switch`)
   - NO kubectl writes, NO HA config changes, NO muting on its own
4. **Verify**: wait/poll up to a budget (e.g. 10 min) for the checker to
   recover; the bridge resolves the alert automatically on recovery.
5. **Escalate or stand down**:
   - Recovered → annotate (Grafana annotation + alert-history entry via a
     new `record_note` relay command) and exit. Human never paged.
   - Not recovered → page via Pushover *with the diagnosis*: what failed,
     what was tried, links (Alertmanager, Grafana explore query).

## Guardrails

- Max 2 remediation attempts per checker per 6h (persisted in a ConfigMap or
  HA helper) — no repair loops fighting hardware failures.
- Idempotency: skip alerts already annotated with an in-flight/exhausted
  triage marker (Alertmanager annotation or HA helper).
- HA token scoped as much as HA allows; all shepherd HA writes limited to
  `script.health_check_relay` + explicitly whitelisted repair switches.
- Safety prompt appended to every run (upgrade-shepherd pattern), including
  "never disable/mute an alert; escalate instead".

## Prereqs in hass-sandbox (small, can ship independently)

- `record_note` relay command → shepherd actions appear in the card's alert
  history (same event shape as `is_repair_event` / `is_mute_event`).
- Author `agent-docs/shepherd-runbooks/*.md` for the top pagers: spa,
  protect, protect_batteries, shade_batteries, fans (from the 7-day Loki
  breakdown these caused ~43 critical episodes/week pre-fix).

## Open questions for Tom

1. Mode on upgrade-shepherd vs sibling deployment (recommended: sibling)?
2. Phase 2 page-delay: OK with criticals waiting ~15m for triage before the
   phone buzzes (UPS stays exempt via its for=0 override + a route bypass)?
3. Which remediations beyond `start_repair` are pre-authorized? (e.g. is the
   shepherd allowed to port-cycle the PowerView gateway via the UniFi MCP
   path, or only via the checker's built-in repair?)
