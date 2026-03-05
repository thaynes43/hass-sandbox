"""Unit tests for DetectionSummaryViewer format helpers and _update_run_detail_helpers."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

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

from detection_summary_viewer.detection_summary_viewer_app import (
    DetectionSummaryViewer,
    _format_timing_value,
    _format_cooldown_value,
)


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
    }


def _initialized_viewer(tmp_path: Path) -> DetectionSummaryViewer:
    args = _base_args(tmp_path)
    with patch("detection_summary_viewer.detection_summary_viewer_app.HAProvisioner") as MockProv:
        mock_prov = AsyncMock()
        mock_prov.ensure_helper.return_value = False
        mock_prov._helper_slug = MagicMock(return_value="garage_detection_summary_run_id")
        MockProv.return_value = mock_prov
        app = _make_viewer(args)
        app.initialize()
    return app


def _make_run_dir(tmp_path: Path, run_id: str) -> Path:
    run_dir = tmp_path / "media" / "detection-summary" / "garage" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _write_summary_json(run_dir: Path, data: dict) -> Path:
    p = run_dir / "summary.json"
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Format function tests
# ---------------------------------------------------------------------------


class TestFormatTimingValue:
    def test_format_timing_normal(self):
        # 10:00:00 UTC → 10:01:30 UTC (1:30 elapsed)
        epoch_start = 36000.0  # 10:00:00 UTC
        epoch_end = epoch_start + 90.0
        timing = {
            "capture_started_epoch": epoch_start,
            "capture_ended_epoch": epoch_end,
            "capture_duration_s": 90.0,
        }
        result = _format_timing_value(timing)
        assert "10:00:00" in result
        assert "10:01:30" in result
        assert "1:30" in result

    def test_format_timing_zero_started(self):
        timing = {
            "capture_started_epoch": 0,
            "capture_ended_epoch": 36090.0,
            "capture_duration_s": 90.0,
        }
        result = _format_timing_value(timing)
        assert result.startswith("?")

    def test_format_timing_over_one_hour(self):
        epoch_start = 36000.0
        epoch_end = epoch_start + 3700.0  # 1h 1m 40s
        timing = {
            "capture_started_epoch": epoch_start,
            "capture_ended_epoch": epoch_end,
            "capture_duration_s": 3700.0,
        }
        result = _format_timing_value(timing)
        # Should show H:MM:SS format
        assert "1:01:40" in result


class TestFormatCooldownValue:
    def test_format_cooldown_ready(self):
        # expires well in the past
        expires = time.time() - 3600.0
        cooldown = {
            "effective_cooldown_s": 60.0,
            "base_cooldown_s": 60.0,
            "backoff_increments": 0,
            "cooldown_expires_at_epoch": expires,
        }
        result = _format_cooldown_value(cooldown)
        assert result.startswith("ready")

    def test_format_cooldown_active(self):
        # expires well in the future
        expires = time.time() + 3600.0
        cooldown = {
            "effective_cooldown_s": 60.0,
            "base_cooldown_s": 60.0,
            "backoff_increments": 0,
            "cooldown_expires_at_epoch": expires,
        }
        result = _format_cooldown_value(cooldown)
        assert result.startswith("on cooldown until")

    def test_format_cooldown_with_backoff(self):
        expires = time.time() + 3600.0
        cooldown = {
            "effective_cooldown_s": 240.0,
            "base_cooldown_s": 60.0,
            "backoff_increments": 2,
            "cooldown_expires_at_epoch": expires,
        }
        result = _format_cooldown_value(cooldown)
        assert "x2" in result


# ---------------------------------------------------------------------------
# _update_run_detail_helpers integration tests
# ---------------------------------------------------------------------------


class TestUpdateRunDetailHelpers:
    def test_update_helpers_sets_both(self, tmp_path: Path):
        app = _initialized_viewer(tmp_path)
        run_id = "run-both"
        run_dir = _make_run_dir(tmp_path, run_id)

        now = time.time()
        summary_data = {
            "summary": {
                "timing": {
                    "capture_started_epoch": 36000.0,
                    "capture_ended_epoch": 36090.0,
                    "capture_duration_s": 90.0,
                }
            },
            "cooldown_state": {
                "effective_cooldown_s": 60.0,
                "base_cooldown_s": 60.0,
                "backoff_increments": 0,
                "cooldown_expires_at_epoch": now - 3600.0,
            },
        }
        _write_summary_json(run_dir, summary_data)

        app._update_run_detail_helpers(run_id, local_run_dir=run_dir)

        # Both call_service calls should have been made
        cs_calls = [str(c) for c in app.call_service.call_args_list]
        timing_called = any("detection_summary_timing" in c for c in cs_calls)
        cooldown_called = any("detection_summary_cooldown" in c for c in cs_calls)
        assert timing_called, "Expected call_service for timing helper"
        assert cooldown_called, "Expected call_service for cooldown helper"

    def test_update_helpers_warns_missing_cooldown(self, tmp_path: Path):
        app = _initialized_viewer(tmp_path)
        run_id = "run-no-cooldown"
        run_dir = _make_run_dir(tmp_path, run_id)

        summary_data = {
            "summary": {
                "timing": {
                    "capture_started_epoch": 36000.0,
                    "capture_ended_epoch": 36090.0,
                    "capture_duration_s": 90.0,
                }
            },
            # No cooldown_state key
        }
        _write_summary_json(run_dir, summary_data)

        app._update_run_detail_helpers(run_id, local_run_dir=run_dir)

        # WARNING should be logged about absent cooldown_state
        warning_calls = [c for c in app.log.mock_calls if c.kwargs.get("level") == "WARNING"]
        assert warning_calls, "Expected WARNING when cooldown_state is absent"

        # Timing helper should still be set
        cs_calls = [str(c) for c in app.call_service.call_args_list]
        timing_called = any("detection_summary_timing" in c for c in cs_calls)
        assert timing_called, "Timing helper should still be set even without cooldown_state"

        # Cooldown helper should be set to "unknown" for old runs without cooldown_state
        cooldown_calls = [
            c for c in app.call_service.call_args_list
            if "detection_summary_cooldown" in str(c)
        ]
        assert cooldown_calls, "Cooldown helper should be set to 'unknown'"
        # Last cooldown call should have value="unknown"
        last_cooldown_call = cooldown_calls[-1]
        call_kwargs = last_cooldown_call.kwargs if last_cooldown_call.kwargs else {}
        call_args = last_cooldown_call.args if last_cooldown_call.args else ()
        assert "value" in call_kwargs and call_kwargs["value"] == "unknown" or \
               "unknown" in call_args, "Cooldown helper should be set to 'unknown' for old runs"

    def test_update_helpers_warns_missing_file(self, tmp_path: Path):
        app = _initialized_viewer(tmp_path)
        run_id = "run-no-file"
        # Do NOT create the run directory or summary.json

        app._update_run_detail_helpers(run_id)

        # WARNING should be logged about missing file
        warning_calls = [c for c in app.log.mock_calls if c.kwargs.get("level") == "WARNING"]
        assert warning_calls, "Expected WARNING when summary.json is missing"

        # No call_service should have been made
        assert app.call_service.call_count == 0, "No call_service calls expected when file is missing"

    def test_update_helpers_warns_on_call_service_failure(self, tmp_path: Path):
        app = _initialized_viewer(tmp_path)
        run_id = "run-cs-fail"
        run_dir = _make_run_dir(tmp_path, run_id)

        now = time.time()
        summary_data = {
            "summary": {
                "timing": {
                    "capture_started_epoch": 36000.0,
                    "capture_ended_epoch": 36090.0,
                    "capture_duration_s": 90.0,
                }
            },
            "cooldown_state": {
                "effective_cooldown_s": 60.0,
                "base_cooldown_s": 60.0,
                "backoff_increments": 0,
                "cooldown_expires_at_epoch": now - 3600.0,
            },
        }
        _write_summary_json(run_dir, summary_data)

        app.call_service.side_effect = RuntimeError("HA connection lost")

        # Must not raise
        app._update_run_detail_helpers(run_id, local_run_dir=run_dir)

        # WARNING should be logged for the call_service failure
        warning_calls = [c for c in app.log.mock_calls if c.kwargs.get("level") == "WARNING"]
        assert warning_calls, "Expected WARNING log when call_service raises"
