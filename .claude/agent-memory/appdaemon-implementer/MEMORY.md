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

### Vestaboard provider (appdaemon/providers/vestaboard/)
- `vestaboard_client.py`: `VestaboardClient(ip, api_key, session=None)` — async context manager, POST/GET to `http://{ip}:7000/local-api/message`, header `X-Vestaboard-Local-Api-Key`
- `character_encoding.py`: `CHAR_TO_CODE` (A-Z=1-26, 1-9=27-35, 0=36, punct), `COLOR_CODES` (red=63..black=70), `blank_grid()`, `encode_char()`, `encode_text()`, `decode_grid()`, `text_to_grid(justify, align)`
- Test pattern: inject `MagicMock` session with `__aenter__`/`__aexit__` AsyncMock; mock `.post`/`.get` returns mock response with `.status` and `.json = AsyncMock()`
