"""Unit tests for detection_summary cooldown exponential backoff."""

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

from detection_summary_app.manager import DetectionSummary, _compute_backoff_cooldown


def _run_coro(coro):
    """Run a coroutine synchronously in a fresh event loop (test helper)."""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(coro)
    finally:
        loop.close()


class TestComputeBackoffCooldown:
    """Group A: _compute_backoff_cooldown pure function (no AppDaemon needed)."""

    def test_a1_skipped_run(self):
        new_cd, reason = _compute_backoff_cooldown(
            bundle_generated=False,
            now=100.0,
            last_image_ts=50.0,
            prev_cooldown_s=120.0,
            base_cooldown_s=60.0,
            window_n=3.0,
            max_cooldown_s=1800.0,
        )
        assert new_cd == 60.0
        assert reason == "skipped_run"

    def test_a2_first_image(self):
        new_cd, reason = _compute_backoff_cooldown(
            bundle_generated=True,
            now=100.0,
            last_image_ts=0.0,
            prev_cooldown_s=60.0,
            base_cooldown_s=60.0,
            window_n=3.0,
            max_cooldown_s=1800.0,
        )
        assert new_cd == 60.0
        assert reason == "first_image"

    def test_a3_within_window_backoff(self):
        base = 60.0
        prev = 60.0
        window_n = 3.0
        window = window_n * prev
        delta = window - 1
        now = 500.0
        last_image_ts = now - delta

        new_cd, reason = _compute_backoff_cooldown(
            bundle_generated=True,
            now=now,
            last_image_ts=last_image_ts,
            prev_cooldown_s=prev,
            base_cooldown_s=base,
            window_n=window_n,
            max_cooldown_s=1800.0,
        )
        assert new_cd == prev * 2
        assert "backed_off" in reason

    def test_a4_exactly_at_window_boundary_backoff(self):
        base = 60.0
        prev = 60.0
        window_n = 3.0
        window = window_n * prev
        delta = window
        now = 500.0
        last_image_ts = now - delta

        new_cd, reason = _compute_backoff_cooldown(
            bundle_generated=True,
            now=now,
            last_image_ts=last_image_ts,
            prev_cooldown_s=prev,
            base_cooldown_s=base,
            window_n=window_n,
            max_cooldown_s=1800.0,
        )
        assert new_cd == prev * 2
        assert "backed_off" in reason

    def test_a5_just_over_window_reset(self):
        base = 60.0
        prev = 60.0
        window_n = 3.0
        window = window_n * prev
        delta = window + 0.001
        now = 500.0
        last_image_ts = now - delta

        new_cd, reason = _compute_backoff_cooldown(
            bundle_generated=True,
            now=now,
            last_image_ts=last_image_ts,
            prev_cooldown_s=prev,
            base_cooldown_s=base,
            window_n=window_n,
            max_cooldown_s=1800.0,
        )
        assert new_cd == base
        assert "reset" in reason

    def test_a6_at_max_cap(self):
        max_cd = 1800.0
        new_cd, reason = _compute_backoff_cooldown(
            bundle_generated=True,
            now=100.0,
            last_image_ts=50.0,
            prev_cooldown_s=max_cd,
            base_cooldown_s=60.0,
            window_n=3.0,
            max_cooldown_s=max_cd,
        )
        assert new_cd == max_cd
        assert "backed_off" in reason

    def test_a7_double_would_exceed_cap(self):
        base = 60.0
        prev = 1000.0
        max_cd = 1800.0
        new_cd, reason = _compute_backoff_cooldown(
            bundle_generated=True,
            now=100.0,
            last_image_ts=50.0,
            prev_cooldown_s=prev,
            base_cooldown_s=base,
            window_n=3.0,
            max_cooldown_s=max_cd,
        )
        assert new_cd == max_cd
        assert "backed_off" in reason

    def test_a8_window_n_zero(self):
        base = 60.0
        prev = 60.0
        new_cd, reason = _compute_backoff_cooldown(
            bundle_generated=True,
            now=100.0,
            last_image_ts=50.0,
            prev_cooldown_s=prev,
            base_cooldown_s=base,
            window_n=0.0,
            max_cooldown_s=1800.0,
        )
        assert new_cd == base
        assert "reset" in reason

    def test_a9_multiple_backoffs_accumulate(self):
        base = 60.0
        max_cd = 1800.0
        window_n = 3.0
        t0 = 100.0

        cd = base
        last_ts = 0.0
        for i in range(5):
            now = t0 + (i + 1) * 1.0
            cd, reason = _compute_backoff_cooldown(
                bundle_generated=True,
                now=now,
                last_image_ts=last_ts,
                prev_cooldown_s=cd,
                base_cooldown_s=base,
                window_n=window_n,
                max_cooldown_s=max_cd,
            )
            last_ts = now

        assert cd <= max_cd
        assert cd >= base
        assert cd == min(max_cd, base * 16)


