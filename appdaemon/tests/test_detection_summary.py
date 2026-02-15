"""Unit tests for detection_summary app (bundle generation)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

# Mock hassapi before importing detection_summary (tests run without AppDaemon)
class _MockHass:
    def __init__(self, ad, config):
        pass


mock_hass = MagicMock()
mock_hass.Hass = _MockHass
sys.modules["hassapi"] = mock_hass

# Add apps to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))

from detection_summary_app.manager import DetectionSummary


class TestDetectionSummary:
    def _make_app(self, args: dict) -> DetectionSummary:
        ad = MagicMock()
        config = MagicMock()
        app = DetectionSummary(ad, config)
        app.args = args
        app.log = MagicMock()
        app.listen_state = MagicMock()
        app.run_in = MagicMock()
        app.call_service = MagicMock()
        return app

    def test_initialize_sets_up_and_listens(self):
        args = {
            "bundle_key": "garage",
            "camera_entity_id": "camera.garage",
            "trigger_entity_id": "binary_sensor.garage_person",
            "storage_backend": "media",
            "snapshot_ha_dir": "/media/detection-summary/garage",
            "media_fs_root": str(Path(__file__).resolve().parent / "_tmp_media"),
            "bundle_runs_subdir": "runs",
            "bundle_best_filename": "best.jpg",
            "task_name": "detection summary",
            "data_instructions": "test",
            "data_structure": {
                "score": {"selector": {"number": {"min": 0, "max": 10}}},
                "summary": {"selector": {"text": None}},
            },
            "image_instructions": "image",
            "snapshot_interval_s": 0,
            "cooldown_s": 0,
            "retention_hours": 1,
            "external_data_provider": "openai",
            "external_data_api_key": "test-key",
        }

        app = self._make_app(args)

        app.initialize()
        app.listen_state.assert_called_once()

