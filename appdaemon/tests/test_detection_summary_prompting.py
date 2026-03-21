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
    ImagePromptResult,
    normalize_score_data,
    default_score_schema,
    ScoreSchemaSpec,
    ScoreFieldSpec,
    STYLE_PROFILES,
    ENVIRONMENT_VARIANTS,
)
from detection_summary_app.prompting.style_variants import (
    get_environment_variant,
    get_style_profile,
    random_environment_variant,
    random_style_profile,
)
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
        result = builder.build(
            base_instructions="Draw a cartoon",
            population_bounds={"max_male_count": 1, "max_female_count": 0, "max_animal_count": 1},
        )
        assert isinstance(result, ImagePromptResult)
        out = result.prompt
        assert "Draw a cartoon" in out
        assert "Reference frames" in out
        assert "Critical constraints" in out
        assert "phantom" in out.lower()
        assert "up to 1 male" in out
        assert "up to 1 animal" in out
        # Style/env metadata should be populated
        assert result.style_profile_id is not None
        assert result.environment_variant_id is not None

    def test_build_includes_narrative_and_notes(self):
        builder = ImagePromptBuilder()
        result = builder.build(
            base_instructions="Base",
            population_bounds={},
            narrative_text="Someone arrived.",
            frame_notes=["- frame_000.jpg: Person at door (m=1, f=0, animals=0)"],
        )
        out = result.prompt
        assert "Narrative context" in out
        assert "Someone arrived" in out
        assert "Frame notes" in out
        assert "frame_000.jpg" in out

    def test_build_includes_bundle_augmentation(self):
        builder = ImagePromptBuilder()
        result = builder.build(
            base_instructions="Base",
            population_bounds={},
            bundle_augmentation="Make it like a cartoon.",
        )
        assert "Make it like a cartoon" in result.prompt


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
        # Should have many profiles for variety
        assert len(STYLE_PROFILES) >= 20

    def test_environment_variants_exist(self):
        assert "default" in ENVIRONMENT_VARIANTS
        assert "underwater" in ENVIRONMENT_VARIANTS
        # Should have many variants for variety
        assert len(ENVIRONMENT_VARIANTS) >= 15

    def test_all_style_profiles_have_required_fields(self):
        for pid, profile in STYLE_PROFILES.items():
            assert profile.id == pid
            assert isinstance(profile.prompt_suffix, str)
            assert isinstance(profile.description, str)
            assert profile.description  # non-empty description

    def test_all_environment_variants_have_required_fields(self):
        for vid, variant in ENVIRONMENT_VARIANTS.items():
            assert variant.id == vid
            assert isinstance(variant.prompt_suffix, str)
            assert isinstance(variant.description, str)
            assert variant.description  # non-empty description

    def test_default_style_has_empty_suffix(self):
        p = get_style_profile("default")
        assert p is not None
        assert p.prompt_suffix == ""

    def test_default_environment_has_empty_suffix(self):
        v = get_environment_variant("default")
        assert v is not None
        assert v.prompt_suffix == ""

    def test_get_style_profile_returns_none_for_unknown(self):
        assert get_style_profile("nonexistent") is None
        assert get_style_profile("") is None
        assert get_style_profile(None) is None

    def test_get_style_profile_returns_profile(self):
        p = get_style_profile("cartoon")
        assert p is not None
        assert p.prompt_suffix  # non-empty

    def test_get_environment_variant_returns_none_for_unknown(self):
        assert get_environment_variant("nonexistent") is None

    def test_random_style_profile_returns_valid(self):
        for _ in range(20):
            p = random_style_profile()
            assert p.id in STYLE_PROFILES

    def test_random_environment_variant_returns_valid(self):
        for _ in range(20):
            v = random_environment_variant()
            assert v.id in ENVIRONMENT_VARIANTS

    def test_image_prompt_builder_applies_style_profile(self):
        builder = ImagePromptBuilder()
        result = builder.build(
            base_instructions="Base",
            population_bounds={},
            style_profile_id="cartoon",
        )
        assert "cartoon" in result.prompt.lower()
        assert result.style_profile_id == "cartoon"

    def test_image_prompt_builder_random_selection_when_no_id(self):
        """When no style/environment IDs are passed, builder randomly selects."""
        builder = ImagePromptBuilder()
        # Run multiple times — at least one should get a non-default style or variant
        results = set()
        for _ in range(50):
            result = builder.build(
                base_instructions="Base",
                population_bounds={},
            )
            out = result.prompt
            # Check if any style suffix appears (non-default profiles have non-empty suffixes)
            has_style = any(
                p.prompt_suffix and p.prompt_suffix in out
                for p in STYLE_PROFILES.values()
                if p.id != "default"
            )
            has_variant = any(
                v.prompt_suffix and v.prompt_suffix in out
                for v in ENVIRONMENT_VARIANTS.values()
                if v.id != "default"
            )
            if has_style:
                results.add("style")
            if has_variant:
                results.add("variant")
        # With 25 styles and 21 variants, 50 runs should hit at least one non-default
        assert "style" in results, "Random style selection never picked a non-default style in 50 runs"
        assert "variant" in results, "Random variant selection never picked a non-default variant in 50 runs"
