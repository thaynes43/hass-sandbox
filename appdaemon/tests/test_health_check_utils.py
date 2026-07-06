"""Unit tests for health_checks.shared.check_utils.

Mocks asyncio subprocesses and aiohttp — no real network access required.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add the health_checks package to sys.path so shared/ is importable
_apps_root = Path(__file__).resolve().parent.parent / "apps"
sys.path.insert(0, str(_apps_root / "health_checks"))

from shared.check_utils import (
    apply_cross_check,
    apply_cross_check_per_device,
    http_check,
    is_implausible_battery_drop,
    ping_check,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    """Run a coroutine in a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_process(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""):
    """Create a mock asyncio.Process."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


# ---------------------------------------------------------------------------
# ping_check tests
# ---------------------------------------------------------------------------

class TestPingCheck:
    def test_successful_ping(self):
        """ping returning 0 should yield ok status."""
        proc = _make_process(returncode=0)
        with patch("shared.check_utils.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc):
            result = _run(ping_check("example.com", timeout_s=2))
        assert result["status"] == "ok"
        assert "ms" in result["detail"]

    def test_failed_ping(self):
        """ping returning non-zero should yield critical status."""
        proc = _make_process(returncode=1, stderr=b"Request timeout")
        with patch("shared.check_utils.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc):
            result = _run(ping_check("bad-host.local", timeout_s=2))
        assert result["status"] == "critical"
        assert result["detail"] == "timeout"

    def test_ping_timeout(self):
        """asyncio.TimeoutError should yield critical status."""
        async def _raise(*a, **kw):
            raise asyncio.TimeoutError()

        with patch("shared.check_utils.asyncio.create_subprocess_exec", new_callable=AsyncMock, side_effect=_raise):
            result = _run(ping_check("slow-host.local", timeout_s=1))
        assert result["status"] == "critical"
        assert "timeout" in result["detail"]

    def test_ping_exception(self):
        """Unexpected exceptions should yield critical status."""
        async def _raise(*a, **kw):
            raise OSError("no route to host")

        with patch("shared.check_utils.asyncio.create_subprocess_exec", new_callable=AsyncMock, side_effect=_raise):
            result = _run(ping_check("unreachable.local"))
        assert result["status"] == "critical"
        assert "ping failed" in result["detail"]

    def test_macos_uses_t_flag(self):
        """On macOS, ping should use -t for timeout."""
        proc = _make_process(returncode=0)
        with patch("shared.check_utils.sys") as mock_sys, \
             patch("shared.check_utils.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc) as mock_exec:
            mock_sys.platform = "darwin"
            _run(ping_check("example.com", timeout_s=3))
            args = mock_exec.call_args[0]
            assert "-t" in args
            assert "3" in args

    def test_linux_uses_W_flag(self):
        """On Linux, ping should use -W for timeout."""
        proc = _make_process(returncode=0)
        with patch("shared.check_utils.sys") as mock_sys, \
             patch("shared.check_utils.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc) as mock_exec:
            mock_sys.platform = "linux"
            _run(ping_check("example.com", timeout_s=3))
            args = mock_exec.call_args[0]
            assert "-W" in args
            assert "3" in args


# ---------------------------------------------------------------------------
# http_check tests
# ---------------------------------------------------------------------------

class TestHttpCheck:
    def test_http_200(self):
        """HTTP 200 should yield ok status."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch("shared.check_utils.aiohttp.ClientSession", return_value=mock_session):
            result = _run(http_check("https://example.com"))
        assert result["status"] == "ok"
        assert "200" in result["detail"]

    def test_http_503(self):
        """HTTP 503 should yield critical status."""
        mock_resp = MagicMock()
        mock_resp.status = 503
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch("shared.check_utils.aiohttp.ClientSession", return_value=mock_session):
            result = _run(http_check("https://down.example.com"))
        assert result["status"] == "critical"
        assert "503" in result["detail"]

    def test_http_timeout(self):
        """Connection timeout should yield critical status."""
        mock_session = MagicMock()
        mock_session.get = MagicMock(side_effect=asyncio.TimeoutError())
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch("shared.check_utils.aiohttp.ClientSession", return_value=mock_session):
            result = _run(http_check("https://slow.example.com", timeout_s=1))
        assert result["status"] == "critical"
        assert "timeout" in result["detail"]

    def test_http_connection_error(self):
        """Connection error should yield critical status."""
        import aiohttp

        mock_session = MagicMock()
        mock_session.get = MagicMock(
            side_effect=aiohttp.ClientError("Connection refused")
        )
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with patch("shared.check_utils.aiohttp.ClientSession", return_value=mock_session):
            result = _run(http_check("https://refused.example.com"))
        assert result["status"] == "critical"
        assert "Connection error" in result["detail"]


# ---------------------------------------------------------------------------
# apply_cross_check tests
# ---------------------------------------------------------------------------

