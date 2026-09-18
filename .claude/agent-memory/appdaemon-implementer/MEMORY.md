# AppDaemon Implementer Memory

## Key files to know

- `appdaemon/apps/apps-dev.yaml` — dev-only app configs; keys end in `_dev`
- `appdaemon/apps/detection_summary_app/profiles.py` — DetectionProfile dataclasses + BUILTIN_PROFILES dict
- `appdaemon/tests/test_detection_profiles.py` — profile unit tests (import path via sys.path.insert into apps/)
- `appdaemon/apps/detection_summary_app/README.md` — docs including built-in profiles table

## Confirmed patterns

### profiles.py pattern
- Add built-in profiles as module-level constants after PROFILE_VEHICLES, before BUILTIN_PROFILES
- Register in BUILTIN_PROFILES dict
- `PROFILE_ANIMALS`: animals required_for_publish=True, people required_for_publish=False, DEFAULT_SCORE_FIELDS only (no extras = 8 fields)
- `PROFILE_PACKAGES`: adds package_count ScoreFieldSpec; all 3 categories required_for_publish=True
- `PROFILE_VEHICLES`: adds vehicle_count + vehicle_type ScoreFieldSpec

### apps-dev.yaml: detection entrances
- Each entrance needs: `detection_summary_{bk}_dev` + `detection_viewer_{bk}_dev`
- viewer self-provisions: input_select, input_text (selected/timing/cooldown), relay script
- `best_min_person_score: 0` disables legacy person gate (needed for package-only or animal-only publishing)
- `best_min_animal_count: 1` required for animal-gated publishing alongside profile
- `debug_preserve_run_dirs: true` for dev apps (prevents cleanup)
- Animal-only profile uses `detection_profile: animals` (built-in, not inline)

### Dashboard editing (MCP)
- Always get config_hash fresh before EACH edit — it changes after every ha_config_set_dashboard call
- Use python_transform for surgical view updates (not jq_transform)
- Transform is single-line; use `;` to chain statements
- Inner Jinja2 templates in markdown content use single-quoted strings with escaped inner quotes
- Dashboard `detection-summary`: views 0=garage, 1=front-door, 2=bulkhead, 3=package, 4=back-deck-pets

### 5-card detection-summary view pattern
Cards order: bubble-card (nav) → summary markdown → generated img → best img → timing/cooldown metadata
- bubble-card entity: `input_select.{bk}_detection_summary_run_id`
- img paths: `/local/detection-summary/{path_segment}/viewer/{{ states('input_select...run_id') }}_generated.png`
- metadata template: `_Detection: {{ states('input_text.{bk}_detection_summary_timing') }}_\n\n_Cooldown: ...cooldown..._\n\n_Selection updated: {{ states.input_text.{bk}_detection_summary_selected.last_updated }}_`

### Test suite
- 1126 unit tests + 6 integration-skipped tests as of vestaboard event-based refactor
- Run: `source .venv/bin/activate && cd appdaemon && python -m pytest tests/ -v --tb=short`
- WSL path: `wsl bash -c "cd /mnt/d/labspace/hass-sandbox && source .venv-wsl/bin/activate && cd appdaemon && python -m pytest tests/ -v --tb=short"`

### Vestaboard event-based communication pattern (confirmed)
- Automations never use `get_app()` to reach the controller — all comms via `fire_event`
- Mixin fires `vestaboard_controller_command` with `command=register_automation` and JSON payload on startup
- Controller creates `RemoteAutomationProxy` (stores metadata) — no live Python reference to the automation app
- Controller fires per-automation events back: `vestaboard_automation_config_{id}`, `vestaboard_automation_enabled_{id}`, `vestaboard_automation_generate_{id}`
- Grid data (characters, preview_frame) MUST be JSON-stringified in event payloads to avoid HA zero-stripping
- `_handle_generate_by_type/ai_art/ai_art_preview` fire generate events; result returns async via `push_automation_frame` or `push_ai_art_preview_result` command
- `apps-dev.yaml` and `apps-prod.yaml`: NO `dependencies:` or `controller_app:` on automation entries
- `RemoteAutomationProxy` lives in vestaboard_controller_app.py (before the main class)
- Controller fires `vestaboard_controller_ready` at end of `_async_startup()` so automations can re-register after restart

