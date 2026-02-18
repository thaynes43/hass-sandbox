"""Unit tests for the people-or-animals publish gate."""

from __future__ import annotations

import sys
from pathlib import Path


# Add apps to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))

from detection_summary_app.publish_gate import should_publish_bundle  # noqa: E402
from detection_summary_app.selection import ScoreResult  # noqa: E402


def test_publish_when_people_score_meets_threshold():
    scored = {0: ScoreResult(0, 0, 0, 5.0, 0.0, 0.0, "standing", "x", {})}
    assert (
        should_publish_bundle(scored=scored, best_person_score=5.0, best_min_person_score=2.0)
        is True
    )


def test_publish_when_any_animals_detected_even_if_people_low():
    scored = {
        0: ScoreResult(0, 0, 2, 0.5, 0.0, 3.0, "none", "cat", {}),
        1: ScoreResult(0, 0, 0, 0.2, 0.0, 1.0, "none", "", {}),
    }
    assert (
        should_publish_bundle(scored=scored, best_person_score=0.5, best_min_person_score=2.0, best_min_animal_count=1)
        is True
    )


def test_skip_when_no_people_and_no_animals():
    scored = {0: ScoreResult(0, 0, 0, 0.1, 0.0, 0.0, "none", "", {})}
    assert (
        should_publish_bundle(scored=scored, best_person_score=0.1, best_min_person_score=2.0)
        is False
    )

