# Dashboard Notify — TODO

Known shortcuts taken for MVP. These need proper investigation and fixes before production hardening.

## 1. Image generation blocks AppDaemon's single callback thread

**Problem**: `_generate_notification` and `_ensure_placeholder` run synchronously in `_tick`, blocking AD's only worker thread for 15-20s per API call. This triggers `Excessive time spent in callback` warnings and prevents other apps from processing events during generation.

**How detection_summary solved it**: Detection summary moved long-running work off the AD callback thread. Dashboard notify initially used `ThreadPoolExecutor` but `self.call_service()` and `self.set_state()` are coroutines that fail silently from non-AD threads ("LOOP NOT RUNNING" / "coroutine was never awaited").

**Proper fix**: Investigate how detection_summary handles this — it likely uses `self.create_task()` or AD's built-in `self.run_in_executor()` with a callback pattern to return to the AD thread for service calls. The generation work (API call, file write) can run off-thread, then schedule a `run_in(0)` callback to do the `call_service`/`set_state` on the AD thread.

## 2. File ownership (`chown`) issues in `/config/www/`

**Problem**: Files in `/config/www/dashboard-notify/` ended up owned by root because they were manually created via `kubectl exec`. The HA shell command runs as uid 568 and couldn't overwrite root-owned files. `cp -f` failures were silently swallowed by `2>/dev/null; true`.

**Why this didn't happen in other apps**: Other apps' `mkdir -p` in the shell command creates the directory (and first files) as the HA user on first run. We manually created the directory and copied the JS file via kubectl before the shell command ever ran, poisoning the ownership.

**Proper fix**: This should be a one-time issue. Once the directory and files are owned by uid 568, subsequent `cp` operations work. No code fix needed — just don't manually create files in `/config/www/` as root. Document this gotcha more prominently.

## 3. Shell command retry / CephFS propagation delay

**Problem**: Added a 5-second `_stage_retry` that re-calls the staging shell command. This was added because the first `call_service` appeared to not copy newly written files.

**Why this is suspicious**: The CephFS mount on the dev machine IS the same mount as `/media` on both the AppDaemon pod and the HA pod. There should be no propagation delay — it's the same filesystem. Other apps (detection_summary, photo_frame_viewer) use the exact same staging pattern without any retry and work fine.

**Likely root cause**: The shell command copy failure was probably caused by the file ownership issue (#2 above), not a timing problem. Once ownership is fixed, the retry is unnecessary overhead.

**Action**: After confirming the ownership fix resolves the copy issue, remove `_stage_retry` and `_stage_to_www` — go back to a direct `self.call_service("shell_command/...")` call like every other app uses.

## 4. `run_every` not firing with `"now"` or `self.datetime()`

**Problem**: `self.run_every(self._tick, "now", 60)` and `self.run_every(self._tick, self.datetime(), 60)` both silently failed to schedule the callback. Worked around by using `self.run_in(self._tick, 5)` for the first tick and `run_every(..., "now+65", 60)` for subsequent ticks.

**Why this is suspicious**: `detection_summary_viewer` uses `self.run_every(callback, "now", 600)` and it works. The AppDaemon version is the same (4.5.13).

**Action**: Investigate why `run_every` with `"now"` works for other apps but not this one. Could be related to the 1-worker-thread config, initialization order, or a subtle difference in how the callback is registered.