class TestComputeBackoffCooldownIntegration:
    """Group B: DetectionSummary._finalize integration (mocked AppDaemon instance)."""

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
        app.fire_event = MagicMock()
        app.create_task = MagicMock(side_effect=_run_coro)
        return app

    def _minimal_args(self) -> dict:
        return {
            "bundle_key": "garage",
            "hass_entities": {
                "camera_entity_id": "camera.garage",
                "trigger_entity_id": "binary_sensor.garage_person",
            },
            "snapshot_ha_dir": "/media/detection-summary/garage",
            "media_fs_root": str(Path(__file__).resolve().parent / "_tmp_media"),
            "data_instructions": "test",
            "image_instructions": "image",
            "cooldown_s": 60,
            "cooldown_backoff_max_s": 1800,
            "cooldown_backoff_window_n": 3,
            "ai_provider_conf": {"provider": "openai", "api_key_env": "OPENAI_API_KEY"},
        }

    def test_b1_first_bundle_sets_last_image_generated_ts(self):
        with patch("detection_summary_app.manager.HAProvisioner") as MockProv:
            mock_prov = AsyncMock()
            mock_prov.ensure_helper.return_value = False
            mock_prov._helper_slug = MagicMock(return_value="garage_detection_summary")
            MockProv.return_value = mock_prov
            with patch("providers.secrets.resolve_secret", return_value="test-key"):
                app = self._make_app(self._minimal_args())
                app.initialize()

        app._last_image_generated_ts = 0.0
        app._effective_cooldown_s = 60.0
        app._active = MagicMock()
        app._active.capture.run_id = "run-1"
        app._active.capture.started_ts = 100.0
        app._active.bundle = {"bundle": "data", "created_at_epoch": 100.0}

        with patch("detection_summary_app.manager.DETECTION_SUMMARY_STORE") as mock_store:
            app._finalize({"run_id": "run-1"})

        assert app._last_image_generated_ts > 0.0
        assert app._effective_cooldown_s == 60.0

    def test_b2_rapid_second_bundle_backs_off(self):
        import time as time_module

        with patch("detection_summary_app.manager.HAProvisioner") as MockProv:
            mock_prov = AsyncMock()
            mock_prov.ensure_helper.return_value = False
            mock_prov._helper_slug = MagicMock(return_value="garage_detection_summary")
            MockProv.return_value = mock_prov
            with patch("providers.secrets.resolve_secret", return_value="test-key"):
                app = self._make_app(self._minimal_args())
                app.initialize()

        now = time_module.time()
        app._last_image_generated_ts = now - 5.0
        app._effective_cooldown_s = 60.0
        app._active = MagicMock()
        app._active.capture.run_id = "run-2"
        app._active.capture.started_ts = now - 5.0
        app._active.bundle = {"bundle": "data", "created_at_epoch": now}

        with patch("detection_summary_app.manager.DETECTION_SUMMARY_STORE"):
            app._finalize({"run_id": "run-2"})

        assert app._effective_cooldown_s == 120.0

    def test_b3_slow_second_bundle_resets(self):
        import time as time_module

        with patch("detection_summary_app.manager.HAProvisioner") as MockProv:
            mock_prov = AsyncMock()
            mock_prov.ensure_helper.return_value = False
            mock_prov._helper_slug = MagicMock(return_value="garage_detection_summary")
            MockProv.return_value = mock_prov
            with patch("providers.secrets.resolve_secret", return_value="test-key"):
                app = self._make_app(self._minimal_args())
                app.initialize()

        now = time_module.time()
        app._last_image_generated_ts = now - 300.0
        app._effective_cooldown_s = 60.0
        app._active = MagicMock()
        app._active.capture.run_id = "run-3"
        app._active.capture.started_ts = now - 300.0
        app._active.bundle = {"bundle": "data", "created_at_epoch": now}

        with patch("detection_summary_app.manager.DETECTION_SUMMARY_STORE"):
            app._finalize({"run_id": "run-3"})

        assert app._effective_cooldown_s == 60.0

    def test_b4_skipped_run_resets_cooldown_not_last_image_ts(self):
        with patch("detection_summary_app.manager.HAProvisioner") as MockProv:
            mock_prov = AsyncMock()
            mock_prov.ensure_helper.return_value = False
            mock_prov._helper_slug = MagicMock(return_value="garage_detection_summary")
            MockProv.return_value = mock_prov
            with patch("providers.secrets.resolve_secret", return_value="test-key"):
                app = self._make_app(self._minimal_args())
                app.initialize()

        app._last_image_generated_ts = 12345.0
        app._effective_cooldown_s = 240.0
        app._active = MagicMock()
        app._active.capture.run_id = "run-4"
        app._active.capture.started_ts = 100.0
        app._active.bundle = None

        app._finalize({"run_id": "run-4"})

        assert app._effective_cooldown_s == 60.0
        assert app._last_image_generated_ts == 12345.0

    def test_b5_cooldown_skip_logs_warning(self):
        import time as time_module

        with patch("detection_summary_app.manager.HAProvisioner") as MockProv:
            mock_prov = AsyncMock()
            mock_prov.ensure_helper.return_value = False
            mock_prov._helper_slug = MagicMock(return_value="garage_detection_summary")
            MockProv.return_value = mock_prov
            with patch("providers.secrets.resolve_secret", return_value="test-key"):
                app = self._make_app(self._minimal_args())
                app.initialize()

        app._last_run_ts = time_module.time() - 10.0
        app._effective_cooldown_s = 60.0
        app._in_flight = False

        app._on_trigger(
            "binary_sensor.garage_person",
            "state",
            "off",
            "on",
            {},
        )

        warning_calls = [
            c
            for c in app.log.mock_calls
            if c.kwargs.get("level") == "WARNING" and "SUPPRESSED" in str(c.args[0] if c.args else "")
        ]
        assert len(warning_calls) >= 1

    def test_b6_after_backoff_warning_logged(self):
        import time as time_module

        with patch("detection_summary_app.manager.HAProvisioner") as MockProv:
            mock_prov = AsyncMock()
            mock_prov.ensure_helper.return_value = False
            mock_prov._helper_slug = MagicMock(return_value="garage_detection_summary")
            MockProv.return_value = mock_prov
            with patch("providers.secrets.resolve_secret", return_value="test-key"):
                app = self._make_app(self._minimal_args())
                app.initialize()

        now = time_module.time()
        app._last_image_generated_ts = now - 5.0
        app._effective_cooldown_s = 60.0
        app._active = MagicMock()
        app._active.capture.run_id = "run-6"
        app._active.capture.started_ts = now - 5.0
        app._active.bundle = {"bundle": "data", "created_at_epoch": now}

        with patch("detection_summary_app.manager.DETECTION_SUMMARY_STORE"):
            app._finalize({"run_id": "run-6"})

        warning_calls = [
            c
            for c in app.log.mock_calls
            if c.kwargs.get("level") == "WARNING" and "backed_off" in str(c.args[0] if c.args else "")
        ]
        assert len(warning_calls) >= 1

    def test_b7_after_reset_info_logged(self):
        import time as time_module

        with patch("detection_summary_app.manager.HAProvisioner") as MockProv:
            mock_prov = AsyncMock()
            mock_prov.ensure_helper.return_value = False
            mock_prov._helper_slug = MagicMock(return_value="garage_detection_summary")
            MockProv.return_value = mock_prov
            with patch("providers.secrets.resolve_secret", return_value="test-key"):
                app = self._make_app(self._minimal_args())
                app.initialize()

        now = time_module.time()
        app._last_image_generated_ts = now - 300.0
        app._effective_cooldown_s = 60.0
        app._active = MagicMock()
        app._active.capture.run_id = "run-7"
        app._active.capture.started_ts = now - 300.0
        app._active.bundle = {"bundle": "data", "created_at_epoch": now}

        with patch("detection_summary_app.manager.DETECTION_SUMMARY_STORE"):
            app._finalize({"run_id": "run-7"})

        info_calls = [
            c
            for c in app.log.mock_calls
            if c.kwargs.get("level") == "INFO" and "reset" in str(c.args[0] if c.args else "")
        ]
        assert len(info_calls) >= 1
