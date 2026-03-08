# Multi-Agent Planner Memory

## Environment detection (IMPORTANT — read before generating test commands)

Before generating test/run commands, detect the OS from the system environment info (platform, shell, OS version). The rules file `.cursor/rules/appdaemon-dev-environment.mdc` has full details, but the key points:

- **Linux (native or WSL)**: Run bash commands directly. Use `.venv/` venv.
  ```bash
  source .venv/bin/activate && cd appdaemon && python -m pytest tests/ -v --tb=short
  ```
- **Windows (PowerShell)**: Wrap in `wsl bash -c "..."`. Use `.venv-wsl/` venv.
  ```bash
  wsl bash -c "cd /mnt/d/labspace/hass-sandbox && source .venv-wsl/bin/activate && cd appdaemon && python -m pytest tests/ -v --tb=short"
  ```

**How to tell**: Check the platform field in the environment info. If `linux` → use Linux commands directly. If `win32` or shell is `powershell` → use the WSL wrapper. Do NOT hardcode `wsl bash` in plans when running on Linux.

## detection_summary_app architecture

- Entry point: `manager.py` (DetectionSummary class extends hass.Hass)
- Pipeline: trigger -> capture frames -> adaptive_select_and_score -> publish_gate -> narrative -> image_gen -> bundle -> finalize
- `ScoreResult` dataclass in `selection.py` used everywhere (8 fixed fields + structured dict)
- `prompting/schema_specs.py` has `ScoreFieldSpec` + `ScoreSchemaSpec` + `DEFAULT_SCORE_FIELDS` tuple
- `score_normalizer.py` converts raw LLM dict -> ScoreResult using schema
- `population.py` computes max counts across frames for image gen constraints
- `publish_gate.py` gates on person_score OR animal_count
- `bundle.py` assembles the final JSON bundle dict
- Config per-instance in `apps-prod.yaml` (garage, bulkhead instances exist)
- Two viewer instances (`detection_summary_viewer`) pair with each summary instance

## Common dependency chains

- New feature on detection_summary_app always touches: selection.py -> prompting/* -> population.py -> publish_gate.py -> bundle.py -> manager.py -> tests
- All steps are sequential (shared files) = single Implementation Agent track
- schema_specs.py is the foundation; changes there cascade to normalizer, prompt builders, and manager

## Test patterns

- Tests in `appdaemon/tests/test_detection_summary_*.py`
- Path setup: `sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))`
- Import from `detection_summary_app.*` directly
- ScoreResult constructed positionally in tests: `ScoreResult(m, f, a, ps, fs, frs, pose, summary, structured)`
- Adding fields to ScoreResult must use defaults to preserve positional construction in existing tests

## Planning patterns

- For detection_summary_app changes: always single track (too many shared files)
- Backward compat is critical: existing YAML configs must work without changes
- Profile/config-driven features: use None/missing = legacy behavior pattern
- Config-only new entrances (no Python changes): still single track because apps-dev.yaml + dashboard are shared resources
- Dashboard edits on the same dashboard MUST be sequential (config_hash changes after each edit)

## Dashboard editing

- detection-summary dashboard URL path: `detection-summary`
- 5-card pattern: bubble-card (select+nav), summary markdown, generated image, best image, timing metadata
- Existing views: garage (0), front-door (1), bulkhead (2)
- CRITICAL: never truncate config_hash -- use the full 16-char hex string
- Always re-fetch config_hash between sequential dashboard edits
- Reference card YAML files may exist in `home-assistant/cards/detection-summary/` -- check and update if present

## Existing entrances (as of 2026-03-08)

| Bundle key | Camera | Trigger | Snapshot dir | Dashboard index |
|-----------|--------|---------|-------------|----------------|
| garage | camera.garage_g5_dome_medium_resolution_channel | binary_sensor.g5_dome_motion | /media/detection-summary/garage | 0 |
| front_door | camera.g4_doorbell_pro_poe_medium_resolution_channel | binary_sensor.g4_doorbell_pro_poe_motion | /media/detection-summary/doorbell/front-door | 1 |
| bulkhead | camera.basement_g5_dome_ultra_medium_resolution_channel | binary_sensor.basement_g5_dome_ultra_motion | /media/detection-summary/bulkhead | 2 |
