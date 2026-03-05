"""Unit tests for DetectionSummary._patch_summary_json_cooldown (cooldown_state in summary.json)."""

from __future__ import annotations

import asyncio
import json
import sys
import time
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


def _make_app(tmp_path: Path, *, write_bundle_json: bool = True) -> DetectionSummary:
    args = {
        "bundle_key": "garage",
        "hass_entities": {
            "camera_entity_id": "camera.garage",
            "trigger_entity_id": "binary_sensor.garage_person",
        },
        "snapshot_ha_dir": "/media/detection-summary/garage",
        "media_fs_root": str(tmp_path / "media"),
        "data_instructions": "test",
        "image_instructions": "image",
        "cooldown_s": 60,
        "cooldown_backoff_max_s": 1800,
        "cooldown_backoff_window_n": 3,
        "write_bundle_json": write_bundle_json,
        "ai_provider_conf": {"provider": "openai", "api_key_env": "OPENAI_API_KEY"},
    }
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


def _initialized_app(tmp_path: Path, *, write_bundle_json: bool = True) -> DetectionSummary:
    with patch("detection_summary_app.manager.HAProvisioner") as MockProv:
        mock_prov = AsyncMock()
        mock_prov.ensure_helper.return_value = False
        mock_prov._helper_slug = MagicMock(return_value="garage_detection_summary")
        MockProv.return_value = mock_prov
        with patch("providers.secrets.resolve_secret", return_value="test-key"):
            app = _make_app(tmp_path, write_bundle_json=write_bundle_json)
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


def _sample_cooldown_state(now: float, base: float = 60.0, effective: float = 60.0) -> dict:
    return {
        "base_cooldown_s": base,
        "effective_cooldown_s": effective,
        "backoff_increments": 0,
        "finalized_at_epoch": now,
        "cooldown_expires_at_epoch": now + effective,
        "cooldown_expires_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + effective)),
    }


class TestPatchSummaryJsonCooldown:
    """Direct tests for _patch_summary_json_cooldown."""

    def test_patch_writes_cooldown_state(self, tmp_path: Path):
        app = _initialized_app(tmp_path)
        run_id = "run-patch-test"
        run_dir = _make_run_dir(tmp_path, run_id)
        _write_summary_json(run_dir, {"summary": {"text": "hello"}})

        now = 1000.0
        cooldown_state = _sample_cooldown_state(now, base=60.0, effective=60.0)
        app._patch_summary_json_cooldown(run_id, cooldown_state)

        result = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        assert "cooldown_state" in result
        cs = result["cooldown_state"]
        assert cs["base_cooldown_s"] == 60.0
        assert cs["effective_cooldown_s"] == 60.0
        assert cs["backoff_increments"] == 0
        assert cs["finalized_at_epoch"] == now
        assert cs["cooldown_expires_at_epoch"] == now + 60.0
        assert cs["cooldown_expires_at_utc"] == time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 60.0))
        # Original data preserved
        assert result.get("summary") == {"text": "hello"}

    def test_patch_noop_when_disabled(self, tmp_path: Path):
        app = _initialized_app(tmp_path, write_bundle_json=False)
        run_id = "run-noop"
        run_dir = _make_run_dir(tmp_path, run_id)
        original = {"summary": {"text": "original"}}
        p = _write_summary_json(run_dir, original)
        original_mtime = p.stat().st_mtime

        cooldown_state = _sample_cooldown_state(1000.0)
        app._patch_summary_json_cooldown(run_id, cooldown_state)

        # File should be unchanged
        assert p.stat().st_mtime == original_mtime
        result = json.loads(p.read_text(encoding="utf-8"))
        assert "cooldown_state" not in result

    def test_patch_warns_when_file_missing(self, tmp_path: Path):
        app = _initialized_app(tmp_path)
        run_id = "run-missing"
        # Do NOT create the run directory or summary.json

        cooldown_state = _sample_cooldown_state(1000.0)
        # Must not raise
        app._patch_summary_json_cooldown(run_id, cooldown_state)

        warning_calls = [
            c for c in app.log.mock_calls
            if c.kwargs.get("level") == "WARNING"
        ]
        assert warning_calls, "Expected a WARNING log when summary.json is missing"
        combined = " ".join(str(c) for c in warning_calls)
        assert run_id in combined

    def test_patch_warns_on_malformed_json(self, tmp_path: Path):
        app = _initialized_app(tmp_path)
        run_id = "run-malformed"
        run_dir = _make_run_dir(tmp_path, run_id)
        p = run_dir / "summary.json"
        p.write_text("THIS IS NOT VALID JSON{{{{", encoding="utf-8")

        cooldown_state = _sample_cooldown_state(1000.0)
        # Must not raise
        app._patch_summary_json_cooldown(run_id, cooldown_state)

        warning_calls = [
            c for c in app.log.mock_calls
            if c.kwargs.get("level") == "WARNING"
        ]
        assert warning_calls, "Expected a WARNING log for malformed JSON"


class TestBackoffIncrementsValues:
    """Verify backoff_increments formula."""

    def _increments(self, new_cd: float, base: float) -> int:
        import math
        return max(0, round(math.log2(new_cd / float(base)))) if new_cd > float(base) else 0

    def test_base_cooldown_zero_increments(self):
        assert self._increments(60.0, 60.0) == 0

    def test_double_base_one_increment(self):
        assert self._increments(120.0, 60.0) == 1

    def test_quadruple_base_two_increments(self):
        assert self._increments(240.0, 60.0) == 2

    def test_patch_writes_correct_backoff_increments(self, tmp_path: Path):
        app = _initialized_app(tmp_path)
        run_id = "run-backoff-verify"
        run_dir = _make_run_dir(tmp_path, run_id)
        _write_summary_json(run_dir, {"summary": {}})

        # Simulate 2× backoff
        now = 2000.0
        cooldown_state = {
            "base_cooldown_s": 60.0,
            "effective_cooldown_s": 120.0,
            "backoff_increments": 1,  # 2× base → 1 increment
            "finalized_at_epoch": now,
            "cooldown_expires_at_epoch": now + 120.0,
            "cooldown_expires_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 120.0)),
        }
        app._patch_summary_json_cooldown(run_id, cooldown_state)

        result = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        assert result["cooldown_state"]["backoff_increments"] == 1


class TestFinalizeWritesCooldownStateIntegration:
    """Integration: _finalize writes cooldown_state to summary.json."""

    def test_finalize_writes_cooldown_state_integration(self, tmp_path: Path):
        app = _initialized_app(tmp_path)

        run_id = "run-finalize-integration"
        run_dir = _make_run_dir(tmp_path, run_id)
        _write_summary_json(run_dir, {"summary": {"text": "test run"}})

        app._last_image_generated_ts = 0.0
        app._effective_cooldown_s = 60.0
        app._active = MagicMock()
        app._active.capture.run_id = run_id
        app._active.capture.started_ts = 1000.0
        app._active.bundle = {"bundle": "data", "created_at_epoch": 1000.0}

        with patch("detection_summary_app.manager.DETECTION_SUMMARY_STORE"):
            app._finalize({"run_id": run_id})

        result = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        assert "cooldown_state" in result, "cooldown_state must be written by _finalize"
        cs = result["cooldown_state"]
        assert "base_cooldown_s" in cs
        assert "effective_cooldown_s" in cs
        assert "backoff_increments" in cs
        assert "finalized_at_epoch" in cs
        assert "cooldown_expires_at_epoch" in cs
        assert "cooldown_expires_at_utc" in cs
