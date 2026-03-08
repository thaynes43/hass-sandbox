"""Unit tests for multi-frame consensus in population.py."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))

from detection_summary_app.population import (
    _max_value,
    _median_value,
    _mode_value,
    augment_image_instructions_with_consensus,
    compute_population_consensus,
)
from detection_summary_app.profiles import PROFILE_DEFAULT, PROFILE_PACKAGES, DetectionProfile, SubjectCategory
from detection_summary_app.prompting.schema_specs import DEFAULT_SCORE_FIELDS
from detection_summary_app.selection import ScoreResult


class TestModeStrategy:
    def test_mode_strategy_single_value(self):
        """Mode of [1,1,1] = 1."""
        assert _mode_value([1, 1, 1]) == 1

    def test_mode_strategy_tie_prefers_lower(self):
        """Mode of [1,1,2,2] = 1 (conservative)."""
        assert _mode_value([1, 1, 2, 2]) == 1

    def test_mode_strategy_clear_winner(self):
        """Mode of [1,2,2,2] = 2."""
        assert _mode_value([1, 2, 2, 2]) == 2

    def test_mode_strategy_empty(self):
        assert _mode_value([]) == 0


class TestMaxStrategy:
    def test_max_strategy_returns_max(self):
        """Max of [1,2,3] = 3."""
        assert _max_value([1, 2, 3]) == 3

    def test_max_strategy_empty(self):
        assert _max_value([]) == 0


class TestMedianStrategy:
    def test_median_strategy_odd(self):
        """Median of [1,2,3] = 2."""
        assert _median_value([1, 2, 3]) == 2

    def test_median_strategy_even_rounds_down(self):
        """Median of [1,2,3,4] = 2 (floor)."""
        assert _median_value([1, 2, 3, 4]) == 2

    def test_median_strategy_empty(self):
        assert _median_value([]) == 0


class TestConsensusComputation:
    def test_consensus_with_default_profile(self):
        """Default profile consensus includes male_count, female_count, animal_count."""
        scored = {
            0: ScoreResult(1, 0, 0, 5, 1, 1, "standing", "x", {}),
            1: ScoreResult(1, 1, 2, 6, 2, 2, "walking", "y", {}),
            2: ScoreResult(1, 0, 0, 4, 1, 1, "standing", "z", {}),
        }
        result = compute_population_consensus(scored, PROFILE_DEFAULT)
        # mode of male_count: [1,1,1] = 1
        assert result["consensus_male_count"] == 1
        assert result["max_male_count"] == 1
        # mode of female_count: [0,1,0] = 0
        assert result["consensus_female_count"] == 0
        assert result["max_female_count"] == 1
        # mode of animal_count: [0,2,0] = 0
        assert result["consensus_animal_count"] == 0
        assert result["max_animal_count"] == 2

    def test_consensus_with_packages_profile(self):
        """Packages profile includes package_count consensus."""
        scored = {
            0: ScoreResult(0, 0, 0, 1, 0, 1, "", "", {}, extra_signals={"package_count": 2}),
            1: ScoreResult(0, 0, 0, 1, 0, 1, "", "", {}, extra_signals={"package_count": 2}),
            2: ScoreResult(0, 0, 0, 1, 0, 1, "", "", {}, extra_signals={"package_count": 1}),
        }
        result = compute_population_consensus(scored, PROFILE_PACKAGES)
        assert "consensus_package_count" in result
        assert "max_package_count" in result
        # mode of [2,2,1] = 2
        assert result["consensus_package_count"] == 2
        assert result["max_package_count"] == 2

    def test_consensus_empty_scored(self):
        """Empty scored dict returns all zeros."""
        result = compute_population_consensus({}, PROFILE_DEFAULT)
        assert result["consensus_male_count"] == 0
        assert result["max_male_count"] == 0
        assert result["consensus_female_count"] == 0
        assert result["consensus_animal_count"] == 0

    def test_consensus_max_strategy(self):
        """Profile with max strategy returns max values."""
        max_profile = DetectionProfile(
            name="test_max",
            categories=(
                SubjectCategory(
                    name="people",
                    count_signals=("male_count",),
                ),
            ),
            score_fields=DEFAULT_SCORE_FIELDS,
            consensus_strategy="max",
        )
        scored = {
            0: ScoreResult(1, 0, 0, 5, 1, 1, "", "", {}),
            1: ScoreResult(3, 0, 0, 5, 1, 1, "", "", {}),
            2: ScoreResult(2, 0, 0, 5, 1, 1, "", "", {}),
        }
        result = compute_population_consensus(scored, max_profile)
        assert result["consensus_male_count"] == 3
        assert result["max_male_count"] == 3


class TestAugmentInstructions:
    def test_augment_instructions_with_consensus(self):
        """Instructions include 'most likely' + 'max' language."""
        consensus = {
            "consensus_male_count": 1,
            "max_male_count": 2,
            "consensus_female_count": 0,
            "max_female_count": 1,
            "consensus_animal_count": 1,
            "max_animal_count": 1,
        }
        result = augment_image_instructions_with_consensus("BASE", consensus, PROFILE_DEFAULT)
        assert "BASE" in result
        assert "most likely" in result.lower()
        assert "max:" in result.lower() or "max: " in result
        assert "Do NOT include more than" in result

    def test_augment_instructions_backward_compat(self):
        """When no consensus data, values are zero."""
        consensus = {
            "consensus_male_count": 0,
            "max_male_count": 0,
            "consensus_female_count": 0,
            "max_female_count": 0,
            "consensus_animal_count": 0,
            "max_animal_count": 0,
        }
        result = augment_image_instructions_with_consensus("BASE", consensus, PROFILE_DEFAULT)
        assert "BASE" in result

    def test_augment_instructions_packages_guardrails(self):
        """Instructions for packages profile include hallucination guardrails."""
        consensus = {
            "consensus_male_count": 1,
            "max_male_count": 1,
            "consensus_female_count": 0,
            "max_female_count": 0,
            "consensus_animal_count": 0,
            "max_animal_count": 0,
            "consensus_package_count": 1,
            "max_package_count": 2,
        }
        result = augment_image_instructions_with_consensus("BASE", consensus, PROFILE_PACKAGES)
        assert "Packages" in result
        assert "clearly visible" in result.lower()
