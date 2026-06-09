# Plan: Configurable Detection Profiles for detection_summary_app

## Overview

Enhance the detection_summary_app to support configurable detection profiles that define what signals to extract from images, which signals gate bundle publishing, and how multi-frame signals are aggregated for image generation accuracy.

Currently the app is hardcoded to detect people (male_count, female_count) and animals (animal_count), with person_score gating bundle generation. This plan introduces a **detection profile** system where each app instance declares which subject categories matter, what schema fields the LLM should return, and how those signals control publishing and image generation.

## Architecture

```
apps-prod.yaml (per-instance config)
  |
  |-- detection_profile: "people_and_packages"  # or inline profile dict
  |
detection_summary_app/
  |-- profiles.py  (NEW) -- DetectionProfile dataclass + built-in profiles + loader
  |-- prompting/
  |     |-- schema_specs.py  (MODIFIED) -- profile-driven schema generation
  |     |-- score_prompt_builder.py  (MODIFIED) -- profile-aware prompt building
  |     |-- image_prompt_builder.py  (MODIFIED) -- profile-aware population constraints
  |     |-- narrative_prompt_builder.py  (MODIFIED) -- profile-aware narrative facts
  |     |-- score_normalizer.py  (MODIFIED) -- profile-aware normalization
  |-- population.py  (MODIFIED) -- profile-driven bounds computation + consensus logic
  |-- publish_gate.py  (MODIFIED) -- profile-driven gating
  |-- selection.py  (MODIFIED) -- ScoreResult generalized + _pick_key profile-aware
  |-- bundle.py  (MODIFIED) -- profile-aware bundle building
  |-- manager.py  (MODIFIED) -- loads profile, passes through pipeline
```

## Constraints

- DO NOT run `deploy.py` or copy files to `X:\`
- DO NOT modify files under `appdaemon/providers/`
- All changes stay within `appdaemon/apps/detection_summary_app/` and `appdaemon/tests/`
- Security rules S1-S7 always apply
- Maintain full backward compatibility: existing garage/bulkhead configs (no `detection_profile` key) must work identically to today
- The `ScoreResult` dataclass is used extensively across tests and modules; changes must be backward-compatible
- Do not break any existing tests

## Design decisions

### 1. DetectionProfile dataclass

A new file `appdaemon/apps/detection_summary_app/profiles.py` defines the profile system:

```python
@dataclass(frozen=True)
class SignalSpec:
    """One extractable signal from a frame."""
    key: str                           # e.g. "male_count", "package_count", "vehicle_type"
    type_hint: str = "int"             # "int", "float", "str"
    default: Any = 0
    prompt_guidance: str = ""          # instruction for the LLM
    participates_in_ranking: bool = True
    participates_in_gating: bool = False
    is_count_signal: bool = False      # True for signals that represent subject counts
    parent_signal: str | None = None   # e.g. male_count.parent_signal = "person_count"


@dataclass(frozen=True)
class SubjectCategory:
    """A category of detectable subject (people, packages, vehicles, animals)."""
    name: str                          # "people", "packages", "vehicles", "animals"
    display_name: str                  # "People", "Packages", etc.
    required_for_publish: bool = False # If True, at least one must be present to publish
    count_signals: tuple[str, ...] = () # Signal keys that count this category
    min_count_for_publish: int = 1     # Threshold for publishing
    # For image generation: which count signals constrain max subjects
    image_constraint_signals: tuple[str, ...] = ()


@dataclass(frozen=True)
class DetectionProfile:
    """Complete detection configuration for an app instance."""
    name: str                          # "default", "packages", "vehicles", etc.
    description: str = ""
    categories: tuple[SubjectCategory, ...] = ()
    score_fields: tuple[ScoreFieldSpec, ...] = ()  # Reuses existing ScoreFieldSpec
    # Consensus strategy for multi-frame signal aggregation (image gen accuracy)
    consensus_strategy: str = "mode"   # "mode" (most common), "max", "median"
