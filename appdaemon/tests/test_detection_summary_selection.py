"""Unit tests for detection_summary_app.selection ranking behavior."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))

from detection_summary_app.selection import ScoreResult, adaptive_select_and_score  # noqa: E402


def test_selection_prefers_higher_person_score_when_other_signals_equal():
    # Two frames, same other signals, different person_score
    def score_index(i: int) -> ScoreResult:
        if i == 0:
            return ScoreResult(0, 0, 0, 3.0, 5.0, 5.0, "standing", "s", {})
        return ScoreResult(0, 0, 0, 7.0, 5.0, 5.0, "standing", "s", {})

    scored, meta = adaptive_select_and_score(total_frames=2, budget=2, score_index=score_index, seed="x")
    assert meta.best_idx == 1

