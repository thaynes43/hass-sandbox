---
name: dashboard-notify-patterns
description: dashboard_notify's worker-thread handoff, its explicit-timer model (no tick poller), and the one-call staging rule that replaced a wrong retry loop
metadata:
  type: reference
---

# dashboard_notify: threading, timers, staging

## Threading model

- Two-phase generation: `_request_*_generation()` on the AD thread → `_generate_*_background()` on a worker thread → `_complete_*_generation()` back on the AD thread via `self.run_in(callback, 0, result=result)`.
- The worker receives a plain `job: dict` with everything it needs (paths, text, `ttl_s`, …) and returns a plain `result: dict` (success flag, error, output paths).
- `_active_generations: set[str]` is the in-memory dedup lock — reserve before starting the thread, release **only** in the completion callback, which always calls `self._active_generations.discard(nid)` first, even on failure.
- Stale-job guard: the completion callback re-checks `_manager.has(nid)` before adding.
- Placeholder guard: the completion callback checks `_manager.count() == 0` before installing the placeholder.
- `threading.Thread(target=_worker, name="dashboard_notify_gen_<job_id>", daemon=True)`.
- **Never** call `call_service`, `set_state` or `listen_event` from the worker thread.

## Timer model (explicit timers, no tick poller)

- `run_every` for `_tick` is gone; replaced by a one-time startup reconcile plus per-config boundary timers.
- `_schedule_handles[nid] = {"start": handle, "end": handle}` tracks per-config boundary timers; `_expiry_handles[nid] = handle` tracks per-notification expiry.
- `_on_schedule_start` / `_on_schedule_end` self-reschedule their next occurrence at the end.
- `_on_notification_expired` fires removal and triggers the placeholder if empty.
- Startup reconcile order: evaluate active schedules → backfill detection bundles → install the placeholder if empty → schedule all boundaries.

## Staging (no retry)

- `_stage_to_www()` and `_stage_retry()` were removed — both were wrong.
- Correct pattern: `self.call_service("shell_command/" + self._stage_shell_command)` **exactly once**, from the completion callback or event handler, after the file is known to exist on disk.
- `_sync_staged_dir()` also calls the staging service directly (one call) when stale files are removed.