### dashboard_notify threading model (confirmed pattern)
- Two-phase generation: `_request_*_generation()` on AD thread → `_generate_*_background()` on worker thread → `_complete_*_generation()` back on AD thread via `self.run_in(callback, 0, result=result)`
- Worker receives a plain `job: dict` with all needed data (paths, text, ttl_s, etc.)
- Worker returns a plain `result: dict` with success flag, error, and output paths
- `_active_generations: set[str]` is the in-memory dedup lock — reserve before thread start, release only in completion callback
- Completion callback always calls `self._active_generations.discard(nid)` first (even on failure)
- Stale-job guard: completion callback re-checks `_manager.has(nid)` before adding
- Placeholder guard: completion callback checks `_manager.count() == 0` before installing placeholder
- Use `threading.Thread(target=_worker, name="dashboard_notify_gen_<job_id>", daemon=True)`
- Never call `call_service`, `set_state`, or `listen_event` from the worker thread

### dashboard_notify timer model (explicit timers, no tick poller)
- `run_every` for `_tick` is gone; replaced with one-time startup reconcile + per-config boundary timers
- `_schedule_handles[nid] = {"start": handle, "end": handle}` tracks per-config boundary timers
- `_expiry_handles[nid] = handle` tracks per-notification expiry timers
- `_on_schedule_start` / `_on_schedule_end` self-reschedule their next occurrence at the end
- `_on_notification_expired` fires removal + triggers placeholder if empty
- Startup reconcile: evaluate active schedules, backfill detection bundles, install placeholder if empty, then schedule all boundaries

### dashboard_notify staging (no retry)
- Removed `_stage_to_www()` and `_stage_retry()` — both were wrong
- Correct pattern: `self.call_service("shell_command/" + self._stage_shell_command)` exactly once, in the completion callback or event handler after the file is known to exist on disk
- `_sync_staged_dir()` also calls the staging service directly (single call) when stale files are removed

### health_checks test idioms (confirmed)
- `tests/test_alertmanager_bridge.py`: bridge is pure decision logic, no HTTP. Helpers: `_make_bridge()` → (bridge, client, log) with for=0; `_make_gated_bridge(default_for_seconds, for_overrides=None, repair_hold_cap_s=1800)` → (bridge, client, log, advance) with an injected clock starting 2026-01-01 UTC; `advance(seconds)` moves it. `_checker(status, name, checks, alerting, repair_state)` builds one snapshot entry. `_batches(client)` = list of posted batches (one per post_alerts await). `_run(coro)` drives async in a fresh loop.
- Bridge for-gate/escalation/repair-hold logic lives in `apps/health_checks/shared/alertmanager_bridge.py` (`_promotion_due`, `_sync_locked`). Escalations (warning→critical) are gated like fresh raises; de-escalations and for=0 apply immediately. Repair hold only withholds CRITICAL promotions while `repair_state.status` in (pending, in_progress), capped at `repair_hold_cap_s` total pending time (default 1800; 0 disables). Escalation-promote posts a 2-alert batch [resolved_old, fresh_new].
- `tests/test_health_check_controller.py`: `_make_app(extra_args)` mocks all AppDaemon methods; `call_service` is a plain MagicMock (fine for sync `_heartbeat_tick`, but async `_persist_mute` does `await self.call_service(...)` → set `app.call_service = AsyncMock()` when driving it). `_startup(app, mock_prov)` runs `_async_startup` under an HAProvisioner patch. Bridge sync/persist run via `self.create_task(...)` which is a MagicMock → coroutine never runs; drive it with `_run(_last_created_coro(app))` and always `_close_created_coros(app)` at test end to avoid "never awaited" warnings.
- To capture the snapshot handed to the bridge (not run a real sync): replace `app._alert_bridge.sync = MagicMock()` then assert on `sync.call_args[0][0]`. Muted checkers publish snapshot alerting `{"enabled": False}`; attrs carry `muted`/`muted_until`. Mutes persist in `input_text.health_check_mute_<id>` and rebuild on register (`_load_persisted_mute` drops expired ones).

### Vestaboard provider (appdaemon/providers/vestaboard/)
- `vestaboard_client.py`: `VestaboardClient(ip, api_key, session=None)` — async context manager, POST/GET to `http://{ip}:7000/local-api/message`, header `X-Vestaboard-Local-Api-Key`
- `character_encoding.py`: `CHAR_TO_CODE` (A-Z=1-26, 1-9=27-35, 0=36, punct), `COLOR_CODES` (red=63..black=70), `blank_grid()`, `encode_char()`, `encode_text()`, `decode_grid()`, `text_to_grid(justify, align)`
- Test pattern: inject `MagicMock` session with `__aenter__`/`__aexit__` AsyncMock; mock `.post`/`.get` returns mock response with `.status` and `.json = AsyncMock()`

