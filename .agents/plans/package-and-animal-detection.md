# Plan: Package Detection + Animal Detection (Summary + Viewer + Dashboard)

## Overview

Add two new detection summary entrances — **package detection** (doorbell) and **animal detection** (back deck pets) — by configuring YAML entries in `apps-dev.yaml` and populating placeholder dashboard views on the `detection-summary` dashboard.

A small amount of Python is needed: adding a built-in `animals` profile to `profiles.py` (and its test). The rest is YAML config and MCP dashboard edits.

## Architecture context

```
appdaemon/apps/detection_summary_app/profiles.py  <-- add PROFILE_ANIMALS built-in profile
appdaemon/tests/test_detection_profiles.py         <-- add tests for animals profile
appdaemon/apps/detection_summary_app/README.md     <-- update built-in profiles table
appdaemon/apps/apps-dev.yaml                       <-- 4 new entries (2 summary + 2 viewer)
detection-summary dashboard (HA MCP)               <-- populate views at index 3 (package) and index 4 (back-deck-pets)
```

The detection_summary_app and detection_summary_viewer apps are already built and configurable. Each entrance needs:
1. A `detection_summary_{bundle_key}_dev` entry in apps-dev.yaml
2. A `detection_viewer_{bundle_key}_dev` entry in apps-dev.yaml
3. A populated dashboard view with the 5-card detection-summary pattern

Self-provisioning handles helpers and relay scripts automatically on startup. All 8 `local_file` camera entities already exist in HA. The `shell_command.ds_refresh_detection_summary_viewer_www` already exists.

## Constraints

- **DO NOT** run `deploy.py` or copy files to `X:\`
- **DO NOT** modify any existing app entries in `apps-dev.yaml`
- Use the **FULL** `config_hash` from `ha_config_get_dashboard` — never truncate
- Dashboard edits must be sequential (each edit changes the `config_hash`)
- All changes stay in the dev environment only

---

## Parallelism analysis

| Todo | Files/resources touched | Dependencies | Track |
|------|------------------------|-------------|-------|
| Add animals built-in profile | `profiles.py` | none | A |
| Add animals profile tests | `test_detection_profiles.py` | after profile code | A |
| Update README profiles table | `README.md` | after profile code | A |
| Add package summary YAML | `apps-dev.yaml` | none | A |
| Add package viewer YAML | `apps-dev.yaml` | after package summary (same file) | A |
| Add animal summary YAML | `apps-dev.yaml` | after package viewer (same file) | A |
| Add animal viewer YAML | `apps-dev.yaml` | after animal summary (same file) | A |
| Populate package dashboard view | `detection-summary` dashboard (MCP) | after all YAML edits | A |
| Populate animal dashboard view | `detection-summary` dashboard (MCP) | after package dashboard (config_hash) | A |
| Run tests | none (read-only) | after all edits | A |

**All work is in a single track (Track A)** because:
- All YAML entries go in the same file (`apps-dev.yaml`)
- Both dashboard edits target the same dashboard (config_hash changes after each edit)

**Result: 1 Implementation Agent, sequential execution.**

---

## Implementation detail

### Todo 1: Add `PROFILE_ANIMALS` built-in profile to `profiles.py`

**File**: `/home/thaynes/workspace/hass-sandbox/appdaemon/apps/detection_summary_app/profiles.py`
**Action**: Add a new `PROFILE_ANIMALS` constant after `PROFILE_VEHICLES`, then register it in `BUILTIN_PROFILES`.

The key difference from `default`: animals is `required_for_publish: true`, people is `required_for_publish: false`. This means only animals gate publishing; people are context but won't trigger a publish on their own.

```python
PROFILE_ANIMALS = DetectionProfile(
    name="animals",
    description="Animals-only detection; people are context only",
    categories=(
        SubjectCategory(
            name="animals",
            display_name="Animals",
            required_for_publish=True,
            count_signals=("animal_count",),
            min_count_for_publish=1,
            image_constraint_signals=("animal_count",),
        ),
        SubjectCategory(
            name="people",
            display_name="People",
            required_for_publish=False,
            count_signals=("male_count", "female_count"),
            min_count_for_publish=1,
            image_constraint_signals=("male_count", "female_count"),
        ),
    ),
    score_fields=DEFAULT_SCORE_FIELDS,
    consensus_strategy="mode",
)
```

Then update `BUILTIN_PROFILES`:

```python
BUILTIN_PROFILES: dict[str, DetectionProfile] = {
    "default": PROFILE_DEFAULT,
    "packages": PROFILE_PACKAGES,
    "vehicles": PROFILE_VEHICLES,
    "animals": PROFILE_ANIMALS,
}
```

### Todo 2: Add tests for animals profile

**File**: `/home/thaynes/workspace/hass-sandbox/appdaemon/tests/test_detection_profiles.py`
**Action**: Add import of `PROFILE_ANIMALS`, add a `TestAnimalsProfile` class, and update the `test_builtin_profiles_has_three` test to expect 4.

Add import:
```python
from detection_summary_app.profiles import (
    ...
    PROFILE_ANIMALS,
    ...
)
```

Add test class (after `TestVehiclesProfile`):
```python
class TestAnimalsProfile:
    def test_animals_profile_has_animals_required(self):
        """Animals profile requires animals for publish, people are context only."""
        p = PROFILE_ANIMALS
        animals_cat = next(c for c in p.categories if c.name == "animals")
        assert animals_cat.required_for_publish is True
        assert "animal_count" in animals_cat.count_signals

    def test_animals_profile_people_not_required(self):
        """People category is NOT required for publish in animals profile."""
        people_cat = next(c for c in PROFILE_ANIMALS.categories if c.name == "people")
        assert people_cat.required_for_publish is False

    def test_animals_profile_no_extra_score_fields(self):
        """Animals profile uses only the 8 default score fields (no extras)."""
        assert len(PROFILE_ANIMALS.score_fields) == 8
        assert PROFILE_ANIMALS.score_fields == DEFAULT_SCORE_FIELDS

    def test_load_profile_by_name_animals(self):
        p = load_profile_by_name("animals")
        assert p.name == "animals"
        assert p is PROFILE_ANIMALS
