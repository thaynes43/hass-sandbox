"""Unit tests for dashboard viewer staging pipeline."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# Mock hassapi before importing detection_summary (tests run without AppDaemon)
class _MockHass:
    def __init__(self, ad, config):
        pass


mock_hass = MagicMock()
mock_hass.Hass = _MockHass
sys.modules["hassapi"] = mock_hass

# Add apps to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))

from detection_summary_app.viewer_cache import ViewerCache, ViewerCacheConfig  # noqa: E402


def _make_cache(*, tmp_media_root: Path) -> tuple[ViewerCache, MagicMock, MagicMock]:
    call_service = MagicMock()
    log = MagicMock()

    def ha_path_to_local_fs(ha_path: str) -> Path:
        # emulate `/media` mapping in tests
        if ha_path.startswith("/media/"):
            return tmp_media_root / ha_path[len("/media/") :]
        return Path(ha_path)

    cache = ViewerCache(
        cfg=ViewerCacheConfig(
            snapshot_ha_dir="/media/detection-summary/garage",
            bundle_runs_subdir="runs",
            captured_subdir="captured",
            viewer_stage_subdir="viewer_stage",
            viewer_www_subdir="viewer",
            refresh_shell_command="ds_refresh_detection_summary_viewer_www",
        ),
        ha_path_to_local_fs=ha_path_to_local_fs,
        call_service=call_service,
        log=log,
    )
    return cache, call_service, log


def test_viewer_stages_renamed_files_and_calls_refresh(tmp_path: Path):
    tmp_media_root = tmp_path / "media"
    cache, call_service, _log = _make_cache(tmp_media_root=tmp_media_root)

    run_id = "rid-1"
    runs_dir = tmp_media_root / "detection-summary" / "garage" / "runs" / run_id
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / "captured").mkdir(parents=True, exist_ok=True)
    (runs_dir / "best.jpg").write_bytes(b"best")
    (runs_dir / "generated.png").write_bytes(b"gen")

    staged = cache.stage_run_ids_to_media([run_id])
    assert staged == [run_id]
    stage_dir = tmp_media_root / "detection-summary" / "garage" / "viewer_stage"
    assert (stage_dir / f"{run_id}_best.jpg").read_bytes() == b"best"
    assert (stage_dir / f"{run_id}_generated.png").read_bytes() == b"gen"

    cache.refresh_www_from_stage()
    assert any(
        c.args
        and c.args[0] == "shell_command/ds_refresh_detection_summary_viewer_www"
        and c.kwargs.get("snapshot_rel") == "detection-summary/garage"
        and c.kwargs.get("viewer_www_subdir") == "viewer"
        and c.kwargs.get("viewer_stage_subdir") == "viewer_stage"
        for c in call_service.mock_calls
    )


def test_viewer_selected_file_paths(tmp_path: Path):
    tmp_media_root = tmp_path / "media"
    cache, _call_service, _log = _make_cache(tmp_media_root=tmp_media_root)
    best, gen = cache.selected_file_paths_for_run("rid-2")
    assert best == "/config/www/detection-summary/garage/viewer/rid-2_best.jpg"
    assert gen == "/config/www/detection-summary/garage/viewer/rid-2_generated.png"

