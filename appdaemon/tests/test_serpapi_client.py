"""Unit tests for SerpApiClient.

Mocks the aiohttp session — no real HTTP and no SerpApi quota is spent.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict

import pytest

from providers.media_providers.serpapi_client import SerpApiClient


# ---------------------------------------------------------------------------
# Helpers — fake aiohttp session/response (async context managers)
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status: int = 200, json_data: Any = None) -> None:
        self.status = status
        self._json_data = json_data if json_data is not None else {"showtimes": []}
        self.raise_called = False

    async def json(self) -> Any:
        return self._json_data

    def raise_for_status(self) -> None:
        self.raise_called = True

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSession:
    def __init__(self, response: _FakeResponse | None = None) -> None:
        self._response = response or _FakeResponse()
        self.get_calls: list[tuple[str, Dict[str, str]]] = []
        self.closed = False

    def get(self, url: str, params: Dict[str, str] | None = None):
        self.get_calls.append((url, dict(params or {})))
        return self._response

    async def close(self) -> None:
        self.closed = True


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# TestGetShowtimes
# ---------------------------------------------------------------------------

class TestGetShowtimes:
    def test_pins_the_result_locale(self):
        """Without hl/gl/google_domain the exit node picks the language.

        On 2026-09-22 one theater's entire film list came back in Lithuanian,
        which matches no TMDb title and whose day labels parse as nothing.
        """
        session = _FakeSession()
        client = SerpApiClient(api_key="test-key", session=session)

        _run(client.get_showtimes(query="AMC Methuen 20 showtimes"))

        url, params = session.get_calls[0]
        assert url.endswith("/search")
        assert params["hl"] == "en"
        assert params["gl"] == "us"
        assert params["google_domain"] == "google.com"

    def test_keeps_engine_query_and_key(self):
        session = _FakeSession()
        client = SerpApiClient(api_key="test-key", session=session)

        _run(client.get_showtimes(query="AMC Methuen 20 showtimes"))

        _url, params = session.get_calls[0]
        assert params["engine"] == "google"
        assert params["q"] == "AMC Methuen 20 showtimes"
        assert params["api_key"] == "test-key"

    def test_location_omitted_when_empty(self):
        session = _FakeSession()
        client = SerpApiClient(api_key="test-key", session=session)

        _run(client.get_showtimes(query="showtimes"))

        _url, params = session.get_calls[0]
        assert "location" not in params

    def test_location_passed_through_when_given(self):
        session = _FakeSession()
        client = SerpApiClient(api_key="test-key", session=session)

        _run(
            client.get_showtimes(
                query="showtimes", location="Boston, Massachusetts, United States"
            )
        )

        _url, params = session.get_calls[0]
        assert params["location"] == "Boston, Massachusetts, United States"

    def test_returns_parsed_body_and_checks_status(self):
        payload = {"showtimes": [{"day": "Today", "movies": []}]}
        response = _FakeResponse(json_data=payload)
        session = _FakeSession(response)
        client = SerpApiClient(api_key="test-key", session=session)

        result = _run(client.get_showtimes(query="showtimes"))

        assert result == payload
        assert response.raise_called is True

    def test_does_not_close_a_borrowed_session(self):
        session = _FakeSession()
        client = SerpApiClient(api_key="test-key", session=session)

        _run(client.close())

        assert session.closed is False


class TestSessionGuard:
    def test_no_session_raises(self):
        client = SerpApiClient(api_key="test-key")
        with pytest.raises(RuntimeError):
            _ = client.session
