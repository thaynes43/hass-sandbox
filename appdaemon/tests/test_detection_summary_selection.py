from __future__ import annotations

from detection_summary_app.selection import ScoreResult, adaptive_select_and_score


def test_adaptive_selection_prefers_face_and_stops_after_no_people_cutoff():
    # Construct a synthetic scoring landscape:
    # - People appear in frames 2..6
    # - Best face frame is 4
    # - After frame 7, no people (should become cutoff)
    def score(i: int) -> ScoreResult:
        if 2 <= i <= 6:
            person = 9
        else:
            person = 0
        face = 10 if i == 4 else (5 if i == 5 else 0)
        frame = 8 if i in (4, 5) else (3 if person else 0)
        pose = "standing" if i in (4, 5) else ("walking" if person else "none")
        summary = f"idx={i}"
        return ScoreResult(person, face, frame, pose, summary, {"person_score": person, "face_score": face, "frame_score": frame})

    scored, meta = adaptive_select_and_score(
        total_frames=12,
        budget=9,
        score_index=score,
        seed="test-seed",
        no_people_threshold=1.0,
        lookahead_after_no_people=2,
    )

    assert len(scored) <= 9
    assert meta.best_idx == 4
    # Cutoff should not extend into the far tail where there are no people.
    assert meta.cutoff_idx_inclusive <= 7

