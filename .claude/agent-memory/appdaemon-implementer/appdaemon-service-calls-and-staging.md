---
name: appdaemon-service-calls-and-staging
description: call_service(callback=) is the only non-blocking form and where its callback runs; HA shell_command results are untrustworthy (60s kill) and must be verified by HTTP probe; the keep_gens prune design
metadata:
  type: reference
---

# AppDaemon service calls + HA shell_command staging

## `call_service(callback=...)` is non-blocking (4.5.13, verified)

- `adapi.call_service` (site-packages `appdaemon/adapi.py` ~line 2022): with `callback` set it does `task = self.AD.loop.create_task(coro)` + `add_done_callback` and returns immediately — the `sync_decorator`'s 60 s `internal_function_timeout` never applies to the service itself.
- **Every** `call_service("shell_command/...")` should pass `callback=`, not just the slow one. Nothing reads a shell_command result, and a blocking call pins the app's pinned worker thread for up to 60 s, starving every `run_in` timer on that app. Log symptom: `Coroutine (<coroutine object Hass.call_service ...>) took too long (01:00), cancelling the task...`
- The callback is **non-async, takes one arg (the result), and runs on the event loop thread** — do not call `get_state`/`call_service` from it (`sync_decorator` returns a Task, not a value, on the main thread). Bounce back to the app thread with `self.run_in(cb, 0, **data)` first.
- `run_in` from a coroutine on the loop thread is safe (it creates a task and registers it in `ad.futures`); do **not** `await` it — unit tests mock `run_in` with a plain `MagicMock`, which is not awaitable.
- Cheap guard test: collect all `call_service` calls whose service starts with `shell_command/` and assert each got a callable `callback`; assert `input_select/*` (state-changing) calls did **not**.
- Keep the callbacks as small named bound methods delegating to one shared implementation (`_on_shell_service_result(service, result)`) rather than `functools.partial`/closures — bound methods stay identity-comparable, so tests can assert exactly which callback was attached.

## HA shell_command results are not trustworthy (photo_frame_viewer, 2026-09-18)

- HA kills every `shell_command` at a hard 60 s. A stalled NFS copy dies before its atomic `mv`, so the target directory never appears while the service call still "completes". Never treat a stage/copy shell_command's return (or a fixed settle delay) as proof the files landed.
- Verify instead with `providers.ha_provisioner.local_file_status` / `local_file_exists(ha_url, url_path)` — an unauthenticated HTTP HEAD on the exact `/local/...` URL the card will load. 200 = there. It sends no `Authorization` header on purpose (`/local/...` is unauthenticated static content).
- HA-side stage commands should detach their work (`( ... ) >> "$log" 2>&1 < /dev/null &`) so the `mv` finishes regardless of HA's timeout; the log is the only record.

## Pass an explicit keep-list; prune on the HA side

- A detached HA-side worker can land its output minutes after the app gave up and already ran its per-gen cleanup — the directory did not exist then, so nothing ever reclaims it. Found 3 such orphans in prod (gens 808 / 2635 / 6015).
- Design: every stage call sends `keep_gens` (space-separated, digits only — it is interpolated into shell); a successful stage deletes every output directory not in `keep_gens`, plus its own. Safe because the app's staging latch means the only directory that can become current before the prune runs is the one being staged. An empty list prunes nothing, so old-app/old-script pairings both degrade to no-ops.
- Implies **one app instance per staging output dir** — gen ids are per-instance counters, so two instances would prune each other. Document that constraint.

Related: [[appdaemon-async-lifecycle-bugs]], [[appdaemon-probe-design]]