class TestApplyCrossCheck:
    def test_all_ok_no_change(self):
        """All ok results should remain ok."""
        results = [
            {"name": "Ping", "status": "ok", "detail": "2ms"},
            {"name": "Entity", "status": "ok", "detail": "on"},
        ]
        apply_cross_check(results)
        assert all(r["status"] == "ok" for r in results)

    def test_all_critical_stays_critical(self):
        """All critical results should stay critical (device truly down)."""
        results = [
            {"name": "Ping", "status": "critical", "detail": "timeout"},
            {"name": "Entity", "status": "critical", "detail": "unavailable"},
        ]
        apply_cross_check(results)
        assert all(r["status"] == "critical" for r in results)

    def test_partial_failure_downgrades_to_warning(self):
        """One critical + one ok = critical downgraded to warning."""
        results = [
            {"name": "Ping", "status": "ok", "detail": "2ms"},
            {"name": "Entity", "status": "critical", "detail": "unavailable"},
        ]
        apply_cross_check(results)
        assert results[0]["status"] == "ok"
        assert results[1]["status"] == "warning"
        assert "partial failure" in results[1]["detail"]

    def test_unknown_treated_as_bad(self):
        """unknown + critical = all bad, stays critical."""
        results = [
            {"name": "Ping", "status": "unknown", "detail": "no data"},
            {"name": "Entity", "status": "critical", "detail": "unavailable"},
        ]
        apply_cross_check(results)
        assert results[0]["status"] == "unknown"
        assert results[1]["status"] == "critical"

    def test_unknown_plus_ok_downgrades_critical(self):
        """unknown + ok + critical = not all bad, critical→warning."""
        results = [
            {"name": "A", "status": "ok", "detail": "fine"},
            {"name": "B", "status": "unknown", "detail": "no data"},
            {"name": "C", "status": "critical", "detail": "down"},
        ]
        apply_cross_check(results)
        assert results[0]["status"] == "ok"
        assert results[1]["status"] == "unknown"  # unchanged
        assert results[2]["status"] == "warning"

    def test_single_result_unchanged(self):
        """A single-check result should not be modified."""
        results = [{"name": "Only", "status": "critical", "detail": "down"}]
        apply_cross_check(results)
        assert results[0]["status"] == "critical"

    def test_empty_results_no_error(self):
        """Empty list should not raise."""
        apply_cross_check([])


class TestApplyCrossCheckPerDevice:
    def test_per_device_isolation(self):
        """Cross-check should be applied independently per device."""
        results = [
            # Device A: ping ok, entity critical → entity becomes warning
            {"name": "DevA Ping", "status": "ok", "detail": "2ms"},
            {"name": "DevA Entity", "status": "critical", "detail": "unavailable"},
            # Device B: both critical → stays critical
            {"name": "DevB Ping", "status": "critical", "detail": "timeout"},
            {"name": "DevB Entity", "status": "critical", "detail": "unavailable"},
        ]
        apply_cross_check_per_device(results, ["DevA", "DevB"])
        assert results[0]["status"] == "ok"
        assert results[1]["status"] == "warning"  # downgraded
        assert results[2]["status"] == "critical"  # stayed
        assert results[3]["status"] == "critical"  # stayed

    def test_all_devices_ok(self):
        """All ok across devices should remain ok."""
        results = [
            {"name": "X Ping", "status": "ok", "detail": "1ms"},
            {"name": "X State", "status": "ok", "detail": "on"},
            {"name": "Y Ping", "status": "ok", "detail": "2ms"},
            {"name": "Y State", "status": "ok", "detail": "on"},
        ]
        apply_cross_check_per_device(results, ["X", "Y"])
        assert all(r["status"] == "ok" for r in results)

    def test_device_with_single_check_unchanged(self):
        """A device with only one check should not be modified."""
        results = [
            {"name": "Solo State", "status": "critical", "detail": "down"},
            {"name": "Duo Ping", "status": "ok", "detail": "1ms"},
            {"name": "Duo State", "status": "critical", "detail": "down"},
        ]
        apply_cross_check_per_device(results, ["Solo", "Duo"])
        assert results[0]["status"] == "critical"  # single check, unchanged
        assert results[1]["status"] == "ok"
        assert results[2]["status"] == "warning"  # downgraded (Duo has ok ping)


# ---------------------------------------------------------------------------
# is_implausible_battery_drop tests
# ---------------------------------------------------------------------------

class TestIsImplausibleBatteryDrop:
    def test_impossible_drop_flagged(self):
        """100% -> 0% is the PowerView disconnect signature: implausible."""
        assert is_implausible_battery_drop(100, 0, healthy_floor=40, low_threshold=5) is True

    def test_gradual_decline_not_flagged(self):
        """8% -> 0% with a low prior baseline is a real dying battery, not a disconnect."""
        assert is_implausible_battery_drop(8, 0, healthy_floor=40, low_threshold=5) is False

    def test_no_baseline_not_flagged(self):
        """No prior healthy reading (cold start) never counts as implausible."""
        assert is_implausible_battery_drop(None, 0, healthy_floor=40, low_threshold=5) is False

    def test_current_value_above_threshold_not_flagged(self):
        """A current reading above low_threshold is never a 'drop to zero'."""
        assert is_implausible_battery_drop(100, 25, healthy_floor=40, low_threshold=5) is False

    def test_baseline_exactly_at_healthy_floor_flagged(self):
        """Boundary: prev == healthy_floor counts as healthy (>=)."""
        assert is_implausible_battery_drop(40, 5, healthy_floor=40, low_threshold=5) is True

    def test_baseline_just_below_healthy_floor_not_flagged(self):
        """Boundary: prev just under healthy_floor is treated as a low baseline."""
        assert is_implausible_battery_drop(39, 5, healthy_floor=40, low_threshold=5) is False

    def test_current_exactly_at_low_threshold_flagged(self):
        """Boundary: curr == low_threshold counts as a zero-ish reading (<=)."""
        assert is_implausible_battery_drop(100, 5, healthy_floor=40, low_threshold=5) is True

    def test_current_just_above_low_threshold_not_flagged(self):
        """Boundary: curr just over low_threshold is not zero-ish."""
        assert is_implausible_battery_drop(100, 6, healthy_floor=40, low_threshold=5) is False

    def test_mid_episode_re_drop_still_flagged(self):
        """A shade that bounced back to 100% and drops again still qualifies —
        this is what keeps a flapping episode alive rather than clearing it."""
        assert is_implausible_battery_drop(100, 0, healthy_floor=40, low_threshold=5) is True
