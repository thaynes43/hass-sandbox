"""Unit tests for population bounds + image prompt augmentation."""

from __future__ import annotations

import sys
from pathlib import Path


# Add apps to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))

from detection_summary_app.population import augment_image_instructions, compute_population_bounds  # noqa: E402
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


def test_augment_image_instructions_includes_bounds_and_caveat():
    out = augment_image_instructions(
        "BASE",
        {"max_male_count": 2, "max_female_count": 1, "max_animal_count": 4},
    )
    assert "BASE" in out
    assert "up to 2 male" in out
    assert "up to 1 female" in out
    assert "up to 4 animal" in out
    assert "best snapshot" in out.lower()