```

Update `test_builtin_profiles_has_three` → rename to `test_builtin_profiles_count` and assert `>= 4` with `"animals"` check:
```python
def test_builtin_profiles_count(self):
    assert len(BUILTIN_PROFILES) >= 4
    assert "default" in BUILTIN_PROFILES
    assert "packages" in BUILTIN_PROFILES
    assert "vehicles" in BUILTIN_PROFILES
    assert "animals" in BUILTIN_PROFILES
```

### Todo 3: Update README built-in profiles table

**File**: `/home/thaynes/workspace/hass-sandbox/appdaemon/apps/detection_summary_app/README.md`
**Action**: Add the `animals` profile to the "Built-in profiles" table (around line 39-43).

Add row:
```
| `animals` | animals (animal_count) | People are context only (not required for publish) |
```

Also update the "Future work (TODO)" section 2 to note that `animals` is now a built-in profile (or simply remove that TODO since it's being implemented).

### Todo 4: Add package detection summary to apps-dev.yaml (was Todo 1)

**File**: `/home/thaynes/workspace/hass-sandbox/appdaemon/apps/apps-dev.yaml`
**Action**: Append the following YAML block at the end of the file.

```yaml
detection_summary_package_dev:
  module: detection_summary_app.manager
  class: DetectionSummary
  ha_url: !secret ha_url
  ha_token_env: TOKEN
  bundle_key: package
  detection_profile: packages
  best_min_person_score: 0
  capture_max_s: 60
  snapshot_ha_dir: /media/detection-summary/doorbell/package
  media_fs_root: !secret media_fs_root
  hass_entities:
    camera_entity_id: camera.g4_doorbell_pro_poe_package_camera
    trigger_entity_id: binary_sensor.g4_doorbell_pro_poe_package_detected
    best_image_camera_entity_id: camera.package_detection_summary_best
    generated_image_camera_entity_id: camera.package_detection_summary_generated
  data_instructions: >
    You are analyzing ONE security camera snapshot from a front door doorbell camera.
    Focus on people, packages/parcels/boxes, and delivery activity.
    Count packages carefully — a box, bag, or parcel on the porch counts even if no
    person is present. Cardboard boxes, shipping bags, and parcels all count as packages.
    Also return male_count and female_count as integer counts of people by gender (0 if none).
    Also return animal_count as an integer count of visible animals/pets (0 if none).
    Also return package_count as an integer count of visible packages (0 if none).
    Return scores on a 0-10 scale (integers preferred) with enough spread to rank frames.
    Favor frames where packages are clearly visible and identifiable.
    If a delivery person is present, favor frames where both the person and package are visible.
    The summary should be short and notification-friendly: 1 sentence, <= 140 characters.
  image_instructions: >
    Create a simple, clean illustration of the front door scene.
    If packages are visible, make them prominent in the illustration.
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