### school_schedule_app / providers/school_schedule (added 2026-09-01)
- **Never log into PowerSchool from the pod during development.** The guardian
  portal forbids concurrent sessions: every login evicts the family's live
  session (and a parent signing in mid-run kills the app's). Build parsers
  against the saved fixtures; if a login is truly unavoidable, do it once and
  `GET /guardian/home.html?ac=logoff` immediately.
- App refresh sits at 05:00 for the same reason — never trigger extra scrapes.
- `appdaemon/tests/fixtures/school_schedule/` is the repo's first test-fixture
  directory. Two files are **oracles** captured live and must keep matching
  exactly: `day_numbers.json` (181 ICS day numbers) and `cycle_by_day.json`
  (six-day rotation from the PowerSchool list view).
- Fixtures are sanitized: school/district → "Example …", teachers → placeholder
  `Last, First` names, student ids → 10001/10002. Keep the mapping global across
  fixture files or the oracle stops matching.
- No bs4/lxml/icalendar/dateutil in the AppDaemon image — parse with `re` +
  `html.unescape` only.
- Redact configured hosts/credentials out of exception strings before putting
  them in `set_state` attributes: aiohttp errors quote the URL they failed on,
  and the frontend renders `sources.*.error`.

### AppDaemon `call_service(callback=...)` is non-blocking (4.5.13, verified)
- `adapi.call_service` (site-packages `appdaemon/adapi.py` ~line 2022): with
  `callback` set it does `task = self.AD.loop.create_task(coro)` +
  `add_done_callback` and returns immediately — the `sync_decorator`'s 60s
  `internal_function_timeout` never applies to the service itself.
- Use it for any service whose HA side can be slow (`shell_command/*`), or the
  app's pinned worker thread is held for up to 60s and every `run_in` timer on
  that app starves. Symptom in the log: `Coroutine (<coroutine object
  Hass.call_service ...>) took too long (01:00), cancelling the task...`
- The callback is **non-async, takes one arg (the result), and runs on the
  event loop thread** — do not call `get_state`/`call_service` from it
  (`sync_decorator` returns a Task, not a value, on the main thread). Bounce
  back to the app thread with `self.run_in(cb, 0, **data)` first.
- `run_in` from a coroutine on the loop thread is safe (it creates a task and
  registers it in `ad.futures`); do not `await` it — unit tests mock `run_in`
  with a plain MagicMock, which is not awaitable.

### HA shell_command results are not trustworthy (photo_frame_viewer, 2026-09-18)
- HA kills every `shell_command` at a hard 60s. A stalled NFS copy dies before
  its atomic `mv`, so the target directory never appears while the service call
  still "completes". Never treat a stage/copy shell_command's return (or a
  fixed settle delay) as proof the files landed.
- Verify instead with `providers.ha_provisioner.local_file_exists(ha_url,
  url_path)` — unauthenticated HTTP HEAD on the exact `/local/...` URL the card
  will load. 200 = there. Sends no Authorization header on purpose
  (`/local/...` is unauthenticated static content).
- HA-side stage commands should detach their work (`( ... ) >> "$log" 2>&1 < /dev/null &`)
  so the `mv` finishes regardless of HA's timeout; the log is the only record.

### Mutation-checking new tests (cheap and worth it)
- The repo's vacuous-test trap is real. Script it: for each (file, anchor,
  broken replacement, test selector) apply the edit, run pytest, restore in a
  `finally`, and assert the return code is non-zero. 16 mutations over the
  photo-frame verification change ran in ~3s total.

### Any "latch" released only by a callback needs an absolute-deadline watchdog
- Pattern bug found in review on 2026-09-18 (`photo_frame_viewer` staging):
  a boolean that blocks work while an async chain runs, released only when the
  chain's result callback fires, and whose timeout is *evaluated inside that
  same callback*. If the chain dies (lost `run_in` bounce, cancelled task), the
  timeout can never fire and the flag blocks the feature until AppDaemon
  restarts.
- Fix is two-part: (1) try/except the hand-back and release the flag there,
  guarded on the id you own so a newer in-flight job is not clobbered;
  (2) a `run_in` timer armed at `timeout + margin` when the work starts,
  carrying the job id, no-op for a superseded id and for a non-owner instance
  (which must still release its own flag). Cancel it on success, on give-up,
  in `terminate()`, and when a new job starts.
