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

from detection_summary import DetectionSummary


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

    def test_generate_bundle_produces_expected_paths_and_best(self):
        run_id = "run-123"
        started_ts = 1000.0

        # Note: summary.selector.text has no config in YAML, which parses as None.
        # Our code normalizes this so HA doesn't receive an empty selector {}.
        args = {
            "bundle_key": "garage",
            "camera_entity_id": "camera.garage",
            "trigger_entity_id": "binary_sensor.garage_person",
            "storage_backend": "media",
            "snapshot_ha_dir": "/media/detection-summary/garage",
            "media_fs_root": str(Path(__file__).resolve().parent / "_tmp_media"),
            "web_path_base": "/local/detection-summary/garage",
            "buffer_subdir": "buffer",
            "bundle_runs_subdir": "runs",
            "bundle_best_filename": "best.jpg",
            "best_image_camera_entity_id": "camera.detection_summary_garage_best",
            "ai_task_entity_id": "ai_task.openai_ai_task",
            "task_name": "detection summary",
            "data_instructions": "test",
            "data_structure": {
                "score": {"selector": {"number": {"min": 0, "max": 10}}},
                "summary": {"selector": {"text": None}},
            },
            "generate_image_enabled": True,
            "image_instructions": "image",
            "max_snapshots": 2,
            "snapshot_interval_s": 0,
            "ring_size": 10,
            "cooldown_s": 0,
            "retention_hours": 1,
            "external_data_provider": "openai",
            "external_data_api_key": "test-key",
        }

        app = self._make_app(args)

        # Fake external data provider: return different scores per snapshot.
        ai_calls = {"n": 0}

        class _FakeDataProvider:
            def generate_data_from_image(self, *, input_image_path: str, instructions: str, expected_keys=None):
                ai_calls["n"] += 1
                person = 1 if ai_calls["n"] == 1 else 9
                frame = 1 if ai_calls["n"] == 1 else 9
                pose = "walking" if ai_calls["n"] == 1 else "standing"
                return {"person_score": person, "frame_score": frame, "pose": pose, "summary": f"s{person}"}

        def call_service_side_effect(service, *a, **kw):
            if service == "camera/snapshot":
                return {"success": True, "result": {}}
            if service == "local_file/update_file_path":
                return {"success": True, "result": {}}
            if service == "ai_task/generate_image":
                return {
                    "success": True,
                    "result": {
                        "response": {
                            "media_source_id": "media-source://media_source/test.png",
                            "url": "/api/media/test.png",
                            "revised_prompt": "rp",
                            "model": "m",
                        }
                    },
                }
            raise AssertionError(f"unexpected service: {service}")

        app.call_service.side_effect = call_service_side_effect

        app.initialize()
        # Ensure _generate_bundle uses our fake provider (avoid real HTTP calls in unit tests).
        app._data_provider = _FakeDataProvider()
        bundle = app._generate_bundle(run_id=run_id, started_ts=started_ts)

        assert bundle["run_id"] == run_id
        assert bundle["bundle_key"] == "garage"

        # Raw buffer snapshots should live under /buffer/
        assert len(bundle["candidates"]) == 2
        assert bundle["candidates"][0]["image_web_path"].startswith("/local/detection-summary/garage/buffer/")

        # Best should be the higher score (second)
        assert bundle["best"]["score"] == 9
        assert bundle["best"]["summary"] == "s9"

        # Bundle artifact folder should be under runs/<run_id>/best.jpg
        artifacts = bundle["bundle_artifacts"]
        assert artifacts["bundle_web_base"].endswith(f"/runs/{run_id}")
        assert artifacts["best_artifact_web_path"].endswith(f"/runs/{run_id}/best.jpg")

        # Generated image metadata should be present
        assert bundle["generated_image"]["media_source_id"] == "media-source://media_source/test.png"

        # If best_image_camera_entity_id is set, bundle should provide a camera proxy URL
        assert bundle["best"]["image_url"] == "/api/camera_proxy/camera.detection_summary_garage_best"

        # camera.snapshot called: once per candidate + once for best artifact
        camera_calls = [c for c in app.call_service.call_args_list if c[0][0] == "camera/snapshot"]
        assert len(camera_calls) == 3