```

### 2. Built-in profiles

```python
PROFILE_DEFAULT = DetectionProfile(
    name="default",
    description="People and animals (current behavior)",
    categories=(
        SubjectCategory(
            name="people",
            display_name="People",
            required_for_publish=True,
            count_signals=("male_count", "female_count"),
            min_count_for_publish=1,  # derived from best_min_person_score logic
            image_constraint_signals=("male_count", "female_count"),
        ),
        SubjectCategory(
            name="animals",
            display_name="Animals",
            required_for_publish=True,
            count_signals=("animal_count",),
            min_count_for_publish=1,
            image_constraint_signals=("animal_count",),
        ),
    ),
    score_fields=DEFAULT_SCORE_FIELDS,  # existing 8 fields unchanged
    consensus_strategy="mode",
)

PROFILE_PACKAGES = DetectionProfile(
    name="packages",
    description="People, animals, and package detection",
    categories=(
        SubjectCategory(name="people", ...same as default...),
        SubjectCategory(name="animals", ...same as default...),
        SubjectCategory(
            name="packages",
            display_name="Packages",
            required_for_publish=True,
            count_signals=("package_count",),
            min_count_for_publish=1,
            image_constraint_signals=("package_count",),
        ),
    ),
    score_fields=DEFAULT_SCORE_FIELDS + (
        ScoreFieldSpec(
            key="package_count", type_hint="int", default=0,
            participates_in_ranking=True, participates_in_gating=True,
            prompt_guidance="integer count of packages/parcels/boxes visible (0 if none)",
        ),
    ),
    consensus_strategy="mode",
)

PROFILE_VEHICLES = DetectionProfile(
    name="vehicles",
    description="People, animals, and vehicle detection",
    categories=(...),
    score_fields=DEFAULT_SCORE_FIELDS + (
        ScoreFieldSpec(key="vehicle_count", ...),
        ScoreFieldSpec(key="vehicle_type", type_hint="str", default="",
            prompt_guidance="vehicle type if visible: car, truck, van, delivery, motorcycle, bicycle, none"),
    ),
)
```

### 3. Profile loading in manager.py

```python
# In initialize():
profile_ref = self.args.get("detection_profile")
if profile_ref is None:
    self._profile = load_default_profile()
elif isinstance(profile_ref, str):
    self._profile = load_profile_by_name(profile_ref)
elif isinstance(profile_ref, dict):
    self._profile = load_profile_from_dict(profile_ref)
```

When no `detection_profile` is set (existing configs), behavior is identical to today.

### 4. ScoreResult generalization

The current `ScoreResult` has fixed fields: `male_count`, `female_count`, `animal_count`, `person_score`, `face_score`, `frame_score`, `pose`, `summary`, `structured`.

To maintain backward compatibility while supporting new signals:
- Keep all existing fields on ScoreResult (no breaking change)
- Add an `extra_signals: dict[str, Any]` field (default `{}`) for profile-specific signals like `package_count`, `vehicle_type`
- The `structured` field already stores raw LLM output; `extra_signals` stores normalized profile-specific values

```python
@dataclass
class ScoreResult:
    male_count: int
    female_count: int
    animal_count: int
    person_score: float
    face_score: float
    frame_score: float
    pose: str
    summary: str
    structured: dict[str, Any]
    extra_signals: dict[str, Any] = field(default_factory=dict)  # NEW
```

### 5. Multi-frame consensus for image generation (replaces naive max)

The current `compute_population_bounds` takes the max of each count across all frames. This causes phantom subjects when one outlier frame reports extra people.

New approach in `population.py`:

```python
def compute_population_consensus(
    scored: dict[int, ScoreResult],
    profile: DetectionProfile,
) -> dict[str, Any]:
    """
    Compute consensus counts across frames using the profile's strategy.

    Strategies:
    - "mode": most frequently occurring count (ties broken by preferring lower)
    - "max": current behavior (backward compat)
    - "median": median count rounded down

    Returns dict with keys like "consensus_male_count", "max_male_count", etc.
    """