### Todo 5: Add package viewer to apps-dev.yaml

**File**: `/home/thaynes/workspace/hass-sandbox/appdaemon/apps/apps-dev.yaml`
**Action**: Append after the package summary entry.

```yaml
detection_viewer_package_dev:
  module: detection_summary_viewer.detection_summary_viewer_app
  class: DetectionSummaryViewer
  ha_url: !secret ha_url
  ha_token_env: TOKEN
  bundle_key: package
  snapshot_ha_dir: /media/detection-summary/doorbell/package
  media_fs_root: !secret media_fs_root
  hass_entities:
    selected_best_image_camera_entity_id: camera.package_detection_summary_selected_best
    selected_generated_image_camera_entity_id: camera.package_detection_summary_selected_generated
  notification_action_prefix: "PACKAGE_DS_VIEW"
```

### Todo 6: Add animal detection summary to apps-dev.yaml

**File**: `/home/thaynes/workspace/hass-sandbox/appdaemon/apps/apps-dev.yaml`
**Action**: Append after the package viewer entry.

```yaml
detection_summary_back_deck_pets_dev:
  module: detection_summary_app.manager
  class: DetectionSummary
  ha_url: !secret ha_url
  ha_token_env: TOKEN
  bundle_key: back_deck_pets
  best_min_person_score: 0
  best_min_animal_count: 1
  detection_profile: animals
  snapshot_ha_dir: /media/detection-summary/back-yard/back-deck-pets
  media_fs_root: !secret media_fs_root
  hass_entities:
    camera_entity_id: camera.back_yard_ai_pro_medium_resolution_channel
    trigger_entity_id: binary_sensor.back_yard_ai_pro_animal_detected
    best_image_camera_entity_id: camera.back_deck_pets_detection_summary_best
    generated_image_camera_entity_id: camera.back_deck_pets_detection_summary_generated
  data_instructions: >
    You are analyzing ONE security camera snapshot from a backyard camera.
    Focus primarily on animals/pets — dogs, cats, birds, deer, squirrels, raccoons, etc.
    Count animals carefully. Identify the species when you can.
    People may appear in the frame but are secondary; still count them accurately.
    Also return male_count and female_count as integer counts of people by gender (0 if none).
    Also return animal_count as an integer count of visible animals/pets (0 if none).
    Return scores on a 0-10 scale (integers preferred) with enough spread to rank frames.
    Favor frames where animals are clearly visible and identifiable by species.
    The summary should be short and notification-friendly: 1 sentence, <= 140 characters.
  image_instructions: >
    Create a simple, clean illustration of the backyard scene.
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

### Todo 7: Add animal viewer to apps-dev.yaml

**File**: `/home/thaynes/workspace/hass-sandbox/appdaemon/apps/apps-dev.yaml`
**Action**: Append after the animal summary entry.

```yaml
detection_viewer_back_deck_pets_dev:
  module: detection_summary_viewer.detection_summary_viewer_app
  class: DetectionSummaryViewer
  ha_url: !secret ha_url
  ha_token_env: TOKEN
  bundle_key: back_deck_pets
  snapshot_ha_dir: /media/detection-summary/back-yard/back-deck-pets
  media_fs_root: !secret media_fs_root
  hass_entities:
    selected_best_image_camera_entity_id: camera.back_deck_pets_detection_summary_selected_best
    selected_generated_image_camera_entity_id: camera.back_deck_pets_detection_summary_selected_generated
  notification_action_prefix: "BACK_DECK_PETS_DS_VIEW"
