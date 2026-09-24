"""Unit tests for multi-frame consensus in population.py."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))

from detection_summary_app.population import (
    _max_value,
    _median_value,
    _mode_value,
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


class TestCategoryTotals:
    """Per-frame category totals: what the image prompt counts people with."""

    @staticmethod
    def _sr(male: int = 0, female: int = 0, animal: int = 0) -> ScoreResult:
        return ScoreResult(
            male_count=male, female_count=female, animal_count=animal,
            person_score=5.0, face_score=5.0, frame_score=5.0,
            pose="standing", summary="", structured={},
        )

    def test_one_person_read_as_both_genders_totals_one(self):
        """Per-signal maxima say 1 man AND 1 woman; no frame ever held two people."""
        scored = {0: self._sr(male=1), 1: self._sr(female=1), 2: self._sr(male=1)}
        result = compute_population_consensus(scored, PROFILE_DEFAULT)
        assert result["max_male_count"] == 1
        assert result["max_female_count"] == 1
        assert result["consensus_people_total"] == 1
        assert result["max_people_total"] == 1

    def test_totals_follow_the_profile_strategy(self):
        scored = {0: self._sr(male=1, female=1), 1: self._sr(male=1), 2: self._sr(male=1, animal=2)}
        result = compute_population_consensus(scored, PROFILE_DEFAULT)
        # people per frame: [2, 1, 1] -> mode 1, max 2
        assert result["consensus_people_total"] == 1
        assert result["max_people_total"] == 2
        # animals per frame: [0, 0, 2] -> mode 0, max 2
        assert result["consensus_animals_total"] == 0
        assert result["max_animals_total"] == 2

    def test_every_category_with_signals_gets_a_total(self):
        scored = {0: ScoreResult(
            male_count=0, female_count=0, animal_count=0, person_score=0.0, face_score=0.0,
            frame_score=5.0, pose="", summary="", structured={}, extra_signals={"package_count": 3},
        )}
        result = compute_population_consensus(scored, PROFILE_PACKAGES)
        assert result["consensus_packages_total"] == 3
        assert result["max_packages_total"] == 3
        assert result["max_people_total"] == 0

    def test_a_category_without_signals_has_no_total(self):
        profile = DetectionProfile(
            name="t",
            categories=(SubjectCategory(name="context", count_signals=()),),
            score_fields=DEFAULT_SCORE_FIELDS,
        )
        result = compute_population_consensus({0: self._sr(male=1)}, profile)
        assert "consensus_context_total" not in result
        assert "max_context_total" not in result

    def test_no_scored_frames_totals_zero(self):
        result = compute_population_consensus({}, PROFILE_DEFAULT)
        assert result["consensus_people_total"] == 0
        assert result["max_people_total"] == 0
