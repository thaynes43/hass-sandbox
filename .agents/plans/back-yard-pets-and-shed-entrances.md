# Plan: Add Back Yard Pets + Shed Detection Summary Entrances

**Status**: Ready for implementation
**Type**: Config-only (no new Python code)
**Tracks**: 1 Implementation Agent (sequential)
**Created**: 2026-03-08

---

## Overview

Add two new detection summary app instances (back_yard_pets, shed) by appending YAML config entries to `apps-dev.yaml` and populating placeholder dashboard views via MCP.

---

## Constraints

- DO NOT run `deploy.py` or copy files to any production target
- DO NOT create new Python files
- DO NOT modify any existing entries in `apps-dev.yaml` — append only
- Use the FULL `config_hash` from `ha_config_get_dashboard` — never truncate (16 hex chars)
- Dashboard edits MUST be sequential — re-fetch `config_hash` between each edit
- All changes stay in the dev environment only

---

## Parallelism analysis

| Todo | Files touched | Dependencies | Track |
|------|---------------|-------------|-------|
| 1-5: YAML config entries | apps-dev.yaml | none | A |
| 6: back-yard-pets dashboard | MCP only | none (placeholder exists) | A |
| 7: shed dashboard | MCP only | todo 6 (config_hash changes) | A |
| 8: Run tests | read-only | todos 1-5 | A |

All work is in Track A (sequential) because:
- Todos 1-5 all edit the same file (`apps-dev.yaml`)
- Todos 6-7 edit the same dashboard (config_hash serialization required)

**Decision: 1 Implementation Agent**

---

## Relevant files

| File | Action |
|------|--------|
| `appdaemon/apps/apps-dev.yaml` | Append 5 new YAML entries |
| Dashboard `detection-summary` (MCP) | Populate views at index 5 and 6 |
| `home-assistant/cards/detection-summary/` | Check if card reference files need creation |

---

## Playbooks to read before executing

- `/home/thaynes/workspace/hass-sandbox/.agents/playbooks/detection-app.md` — entrance setup workflow
- `/home/thaynes/workspace/hass-sandbox/.agents/playbooks/ha-dashboard.md` — dashboard editing via MCP, config_hash rules

---

## Implementation detail

### Todos 1-5: Append YAML config entries to apps-dev.yaml

**File**: `/home/thaynes/workspace/hass-sandbox/appdaemon/apps/apps-dev.yaml`
**Action**: Append the following 5 entries at the end of the file (after the existing `detection_viewer_back_deck_pets_dev` entry).

**Entry 1 — detection_summary_back_yard_pets_dev:**

```yaml
detection_summary_back_yard_pets_dev:
  module: detection_summary_app.manager
  class: DetectionSummary
  ha_url: !secret ha_url
  ha_token_env: TOKEN
  bundle_key: back_yard_pets
  best_min_person_score: 0
  best_min_animal_count: 1
  detection_profile: animals
  snapshot_ha_dir: /media/detection-summary/back-yard/back-yard-pets
  media_fs_root: !secret media_fs_root
  hass_entities:
    camera_entity_id: camera.shed_g5_ptz_medium_resolution_channel
    trigger_entity_id: binary_sensor.shed_g5_ptz_animal_detected
    best_image_camera_entity_id: camera.back_yard_pets_detection_summary_best
    generated_image_camera_entity_id: camera.back_yard_pets_detection_summary_generated
  data_instructions: >
    You are analyzing ONE security camera snapshot from a backyard camera near a shed.
    Focus primarily on animals/pets — dogs, cats, birds, deer, squirrels, raccoons, etc.
    Count animals carefully. Identify the species when you can.
    People may appear in the frame but are secondary; still count them accurately.
    Also return male_count and female_count as integer counts of people by gender (0 if none).
    Also return animal_count as an integer count of visible animals/pets (0 if none).
    Return scores on a 0-10 scale (integers preferred) with enough spread to rank frames.
    Favor frames where animals are clearly visible and identifiable by species.
    The summary should be short and notification-friendly: 1 sentence, <= 140 characters.
  image_instructions: >
    Create a simple, clean illustration of the backyard scene near a shed.
    Animals are the focus — make them prominent and clearly identifiable by species.
  debug_preserve_run_dirs: true
  ai_provider_conf:
    simple_text:
      bundle: ollama-qwen9b
      base_url: !secret ollama_url
    multimodal:
      bundle: ollama-qwen9b
      base_url: !secret ollama_url
    image:
      bundle: comfyui-qwen-edit
      base_url: !secret comfyui_url
```