```

### Todo 8: Populate package dashboard view (index 3)

**Dashboard**: `detection-summary`
**View index**: 3 (path: `package`)
**Action**: Use MCP `ha_config_get_dashboard` to get the current `config_hash`, then `ha_config_set_dashboard` with `python_transform` to populate the view.

The view already exists as a placeholder with an empty grid section. Replace its sections with the 5-card detection-summary pattern.

**Card pattern** (follow the exact structure from the existing garage view at index 0):

| Card # | Type | Content |
|--------|------|---------|
| 1 | `custom:bubble-card` | select entity `input_select.package_detection_summary_run_id`, icon `mdi:package-variant-closed`, name "Package Detection - Navigation", with prev/first/next sub-buttons |
| 2 | `markdown` | `{{ states('input_text.package_detection_summary_selected') }}` |
| 3 | `markdown` | `<img src="/local/detection-summary/doorbell/package/viewer/{{ states('input_select.package_detection_summary_run_id') }}_generated.png" />` |
| 4 | `markdown` | `<img src="/local/detection-summary/doorbell/package/viewer/{{ states('input_select.package_detection_summary_run_id') }}_best.jpg" style="width:100%;border-radius:12px;object-fit:cover;" />` |
| 5 | `markdown` | Timing + cooldown + last_updated metadata (same pattern as garage) |

**View-level settings to set**:
- `title`: "Package Detection Summary"
- `max_columns`: 1
- `dense_section_placement`: True

**CRITICAL**: The `config_hash` from `ha_config_get_dashboard` must be used in full. The current hash is `d13caa2493cb8ccc` but it will change after any prior edit. Always re-fetch before this step.

### Todo 9: Populate animal dashboard view (index 4)

**Dashboard**: `detection-summary`
**View index**: 4 (path: `back-deck-pets`)
**Action**: Same as Todo 5 but for the animal view. Must get a FRESH `config_hash` after Todo 5 completes.

**Card pattern**:

| Card # | Type | Content |
|--------|------|---------|
| 1 | `custom:bubble-card` | select entity `input_select.back_deck_pets_detection_summary_run_id`, icon `mdi:dog`, name "Back Deck Pets - Navigation", with prev/first/next sub-buttons |
| 2 | `markdown` | `{{ states('input_text.back_deck_pets_detection_summary_selected') }}` |
| 3 | `markdown` | `<img src="/local/detection-summary/back-yard/back-deck-pets/viewer/{{ states('input_select.back_deck_pets_detection_summary_run_id') }}_generated.png" />` |
| 4 | `markdown` | `<img src="/local/detection-summary/back-yard/back-deck-pets/viewer/{{ states('input_select.back_deck_pets_detection_summary_run_id') }}_best.jpg" style="width:100%;border-radius:12px;object-fit:cover;" />` |
| 5 | `markdown` | Timing + cooldown + last_updated metadata |

**View-level settings to set**:
- `title`: "Back Deck Pets Detection Summary"
- `max_columns`: 1
- `dense_section_placement`: True

### Todo 10: Run tests

Run the full test suite to verify no regressions:

```bash
source .venv/bin/activate && cd appdaemon && python -m pytest tests/ -v --tb=short
```

---

## Reference: existing bubble-card structure (from garage view)

Use this exact structure for the bubble-card in each new view (substituting bundle_key, icon, name, and entity):

```python
{
    'type': 'custom:bubble-card',
    'card_type': 'select',
    'entity': 'input_select.{bk}_detection_summary_run_id',
    'name': '{display_name} - Navigation',
    'show_name': True,
    'rows': 1.719,
    'icon': '{icon}',
    'sub_button': {
        'main': [],
        'bottom': [
            {
                'sub_button_type': 'button',
                'icon': 'mdi:chevron-left',
                'tap_action': {
                    'action': 'call-service',
                    'service': 'input_select.select_previous',
                    'target': {'entity_id': 'input_select.{bk}_detection_summary_run_id'},
                    'data': {'cycle': False}
                }
            },
            {
                'sub_button_type': 'button',
                'icon': 'mdi:star-four-points',
                'tap_action': {
                    'action': 'call-service',
                    'service': 'input_select.select_first',
                    'target': {'entity_id': 'input_select.{bk}_detection_summary_run_id'}
                }
            },
            {
                'sub_button_type': 'button',
                'icon': 'mdi:chevron-right',
                'tap_action': {
                    'action': 'call-service',
                    'service': 'input_select.select_next',
                    'target': {'entity_id': 'input_select.{bk}_detection_summary_run_id'},
                    'data': {'cycle': False}
                }
            }
        ]
    }
}
```

## Reference: existing timing/metadata card content (from garage view)

```
_Detection: {{ states('input_text.{bk}_detection_summary_timing') }}_\n\n_Cooldown: {{ states('input_text.{bk}_detection_summary_cooldown') }}_\n\n_Selection updated: {{ states.input_text.{bk}_detection_summary_selected.last_updated }}_
```

---

## Validation checklist

### Built-in animals profile
- [ ] `PROFILE_ANIMALS` exists in `profiles.py` with `name="animals"`
- [ ] Animals category has `required_for_publish=True`
- [ ] People category has `required_for_publish=False`
- [ ] `"animals"` registered in `BUILTIN_PROFILES` dict
- [ ] `load_profile_by_name("animals")` returns `PROFILE_ANIMALS`
- [ ] `PROFILE_ANIMALS` uses only `DEFAULT_SCORE_FIELDS` (no extras — 8 fields)
- [ ] `TestAnimalsProfile` test class exists in `test_detection_profiles.py`
- [ ] `test_builtin_profiles_count` asserts `>= 4` and includes `"animals"`
- [ ] README built-in profiles table includes `animals` row

### apps-dev.yaml entries
- [ ] `detection_summary_package_dev` entry exists with correct `bundle_key: package`
- [ ] Package summary has `detection_profile: packages`
- [ ] Package summary has `best_min_person_score: 0`
- [ ] Package summary has `capture_max_s: 60`
- [ ] Package summary has `snapshot_ha_dir: /media/detection-summary/doorbell/package`
- [ ] Package summary references correct camera entities (`camera.g4_doorbell_pro_poe_package_camera`, `binary_sensor.g4_doorbell_pro_poe_package_detected`)
- [ ] Package summary has `debug_preserve_run_dirs: true`
- [ ] Package summary has correct `ai_provider_conf` with ollama + comfyui bundles
- [ ] `detection_viewer_package_dev` entry exists with correct `bundle_key: package`
- [ ] Package viewer has `snapshot_ha_dir: /media/detection-summary/doorbell/package`
- [ ] Package viewer has `notification_action_prefix: "PACKAGE_DS_VIEW"`
- [ ] Package viewer references correct selected camera entities
- [ ] `detection_summary_back_deck_pets_dev` entry exists with correct `bundle_key: back_deck_pets`
- [ ] Animal summary has `best_min_person_score: 0`
- [ ] Animal summary has `best_min_animal_count: 1`
- [ ] Animal summary has `detection_profile: animals` (built-in profile reference, NOT inline)
- [ ] Animal summary has `snapshot_ha_dir: /media/detection-summary/back-yard/back-deck-pets`
- [ ] Animal summary references correct camera entities (`camera.back_yard_ai_pro_medium_resolution_channel`, `binary_sensor.back_yard_ai_pro_animal_detected`)
- [ ] Animal summary has `debug_preserve_run_dirs: true`
- [ ] Animal summary has correct `ai_provider_conf` with ollama + comfyui bundles
- [ ] `detection_viewer_back_deck_pets_dev` entry exists with correct `bundle_key: back_deck_pets`
- [ ] Animal viewer has `snapshot_ha_dir: /media/detection-summary/back-yard/back-deck-pets`
- [ ] Animal viewer has `notification_action_prefix: "BACK_DECK_PETS_DS_VIEW"`
- [ ] Animal viewer references correct selected camera entities
- [ ] All 4 new entries have keys ending in `_dev`
- [ ] No existing entries in apps-dev.yaml were modified

### Dashboard views
- [ ] Package view (index 3) has `title: "Package Detection Summary"`, `max_columns: 1`, `dense_section_placement: True`
- [ ] Package view has 5 cards in the correct order (bubble-card, summary, generated img, best img, metadata)
- [ ] Package bubble-card uses `input_select.package_detection_summary_run_id` entity
- [ ] Package bubble-card has icon `mdi:package-variant-closed`
- [ ] Package image URLs use path `detection-summary/doorbell/package/viewer/`
- [ ] Package metadata card references `input_text.package_detection_summary_timing`, `package_detection_summary_cooldown`, `package_detection_summary_selected`
- [ ] Animal view (index 4) has `title: "Back Deck Pets Detection Summary"`, `max_columns: 1`, `dense_section_placement: True`
- [ ] Animal view has 5 cards in the correct order
- [ ] Animal bubble-card uses `input_select.back_deck_pets_detection_summary_run_id` entity
- [ ] Animal bubble-card has icon `mdi:dog`
- [ ] Animal image URLs use path `detection-summary/back-yard/back-deck-pets/viewer/`
- [ ] Animal metadata card references `input_text.back_deck_pets_detection_summary_timing`, `back_deck_pets_detection_summary_cooldown`, `back_deck_pets_detection_summary_selected`
- [ ] Both views have the 3 sub-buttons (prev/first/next) with correct entity targets
- [ ] Existing views (garage at 0, front-door at 1, bulkhead at 2) are unchanged

### Tests
- [ ] Full test suite passes with no regressions

---

## Agent prompts

### Implementation Agent (1 agent, sequential)

```text
You are an Implementation Agent. Your task is fully described in the plan file at:

  /home/thaynes/workspace/hass-sandbox/.agents/plans/package-and-animal-detection.md

