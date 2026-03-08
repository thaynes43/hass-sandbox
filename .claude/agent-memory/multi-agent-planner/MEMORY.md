# Multi-Agent Planner Memory

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
