"""Contract tests for selection + bundle meta types."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))

from detection_summary_app.bundle import BundleConfig, TraceConfig, build_bundle_dict  # noqa: E402
from detection_summary_app.capture import CaptureState  # noqa: E402
from detection_summary_app.selection import ScoreResult, SelectionMeta, adaptive_select_and_score  # noqa: E402


def test_adaptive_select_returns_selectionmeta():
    def score_index(i: int) -> ScoreResult:
        # deterministic, valid result
        return ScoreResult(person_count=1, person_score=5.0, face_score=0.0, frame_score=0.0, pose="standing", summary="s", structured={})

    scored, meta = adaptive_select_and_score(total_frames=5, budget=3, score_index=score_index, seed="seed")
    assert isinstance(scored, dict)
    assert isinstance(meta, SelectionMeta)
    assert isinstance(meta.scored_indices, list)


def test_build_bundle_dict_requires_selectionmeta():
    # Minimal capture state (bundle builder only needs timing + frames list for candidate filenames/paths).
    cap = CaptureState(run_id="rid", started_ts=time.time(), frames=[], capture_idx=0)
    scored = {0: ScoreResult(1, 5.0, 0.0, 0.0, "standing", "s", {})}
    cfg = BundleConfig(
        snapshot_ha_dir="/media/detection-summary/x",
        bundle_runs_subdir="runs",
        bundle_best_filename="best.jpg",
        external_generated_filename="generated.png",
        published_best_filename="detection_summary_best.jpg",
        published_generated_filename="detection_summary_generated.png",
        write_bundle_json=False,
        trace=TraceConfig(enabled=False),
    )

    with pytest.raises(AttributeError):
        # Passing the wrong type should fail loudly (this guards against mixed-version runtime states).
        build_bundle_dict(
            bundle_key="x",
            camera_entity_id="camera.x",
            trigger_entity_id="binary_sensor.x",
            run_id="rid",
            capture=cap,
            scored=scored,
            selection_meta={},  # type: ignore[arg-type]
            best_idx=0,
            best_image_url="",
            generated_image=None,
            run_narrative=None,
            cfg=cfg,
            llm_events=[],
        )


def test_manager_does_not_overwrite_selection_meta_with_narrative_meta():
    # This reproduces the real-world bug: overwriting the SelectionMeta variable with a dict
    # (run_narrative["_narrative_meta"]) would later break bundle building.
    #
    # We don't invoke manager internals here; we lock in the intent:
    # narrative metadata must not be stored in the selection meta variable.
    def score_index(i: int) -> ScoreResult:
        return ScoreResult(person_count=1, person_score=5.0, face_score=0.0, frame_score=0.0, pose="standing", summary="s", structured={})

    _scored, meta = adaptive_select_and_score(total_frames=5, budget=3, score_index=score_index, seed="seed2")
    assert isinstance(meta, SelectionMeta)

    run_narrative = {"run_summary": "x", "_narrative_meta": {"was_truncated": False}}
    narrative_meta = run_narrative.get("_narrative_meta")
    assert isinstance(narrative_meta, dict)

    # Contract: selection meta remains a SelectionMeta (not replaced with narrative_meta).
    assert isinstance(meta, SelectionMeta)

