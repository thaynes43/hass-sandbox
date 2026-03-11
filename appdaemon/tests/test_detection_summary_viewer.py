"""Unit tests for DetectionSummaryViewer app (init, provisioning, event listener setup)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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

from detection_summary_viewer.detection_summary_viewer_app import DetectionSummaryViewer


def _run_coro(coro):
    """Run a coroutine synchronously in a fresh event loop (test helper)."""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_viewer(args: dict) -> DetectionSummaryViewer:
    ad = MagicMock()
    config = MagicMock()
    app = DetectionSummaryViewer(ad, config)
    app.args = args
    app.log = MagicMock()
    app.listen_state = MagicMock()
    app.listen_event = MagicMock()
    app.run_in = MagicMock()
    app.run_every = MagicMock()
    app.call_service = MagicMock()
    app.get_state = MagicMock(return_value=None)
    app.create_task = MagicMock(side_effect=_run_coro)
    return app


def _base_args(tmp_path: Path) -> dict:
    return {
        "bundle_key": "garage",
        "ha_url": "http://homeassistant.local:8123",
        "ha_token_env": "TOKEN",
        "snapshot_ha_dir": "/media/detection-summary/garage",
        "media_fs_root": str(tmp_path / "media"),
        "notification_action_prefix": "GARAGE_DS_VIEW",
    }


class TestDetectionSummaryViewerInit:
    def test_initialize_provisions_helpers_and_relay(self, tmp_path: Path):
        args = _base_args(tmp_path)

        with patch("detection_summary_viewer.detection_summary_viewer_app.HAProvisioner") as MockProv:
            mock_prov = AsyncMock()
            mock_prov.ensure_helper.return_value = False
            mock_prov.ensure_script.return_value = False
            mock_prov._helper_slug = MagicMock(return_value="garage_detection_summary_run_id")
            MockProv.return_value = mock_prov

            app = _make_viewer(args)
            app.initialize()
            app._async_startup_wrapper({})

        # 4 helpers: input_select (run picker) + input_text (selected summary, timing, cooldown)
        assert mock_prov.ensure_helper.call_count == 4
        # 1 relay script
        assert mock_prov.ensure_script.call_count == 1

        # Listeners must have been registered
        assert app.listen_event.call_count >= 1  # detection_summary/run_published + notification action
        assert app.listen_state.call_count >= 1  # run picker

        # run_every for periodic sync + auto-reset
        assert app.run_every.call_count >= 1

    def test_initialize_skips_provisioning_when_no_credentials(self, tmp_path: Path):
        args = {
            "bundle_key": "garage",
            # No ha_url / ha_token_env
            "snapshot_ha_dir": "/media/detection-summary/garage",
            "media_fs_root": str(tmp_path / "media"),
        }

        app = _make_viewer(args)
        app.initialize()
        app._async_startup_wrapper({})

        warning_calls = [c for c in app.log.mock_calls if "WARNING" in str(c)]
        assert warning_calls, "Expected a WARNING log about missing provisioner credentials"

    def test_initialize_supports_env_backed_media_fs_root_and_ha_url(self, tmp_path: Path):
        args = {
            "bundle_key": "garage",
            "ha_url_env": "HA_URL",
            "ha_token_env": "TOKEN",
            "snapshot_ha_dir": "/media/detection-summary/garage",
            "media_fs_root_env": "MEDIA_FS_ROOT",
        }

        with patch(
            "providers.secrets.resolve_secret",
            side_effect=lambda env_var: {
                "HA_URL": "http://homeassistant.local:8123",
                "MEDIA_FS_ROOT": str(tmp_path / "media"),
                "TOKEN": "tok",
            }[env_var],
        ), patch("detection_summary_viewer.detection_summary_viewer_app.HAProvisioner") as MockProv:
            mock_prov = AsyncMock()
            mock_prov.ensure_helper.return_value = False
            mock_prov.ensure_script.return_value = False
            mock_prov._helper_slug = MagicMock(return_value="slug")
            MockProv.return_value = mock_prov

            app = _make_viewer(args)
            app.initialize()
            app._async_startup_wrapper({})

        assert app.media_fs_root == str(tmp_path / "media")
        MockProv.assert_called_once_with(
            ha_url="http://homeassistant.local:8123",
            ha_token_env="TOKEN",
        )

    def test_initialize_auto_derives_entity_ids_from_bundle_key(self, tmp_path: Path):
        args = _base_args(tmp_path)
        args.pop("ha_url", None)
        args.pop("ha_token_env", None)

        app = _make_viewer(args)
        app.initialize()

        assert app.run_picker_entity_id == "input_select.garage_detection_summary_run_id"
        assert app.selected_summary_text_entity_id == "input_text.garage_detection_summary_selected"

    def test_initialize_uses_explicit_entity_ids_when_provided(self, tmp_path: Path):
        args = _base_args(tmp_path)
        args.pop("ha_url", None)
        args.pop("ha_token_env", None)
        args["hass_entities"] = {
            "run_picker_entity_id": "input_select.custom_run_picker",
            "selected_summary_text_entity_id": "input_text.custom_selected",
            "selected_best_image_camera_entity_id": "camera.custom_best",
            "selected_generated_image_camera_entity_id": "camera.custom_gen",
        }

        app = _make_viewer(args)
        app.initialize()

        assert app.run_picker_entity_id == "input_select.custom_run_picker"
        assert app.selected_summary_text_entity_id == "input_text.custom_selected"
        assert app.selected_best_image_camera_entity_id == "camera.custom_best"

    def test_no_notification_listener_when_prefix_absent(self, tmp_path: Path):
        args = _base_args(tmp_path)
        args.pop("notification_action_prefix", None)

        with patch("detection_summary_viewer.detection_summary_viewer_app.HAProvisioner") as MockProv:
            mock_prov = AsyncMock()
            mock_prov.ensure_helper.return_value = False
            mock_prov.ensure_script.return_value = False
            mock_prov._helper_slug = MagicMock(return_value="slug")
            MockProv.return_value = mock_prov

            app = _make_viewer(args)
            app.initialize()
            app._async_startup_wrapper({})

        # Without a prefix, the mobile notification action listener must not be registered
        notification_listens = [
            c for c in app.listen_event.mock_calls
            if "mobile_app_notification_action" in str(c)
        ]
        assert not notification_listens

    def test_run_published_event_listener_always_registered(self, tmp_path: Path):
        args = _base_args(tmp_path)

        with patch("detection_summary_viewer.detection_summary_viewer_app.HAProvisioner") as MockProv:
            mock_prov = AsyncMock()
            mock_prov.ensure_helper.return_value = False
            mock_prov.ensure_script.return_value = False
            mock_prov._helper_slug = MagicMock(return_value="slug")
            MockProv.return_value = mock_prov

            app = _make_viewer(args)
            app.initialize()
            app._async_startup_wrapper({})

        run_published_listens = [
            c for c in app.listen_event.mock_calls
            if "detection_summary/run_published" in str(c)
        ]
        assert run_published_listens, "Expected listener for detection_summary/run_published"