**Entry 2 — detection_viewer_back_yard_pets_dev:**

```yaml
detection_viewer_back_yard_pets_dev:
  module: detection_summary_viewer.detection_summary_viewer_app
  class: DetectionSummaryViewer
  ha_url: !secret ha_url
  ha_token_env: TOKEN
  bundle_key: back_yard_pets
  snapshot_ha_dir: /media/detection-summary/back-yard/back-yard-pets
  media_fs_root: !secret media_fs_root
  hass_entities:
    selected_best_image_camera_entity_id: camera.back_yard_pets_detection_summary_selected_best
    selected_generated_image_camera_entity_id: camera.back_yard_pets_detection_summary_selected_generated
  notification_action_prefix: "BACK_YARD_PETS_DS_VIEW"
```

**Entry 3 — detection_summary_shed_dev:**

```yaml
detection_summary_shed_dev:
  module: detection_summary_app.manager
  class: DetectionSummary
  ha_url: !secret ha_url
  ha_token_env: TOKEN
  bundle_key: shed
  snapshot_ha_dir: /media/detection-summary/shed
  media_fs_root: !secret media_fs_root
  hass_entities:
    camera_entity_id: camera.shed_ai_turret_medium_resolution_channel
    trigger_entity_id: binary_sensor.ai_turret_motion
    best_image_camera_entity_id: camera.shed_detection_summary_best
    generated_image_camera_entity_id: camera.shed_detection_summary_generated
  data_instructions: >
    You are analyzing ONE security camera snapshot from a shed/outdoor building camera.
    Focus ONLY on the people and any animals.
    The summary should be short and notification-friendly: 1 sentence, <= 140 characters.
    Include only: person/animal count, what they are doing, and where they are in
    frame (e.g. "near left", "center", "back").
    Also return male_count and female_count as integer counts of people by gender (0 if none).
    Also return animal_count as an integer count of visible animals/pets (0 if none).
    Return scores on a 0-10 scale (integers preferred) with enough spread to rank frames.
    Heavily favor frames where at least one person's FACE is clearly visible.
    face_score meaning: 0=no face visible, 10=clear unobstructed face.
    Prefer frames where the person is clearly visible and stationary.
    If animals are present and clearly visible, increase frame_score appropriately.
  image_instructions: >
    Create a simple, clean illustration representing the scene near a shed/outdoor building.
  debug_preserve_run_dirs: true
  ai_provider_conf:
    simple_text:
      bundle: ollama-qwen9b
      base_url: !secret ollama_url
    multimodal:
      bundle: ollama-qwen9b
      base_url: !secret ollama_url
    image:
      bundle: comfyui-qwen-edit
      base_url: !secret comfyui_url
```

**Entry 4 — detection_viewer_shed_dev:**

```yaml
detection_viewer_shed_dev:
  module: detection_summary_viewer.detection_summary_viewer_app
  class: DetectionSummaryViewer
  ha_url: !secret ha_url
  ha_token_env: TOKEN
  bundle_key: shed
  snapshot_ha_dir: /media/detection-summary/shed
  media_fs_root: !secret media_fs_root
  hass_entities:
    selected_best_image_camera_entity_id: camera.shed_detection_summary_selected_best
    selected_generated_image_camera_entity_id: camera.shed_detection_summary_selected_generated
  notification_action_prefix: "SHED_DS_VIEW"
```

**Entry 5 — shed_door_notify_dev:**

```yaml
shed_door_notify_dev:
  module: door_notify.door_notify
  class: DoorNotify
  notify_services:
    - notify.mobile_app_toms_iphone_15_pro
    - notify.mobile_app_toms_iphone_air
    - notify.mobile_app_kellies_iphone_air
  doors:
    - binary_sensor.shed_door_open_closed_sensor_window_door_is_open
  door_open_state: "on"
  door_closed_state: "off"
  ai_enabled: true
  ai_bundle_key: shed
  ai_wait_timeout_s: 180
  ai_max_bundle_age_s: 900
  notification_url: "/detection-summary/shed"
```

No `door_notify` entry for back_yard_pets (no door sensor involved).

### Todo 6: Populate back-yard-pets dashboard view (index 5)

**Dashboard**: `detection-summary`
**View index**: 5 (path: `back-yard-pets`, currently a placeholder)

