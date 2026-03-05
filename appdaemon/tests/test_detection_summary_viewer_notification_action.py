"""Unit tests for selecting runs via mobile notification action buttons (viewer app)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch


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
        "notification_action_prefix": "GARAGE_DS_VIEW",
    }
    app.initialize()
    return app


def test_notification_action_selects_only_when_run_is_in_picker_options(tmp_path: Path):
    tmp_media_root = tmp_path / "media"
    app = _make_viewer(tmp_media_root)
    app._sync_run_picker_periodic = MagicMock()

    run_id = "1be14dfe-715c-43f3-8164-f4483ef306fa"
    short = run_id[:8]
    app.get_state = MagicMock(
        return_value={
            "state": run_id,
            "attributes": {"options": [run_id, "other"]},
        }
    )

    app._on_mobile_app_notification_action(
        "mobile_app_notification_action",
        {"action": f"GARAGE_DS_VIEW:{short}"},
        {},
    )

    assert app._sync_run_picker_periodic.call_count == 1
    assert any(
        c.args
        and c.args[0] == "input_select/select_option"
        and c.kwargs.get("option") == run_id
        for c in app.call_service.mock_calls
    )


def test_notification_action_ignores_run_not_in_picker_options(tmp_path: Path):
    tmp_media_root = tmp_path / "media"
    app = _make_viewer(tmp_media_root)
    app._sync_run_picker_periodic = MagicMock()

    run_id = "rid-missing"
    short = run_id[:8]
    app.get_state = MagicMock(
        return_value={
            "state": "other",
            "attributes": {"options": ["other"]},
        }
    )

    app._on_mobile_app_notification_action(
        "mobile_app_notification_action",
        {"action": f"GARAGE_DS_VIEW:{short}"},
        {},
    )

    assert app._sync_run_picker_periodic.call_count == 1
    assert not any(
        c.args and c.args[0] == "input_select/select_option" for c in app.call_service.mock_calls
    )


def test_notification_action_ignored_when_prefix_does_not_match(tmp_path: Path):
    tmp_media_root = tmp_path / "media"
    app = _make_viewer(tmp_media_root)
    app._sync_run_picker_periodic = MagicMock()

    app._on_mobile_app_notification_action(
        "mobile_app_notification_action",
        {"action": "WRONG_PREFIX:some-run-id"},
        {},
    )

    assert app._sync_run_picker_periodic.call_count == 0
    assert not app.call_service.mock_calls


def test_notification_action_ignored_when_no_prefix_configured(tmp_path: Path):
    tmp_media_root = tmp_path / "media"
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
    app.create_task = MagicMock()
    app.args = {
        "bundle_key": "garage",
        "snapshot_ha_dir": "/media/detection-summary/garage",
        "media_fs_root": str(tmp_media_root),
        # No notification_action_prefix
    }
    app.initialize()
    app._sync_run_picker_periodic = MagicMock()

    # With no prefix, parse returns None for any action
    result = app._parse_run_id_from_action("GARAGE_DS_VIEW:some-run-id")
    assert result is None