Read the full plan file before doing anything else. It contains architecture context,
detailed implementation instructions with exact YAML blocks, MCP dashboard editing
instructions, and a validation checklist.

Also read these rule/playbook files before making any changes:
- /home/thaynes/workspace/hass-sandbox/.agents/playbooks/detection-app.md
- /home/thaynes/workspace/hass-sandbox/.agents/playbooks/ha-dashboard.md
- /home/thaynes/workspace/hass-sandbox/.claude/rules/appdaemon.md

Work through all 10 todos in the plan in order:
- Todos 1-3: Add PROFILE_ANIMALS to profiles.py, add tests, update README
- Todos 4-7: Append 4 new YAML entries to END of apps-dev.yaml (do NOT modify existing entries)
- Todos 8-9: Populate dashboard views via MCP
- Todo 10: Run tests

For the profile code (Todo 1): Add PROFILE_ANIMALS to profiles.py following the
exact pattern of PROFILE_PACKAGES and PROFILE_VEHICLES. Register it in BUILTIN_PROFILES.

For the tests (Todo 2): Add PROFILE_ANIMALS import, TestAnimalsProfile class, and
update the builtin count assertion. Follow the exact code in the plan.

For the README (Todo 3): Add animals to the built-in profiles table.

For the YAML edits (Todos 4-7): Append all 4 new entries to the END of
/home/thaynes/workspace/hass-sandbox/appdaemon/apps/apps-dev.yaml
Do NOT modify any existing entries. Use the exact YAML from the plan.
The animal summary uses `detection_profile: animals` (the built-in profile from Todo 1).

