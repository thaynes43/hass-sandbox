---
name: health-checks-test-idioms
description: Test helpers and mocking traps for the health_checks controller and Alertmanager bridge — async call_service, create_task coroutines that never run, capturing the bridge snapshot, and testing wall-clock repair waits
metadata:
  type: reference
---

# health_checks test idioms (confirmed)

## `tests/test_alertmanager_bridge.py`

The bridge is pure decision logic — no HTTP. Helpers:

- `_make_bridge()` → `(bridge, client, log)` with `for=0`.
- `_make_gated_bridge(default_for_seconds, for_overrides=None, repair_hold_cap_s=1800)` → `(bridge, client, log, advance)` with an injected clock starting 2026-01-01 UTC; `advance(seconds)` moves it.
- `_checker(status, name, checks, alerting, repair_state)` builds one snapshot entry.
- `_batches(client)` = list of posted batches (one per `post_alerts` await).
- `_run(coro)` drives async in a fresh loop.

Logic lives in `apps/health_checks/shared/alertmanager_bridge.py`
(`_promotion_due`, `_sync_locked`). Escalations (warning→critical) are gated
like fresh raises; de-escalations and `for=0` apply immediately. Repair hold
only withholds CRITICAL promotions while `repair_state.status` is
`pending`/`in_progress`, capped at `repair_hold_cap_s` total pending time
(default 1800; 0 disables). An escalation-promote posts a 2-alert batch
`[resolved_old, fresh_new]`.

## `tests/test_health_check_controller.py`

- `_make_app(extra_args)` mocks all AppDaemon methods. `call_service` is a plain `MagicMock` — fine for the sync `_heartbeat_tick`, but the async `_persist_mute` does `await self.call_service(...)`, so set `app.call_service = AsyncMock()` when driving it.
- `_startup(app, mock_prov)` runs `_async_startup` under an `HAProvisioner` patch.
- Bridge sync/persist run via `self.create_task(...)`, which is a `MagicMock` → the coroutine never runs. Drive it with `_run(_last_created_coro(app))`, and always `_close_created_coros(app)` at test end to avoid "never awaited" warnings.
- To capture the snapshot handed to the bridge without running a real sync: replace `app._alert_bridge.sync = MagicMock()` and assert on `sync.call_args[0][0]`.
- Muted checkers publish snapshot alerting `{"enabled": False}`; attrs carry `muted`/`muted_until`. Mutes persist in `input_text.health_check_mute_<id>` and rebuild on register (`_load_persisted_mute` drops expired ones).

## Wall-clock repair waits (#208, #210)

Every repair/recovery wait in `health_checks` counts
`elapsed = max(elapsed + poll, int(time.monotonic() - started))`. To test one,
patch `time.monotonic` globally to a dict-backed clock and advance it inside
the mocked check (#208 shape: `asyncio.sleep` a no-op `AsyncMock`, each check
+60 s → a 300 s wait is 6 checks). Where the wait starts with one long sleep
(shade gateway's settle), a fake sleep that adds its argument (plus a late
wake-up) is the only way to exercise the start of the clock.

Sizing a wall-clock switch: `get_state` in AppDaemon reads its local state
cache (no request), so a loop that only reads entities never drifted from its
sleeps; only pings/HTTP in the loop stretched the old sleep-counted windows.
Loki keeps 30 days (a query over 30d1h is rejected); repair lines are under
`{namespace="home-automation", app="appdaemon"}`.

Related: [[appdaemon-testing-discipline]]
