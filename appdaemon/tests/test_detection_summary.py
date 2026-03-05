"""Unit tests for detection_summary app (bundle generation)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Mock hassapi before importing detection_summary (tests run without AppDaemon)
class _MockHass:
    def __init__(self, ad, config):
        pass


mock_hass = MagicMock()
mock_hass.Hass = _MockHass
sys.modules["hassapi"] = mock_hass

# Add appdaemon root and apps to path for imports (providers lives at appdaemon/providers)
_repo = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo))
sys.path.insert(0, str(_repo / "apps"))

from detection_summary_app.manager import DetectionSummary


def _run_coro(coro):
    """Run a coroutine synchronously in a fresh event loop (test helper)."""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(coro)
    finally:
        loop.close()


class TestDetectionSummary:
    def _make_app(self, args: dict) -> DetectionSummary:
        ad = MagicMock()
        config = MagicMock()
        app = DetectionSummary(ad, config)
        app.args = args
        app.log = MagicMock()
        app.listen_state = MagicMock()
        app.listen_event = MagicMock()
        app.run_in = MagicMock()
        app.run_every = MagicMock()
        app.call_service = MagicMock()
        app.create_task = MagicMock(side_effect=_run_coro)
        return app

    def test_initialize_sets_up_and_listens(self):
        args = {
            "bundle_key": "garage",
            "ha_url": "http://homeassistant.local:8123",
            "ha_token_env": "TOKEN",
            "hass_entities": {
                "camera_entity_id": "camera.garage",
                "trigger_entity_id": "binary_sensor.garage_person",
            },
            "snapshot_ha_dir": "/media/detection-summary/garage",
            "media_fs_root": str(Path(__file__).resolve().parent / "_tmp_media"),
            "data_instructions": "test",
            "image_instructions": "image",
            "snapshot_interval_s": 0,
            "cooldown_s": 0,
            "retention_hours": 1,
            "ai_provider_conf": {"provider": "openai", "api_key_env": "OPENAI_API_KEY"},
        }

        with patch("detection_summary_app.manager.HAProvisioner") as MockProv:
            mock_prov = AsyncMock()
            mock_prov.ensure_helper.return_value = False
            mock_prov.ensure_script.return_value = False
            mock_prov._helper_slug = MagicMock(return_value="garage_detection_summary")
            MockProv.return_value = mock_prov
            with patch("providers.secrets.resolve_secret", return_value="test-key"):
                app = self._make_app(args)
                app.initialize()
                # Simulate AppDaemon firing the run_in(0) callback — triggers async startup
                app._async_startup_wrapper({})

        # Trigger listener must have been registered
        assert app.listen_state.call_count >= 1
        # Provisioning must have been attempted (1 helper: summary text only).
        # Run picker, selected summary, and relay script are now provisioned by detection_summary_viewer.
        assert mock_prov.ensure_helper.call_count == 1
        assert mock_prov.ensure_script.call_count == 0

    def test_initialize_skips_provisioning_when_credentials_absent(self):
        """When ha_url / ha_token_env are absent, provisioning is skipped gracefully."""
        args = {
            "bundle_key": "garage",
            # No ha_url / ha_token_env
            "hass_entities": {
                "camera_entity_id": "camera.garage",
                "trigger_entity_id": "binary_sensor.garage_person",
            },
            "snapshot_ha_dir": "/media/detection-summary/garage",
            "media_fs_root": str(Path(__file__).resolve().parent / "_tmp_media"),
            "data_instructions": "test",
            "image_instructions": "image",
            "ai_provider_conf": {"provider": "openai", "api_key_env": "OPENAI_API_KEY"},
        }

        with patch("providers.secrets.resolve_secret", return_value="test-key"):
            app = self._make_app(args)
            app.initialize()
            app._async_startup_wrapper({})

        # listen_state must still be registered even without provisioning
        assert app.listen_state.call_count >= 1
        # A WARNING must have been logged about missing credentials
        warning_calls = [c for c in app.log.mock_calls if "WARNING" in str(c)]
        assert warning_calls, "Expected a WARNING log about missing provisioner credentials"