1. Call `ha_config_get_dashboard` with `url_path: "detection-summary"` to get the current config and `config_hash`.
2. Use `ha_config_set_dashboard` with `python_transform` to populate view index 5.

The transform must set the view's sections to contain a single grid section with 5 cards following the detection-summary pattern.

**Substitution values:**
- `{bk}` = `back_yard_pets`
- `{img_base}` = `detection-summary/back-yard/back-yard-pets/viewer`
- `{icon}` = `mdi:paw`
- `{name}` = `Back Yard Pets - Navigation`
- `{title}` = `Back Yard Pets Detection Summary`

**python_transform** (single line, use this exact transform):

```python
config['views'][5]['title'] = 'Back Yard Pets Detection Summary'; config['views'][5]['dense_section_placement'] = True; config['views'][5]['sections'] = [{'type': 'grid', 'cards': [{'type': 'custom:bubble-card', 'card_type': 'select', 'entity': 'input_select.back_yard_pets_detection_summary_run_id', 'name': 'Back Yard Pets - Navigation', 'show_name': True, 'rows': 1.719, 'icon': 'mdi:paw', 'sub_button': {'main': [], 'bottom': [{'sub_button_type': 'button', 'icon': 'mdi:chevron-left', 'tap_action': {'action': 'call-service', 'service': 'input_select.select_previous', 'target': {'entity_id': 'input_select.back_yard_pets_detection_summary_run_id'}, 'data': {'cycle': False}}}, {'sub_button_type': 'button', 'icon': 'mdi:star-four-points', 'tap_action': {'action': 'call-service', 'service': 'input_select.select_first', 'target': {'entity_id': 'input_select.back_yard_pets_detection_summary_run_id'}}}, {'sub_button_type': 'button', 'icon': 'mdi:chevron-right', 'tap_action': {'action': 'call-service', 'service': 'input_select.select_next', 'target': {'entity_id': 'input_select.back_yard_pets_detection_summary_run_id'}, 'data': {'cycle': False}}}]}}, {'type': 'markdown', 'content': "{{ states('input_text.back_yard_pets_detection_summary_selected') }}"}, {'type': 'markdown', 'content': '<img src="/local/detection-summary/back-yard/back-yard-pets/viewer/{{ states(\'input_select.back_yard_pets_detection_summary_run_id\') }}_generated.png" />'}, {'type': 'markdown', 'content': '<img src="/local/detection-summary/back-yard/back-yard-pets/viewer/{{ states(\'input_select.back_yard_pets_detection_summary_run_id\') }}_best.jpg" style="width:100%;border-radius:12px;object-fit:cover;" />'}, {'type': 'markdown', 'content': "_Detection: {{ states('input_text.back_yard_pets_detection_summary_timing') }}_\n\n_Cooldown: {{ states('input_text.back_yard_pets_detection_summary_cooldown') }}_\n\n_Selection updated: {{ states.input_text.back_yard_pets_detection_summary_selected.last_updated }}_"}]}]; config['views'][5]['cards'] = []
```

### Todo 7: Populate shed dashboard view (index 6)

**Dashboard**: `detection-summary`
**View index**: 6 (path: `shed`, currently a placeholder)

1. Re-fetch `config_hash` by calling `ha_config_get_dashboard` with `url_path: "detection-summary"` (CRITICAL: the hash changed after todo 6).
2. Use `ha_config_set_dashboard` with `python_transform` to populate view index 6.

**Substitution values:**
- `{bk}` = `shed`
- `{img_base}` = `detection-summary/shed/viewer`
- `{icon}` = `mdi:greenhouse`
- `{name}` = `Shed Detection - Navigation`
- `{title}` = `Shed Detection Summary`

**python_transform** (single line, use this exact transform):