For the dashboard edits (Todos 8-9): Use ha_config_get_dashboard to get the
FULL config_hash before EACH edit. Use ha_config_set_dashboard with python_transform.
CRITICAL: Never truncate the config_hash — use the full string. The existing views
at indices 3 (package) and 4 (back-deck-pets) are empty placeholders — populate them
with the 5-card detection-summary pattern described in the plan. Use the "Reference"
sections in the plan for the exact bubble-card structure and metadata card content.
After each dashboard edit, verify with ha_config_get_dashboard.

After completing all changes, run the full test suite and fix any failures:

  source .venv/bin/activate && cd appdaemon && python -m pytest tests/ -v --tb=short

DO NOT run deploy.py or copy any files to X:\. All changes stay in the dev environment only.
```

### Validation Agent (read-only)

```text
You are a Validation Agent. Review the implementation described in the plan file at:

  /home/thaynes/workspace/hass-sandbox/.agents/plans/package-and-animal-detection.md

Read the full plan file — the "Validation checklist" section lists every requirement to verify.

Also read these rule files:
- /home/thaynes/workspace/hass-sandbox/.agents/playbooks/detection-app.md
- /home/thaynes/workspace/hass-sandbox/.agents/playbooks/ha-dashboard.md

DO NOT modify any files. Your job is to READ and VERIFY only.

