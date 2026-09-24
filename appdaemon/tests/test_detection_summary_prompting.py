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
    FrameNote,
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
from detection_summary_app.population import compute_population_consensus
from detection_summary_app.profiles import PROFILE_DEFAULT, PROFILE_PACKAGES
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


def _score(male: int = 0, female: int = 0, animal: int = 0, **extra: int) -> ScoreResult:
    return ScoreResult(
        male_count=male,
        female_count=female,
        animal_count=animal,
        person_score=5.0,
        face_score=5.0,
        frame_score=5.0,
        pose="standing",
        summary="",
        structured={},
        extra_signals=dict(extra),
    )


def _count_lines(result: ImagePromptResult) -> list[str]:
    block = result.prompt.split("Subjects to draw, each exactly once:\n")[1]
    return block.split("\n\n")[0].splitlines()


def _note_lines(result: ImagePromptResult) -> list[str]:
    return [ln for ln in result.prompt.splitlines() if ln.startswith("- Image ")]


def _build_from_frames(frames: list[ScoreResult], profile=PROFILE_DEFAULT) -> ImagePromptResult:
    """Build with counts computed from per-frame scores, the way the manager does."""
    scored = dict(enumerate(frames))
    return ImagePromptBuilder().build(
        base_instructions="Base",
        population_bounds={},
        consensus_bounds=compute_population_consensus(scored, profile),
        profile=profile,
        style_profile_id="default",
        environment_variant_id="default",
    )


