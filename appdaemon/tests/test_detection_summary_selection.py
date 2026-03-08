"""Unit tests for detection_summary_app.selection ranking behavior."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))

from detection_summary_app.selection import ScoreResult, _pick_key, adaptive_select_and_score  # noqa: E402
from detection_summary_app.profiles import PROFILE_DEFAULT, PROFILE_PACKAGES  # noqa: E402


def test_selection_prefers_higher_person_score_when_other_signals_equal():
    # Two frames, same other signals, different person_score
    def score_index(i: int) -> ScoreResult:
        if i == 0:
            return ScoreResult(0, 0, 0, 3.0, 5.0, 5.0, "standing", "s", {})
        return ScoreResult(0, 0, 0, 7.0, 5.0, 5.0, "standing", "s", {})

    scored, meta = adaptive_select_and_score(total_frames=2, budget=2, score_index=score_index, seed="x")
    assert meta.best_idx == 1


def test_score_result_extra_signals_default():
    """ScoreResult().extra_signals defaults to {}."""
    r = ScoreResult(0, 0, 0, 0, 0, 0, "none", "", {})
    assert r.extra_signals == {}


def test_score_result_backward_compat_positional():
    """Positional construction still works (9 positional args)."""
    r = ScoreResult(1, 2, 3, 4.0, 5.0, 6.0, "standing", "hello", {"raw": True})
    assert r.male_count == 1
    assert r.female_count == 2
    assert r.animal_count == 3
    assert r.person_score == 4.0
    assert r.face_score == 5.0
    assert r.frame_score == 6.0
    assert r.pose == "standing"
    assert r.summary == "hello"
    assert r.structured == {"raw": True}
    assert r.extra_signals == {}


def test_pick_key_with_profile():
    """Profile-aware ranking includes extra signals."""
    r1 = ScoreResult(0, 0, 0, 0, 0, 1, "", "", {}, extra_signals={"package_count": 2})
    r2 = ScoreResult(0, 0, 0, 0, 0, 1, "", "", {}, extra_signals={"package_count": 0})
    # With packages profile, r1 has subjects (package_count > 0), r2 does not
    key1 = _pick_key(r1, profile=PROFILE_PACKAGES)
    key2 = _pick_key(r2, profile=PROFILE_PACKAGES)
    assert key1 > key2


def test_pick_key_without_profile_backward_compat():
    """Without profile, _pick_key uses legacy behavior."""
    r = ScoreResult(0, 0, 1, 5.0, 3.0, 4.0, "standing", "x", {})
    key = _pick_key(r)
    assert key[0] == 1  # has_subject (animal_count > 0)