```

The image prompt builder will use consensus values as the "expected" count and max values as the "hard limit", giving the LLM a more accurate target:
- "The scene most likely contains 1 male person and 1 female person (do not exceed 2 male, 1 female)"

### 6. Profile-driven publish gate

```python
def should_publish_bundle(
    *,
    scored: dict[int, ScoreResult],
    profile: DetectionProfile,
    best_person_score: float,         # kept for backward compat
    best_min_person_score: float,     # kept for backward compat
    best_min_animal_count: int = 1,   # kept for backward compat
) -> bool:
    """
    Profile-driven: publish if ANY required_for_publish category meets its threshold.
    Fallback: if no profile categories, use legacy person_score + animal_count logic.
    """
```

### 7. Configuration schema (apps-prod.yaml)

Existing configs (no change needed):
```yaml
detection_summary_garage:
  # ... no detection_profile key = default profile = current behavior
```

New config with named profile:
```yaml
detection_summary_front_door:
  detection_profile: packages
  # ...
```

New config with inline profile:
```yaml
detection_summary_driveway:
  detection_profile:
    name: custom_driveway
    categories:
      - name: vehicles
        required_for_publish: true
        count_signals: [vehicle_count]
        min_count_for_publish: 1
      - name: people
        required_for_publish: true
        count_signals: [male_count, female_count]
    extra_score_fields:
      - key: vehicle_count
        type_hint: int
        default: 0
        prompt_guidance: "integer count of vehicles visible (0 if none)"
      - key: vehicle_type
        type_hint: str
        default: ""
        prompt_guidance: "vehicle type: car, truck, van, delivery, motorcycle, none"
    consensus_strategy: mode
```

---

## Implementation details

### Step 1: Create profiles.py + update ScoreResult

**Files:**
- `appdaemon/apps/detection_summary_app/profiles.py` (NEW)
- `appdaemon/apps/detection_summary_app/selection.py` (MODIFY)

**profiles.py** must contain:
- `SignalSpec` -- not needed if we reuse `ScoreFieldSpec` from schema_specs. Decision: reuse `ScoreFieldSpec`. No new class needed.
- `SubjectCategory` dataclass as designed above
- `DetectionProfile` dataclass as designed above
- `BUILTIN_PROFILES: dict[str, DetectionProfile]` with at least: `"default"`, `"packages"`, `"vehicles"`
- `load_profile_by_name(name: str) -> DetectionProfile` -- looks up in BUILTIN_PROFILES
- `load_profile_from_dict(data: dict) -> DetectionProfile` -- constructs from inline YAML dict
- `load_default_profile() -> DetectionProfile` -- returns BUILTIN_PROFILES["default"]

**selection.py** changes:
- Add `extra_signals: dict[str, Any] = field(default_factory=dict)` to `ScoreResult` (line 19)
- This is backward-compatible because it has a default value
- NOTE: The existing `ScoreResult(0, 0, 0, 0, 0, 0, "none", "", {})` positional calls in selection.py (line 138, 140) still work because extra_signals has a default
- `_pick_key` function: add profile-aware variant that considers extra count signals

**selection.py _pick_key enhancement:**
```python
def _pick_key(res: ScoreResult, profile: DetectionProfile | None = None) -> tuple:
    """Rank key for frame selection. Profile-aware when provided."""
    # If no profile, use existing logic (backward compat)
    if profile is None:
        has_subject = 1 if (int(res.animal_count) > 0 or res.person_score > 0) else 0
        ...existing logic...

    # Profile-aware: check all category count signals
    total_subjects = 0
    for cat in profile.categories:
        for sig_key in cat.count_signals:
            val = _get_signal_value(res, sig_key)
            total_subjects += max(0, int(val))
    has_subject = 1 if total_subjects > 0 or res.person_score > 0 else 0
    ...rest similar...