class TestImagePromptBuilder:
    def test_build_without_a_profile_counts_from_the_bounds(self):
        builder = ImagePromptBuilder()
        result = builder.build(
            base_instructions="Draw a cartoon",
            population_bounds={"max_male_count": 1, "max_female_count": 0, "max_animal_count": 1},
        )
        assert isinstance(result, ImagePromptResult)
        out = result.prompt
        assert out.startswith("Draw a cartoon\n")
        assert _count_lines(result) == ["- People: at most 1", "- Animals: at most 1"]
        assert "clearly visible" in out
        # Style/env metadata should be populated
        assert result.style_profile_id is not None
        assert result.environment_variant_id is not None

    def test_the_scene_of_image_1_is_what_gets_drawn(self):
        """Several references are a composition cue to an edit model; Image 1 is the anchor."""
        result = ImagePromptBuilder().build(
            base_instructions="Base",
            population_bounds={},
            frame_notes=[
                FrameNote(summary="A man at the door.", male_count=1, is_primary=True),
                FrameNote(summary="A man leaving.", male_count=1),
            ],
            input_paths_count=2,
        )
        out = result.prompt
        assert (
            "Draw the scene of Image 1 (the primary frame): keep its camera view and composition"
            in out
        )
        assert "The illustration shows that one moment." in out
        assert "Anyone or anything that appears in several images is one individual" in out
        assert "Use Image 2 only to see a subject more clearly" in out

    def test_the_prompt_never_asks_for_a_composite_of_the_frames(self):
        """'A composite of the event' is how the same man got drawn once per frame."""
        result = ImagePromptBuilder().build(
            base_instructions="Base",
            population_bounds={},
            frame_notes=[FrameNote(summary="x", male_count=1, is_primary=True), FrameNote(male_count=1)],
            input_paths_count=2,
        )
        out = result.prompt.lower()
        assert "composite" not in out
        assert "across the provided frames" not in out
        assert "narrative" not in out

    def test_a_later_image_is_never_described_in_its_own_words(self):
        """Each later summary places the same person somewhere else — a second person."""
        result = ImagePromptBuilder().build(
            base_instructions="Base",
            population_bounds={},
            frame_notes=[
                FrameNote(
                    summary="1 man standing at the door, center.",
                    time_offset_s=7.5,
                    male_count=1,
                    is_primary=True,
                ),
                FrameNote(summary="1 man walking in, near left.", time_offset_s=0.0, male_count=1),
                FrameNote(summary="1 man walking away, right side.", time_offset_s=15.0, male_count=1),
            ],
            input_paths_count=3,
        )
        assert "near left" not in result.prompt
        assert "right side" not in result.prompt
        assert _note_lines(result) == [
            "- Image 1 (primary frame) t=7.5s: 1 man standing at the door, center. (m=1, f=0, animals=0)",
            "- Image 2 t=0.0s: the same subjects as Image 1 at another moment; nobody new.",
            "- Image 3 t=15.0s: the same subjects as Image 1 at another moment; nobody new.",
        ]

    def test_a_later_image_names_only_what_it_adds_to_image_1(self):
        result = ImagePromptBuilder().build(
            base_instructions="Base",
            population_bounds={},
            frame_notes=[
                FrameNote(summary="A man at the door.", male_count=1, is_primary=True),
                FrameNote(summary="A man and a dog.", male_count=1, animal_count=1),
                FrameNote(summary="Three people.", male_count=2, female_count=1),
            ],
            input_paths_count=3,
        )
        assert _note_lines(result)[1:] == [
            "- Image 2: the same scene at another moment, with 1 animal more than Image 1: "
            "add only that one; everyone and everything else in it is already in Image 1.",
            "- Image 3: the same scene at another moment, with 2 people more than Image 1: "
            "add only those, each once; everyone and everything else in it is already in Image 1.",
        ]

    def test_a_later_image_is_described_by_its_profile_categories(self):
        """A frame sent for a package names the package, not just people and animals."""
        result = ImagePromptBuilder().build(
            base_instructions="Base",
            population_bounds={},
            frame_notes=[
                FrameNote(
                    summary="A courier at the door.",
                    male_count=1,
                    is_primary=True,
                    category_counts=(("people", 1), ("animals", 0), ("packages", 0)),
                ),
                FrameNote(
                    summary="A package on the step.",
                    category_counts=(("people", 0), ("animals", 0), ("packages", 1)),
                ),
                FrameNote(
                    summary="Two parcels and a dog.",
                    category_counts=(("people", 1), ("animals", 1), ("packages", 2)),
                ),
            ],
            input_paths_count=3,
        )
        assert _note_lines(result)[1:] == [
            "- Image 2: the same scene at another moment, with 1 package more than Image 1: "
            "add only that one; everyone and everything else in it is already in Image 1.",
            "- Image 3: the same scene at another moment, with 1 animal and 2 packages more "
            "than Image 1: add only those, each once; everyone and everything else in it is "
            "already in Image 1.",
        ]

    def test_a_flipped_gender_is_not_a_new_person(self):
        """The scorer reading one person as a man, then a woman, adds nobody."""
        result = ImagePromptBuilder().build(
            base_instructions="Base",
            population_bounds={},
            frame_notes=[
                FrameNote(summary="A man at the door.", male_count=1, is_primary=True),
                FrameNote(summary="A woman at the door.", female_count=1),
            ],
            input_paths_count=2,
        )
        assert _note_lines(result)[1] == (
            "- Image 2: the same subjects as Image 1 at another moment; nobody new."
        )

    def test_notes_are_labelled_by_position_not_filename(self):
        """The provider renames every upload, so a filename names nothing.

        This is the exact rendered block, because the labels are the only
        handle the model has on which reference a note describes.
        """
        builder = ImagePromptBuilder()
        result = builder.build(
            base_instructions="Base",
            population_bounds={},
            frame_notes=[
                FrameNote(
                    summary="A man at the door.",
                    time_offset_s=1.25,
                    male_count=1,
                    is_primary=True,
                ),
                FrameNote(
                    summary="A dog crosses the drive.",
                    time_offset_s=0.5,
                    animal_count=1,
                ),
                FrameNote(summary="", time_offset_s=None, female_count=2),
            ],
            input_paths_count=3,
        )
        block = result.prompt.split("What each image shows:\n")[1]
        rendered = "\n".join(block.splitlines()[:3])
        assert rendered == (
            "- Image 1 (primary frame) t=1.2s: A man at the door. (m=1, f=0, animals=0)\n"
            "- Image 2 t=0.5s: the same scene at another moment, with 1 animal more than Image 1: "
            "add only that one; everyone and everything else in it is already in Image 1.\n"
            "- Image 3: the same scene at another moment, with 1 person more than Image 1: "
            "add only that one; everyone and everything else in it is already in Image 1."
        )
        # No filename the model never receives.
        assert "frame_0" not in result.prompt
        assert ".jpg" not in result.prompt

    def test_note_order_is_the_order_the_frames_are_sent(self):
        """Notes are positional, so the builder must not re-sort them.

        The common case the labelling exists for: the best (primary) frame is
        not the earliest one, so chronological order and send order differ.
        """
        builder = ImagePromptBuilder()
        result = builder.build(
            base_instructions="Base",
            population_bounds={},
            frame_notes=[
                FrameNote(summary="best, captured late", time_offset_s=4.0, is_primary=True),
                FrameNote(summary="earliest frame", time_offset_s=0.0),
            ],
            input_paths_count=2,
        )
        assert _note_lines(result) == [
            "- Image 1 (primary frame) t=4.0s: best, captured late (m=0, f=0, animals=0)",
            "- Image 2 t=0.0s: the same subjects as Image 1 at another moment; nobody new.",
        ]

    def test_no_note_claims_to_be_primary_when_the_best_frame_was_not_sent(self):
        """The qualifier is carried, not inferred from being first.

        best.jpg can legitimately be missing (never written, or the wait for
        it timed out), in which case the best frame is not among the uploads
        and calling the first one primary would be untrue.
        """
        builder = ImagePromptBuilder()
        result = builder.build(
            base_instructions="Base",
            population_bounds={},
            frame_notes=[
                FrameNote(summary="best-animals frame", time_offset_s=1.0, animal_count=2),
                FrameNote(summary="best-females frame", time_offset_s=0.0, female_count=1),
            ],
            input_paths_count=2,
        )
        assert "primary frame" not in result.prompt
        assert "Draw the scene of Image 1: keep its camera view" in result.prompt
        assert _note_lines(result) == [
            "- Image 1 t=1.0s: best-animals frame (m=0, f=0, animals=2)",
            "- Image 2 t=0.0s: the same scene at another moment, with 1 person more than Image 1: "
            "add only that one; everyone and everything else in it is already in Image 1.",
        ]

    def test_count_matches_the_number_of_notes(self):
        """The count the model is told and the notes it gets describe one set."""
        builder = ImagePromptBuilder()
        notes = [FrameNote(summary=f"frame {i}") for i in range(3)]
        result = builder.build(
            base_instructions="Base",
            population_bounds={},
            frame_notes=notes,
            input_paths_count=len(notes),
        )
        assert "You are provided 3 images" in result.prompt
        assert "Use Images 2-3 only" in result.prompt
        assert len(_note_lines(result)) == 3

    def test_one_image_is_described_as_one(self):
        result = ImagePromptBuilder().build(
            base_instructions="Base",
            population_bounds={},
            frame_notes=[FrameNote(summary="A man at the door.", male_count=1, is_primary=True)],
            input_paths_count=1,
        )
        out = result.prompt
        assert "You are provided 1 image:" in out
        assert "Draw the scene of the reference image" in out
        assert "Image 2" not in out
        assert "several images" not in out

    def test_counts_are_exact_when_the_frames_agree(self):
        result = _build_from_frames([_score(male=1), _score(male=1), _score(male=1)])
        assert _count_lines(result) == ["- People: exactly 1", "- Animals: none"]

    def test_a_person_read_as_both_genders_is_still_one_person(self):
        """No frame held two people, so the total is one whatever the per-gender maxima say."""
        result = _build_from_frames([_score(male=1), _score(female=1), _score(male=1)])
        assert _count_lines(result)[0] == "- People: exactly 1"

        result = _build_from_frames([_score(male=1), _score(female=1)])
        assert _count_lines(result)[0] == "- People: exactly 1"

    def test_a_group_is_counted_as_one_total(self):
        """Gender is left to Image 1 and its note; the count line carries no breakdown."""
        result = _build_from_frames([_score(male=1, female=1), _score(male=1, female=1)])
        assert _count_lines(result)[0] == "- People: exactly 2"

    def test_counts_are_a_ceiling_when_the_frames_disagree(self):
        result = _build_from_frames(
            [_score(male=1), _score(male=2), _score(male=1, animal=1)]
        )
        assert _count_lines(result) == ["- People: at most 2", "- Animals: at most 1"]

    def test_extra_profile_categories_are_counted(self):
        result = _build_from_frames(
            [_score(package_count=1), _score(package_count=1)], profile=PROFILE_PACKAGES
        )
        assert _count_lines(result) == [
            "- People: none",
            "- Animals: none",
            "- Packages: exactly 1",
        ]
        assert "keep the people, animals and packages where it shows them" in result.prompt

    def test_consensus_without_totals_falls_back_to_the_signals(self):
        result = ImagePromptBuilder().build(
            base_instructions="Base",
            population_bounds={},
            consensus_bounds={
                "consensus_male_count": 1,
                "max_male_count": 1,
                "consensus_female_count": 0,
                "max_female_count": 0,
                "consensus_animal_count": 0,
                "max_animal_count": 2,
            },
            profile=PROFILE_DEFAULT,
        )
        assert _count_lines(result) == ["- People: exactly 1", "- Animals: at most 2"]

    def test_style_and_setting_directives_keep_each_subject_once(self):
        result = ImagePromptBuilder().build(
            base_instructions="Base",
            population_bounds={},
            style_profile_id="pop-art",
            environment_variant_id="underwater",
        )
        out = result.prompt
        assert (
            "Rendering style directive (apply to visual appearance only — "
            "keep exactly the subjects above, each drawn once, in one scene):"
        ) in out
        assert (
            "Environment/setting directive (modify background and setting only — "
            "the subjects above must stay clearly present, each drawn once):"
        ) in out

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