- Pick the margin above one retry interval + one probe timeout so the normal
  deadline path still wins under scheduling jitter.

### Mocked `run_in`/`create_task` returning one shared handle hides cancel bugs
- `MagicMock()` returns the SAME `return_value` for every call, so two handles
  stored from two `run_in` calls compare equal. Every
  `assert handle in cancel_timer.call_args_list` then passes even when the code
  cancels the wrong timer — a mutation check caught exactly this.
- Give the double distinct handles:
  `handles = itertools.count(); app.run_in = MagicMock(side_effect=lambda *a, **k: f"handle-{next(handles)}")`.

### HA shell_command file staging: pass an explicit keep-list, prune on the HA side
- A detached HA-side worker can land its output minutes after the app gave up
  and already ran its per-gen cleanup — the directory did not exist then, so
  nothing ever reclaims it. Found 3 such orphans in prod (gens 808/2635/6015).
- Design: every stage call sends `keep_gens` (space-separated, digits only —
  it is interpolated into shell); a successful stage deletes every output
  directory not in `keep_gens` plus its own. Safe because the app's staging
  latch means the only dir that can become current before the prune runs is the
  one being staged. Empty list = prune nothing, so old app/old script pairings
  both degrade to no-ops.
- Implies **one app instance per staging output dir** — gen ids are per-instance
  counters, so two instances would prune each other. Document that constraint.

### A yes/no health probe hides the non-self-healing half of its failure modes
- Found in review 2026-09-18: `local_file_exists` collapsed 404 / 301 / 401 /
  timeout / connection-error into `False`, so the give-up WARNING read
  identically for "the file has not landed yet" (transient, next poll fixes it)
  and "the configured URL is wrong / the service is down" (never self-heals,
  feature silently frozen forever).
- Shape of the fix: `*_status(...) -> int` returning the real HTTP status plus a
  negative sentinel (`STATUS_UNREACHABLE = -1`) for "no answer at all"; keep the
  boolean as a thin `== 200` wrapper so existing callers and tests stay valid.
  Callers remember the last status for the in-flight job and branch the operator
  hint on it — and say explicitly which branches will NOT self-heal and which
  log will not explain them.
- General rule: any probe whose result reaches a human must preserve *what the
  other side said*, not just whether it was what we wanted.

### Every `call_service("shell_command/...")` should pass `callback=`
- Not just the slow one. Nothing reads a shell_command result, and a blocking
  call pins the app's worker thread for up to AppDaemon's 60s
  internal_function_timeout — which starves every `run_in` timer on that app.
- Cheap guard test: collect all `call_service` calls whose service starts with
  `shell_command/` and assert each got a callable `callback`; assert
  `input_select/*` (state-changing) calls did NOT.
- Keep the callbacks as small named bound methods delegating to one shared
  implementation (`_on_shell_service_result(service, result)`), not
  `functools.partial`/closures — bound methods stay identity-comparable, so
  tests can assert exactly which callback was attached.

### Timeout constants must be derived when the inputs they race are tunable
- Round-3 finding on `photo_frame_viewer`: a watchdog armed at
  `timeout + FIXED_MARGIN` only outran the normal give-up path at the *default*
  retry interval. Raise the documented (unclamped) `stage_verify_interval_s` and
  the backstop fires first, killing a healthy job and logging a phantom fault.
- Rule: when a backstop timer must lose a race against a normal path, compute
  its delay from the same live values the normal path uses
  (`timeout + retry_interval + probe_timeout + named_slack`), and import the
  probe timeout from the provider that owns it rather than duplicating the
  literal. Test the *invariant* across several interval values, not the default.

### Don't let a long-running async window read a mutable "latest" field
- Same app, same review: `_on_batch_ready` wrote `_staged_filter_name`, and the
  settle step read that live field minutes later — so a generation got published
  under the *next* album's title once verification stretched the window from 3s
  to minutes. It was invisible while the window was short.
- Pattern: snapshot the value into the job's own context when the job starts and
  free the live field for the next job. On failure, hand the snapshot back only
  `if not <live field>` so a newer arrival still wins. Clear the snapshot in the
  context teardown, and read it into a local *before* teardown where the settle
  path clears first.
- Watch for tests that encode the old drift: one asserted a title set *after* a
  stage began still landed on that stage. Fix the sequence (let the prior stage
  settle first), don't relax the assertion.