```

Add a helper `_get_signal_value(res: ScoreResult, key: str) -> Any` that checks known attrs first, then `extra_signals`.

### Step 2: Update prompting layer

**Files:**
- `appdaemon/apps/detection_summary_app/prompting/schema_specs.py` (MODIFY)
- `appdaemon/apps/detection_summary_app/prompting/score_prompt_builder.py` (MODIFY)
- `appdaemon/apps/detection_summary_app/prompting/score_normalizer.py` (MODIFY)
- `appdaemon/apps/detection_summary_app/prompting/__init__.py` (MODIFY)

**schema_specs.py:**
- Add `schema_from_profile(profile: DetectionProfile) -> ScoreSchemaSpec` function
- This merges the profile's `score_fields` into a `ScoreSchemaSpec`
- When profile has extra fields beyond DEFAULT_SCORE_FIELDS, they get appended

**score_prompt_builder.py:**
- `ScorePromptBuilder.__init__` accepts optional `profile: DetectionProfile`
- When profile is set, `build()` adds category-specific guidance (e.g., "Also detect packages...")
- Schema is derived from profile

**score_normalizer.py:**
- `normalize_score_data` populates `extra_signals` for any schema fields not in the hardcoded ScoreResult attrs
- After building the ScoreResult with the 8 standard fields, iterate remaining schema fields and put them in `extra_signals`

**prompting/__init__.py:**
- Export new names: `schema_from_profile`

### Step 3: Update population.py + publish_gate.py

**Files:**
- `appdaemon/apps/detection_summary_app/population.py` (MODIFY)
- `appdaemon/apps/detection_summary_app/publish_gate.py` (MODIFY)

**population.py:**
- Add `compute_population_consensus(scored, profile) -> dict[str, Any]` implementing mode/max/median strategies
- Keep existing `compute_population_bounds` for backward compat; have it call consensus internally when profile is default
- Add `augment_image_instructions_from_profile(base_instructions, consensus, profile) -> str` that generates profile-aware constraints

The consensus logic for "mode" strategy:
```python
from collections import Counter

def _mode_value(values: list[int]) -> int:
    """Most common value. Ties broken by preferring lower count (conservative)."""
    if not values:
        return 0
    counts = Counter(values)
    max_freq = max(counts.values())
    candidates = [v for v, c in counts.items() if c == max_freq]
    return min(candidates)  # conservative: prefer lower on tie