Verify each checklist item:
1. Read /home/thaynes/workspace/hass-sandbox/appdaemon/apps/detection_summary_app/profiles.py
   and verify PROFILE_ANIMALS exists with correct categories and is in BUILTIN_PROFILES.
2. Read /home/thaynes/workspace/hass-sandbox/appdaemon/tests/test_detection_profiles.py
   and verify TestAnimalsProfile class exists and builtin count assertion updated.
3. Read /home/thaynes/workspace/hass-sandbox/appdaemon/apps/detection_summary_app/README.md
   and verify animals row in built-in profiles table.
4. Read /home/thaynes/workspace/hass-sandbox/appdaemon/apps/apps-dev.yaml and verify
   all 4 new entries match the plan exactly.
5. Use ha_config_get_dashboard with url_path "detection-summary" and verify:
   - Views at indices 3 and 4 have the correct 5-card structure
   - All entity references, image paths, icons, and titles match the plan
   - Existing views at indices 0, 1, 2 are unchanged
3. Run the full test suite:
   source .venv/bin/activate && cd appdaemon && python -m pytest tests/ -v --tb=short

Output a PASS or FAIL verdict.

If FAIL, list every failing checklist item with:
  - File path or resource where the issue is
  - What is wrong or missing
  - What the fix should be

Then produce a copy-pasteable prompt for the Implementation Agent in a fenced
```text``` block.
```

### Implementation Agent re-prompt template (for Validation Agent to use on FAIL)

```text
You are the Implementation Agent for the package-and-animal-detection plan.

Validation Agent has completed a read-only validation pass. The following defects
were found that you must fix.

DEFECT 1

File/Resource: <path or dashboard name>

<What is wrong. What the fix should be.>

REQUIRED FIX

1. <First action>
2. <Second action>

Read the plan file at /home/thaynes/workspace/hass-sandbox/.agents/plans/package-and-animal-detection.md
and rules before making changes. Do not run deploy.py or copy files to X:\. Run the
full test suite after your changes and confirm it passes:

  source .venv/bin/activate && cd appdaemon && python -m pytest tests/ -v --tb=short
```

---

## Final planner review

After Validation returns PASS:
1. Re-read `apps-dev.yaml` and compare to the plan
2. Use `ha_config_get_dashboard` to verify both dashboard views
3. Run the full test suite
4. Code-review for stale config, missed entity references, or YAML formatting issues
5. Fix any remaining issues directly
