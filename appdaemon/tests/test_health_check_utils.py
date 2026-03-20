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

from shared.check_utils import http_check, ping_check


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
