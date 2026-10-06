"""Unit tests for detection_summary app (bundle generation)."""

from __future__ import annotations

import asyncio
import sys
from importlib import import_module
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

_capture_mod = import_module("detection_summary_app.capture")
CaptureState = _capture_mod.CaptureState
CapturedFrame = _capture_mod.CapturedFrame
_manager_mod = import_module("detection_summary_app.manager")
DetectionSummary = _manager_mod.DetectionSummary
_Run = _manager_mod._Run
_selection_mod = import_module("detection_summary_app.selection")
ScoreResult = _selection_mod.ScoreResult
SelectionMeta = _selection_mod.SelectionMeta


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

    def test_initialize_with_bundle_refs_succeeds(self):
        """Capability-pointer ai_provider_conf (bundle refs) resolves and initializes."""
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
            "ai_provider_conf": {
                "simple_text": "openai-default",
                "multimodal": "openai-default",
                "image": "openai-default",
            },
        }

        def resolve_secret(env_var: str) -> str:
            if env_var in ("OPENAPI_TOKEN", "OPENAI_API_KEY", "TOKEN"):
                return "test-key"
            raise ValueError(f"Unknown env var: {env_var}")

        with patch("detection_summary_app.manager.HAProvisioner") as MockProv:
            mock_prov = AsyncMock()
            mock_prov.ensure_helper.return_value = False
            mock_prov.ensure_script.return_value = False
            mock_prov._helper_slug = MagicMock(return_value="garage_detection_summary")
            MockProv.return_value = mock_prov
            with patch("providers.secrets.resolve_secret", side_effect=resolve_secret):
                app = self._make_app(args)
                app.initialize()
                app._async_startup_wrapper({})

        assert app.listen_state.call_count >= 1
        assert mock_prov.ensure_helper.call_count == 1

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

    def test_initialize_supports_env_backed_media_fs_root_and_ha_url(self):
        args = {
            "bundle_key": "garage",
            "ha_url_env": "HA_URL",
            "ha_token_env": "TOKEN",
            "hass_entities": {
                "camera_entity_id": "camera.garage",
                "trigger_entity_id": "binary_sensor.garage_person",
            },
            "snapshot_ha_dir": "/media/detection-summary/garage",
            "media_fs_root_env": "MEDIA_FS_ROOT",
            "data_instructions": "test",
            "image_instructions": "image",
            "ai_provider_conf": {"provider": "openai", "api_key_env": "OPENAI_API_KEY"},
        }

        def resolve_secret(env_var: str) -> str:
            return {
                "HA_URL": "http://homeassistant.local:8123",
                "MEDIA_FS_ROOT": str(Path(__file__).resolve().parent / "_tmp_media"),
                "TOKEN": "test-key",
                "OPENAI_API_KEY": "test-key",
            }[env_var]

        with patch("detection_summary_app.manager.HAProvisioner") as MockProv:
            mock_prov = AsyncMock()
            mock_prov.ensure_helper.return_value = False
            mock_prov._helper_slug = MagicMock(return_value="garage_detection_summary")
            MockProv.return_value = mock_prov
            with patch("providers.secrets.resolve_secret", side_effect=resolve_secret):
                app = self._make_app(args)
                app.initialize()
                app._async_startup_wrapper({})

        assert app.media_fs_root.endswith("_tmp_media")
        MockProv.assert_called_once_with(
            ha_url="http://homeassistant.local:8123",
            ha_token_env="TOKEN",
        )

    def test_initialize_requires_api_key_env_for_gemini_narrative(self):
        args = {
            "bundle_key": "garage",
            "hass_entities": {
                "camera_entity_id": "camera.garage",
                "trigger_entity_id": "binary_sensor.garage_person",
            },
            "snapshot_ha_dir": "/media/detection-summary/garage",
            "media_fs_root": str(Path(__file__).resolve().parent / "_tmp_media"),
            "data_instructions": "test",
            "image_instructions": "image",
            "ai_data_enabled": False,
            "run_narrative_enabled": True,
            "external_image_gen_enabled": False,
            "ai_provider_conf": {"provider": "gemini"},
        }

        app = self._make_app(args)
        with pytest.raises(ValueError) as exc_info:
            app.initialize()
        assert "api_key_env" in str(exc_info.value)
        assert "gemini" in str(exc_info.value).lower()

    def test_initialize_rejects_ollama_for_image_generation(self):
        args = {
            "bundle_key": "garage",
            "hass_entities": {
                "camera_entity_id": "camera.garage",
                "trigger_entity_id": "binary_sensor.garage_person",
            },
            "snapshot_ha_dir": "/media/detection-summary/garage",
            "media_fs_root": str(Path(__file__).resolve().parent / "_tmp_media"),
            "data_instructions": "test",
            "image_instructions": "image",
            "external_image_gen_enabled": True,
            "ai_provider_conf": {"provider": "ollama", "base_url": "http://localhost:11434"},
        }

        app = self._make_app(args)
        with pytest.raises(ValueError) as exc_info:
            app.initialize()
        assert "image generation" in str(exc_info.value).lower()
        assert "ollama" in str(exc_info.value).lower()

    def test_build_bundle_calls_multimodal_provider_when_log_events_enabled(self):
        args = {
            "bundle_key": "garage",
            "hass_entities": {
                "camera_entity_id": "camera.garage",
                "trigger_entity_id": "binary_sensor.garage_person",
            },
            "snapshot_ha_dir": "/media/detection-summary/garage",
            "media_fs_root": str(Path(__file__).resolve().parent / "_tmp_media"),
            "data_instructions": "test scoring prompt",
            "image_instructions": "image",
            "ai_data_enabled": True,
            "run_narrative_enabled": False,
            "external_image_gen_enabled": False,
            "log_llm_events": True,
            "analyze_max_snapshots": 1,
            "ai_provider_conf": {"provider": "openai", "api_key_env": "OPENAI_API_KEY"},
        }

        with patch("providers.secrets.resolve_secret", return_value="test-key"):
            app = self._make_app(args)
            app.initialize()

        fake_provider = MagicMock()
        fake_provider.generate_from_image.return_value = {
            "male_count": 1,
            "female_count": 0,
            "animal_count": 0,
            "person_score": 8,
            "face_score": 7,
            "frame_score": 8,
            "pose": "standing",
            "summary": "Person at the door.",
            "_meta": {"model": "gpt-5.2"},
        }
        app._multimodal_provider = fake_provider

        run_id = "run-score-test"
        local_run_dir = app._ha_path_to_local_fs(app.snapshot_ha_dir) / app.bundle_runs_subdir / run_id
        frames_dir = local_run_dir / app.captured_subdir
        frames_dir.mkdir(parents=True, exist_ok=True)
        frame_path = frames_dir / "frame_000.jpg"
        frame_path.write_bytes(b"\xff\xd8\xff" + b"\x00" * 16)

        run = _Run(
            capture=CaptureState(
                run_id=run_id,
                started_ts=1.0,
                ended_ts=2.0,
                frames=[
                    CapturedFrame(
                        idx=0,
                        filename="frame_000.jpg",
                        image_ha_path=f"{app.snapshot_ha_dir}/{app.bundle_runs_subdir}/{run_id}/{app.captured_subdir}/frame_000.jpg",
                        captured_ts=1.5,
                    )
                ],
                capture_idx=1,
            )
        )

        def fake_select(*, total_frames, budget, score_index, score_indices=None, seed, no_people_threshold, lookahead_after_no_people=2):
            scored = score_indices([0]) if score_indices else {0: score_index(0)}
            return scored, SelectionMeta(
                budget=1,
                scored_indices=[0],
                probes=[0],
                cutoff_idx_inclusive=0,
                best_idx=0,
            )

        with patch("detection_summary_app.manager.adaptive_select_and_score", side_effect=fake_select):
            with patch("detection_summary_app.manager.should_publish_bundle", return_value=False):
                with patch("detection_summary_app.manager.delete_run_dir", return_value=False):
                    result = app._build_bundle(run)

        assert result is None
        fake_provider.generate_from_image.assert_called_once()
        # Prompt builder must have injected schema + scoring guidance
        call_kw = fake_provider.generate_from_image.call_args.kwargs
        instr = call_kw.get("instructions", "")
        assert "male_count" in instr
        assert "animal_count" in instr
        assert "Scoring guidance" in instr

    def test_build_bundle_preserves_skipped_run_dir_in_debug_mode(self):
        args = {
            "bundle_key": "garage",
            "hass_entities": {
                "camera_entity_id": "camera.garage",
                "trigger_entity_id": "binary_sensor.garage_person",
            },
            "snapshot_ha_dir": "/media/detection-summary/garage",
            "media_fs_root": str(Path(__file__).resolve().parent / "_tmp_media"),
            "data_instructions": "test scoring prompt",
            "image_instructions": "image",
            "ai_data_enabled": True,
            "run_narrative_enabled": False,
            "external_image_gen_enabled": False,
            "debug_preserve_run_dirs": True,
            "analyze_max_snapshots": 1,
            "ai_provider_conf": {"provider": "openai", "api_key_env": "OPENAI_API_KEY"},
        }

        with patch("providers.secrets.resolve_secret", return_value="test-key"):
            app = self._make_app(args)
            app.initialize()

        fake_provider = MagicMock()
        fake_provider.generate_from_image.return_value = {
            "male_count": 0,
            "female_count": 0,
            "animal_count": 0,
            "person_score": 0,
            "face_score": 0,
            "frame_score": 0,
            "pose": "",
            "summary": "",
            "_meta": {"model": "gpt-5.2"},
        }
        app._multimodal_provider = fake_provider

        run_id = "run-preserve-test"
        local_run_dir = app._ha_path_to_local_fs(app.snapshot_ha_dir) / app.bundle_runs_subdir / run_id
        frames_dir = local_run_dir / app.captured_subdir
        frames_dir.mkdir(parents=True, exist_ok=True)
        (frames_dir / "frame_000.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 16)

        run = _Run(
            capture=CaptureState(
                run_id=run_id,
                started_ts=1.0,
                ended_ts=2.0,
                frames=[
                    CapturedFrame(
                        idx=0,
                        filename="frame_000.jpg",
                        image_ha_path=f"{app.snapshot_ha_dir}/{app.bundle_runs_subdir}/{run_id}/{app.captured_subdir}/frame_000.jpg",
                        captured_ts=1.5,
                    )
                ],
                capture_idx=1,
            )
        )

        def fake_select(*, total_frames, budget, score_index, score_indices=None, seed, no_people_threshold, lookahead_after_no_people=2):
            scored = score_indices([0]) if score_indices else {0: score_index(0)}
            return scored, SelectionMeta(
                budget=1,
                scored_indices=[0],
                probes=[0],
                cutoff_idx_inclusive=0,
                best_idx=0,
            )

        with patch("detection_summary_app.manager.adaptive_select_and_score", side_effect=fake_select):
            with patch("detection_summary_app.manager.should_publish_bundle", return_value=False):
                with patch("detection_summary_app.manager.delete_run_dir") as delete_run_dir:
                    result = app._build_bundle(run)

        assert result is None
        delete_run_dir.assert_not_called()

    # --- Ollama serialization (haynes-ops#3450) ---

    def _ollama_scoring_app_and_run(self, tmp_path: Path, n_frames: int):
        args = {
            "bundle_key": "garage",
            "hass_entities": {
                "camera_entity_id": "camera.garage",
                "trigger_entity_id": "binary_sensor.garage_person",
            },
            "snapshot_ha_dir": "/media/detection-summary/garage",
            "media_fs_root": str(tmp_path),
            "data_instructions": "test scoring prompt",
            "image_instructions": "image",
            "ai_data_enabled": True,
            "run_narrative_enabled": False,
            "external_image_gen_enabled": False,
            "log_llm_events": False,
            "analyze_max_snapshots": n_frames,
            "external_data_parallelism": 4,
            "ai_provider_conf": {"provider": "openai", "api_key_env": "OPENAI_API_KEY"},
        }
        with patch("providers.secrets.resolve_secret", return_value="test-key"):
            app = self._make_app(args)
            app.initialize()

        run_id = "run-ollama-gate"
        frames_dir = app._ha_path_to_local_fs(app.snapshot_ha_dir) / app.bundle_runs_subdir / run_id / app.captured_subdir
        frames_dir.mkdir(parents=True, exist_ok=True)
        frames = []
        for i in range(n_frames):
            (frames_dir / f"frame_{i:03d}.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 16)
            frames.append(
                CapturedFrame(
                    idx=i,
                    filename=f"frame_{i:03d}.jpg",
                    image_ha_path=f"{app.snapshot_ha_dir}/{app.bundle_runs_subdir}/{run_id}/{app.captured_subdir}/frame_{i:03d}.jpg",
                    captured_ts=1.0 + i,
                )
            )
        run = _Run(
            capture=CaptureState(
                run_id=run_id, started_ts=1.0, ended_ts=1.0 + n_frames, frames=frames, capture_idx=n_frames
            )
        )

        def fake_select(*, total_frames, budget, score_index, score_indices=None, seed, no_people_threshold, lookahead_after_no_people=2):
            idx = list(range(total_frames))
            scored = score_indices(idx)
            return scored, SelectionMeta(
                budget=total_frames, scored_indices=idx, probes=idx, cutoff_idx_inclusive=total_frames - 1, best_idx=0
            )

        return app, run, fake_select

    def test_build_bundle_sends_ollama_scoring_calls_one_at_a_time(self, tmp_path):
        """Four scoring threads per run must still put only one request on the Ollama endpoint at a time."""
        import json as _json
        import threading
        import time as _time
        import uuid as _uuid

        from providers.ai_providers.ollama.ollama_multimodal_text_provider import (
            OllamaMultimodalConfig,
            OllamaMultimodalTextProvider,
        )

        app, run, fake_select = self._ollama_scoring_app_and_run(tmp_path, n_frames=6)
        app._multimodal_provider = OllamaMultimodalTextProvider(
            OllamaMultimodalConfig(base_url=f"http://ollama-{_uuid.uuid4().hex[:8]}.test:11434")
        )

        lock = threading.Lock()
        state = {"current": 0, "max": 0, "calls": 0}
        body = _json.dumps(
            {"message": {"content": _json.dumps({"male_count": 1, "person_score": 8, "summary": "person"})}, "done": True}
        ).encode("utf-8")

        def fake_urlopen(req, timeout=None):
            with lock:
                state["current"] += 1
                state["calls"] += 1
                state["max"] = max(state["max"], state["current"])
            _time.sleep(0.03)
            with lock:
                state["current"] -= 1
            resp = MagicMock()
            resp.read.return_value = body
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with patch("detection_summary_app.manager.adaptive_select_and_score", side_effect=fake_select):
                with patch("detection_summary_app.manager.should_publish_bundle", return_value=False):
                    with patch("detection_summary_app.manager.delete_run_dir", return_value=False):
                        app._build_bundle(run)

        assert state["calls"] == 6
        assert state["max"] == 1

    def test_build_bundle_skips_a_frame_whose_ollama_queue_wait_timed_out(self, tmp_path):
        """A bounded queue wait that expires is logged as a warning and that frame is skipped, not fatal."""
        from providers.ai_providers.ollama._request_gate import OllamaQueueTimeout

        app, run, fake_select = self._ollama_scoring_app_and_run(tmp_path, n_frames=2)
        fake_provider = MagicMock()
        fake_provider.generate_from_image.side_effect = [
            OllamaQueueTimeout("ollama queue wait exceeded 300s for http://ollama (multimodal); skipped."),
            {"male_count": 1, "person_score": 8, "face_score": 5, "frame_score": 6, "summary": "person"},
        ]
        app._multimodal_provider = fake_provider
        app.external_data_parallelism = 1  # deterministic order: frame 0 times out, frame 1 scores

        with patch("detection_summary_app.manager.adaptive_select_and_score", side_effect=fake_select) as sel:
            with patch("detection_summary_app.manager.should_publish_bundle", return_value=False):
                with patch("detection_summary_app.manager.delete_run_dir", return_value=False):
                    result = app._build_bundle(run)

        assert result is None
        assert fake_provider.generate_from_image.call_count == 2
        warnings = [
            c for c in app.log.call_args_list if c.kwargs.get("level") == "WARNING" and "data gen failed" in str(c.args[0])
        ]
        assert len(warnings) == 1
        assert "queue wait exceeded" in str(warnings[0].args[0])
        sel.assert_called_once()

    def test_build_bundle_publishes_without_narrative_when_its_ollama_queue_wait_timed_out(self, tmp_path):
        """The gated narrative call: a queue timeout logs 'run narrative failed' and the run still publishes."""
        from providers.ai_providers.ollama._request_gate import OllamaQueueTimeout

        app, run, fake_select = self._ollama_scoring_app_and_run(tmp_path, n_frames=1)
        app.run_narrative_enabled = True
        app.external_image_gen_enabled = False
        fake_vision = MagicMock()
        fake_vision.generate_from_image.return_value = {
            "male_count": 1, "person_score": 8, "face_score": 5, "frame_score": 6, "summary": "person at the door",
        }
        fake_text = MagicMock()
        fake_text.generate_from_text.side_effect = OllamaQueueTimeout(
            "ollama queue wait exceeded 300s for http://ollama (simple_text model=qwen3.5:9b); skipped."
        )
        app._multimodal_provider = fake_vision
        app._simple_text_provider = fake_text

        with patch("detection_summary_app.manager.adaptive_select_and_score", side_effect=fake_select):
            with patch("detection_summary_app.manager.should_publish_bundle", return_value=True):
                result = app._build_bundle(run)

        fake_text.generate_from_text.assert_called_once()
        failed = [
            c for c in app.log.call_args_list
            if c.kwargs.get("level") == "WARNING" and "run narrative failed" in str(c.args[0])
        ]
        assert len(failed) == 1
        assert "queue wait exceeded" in str(failed[0].args[0])
        assert isinstance(result, dict)  # the run still publishes
        narrative = result["run_narrative"]
        assert narrative["run_summary"] is None
        assert "OllamaQueueTimeout" in narrative["error"]
        assert narrative["_narrative_meta"]["failed"] is True
