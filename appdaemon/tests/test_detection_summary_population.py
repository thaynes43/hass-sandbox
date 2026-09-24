"""Unit tests for population bounds."""

from __future__ import annotations

import sys
from pathlib import Path


# Add apps to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))

from detection_summary_app.population import compute_population_bounds  # noqa: E402
from detection_summary_app.selection import ScoreResult  # noqa: E402


def test_compute_population_bounds_empty():
    assert compute_population_bounds({}) == {
        "max_male_count": 0,
        "max_female_count": 0,
        "max_animal_count": 0,
    }


def test_compute_population_bounds_maxes():
    scored = {
        0: ScoreResult(1, 0, 0, 5, 1, 1, "standing", "x", {}),
        1: ScoreResult(1, 1, 2, 6, 2, 2, "walking", "y", {}),
        2: ScoreResult(0, 0, 3, 0, 0, 0, "none", "", {}),
    }
    assert compute_population_bounds(scored) == {
        "max_male_count": 1,
        "max_female_count": 1,
        "max_animal_count": 3,
    }
