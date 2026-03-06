"""Unit tests for detection_summary prompting/schema abstractions."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Add appdaemon root and apps to path
_repo = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo))
sys.path.insert(0, str(_repo / "apps"))

from detection_summary_app.prompting import (
    ScorePromptBuilder,
    ImagePromptBuilder,
    NarrativePromptBuilder,
    normalize_score_data,
    default_score_schema,
    ScoreSchemaSpec,
    ScoreFieldSpec,
    STYLE_PROFILES,
    ENVIRONMENT_VARIANTS,
)
from detection_summary_app.prompting.style_variants import get_environment_variant, get_style_profile
from detection_summary_app.selection import ScoreResult


class TestScoreSchemaSpec:
    def test_expected_keys_matches_default_schema(self):
        schema = default_score_schema()
        keys = schema.expected_keys()
        assert "male_count" in keys
        assert "female_count" in keys
        assert "animal_count" in keys
        assert "person_score" in keys
        assert "face_score" in keys
        assert "frame_score" in keys
        assert "pose" in keys
        assert "summary" in keys
        assert len(keys) == 8

    def test_scoring_guidance_includes_animals(self):
        schema = default_score_schema()
        block = schema.scoring_guidance_block()
        assert "animals" in block.lower()
        assert "frame_score" in block
        assert "face_score" in block
        assert "pose" in block
        assert "summary" in block

    def test_required_fields_block_includes_all_fields(self):
        schema = default_score_schema()
        block = schema.required_fields_block()
        assert "male_count" in block
        assert "female_count" in block
        assert "animal_count" in block


class TestScorePromptBuilder:
    def test_build_includes_app_instructions_and_schema(self):
        builder = ScorePromptBuilder()
        out = builder.build("Analyze this security camera frame.")
        assert "Analyze this security camera frame" in out
        assert "male_count" in out
        assert "female_count" in out
        assert "animal_count" in out
        assert "Scoring guidance" in out
        assert "animals" in out.lower()

    def test_build_empty_app_instructions_still_has_schema(self):
        builder = ScorePromptBuilder()
        out = builder.build("")
        assert "Additional required fields" in out
        assert "male_count" in out


class TestScoreNormalizer:
    def test_normalize_full_data(self):
        data = {
            "male_count": 1,
            "female_count": 2,
            "animal_count": 1,
            "person_score": 7.5,
            "face_score": 6,
            "frame_score": 8,
            "pose": "standing",
            "summary": "Two people and a dog.",
        }
        res = normalize_score_data(data)
        assert isinstance(res, ScoreResult)
        assert res.male_count == 1
        assert res.female_count == 2
        assert res.animal_count == 1
        assert res.person_score == 7.5
        assert res.face_score == 6.0
        assert res.frame_score == 8.0
        assert res.pose == "standing"
        assert res.summary == "Two people and a dog."

    def test_normalize_uses_person_score_as_frame_score_fallback(self):
        data = {
            "male_count": 0,
            "female_count": 0,
            "animal_count": 0,
            "person_score": 5,
            "face_score": 0,
            # frame_score missing
            "pose": "",
            "summary": "",
        }
        res = normalize_score_data(data)
        assert res.frame_score == 5.0

    def test_normalize_alt_key_person_score(self):
        data = {"score": 3, "male_count": 0, "female_count": 0, "animal_count": 0}
        res = normalize_score_data(data)
        assert res.person_score == 3.0

    def test_normalize_missing_fields_default_to_zero_or_empty(self):
        data = {}
        res = normalize_score_data(data)
        assert res.male_count == 0
        assert res.female_count == 0
        assert res.animal_count == 0
        assert res.person_score == 0.0
        assert res.face_score == 0.0
        assert res.frame_score == 0.0
        assert res.pose == ""
        assert res.summary == ""

    def test_normalize_reduced_schema_animals_only(self):
        """Schema-driven normalization when field set is reduced (animals-only)."""
        animals_only = ScoreSchemaSpec(
            fields=(
                ScoreFieldSpec("animal_count", type_hint="int", default=0),
                ScoreFieldSpec("frame_score", type_hint="float", default=0.0),
                ScoreFieldSpec("summary", type_hint="str", default=""),
            )
        )
        data = {"animal_count": 2, "frame_score": 6, "summary": "Dog in yard"}
        res = normalize_score_data(data, schema=animals_only)
        assert res.animal_count == 2
        assert res.frame_score == 6.0
        assert res.summary == "Dog in yard"
        # Default schema fields not in reduced schema get defaults from ScoreResult constructor
        # Actually - the normalizer builds ScoreResult with all fields. The reduced schema
        # only has 3 fields. We'd need to change the normalizer to support partial schemas.
        # For now, the default schema has all 8 fields. A reduced schema would need to
        # map to ScoreResult - we could have default 0 for missing schema fields.
        # Let me check - normalize_score_data iterates over schema.fields and extracts.
        # For animals_only, we only have animal_count, frame_score, summary. The ScoreResult
        # requires male_count, female_count, animal_count, person_score, face_score,
        # frame_score, pose, summary, structured. So we need defaults for fields not in schema.
        assert res.male_count == 0
        assert res.female_count == 0
        assert res.person_score == 0.0
        assert res.face_score == 0.0
        assert res.pose == ""


class TestImagePromptBuilder:
    def test_build_includes_base_and_constraints(self):
        builder = ImagePromptBuilder()
        out = builder.build(
            base_instructions="Draw a cartoon",
            population_bounds={"max_male_count": 1, "max_female_count": 0, "max_animal_count": 1},
        )
        assert "Draw a cartoon" in out
        assert "Reference frames" in out
        assert "Critical constraints" in out
        assert "phantom" in out.lower()
        assert "up to 1 male" in out
        assert "up to 1 animal" in out

    def test_build_includes_narrative_and_notes(self):
        builder = ImagePromptBuilder()
        out = builder.build(
            base_instructions="Base",
            population_bounds={},
            narrative_text="Someone arrived.",
            frame_notes=["- frame_000.jpg: Person at door (m=1, f=0, animals=0)"],
        )
        assert "Narrative context" in out
        assert "Someone arrived" in out
        assert "Frame notes" in out
        assert "frame_000.jpg" in out

    def test_build_includes_bundle_augmentation(self):
        builder = ImagePromptBuilder()
        out = builder.build(
            base_instructions="Base",
            population_bounds={},
            bundle_augmentation="Make it like a cartoon.",
        )
        assert "Make it like a cartoon" in out


class TestNarrativePromptBuilder:
    def test_build_default_includes_max_chars(self):
        builder = NarrativePromptBuilder()
        out = builder.build(max_chars=180)
        assert "180" in out
        assert "run_summary" in out
        assert "people_min" in out


class TestStyleVariants:
    def test_style_profiles_exist(self):
        assert "cartoon" in STYLE_PROFILES
        assert "default" in STYLE_PROFILES

    def test_environment_variants_exist(self):
        assert "default" in ENVIRONMENT_VARIANTS
        assert "underwater" in ENVIRONMENT_VARIANTS

    def test_get_style_profile_returns_none_for_unknown(self):
        assert get_style_profile("nonexistent") is None
        assert get_style_profile("") is None
        assert get_style_profile(None) is None

    def test_get_style_profile_returns_profile(self):
        p = get_style_profile("cartoon")
        assert p is not None
        assert "cartoon" in p.prompt_suffix.lower()

    def test_get_environment_variant_returns_none_for_unknown(self):
        assert get_environment_variant("nonexistent") is None

    def test_image_prompt_builder_applies_style_profile(self):
        builder = ImagePromptBuilder()
        out = builder.build(
            base_instructions="Base",
            population_bounds={},
            style_profile_id="cartoon",
        )
        assert "cartoon" in out.lower()