```python
config['views'][6]['title'] = 'Shed Detection Summary'; config['views'][6]['dense_section_placement'] = True; config['views'][6]['sections'] = [{'type': 'grid', 'cards': [{'type': 'custom:bubble-card', 'card_type': 'select', 'entity': 'input_select.shed_detection_summary_run_id', 'name': 'Shed Detection - Navigation', 'show_name': True, 'rows': 1.719, 'icon': 'mdi:greenhouse', 'sub_button': {'main': [], 'bottom': [{'sub_button_type': 'button', 'icon': 'mdi:chevron-left', 'tap_action': {'action': 'call-service', 'service': 'input_select.select_previous', 'target': {'entity_id': 'input_select.shed_detection_summary_run_id'}, 'data': {'cycle': False}}}, {'sub_button_type': 'button', 'icon': 'mdi:star-four-points', 'tap_action': {'action': 'call-service', 'service': 'input_select.select_first', 'target': {'entity_id': 'input_select.shed_detection_summary_run_id'}}}, {'sub_button_type': 'button', 'icon': 'mdi:chevron-right', 'tap_action': {'action': 'call-service', 'service': 'input_select.select_next', 'target': {'entity_id': 'input_select.shed_detection_summary_run_id'}, 'data': {'cycle': False}}}]}}, {'type': 'markdown', 'content': "{{ states('input_text.shed_detection_summary_selected') }}"}, {'type': 'markdown', 'content': '<img src="/local/detection-summary/shed/viewer/{{ states(\'input_select.shed_detection_summary_run_id\') }}_generated.png" />'}, {'type': 'markdown', 'content': '<img src="/local/detection-summary/shed/viewer/{{ states(\'input_select.shed_detection_summary_run_id\') }}_best.jpg" style="width:100%;border-radius:12px;object-fit:cover;" />'}, {'type': 'markdown', 'content': "_Detection: {{ states('input_text.shed_detection_summary_timing') }}_\n\n_Cooldown: {{ states('input_text.shed_detection_summary_cooldown') }}_\n\n_Selection updated: {{ states.input_text.shed_detection_summary_selected.last_updated }}_"}]}]; config['views'][6]['cards'] = []
```

### Todo 8: Run tests

```bash
source /home/thaynes/workspace/hass-sandbox/.venv/bin/activate && cd /home/thaynes/workspace/hass-sandbox/appdaemon && python -m pytest tests/ -v --tb=short
```

Tests must pass. Since this is config-only work, existing tests should not be affected.

---

## Validation checklist

### apps-dev.yaml entries
- [ ] `detection_summary_back_yard_pets_dev` entry exists with correct `bundle_key: back_yard_pets`
- [ ] `detection_summary_back_yard_pets_dev` has `detection_profile: animals`
- [ ] `detection_summary_back_yard_pets_dev` has `best_min_person_score: 0` and `best_min_animal_count: 1`
- [ ] `detection_summary_back_yard_pets_dev` has correct camera/trigger entity IDs (shed_g5_ptz)
- [ ] `detection_viewer_back_yard_pets_dev` entry exists with correct `bundle_key: back_yard_pets`
- [ ] `detection_viewer_back_yard_pets_dev` has correct selected camera entity IDs
- [ ] `detection_viewer_back_yard_pets_dev` has `notification_action_prefix: "BACK_YARD_PETS_DS_VIEW"`
- [ ] No `door_notify` entry for back_yard_pets (correct — no door sensor)
- [ ] `detection_summary_shed_dev` entry exists with correct `bundle_key: shed`
- [ ] `detection_summary_shed_dev` does NOT have `detection_profile` (uses default)
- [ ] `detection_summary_shed_dev` has correct camera/trigger entity IDs (shed_ai_turret, ai_turret_motion)
- [ ] `detection_viewer_shed_dev` entry exists with correct `bundle_key: shed`
- [ ] `detection_viewer_shed_dev` has `notification_action_prefix: "SHED_DS_VIEW"`
- [ ] `shed_door_notify_dev` entry exists with correct door sensor entity
- [ ] `shed_door_notify_dev` has `door_open_state: "on"` and `door_closed_state: "off"` (binary_sensor)
- [ ] `shed_door_notify_dev` has `notification_url: "/detection-summary/shed"`
- [ ] `shed_door_notify_dev` has all 3 notify services
- [ ] All new entries use the correct `ai_provider_conf` (ollama-qwen9b + comfyui-qwen-edit)
- [ ] All new entries have `debug_preserve_run_dirs: true` where applicable (summary apps only)
- [ ] No existing entries in `apps-dev.yaml` were modified or deleted

### Dashboard
- [ ] View index 5 (path: `back-yard-pets`) has 5 cards in the detection-summary pattern
- [ ] View index 5 bubble-card entity is `input_select.back_yard_pets_detection_summary_run_id`
- [ ] View index 5 image paths use `detection-summary/back-yard/back-yard-pets/viewer/`
- [ ] View index 5 icon is `mdi:paw`
- [ ] View index 6 (path: `shed`) has 5 cards in the detection-summary pattern
- [ ] View index 6 bubble-card entity is `input_select.shed_detection_summary_run_id`
- [ ] View index 6 image paths use `detection-summary/shed/viewer/`
- [ ] View index 6 icon is `mdi:greenhouse`
- [ ] Both views have `dense_section_placement: true`
- [ ] Neither view edit affected views 0-4 (garage, front-door, bulkhead, package, back-deck-pets)