```

For each count signal in the profile, compute mode/max/median across all scored frames:
```python
{
    "consensus_male_count": 1,     # mode
    "max_male_count": 2,           # hard limit
    "consensus_female_count": 0,
    "max_female_count": 1,
    "consensus_animal_count": 0,
    "max_animal_count": 0,
    "consensus_package_count": 1,  # profile-specific
    "max_package_count": 2,
}
```

**publish_gate.py:**
- Add `profile: DetectionProfile | None = None` parameter to `should_publish_bundle`
- When profile is None, use existing logic (backward compat)
- When profile is set: iterate `profile.categories` where `required_for_publish=True`, check if any frame meets the `min_count_for_publish` for that category's `count_signals`
- Also keep the `best_person_score >= best_min_person_score` check as a fallback

### Step 4: Update image_prompt_builder.py + narrative_prompt_builder.py

**Files:**
- `appdaemon/apps/detection_summary_app/prompting/image_prompt_builder.py` (MODIFY)
- `appdaemon/apps/detection_summary_app/prompting/narrative_prompt_builder.py` (MODIFY)

**image_prompt_builder.py:**
- `build()` accepts optional `consensus_bounds: dict[str, Any]` alongside `population_bounds`
- When consensus_bounds is provided, generate "most likely" + "hard limit" constraints instead of just max:
  - "The scene most likely contains 1 male person(s) and 0 female person(s) (max: 2 male, 1 female)"
  - "The scene most likely contains 1 package(s) (max: 2)"
- For categories with no consensus data, fall back to max bounds
- Profile-specific hallucination guardrails: "Do NOT include packages unless clearly visible in the reference frames"

**narrative_prompt_builder.py:**
- `build()` accepts optional `profile: DetectionProfile`
- When profile includes non-default categories, add them to the JSON schema description:
  - "Each item also includes: package_count, vehicle_type"
- Update output JSON spec to include profile-specific signals in key_events

### Step 5: Update bundle.py + manager.py

**Files:**
- `appdaemon/apps/detection_summary_app/bundle.py` (MODIFY)
- `appdaemon/apps/detection_summary_app/manager.py` (MODIFY)

**bundle.py:**
- `build_bundle_dict` accepts optional `profile: DetectionProfile`
- `_cand()` includes `extra_signals` from ScoreResult
- `bundle["summary"]["scores"]` includes extra signal values
- `_rank_key` becomes profile-aware (delegates to selection._pick_key with profile)

**manager.py:**
- `initialize()`: load profile from `self.args.get("detection_profile")`
- Pass profile through to:
  - `ScorePromptBuilder(schema=schema_from_profile(self._profile))`
  - `should_publish_bundle(..., profile=self._profile)`
  - `compute_population_consensus(scored, self._profile)` (replaces `compute_population_bounds`)
  - `self._image_prompt_builder.build(..., consensus_bounds=consensus)`
  - `build_bundle_dict(..., profile=self._profile)`
- The `data_instructions` in YAML continues to be the base instruction; the profile adds structured schema guidance on top

### Step 6: Tests

**New test files:**
- `appdaemon/tests/test_detection_profiles.py` (NEW)
- `appdaemon/tests/test_detection_summary_consensus.py` (NEW)

**Modified test files:**
- `appdaemon/tests/test_detection_summary_prompting.py` (MODIFY)
- `appdaemon/tests/test_detection_summary_publish_gate.py` (MODIFY)
- `appdaemon/tests/test_detection_summary_population.py` (MODIFY)
- `appdaemon/tests/test_detection_summary_selection.py` (MODIFY)

### Test case tables

#### test_detection_profiles.py

| Test | What it verifies |
|------|-----------------|
| test_default_profile_matches_current_behavior | Default profile has people+animals categories, 8 score fields |
| test_packages_profile_has_package_count | Packages profile adds package_count field |
| test_vehicles_profile_has_vehicle_signals | Vehicles profile adds vehicle_count + vehicle_type |
| test_load_profile_by_name_default | load_profile_by_name("default") returns default profile |
| test_load_profile_by_name_packages | load_profile_by_name("packages") returns packages profile |
| test_load_profile_by_name_unknown_raises | Unknown name raises ValueError |
| test_load_profile_from_dict_minimal | Inline dict with just categories works |
| test_load_profile_from_dict_with_extra_fields | Inline dict with extra_score_fields works |
| test_load_default_profile | load_default_profile() returns default |
| test_subject_category_people_signals | People category count_signals = (male_count, female_count) |

#### test_detection_summary_consensus.py

| Test | What it verifies |
|------|-----------------|
| test_mode_strategy_single_value | Mode of [1,1,1] = 1 |
| test_mode_strategy_tie_prefers_lower | Mode of [1,1,2,2] = 1 (conservative) |
| test_mode_strategy_clear_winner | Mode of [1,2,2,2] = 2 |
| test_max_strategy_returns_max | Max of [1,2,3] = 3 |
| test_median_strategy_odd | Median of [1,2,3] = 2 |
| test_median_strategy_even_rounds_down | Median of [1,2,3,4] = 2 (floor) |
| test_consensus_with_default_profile | Default profile consensus matches current max behavior |
| test_consensus_with_packages_profile | Packages profile includes package_count consensus |
| test_consensus_empty_scored | Empty scored dict returns all zeros |
| test_augment_instructions_with_consensus | Instructions include "most likely" + "max" language |
| test_augment_instructions_backward_compat | When no consensus, falls back to max-only language |

#### Updated tests in existing files

| File | Test | What it verifies |
|------|------|-----------------|
| test_detection_summary_prompting.py | test_score_prompt_with_packages_profile | Score prompt includes package_count guidance |
| test_detection_summary_prompting.py | test_normalize_with_extra_signals | Normalizer puts package_count in extra_signals |
| test_detection_summary_publish_gate.py | test_publish_with_packages_profile | Publish gate accepts package-only detections |
| test_detection_summary_publish_gate.py | test_publish_backward_compat_no_profile | No profile = same as today |
| test_detection_summary_population.py | test_consensus_mode_population | Mode consensus for population bounds |
| test_detection_summary_selection.py | test_pick_key_with_profile | Profile-aware ranking includes extra signals |
| test_detection_summary_selection.py | test_score_result_extra_signals_default | ScoreResult().extra_signals == {} |
| test_detection_summary_selection.py | test_score_result_backward_compat_positional | Positional construction still works |

---

## Parallelism analysis

| Step | Files touched | Dependencies | Track |
|------|---------------|-------------|-------|
| 1: profiles.py + ScoreResult | profiles.py (NEW), selection.py | none | A |
| 2: prompting layer | prompting/schema_specs.py, score_prompt_builder.py, score_normalizer.py, prompting/__init__.py | Step 1 (uses DetectionProfile, ScoreResult.extra_signals) | A |
| 3: population + publish_gate | population.py, publish_gate.py | Step 1 (uses DetectionProfile) | A |
| 4: image + narrative builders | prompting/image_prompt_builder.py, narrative_prompt_builder.py | Steps 1-3 (uses consensus, profile) | A |
| 5: bundle + manager | bundle.py, manager.py | Steps 1-4 (integrates everything) | A |
| 6: tests | tests/* (new + modified) | Steps 1-5 | A |

**All steps are in Track A** (sequential) because they share files and have cascading dependencies. This is a single Implementation Agent task.

---

## Validation checklist

### Backward compatibility
- [ ] Existing garage/bulkhead configs in apps-prod.yaml work without any `detection_profile` key
- [ ] `ScoreResult(0, 0, 0, 5.0, 3.0, 4.0, "standing", "x", {})` positional construction still works
- [ ] `ScoreResult` has `extra_signals` field defaulting to `{}`
- [ ] `should_publish_bundle` without `profile` param behaves identically to before
- [ ] `compute_population_bounds` without profile param behaves identically to before
- [ ] All existing tests pass without modification (new tests may be added; existing tests must not be broken)

### Profile system
- [ ] `profiles.py` exists at `appdaemon/apps/detection_summary_app/profiles.py`
- [ ] `DetectionProfile` dataclass is frozen
- [ ] `SubjectCategory` dataclass is frozen
- [ ] At least 3 built-in profiles: "default", "packages", "vehicles"
- [ ] `load_profile_by_name("default")` returns the default profile
- [ ] `load_profile_by_name("unknown")` raises ValueError
- [ ] `load_profile_from_dict({...})` constructs a profile from inline YAML data
- [ ] Default profile's categories match current people+animals behavior exactly
- [ ] Default profile's score_fields match DEFAULT_SCORE_FIELDS exactly

### Consensus system
- [ ] `compute_population_consensus` exists in population.py
- [ ] Mode strategy: most frequent count, ties broken by lower value
- [ ] Max strategy: backward-compatible max
- [ ] Consensus dict includes both `consensus_*` and `max_*` keys for each count signal
- [ ] `augment_image_instructions` (or new variant) uses "most likely" + "hard limit" language when consensus is available

### Prompting
- [ ] `schema_from_profile` exists and generates ScoreSchemaSpec from a DetectionProfile
- [ ] Score prompt includes guidance for profile-specific signals (e.g., package_count)
- [ ] Score normalizer populates `ScoreResult.extra_signals` for non-standard fields
- [ ] Image prompt includes consensus-based constraints when available
- [ ] Image prompt includes profile-specific hallucination guardrails

### Publish gate
- [ ] Profile-driven gating: publishes when any required category meets threshold
- [ ] Packages-only detection (no people, no animals) publishes when profile requires packages
- [ ] Falls back to legacy logic when no profile is provided

### Bundle
- [ ] Bundle dict includes extra_signals in candidate data
- [ ] Bundle summary scores include profile-specific signals

### Manager integration
- [ ] Manager loads profile from `self.args.get("detection_profile")`
- [ ] None/missing profile = default profile
- [ ] String profile = load_profile_by_name
- [ ] Dict profile = load_profile_from_dict
- [ ] Profile is passed to score prompt builder, publish gate, population consensus, image prompt builder, bundle builder

### Tests
- [ ] `test_detection_profiles.py` exists with at least 10 tests
- [ ] `test_detection_summary_consensus.py` exists with at least 10 tests
- [ ] All existing test files still pass
- [ ] Full test suite passes: `wsl bash -c "cd /mnt/d/labspace/hass-sandbox && source .venv-wsl/bin/activate && cd appdaemon && python -m pytest tests/ -v --tb=short"`

### Security
- [ ] No credentials in any new/modified file under `appdaemon/apps/`
- [ ] No external HTTP calls added to `appdaemon/apps/`
- [ ] No secrets in event payloads or state attributes

---

## Agent prompts

### Implementation Agent

```text
You are an Implementation Agent. Your task is fully described in the plan file at:

  /mnt/d/labspace/hass-sandbox/.agents/plans/detection-profiles.md

