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
- 506 unit tests + 6 integration-skipped tests as of this session
- Run: `source .venv/bin/activate && cd appdaemon && python -m pytest tests/ -v --tb=short`
- WSL path: `wsl bash -c "cd /mnt/d/labspace/hass-sandbox && source .venv-wsl/bin/activate && cd appdaemon && python -m pytest tests/ -v --tb=short"`