### Tests
- [ ] All existing unit tests pass

---

## Agent prompts

### Implementation Agent

```text
You are an Implementation Agent. Your task is fully described in the plan file at:

  /home/thaynes/workspace/hass-sandbox/.agents/plans/back-yard-pets-and-shed-entrances.md

Read the full plan file before doing anything else. It contains architecture context,
detailed implementation instructions including exact YAML to append and exact
python_transform strings for dashboard edits, and a validation checklist.

Also read these playbooks before making changes:
- /home/thaynes/workspace/hass-sandbox/.agents/playbooks/detection-app.md
- /home/thaynes/workspace/hass-sandbox/.agents/playbooks/ha-dashboard.md

Work through all todos in the plan in order (1-8). Key rules:
- Todos 1-5: Append YAML entries to the END of apps-dev.yaml. Do NOT modify existing entries.
- Todo 6: Populate back-yard-pets dashboard view (index 5) via MCP. Get config_hash first.
- Todo 7: Populate shed dashboard view (index 6) via MCP. MUST re-fetch config_hash after todo 6.
- Todo 8: Run the test suite.

CRITICAL: Use the FULL config_hash from ha_config_get_dashboard — never truncate it.
Dashboard edits must be sequential — re-fetch config_hash between each edit.

After completing all changes, run the full test suite and fix any failures:

  source /home/thaynes/workspace/hass-sandbox/.venv/bin/activate && cd /home/thaynes/workspace/hass-sandbox/appdaemon && python -m pytest tests/ -v --tb=short

DO NOT run deploy.py or copy any files to production. All changes stay in the dev environment only.
DO NOT create new Python files.
DO NOT modify any existing entries in apps-dev.yaml.
```

### Validation Agent

```text
You are a Validation Agent. Review the implementation described in the plan file at:

  /home/thaynes/workspace/hass-sandbox/.agents/plans/back-yard-pets-and-shed-entrances.md

Read the full plan file — the "Validation checklist" section lists every requirement to verify.

Also read these playbooks:
- /home/thaynes/workspace/hass-sandbox/.agents/playbooks/detection-app.md
- /home/thaynes/workspace/hass-sandbox/.agents/playbooks/ha-dashboard.md

DO NOT modify any files. Your job is to READ and VERIFY only.

Verify each checklist item:
1. Read /home/thaynes/workspace/hass-sandbox/appdaemon/apps/apps-dev.yaml and verify all 5 new entries.
2. Use ha_config_get_dashboard with url_path "detection-summary" to verify views 5 and 6.
3. Confirm views 0-4 were not modified (spot-check titles and paths).
4. Run the full test suite:

  source /home/thaynes/workspace/hass-sandbox/.venv/bin/activate && cd /home/thaynes/workspace/hass-sandbox/appdaemon && python -m pytest tests/ -v --tb=short

Output a PASS or FAIL verdict.

If FAIL, list every failing checklist item with:
  - File path and what is wrong
  - What the fix should be

Then produce a copy-pasteable prompt for the Implementation Agent in a fenced text block.
```

### Implementation Agent re-prompt template (for Validation Agent to use on FAIL)

```text
You are the Implementation Agent for the back-yard-pets-and-shed-entrances plan.

Validation Agent has completed a read-only validation pass. The following defects
were found that you must fix.

DEFECT 1

File: <path>

<What is wrong. What the fix should be.>

REQUIRED FIX

1. <First action>
2. <Second action>

Read the plan file at /home/thaynes/workspace/hass-sandbox/.agents/plans/back-yard-pets-and-shed-entrances.md
and the referenced playbooks before making changes.

DO NOT run deploy.py or copy files to production. Run the full test suite after
your changes and confirm it passes:

  source /home/thaynes/workspace/hass-sandbox/.venv/bin/activate && cd /home/thaynes/workspace/hass-sandbox/appdaemon && python -m pytest tests/ -v --tb=short
```

---

## Final planner review

After validation passes, the Planner Agent will:
1. Re-read `apps-dev.yaml` and verify all 5 new entries match the plan exactly
2. Fetch the dashboard via MCP and verify views 5 and 6 match the 5-card pattern
3. Run the test suite one final time
4. Check that no existing config was accidentally modified
5. Fix any remaining issues directly