Read the full plan file before doing anything else. It contains architecture context,
detailed implementation instructions, test case tables, and a validation checklist.

Also read these rule files before making any changes:
- .agents/rules/appdaemon-architecture.md
- .agents/rules/appdaemon-coding-guidelines.md
- .agents/rules/security-policy.md

Work through all steps in the plan in order (Steps 1-6). After completing all code changes,
run the full test suite and fix any failures before finishing:

  wsl bash -c "cd /mnt/d/labspace/hass-sandbox && source .venv-wsl/bin/activate && cd appdaemon && python -m pytest tests/ -v --tb=short"

DO NOT run deploy.py or copy any files to X:\. All changes stay in the dev environment only.
```

### Validation Agent

```text
You are a Validation Agent. Review the implementation described in the plan file at:

  /mnt/d/labspace/hass-sandbox/.agents/plans/detection-profiles.md

Read the full plan file -- the "Validation checklist" section lists every requirement to verify.

Also read these rule files:
- .agents/rules/appdaemon-architecture.md
- .agents/rules/appdaemon-coding-guidelines.md
- .agents/rules/security-policy.md

DO NOT modify any files. Your job is to READ and VERIFY only.

Verify each checklist item by reading the relevant source files. Run the full test suite
and include the result in your report:

  wsl bash -c "cd /mnt/d/labspace/hass-sandbox && source .venv-wsl/bin/activate && cd appdaemon && python -m pytest tests/ -v --tb=short"

Output a PASS or FAIL verdict.

If FAIL, list every failing checklist item with:
  - File path and method/line where the issue is
  - What is wrong or missing
  - What the fix should be

Then produce a copy-pasteable prompt for the Implementation Agent in a fenced
```text``` block.
```

---

## Re-prompt template (for Validation Agent to use on FAIL)

```text
You are Implementation Agent for the detection-profiles plan.

Validation Agent has completed a read-only validation pass. The following defects
were found that you must fix.

DEFECT 1

File: <path>

<What is wrong. What the fix should be.>

REQUIRED FIX

1. <First action>
2. <Second action>

Read the plan file at /mnt/d/labspace/hass-sandbox/.agents/plans/detection-profiles.md
and rules before making changes. Do not run deploy.py or copy files to X:\. Run the full
test suite after your changes and confirm it passes:

  wsl bash -c "cd /mnt/d/labspace/hass-sandbox && source .venv-wsl/bin/activate && cd appdaemon && python -m pytest tests/ -v --tb=short"
```

---

## Final planner review

After Validation returns PASS:
1. Re-read all implemented files and compare to this plan
2. Run the full test suite again
3. Code-review for: missed backward compatibility, weak validation, stale config drift, leftover artifacts
4. Fix any remaining issues directly
