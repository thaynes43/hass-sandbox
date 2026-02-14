"""Unit tests for detection_summary_store."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))

from detection_summary_store import DetectionSummaryStore, StoreConfig


def _bundle(run_id: str, ts: float, score: float) -> dict:
    return {
        "run_id": run_id,
        "created_at_epoch": ts,
        "best": {"score": score, "summary": f"s{score}", "image_web_path": f"/local/x/{run_id}.jpg"},
        "consumed": False,
    }


def test_publish_and_get_best_bundle_by_score_and_time_window():
    with TemporaryDirectory() as td:
        store = DetectionSummaryStore(
            config=StoreConfig(state_path=Path(td) / "store.json", max_bundles_per_key=50)
        )
        base = 1000.0
        store.publish_bundle("garage", _bundle("a", base + 1, 1))
        store.publish_bundle("garage", _bundle("b", base + 2, 9))
        store.publish_bundle("garage", _bundle("c", base + 3, 5))

        best = store.get_best_bundle("garage", base, base + 10)
        assert best is not None
        assert best["run_id"] == "b"


def test_mark_consumed_excludes_by_default():
    with TemporaryDirectory() as td:
        store = DetectionSummaryStore(
            config=StoreConfig(state_path=Path(td) / "store.json", max_bundles_per_key=50)
        )
        base = 1000.0
        store.publish_bundle("garage", _bundle("a", base + 1, 1))
        store.publish_bundle("garage", _bundle("b", base + 2, 9))

        assert store.mark_consumed("garage", "b") is True
        best = store.get_best_bundle("garage", base, base + 10)
        assert best is not None
        assert best["run_id"] == "a"

        best_including = store.get_best_bundle("garage", base, base + 10, include_consumed=True)
        assert best_including is not None
        assert best_including["run_id"] == "b"


def test_cleanup_prunes_old_entries():
    with TemporaryDirectory() as td:
        store = DetectionSummaryStore(
            config=StoreConfig(state_path=Path(td) / "store.json", max_bundles_per_key=50)
        )
        now = time.time()
        store.publish_bundle("garage", _bundle("old", now - 10 * 3600, 9))
        store.publish_bundle("garage", _bundle("new", now - 60, 1))

        store.cleanup(retention_hours=1)
        best = store.get_best_bundle("garage", now - 24 * 3600, now)
        assert best is not None
        assert best["run_id"] == "new"

