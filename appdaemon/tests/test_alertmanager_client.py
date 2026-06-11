"""Unit tests for AlertmanagerClient.

Mocks aiohttp.ClientSession — no real HTTP and no real Alertmanager required.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import aiohttp
import pytest

_repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo_root / "apps"))
sys.path.insert(0, str(_repo_root))

from providers.alertmanager.alertmanager_client import (
    AlertmanagerClient,
    AlertmanagerError,
)


# ---------------------------------------------------------------------------
# Helpers — fake aiohttp session/response (async context managers)
# ---------------------------------------------------------------------------

class _FakeResponse:
    """Stand-in for aiohttp.ClientResponse usable as ``async with``."""

    def __init__(self, status: int = 200, text_data: str = "") -> None:
        self.status = status
        self._text_data = text_data

    async def text(self) -> str:
        return self._text_data

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSession:
    """Stand-in for aiohttp.ClientSession usable as ``async with``."""

    def __init__(self, response: _FakeResponse | None = None, exc: Exception | None = None) -> None:
        self._response = response
        self._exc = exc
        self.post_calls: list[tuple[str, dict]] = []

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def post(self, url: str, **kwargs):
        self.post_calls.append((url, kwargs))
        if self._exc is not None:
            raise self._exc
        return self._response


def _patch_session(session: _FakeSession):
    return patch(
        "providers.alertmanager.alertmanager_client.aiohttp.ClientSession",
        return_value=session,
    )


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _sample_alerts() -> list[dict]:
    return [
        {
            "labels": {"alertname": "ProtectWebsocketDown", "severity": "critical"},
            "annotations": {"summary": "Protect websocket dead"},
            "startsAt": "2026-06-10T12:00:00+00:00",
        },
        {
            "labels": {"alertname": "ComfyUIQueueStuck", "severity": "warning"},
            "annotations": {"summary": "queue not draining"},
            "startsAt": "2026-06-10T12:00:00+00:00",
            "endsAt": "2026-06-10T12:05:00+00:00",
        },
    ]


# ---------------------------------------------------------------------------
# Tests — construction
# ---------------------------------------------------------------------------

class TestConstruction:
    def test_base_url_trailing_slash_stripped(self):
        client = AlertmanagerClient("http://alertmanager.test:9093/")
        assert client.base_url == "http://alertmanager.test:9093"

    def test_base_url_without_trailing_slash_unchanged(self):
        client = AlertmanagerClient("http://alertmanager.test:9093")
        assert client.base_url == "http://alertmanager.test:9093"


# ---------------------------------------------------------------------------
# Tests — post_alerts
# ---------------------------------------------------------------------------

class TestPostAlerts:
    def test_posts_to_v2_alerts_with_exact_payload(self):
        client = AlertmanagerClient("http://alertmanager.test:9093")
        alerts = _sample_alerts()
        session = _FakeSession(response=_FakeResponse(status=200))

        with _patch_session(session):
            _run(client.post_alerts(alerts))

        assert len(session.post_calls) == 1
        url, kwargs = session.post_calls[0]
        assert url == "http://alertmanager.test:9093/api/v2/alerts"
        assert kwargs == {"json": alerts}

    def test_trailing_slash_base_url_builds_clean_url(self):
        client = AlertmanagerClient("http://alertmanager.test:9093/")
        session = _FakeSession(response=_FakeResponse(status=200))

        with _patch_session(session):
            _run(client.post_alerts(_sample_alerts()))

        url, _ = session.post_calls[0]
        assert url == "http://alertmanager.test:9093/api/v2/alerts"

    def test_empty_list_makes_no_http_call(self):
        client = AlertmanagerClient("http://alertmanager.test:9093")
        session = _FakeSession(response=_FakeResponse(status=200))

        with _patch_session(session) as mock_session_cls:
            _run(client.post_alerts([]))

        mock_session_cls.assert_not_called()
        assert session.post_calls == []

    def test_2xx_non_200_is_success(self):
        client = AlertmanagerClient("http://alertmanager.test:9093")
        session = _FakeSession(response=_FakeResponse(status=201))

        with _patch_session(session):
            _run(client.post_alerts(_sample_alerts()))  # should not raise

    def test_timeout_config_passed_to_session(self):
        client = AlertmanagerClient("http://alertmanager.test:9093", timeout_s=7)
        session = _FakeSession(response=_FakeResponse(status=200))

        with _patch_session(session) as mock_session_cls:
            _run(client.post_alerts(_sample_alerts()))

        timeout = mock_session_cls.call_args.kwargs["timeout"]
        assert isinstance(timeout, aiohttp.ClientTimeout)
        assert timeout.total == 7


# ---------------------------------------------------------------------------
# Tests — error handling
# ---------------------------------------------------------------------------

class TestErrors:
    def test_non_2xx_raises_with_status_in_message(self):
        client = AlertmanagerClient("http://alertmanager.test:9093")
        session = _FakeSession(response=_FakeResponse(status=500, text_data="boom"))

        with _patch_session(session):
            with pytest.raises(AlertmanagerError, match="HTTP 500"):
                _run(client.post_alerts(_sample_alerts()))

    def test_non_2xx_message_includes_body(self):
        client = AlertmanagerClient("http://alertmanager.test:9093")
        session = _FakeSession(
            response=_FakeResponse(status=400, text_data="invalid label set")
        )

        with _patch_session(session):
            with pytest.raises(AlertmanagerError, match="invalid label set"):
                _run(client.post_alerts(_sample_alerts()))

    def test_300_status_is_an_error(self):
        client = AlertmanagerClient("http://alertmanager.test:9093")
        session = _FakeSession(response=_FakeResponse(status=300))

        with _patch_session(session):
            with pytest.raises(AlertmanagerError, match="HTTP 300"):
                _run(client.post_alerts(_sample_alerts()))

    def test_http_error_not_double_wrapped(self):
        """The HTTP-status AlertmanagerError must re-raise as-is, not get
        re-wrapped into the generic 'POST failed' message."""
        client = AlertmanagerClient("http://alertmanager.test:9093")
        session = _FakeSession(response=_FakeResponse(status=503, text_data=""))

        with _patch_session(session):
            with pytest.raises(AlertmanagerError) as excinfo:
                _run(client.post_alerts(_sample_alerts()))

        assert "POST failed" not in str(excinfo.value)
        assert "HTTP 503" in str(excinfo.value)

    def test_connection_error_wrapped(self):
        client = AlertmanagerClient("http://alertmanager.test:9093")
        session = _FakeSession(exc=aiohttp.ClientError("connection refused"))

        with _patch_session(session):
            with pytest.raises(AlertmanagerError, match="POST failed"):
                _run(client.post_alerts(_sample_alerts()))

    def test_connection_error_chains_cause(self):
        client = AlertmanagerClient("http://alertmanager.test:9093")
        original = aiohttp.ClientError("connection refused")
        session = _FakeSession(exc=original)

        with _patch_session(session):
            with pytest.raises(AlertmanagerError) as excinfo:
                _run(client.post_alerts(_sample_alerts()))

        assert excinfo.value.__cause__ is original

    def test_timeout_wrapped(self):
        client = AlertmanagerClient("http://alertmanager.test:9093")
        session = _FakeSession(exc=asyncio.TimeoutError())

        with _patch_session(session):
            with pytest.raises(AlertmanagerError, match="POST failed"):
                _run(client.post_alerts(_sample_alerts()))
