# AppDaemon Implementer Memory

Index only — one line per entry. Detail lives in the topic files; add new
detail there, never inline here.

## AppDaemon runtime behaviour (third-party internals, not derivable from this repo)

- [set_state value filtering](appdaemon-set-state-semantics.md) — AD 4.5.13 drops `0`/`False` (and `state=0`), turns `True` into `"true"`; `""` and `[]` survive. Publish attributes as non-empty strings
- [Service calls and HA staging](appdaemon-service-calls-and-staging.md) — `call_service(callback=)` is the only non-blocking form and where its callback runs; shell_command results are untrustworthy (60s kill), verify by HTTP probe; `keep_gens` prune design
- [Async/lifecycle bug shapes](appdaemon-async-lifecycle-bugs.md) — callback-released latches, fixed timeout margins, mutable "latest" fields read late, arrival-order metadata, memory-only counters across reloads
- [Probe design](appdaemon-probe-design.md) — a boolean probe hides which failures never self-heal; preserve what the other side said

## Testing

- [Testing discipline](appdaemon-testing-discipline.md) — mutation-check every new test; give mocked `run_in`/`create_task` distinct handles; never encode a sequence the producer cannot emit
- [health_checks test idioms](health-checks-test-idioms.md) — bridge/controller helpers, async `call_service`, `create_task` coroutines that never run, capturing the bridge snapshot
- Run the suite: `source <a venv>/bin/activate && cd appdaemon && python -m pytest tests/ -q --tb=short` — ~3 min, so run it in the background. Agent worktrees have no `.venv`, and neither does `~/repos/hass-sandbox` — borrow one from another `~/work/hass-sandbox-*/` worktree
- Suite size: 3841 passed + 6 integration-skipped as of `assist_exposure_guard` (2026-09-18)

## Per-app / per-provider notes

- [Detection summary + dashboards](detection-summary-and-dashboards.md) — `profiles.py` conventions, dev entrance pairs, HA MCP `config_hash` gotcha, the 5-card view shape
- [Vestaboard patterns](vestaboard-patterns.md) — event-only controller/automation decoupling (no `get_app`), JSON-stringified grids, provider client + encoding API
- [dashboard_notify patterns](dashboard-notify-patterns.md) — worker-thread handoff, explicit timers (no tick poller), one-call staging
- [school_schedule / PowerSchool](school-schedule-powerschool.md) — never log in from the pod (it evicts the family's session); fixture oracles and sanitisation mapping

## Key files

- `appdaemon/apps/apps-dev.yaml` — dev-only app configs; keys end in `_dev`. `apps-prod.yaml` entries carry `disable: true` (the Docker build strips it)
- `appdaemon/providers/ha_provisioner/` — `HAProvisioner`, `HaAdminClient`, `AssistExposureClient`, `local_file_status`
- `.agents/rules/appdaemon-documentation.md` — the documentation map + app dependency graph; every new app must be added to both
