"""Unit tests for selected-run materialization in the viewer app."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# Mock hassapi before importing viewer (tests run without AppDaemon)
class _MockHass:
    def __init__(self, ad, config):
        pass


mock_hass = MagicMock()
mock_hass.Hass = _MockHass
sys.modules["hassapi"] = mock_hass

# Add appdaemon root and apps to path for imports
_repo = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo))
sys.path.insert(0, str(_repo / "apps"))

from detection_summary_viewer.detection_summary_viewer_app import DetectionSummaryViewer  # noqa: E402


def _make_viewer(tmp_media_root: Path) -> DetectionSummaryViewer:
    ad = MagicMock()
    config = MagicMock()
    app = DetectionSummaryViewer(ad, config)
    app.log = MagicMock()
    app.listen_state = MagicMock()
    app.listen_event = MagicMock()
    app.run_every = MagicMock()
    app.run_in = MagicMock()
    app.get_state = MagicMock(return_value=None)
    app.call_service = MagicMock()
    app.create_task = MagicMock()  # don't run async startup; tests call handlers directly
    app.args = {
        "bundle_key": "garage",
        "snapshot_ha_dir": "/media/detection-summary/garage",
        "media_fs_root": str(tmp_media_root),
    }
    app.initialize()
    return app


def test_materialize_selected_copies_best_and_generated(tmp_path: Path):
    tmp_media_root = tmp_path / "media"
    app = _make_viewer(tmp_media_root)

    run_id = "rid-1"
    base = tmp_media_root / "detection-summary" / "garage"
    run_dir = base / "runs" / run_id
    (run_dir / "captured").mkdir(parents=True, exist_ok=True)

    best_bytes = b"best-jpg"
    gen_bytes = b"generated-png"
    (run_dir / "best.jpg").write_bytes(best_bytes)
    (run_dir / "generated.png").write_bytes(gen_bytes)

    app._materialize_selected_run(run_id)

    best_dst = base / app.selected_best_filename
    gen_dst = base / app.selected_generated_filename
    assert best_dst.read_bytes() == best_bytes
    assert gen_dst.read_bytes() == gen_bytes
    # No HA service calls for local_file cameras are expected during materialization.
    assert not any(c.args and c.args[0] == "local_file/update_file_path" for c in app.call_service.mock_calls)


def test_materialize_selected_generated_missing_uses_best_fallback(tmp_path: Path):
    tmp_media_root = tmp_path / "media"
    app = _make_viewer(tmp_media_root)

    run_id = "rid-2"
    base = tmp_media_root / "detection-summary" / "garage"
    run_dir = base / "runs" / run_id
    (run_dir / "captured").mkdir(parents=True, exist_ok=True)

    best_bytes = b"best-jpg"
    (run_dir / "best.jpg").write_bytes(best_bytes)
    # generated.png intentionally missing

    app._materialize_selected_run(run_id)

    gen_dst = base / app.selected_generated_filename
    assert gen_dst.read_bytes() == best_bytes

    assert any("generated missing; using best as fallback" in str(c.args[0]) for c in app.log.mock_calls)


def test_materialize_selected_does_not_interleave_between_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    tmp_media_root = tmp_path / "media"
    app = _make_viewer(tmp_media_root)

    base = tmp_media_root / "detection-summary" / "garage"
    run1 = "rid-a"
    run2 = "rid-b"
    for rid, best_b, gen_b in [
        (run1, b"best-a", b"gen-a"),
        (run2, b"best-b", b"gen-b"),
    ]:
        run_dir = base / "runs" / rid
        (run_dir / "captured").mkdir(parents=True, exist_ok=True)
        (run_dir / "best.jpg").write_bytes(best_b)
        (run_dir / "generated.png").write_bytes(gen_b)

    original_atomic = app._atomic_copy
    state = {"count": 0}

    def slow_atomic(src: Path, dst: Path) -> None:
        original_atomic(src, dst)
        state["count"] += 1

    monkeypatch.setattr(app, "_atomic_copy", slow_atomic)

    import threading
    import time

    t1 = threading.Thread(target=lambda: app._materialize_selected_run(run1))
    t1.daemon = True
    t1.start()

    deadline = time.time() + 2.0
    while time.time() < deadline and state["count"] < 1:
        time.sleep(0.01)
    app._materialize_selected_run(run2)
    t1.join(timeout=2.0)

    best_dst = base / app.selected_best_filename
    gen_dst = base / app.selected_generated_filename
    assert best_dst.read_bytes() == b"best-b"
    assert gen_dst.read_bytes() == b"gen-b"
