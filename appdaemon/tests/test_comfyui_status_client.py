"""Unit tests for ComfyUIStatusClient.

Mocks aiohttp.ClientSession — no real HTTP and no real ComfyUI required.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import aiohttp
import pytest

_repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo_root / "apps"))
sys.path.insert(0, str(_repo_root))

from providers.ai_providers.comfyui.comfyui_status_client import (
    ComfyUIStatusClient,
    ComfyUIStatusError,
)


# ---------------------------------------------------------------------------
# Helpers — fake aiohttp session/response (async context managers)
# ---------------------------------------------------------------------------

class _FakeResponse:
    """Stand-in for aiohttp.ClientResponse usable as ``async with``."""

    def __init__(
        self,
        status: int = 200,
        json_data: object = None,
        json_exc: Exception | None = None,
    ) -> None:
        self.status = status
        self._json_data = json_data
        self._json_exc = json_exc

    async def json(self, content_type: str | None = "application/json"):
        if self._json_exc is not None:
            raise self._json_exc
        return self._json_data

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSession:
    """Stand-in for aiohttp.ClientSession usable as ``async with``."""

    def __init__(self, response: _FakeResponse | None = None, exc: Exception | None = None) -> None:
        self._response = response
        self._exc = exc
        self.get_calls: list[tuple[str, dict]] = []

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def get(self, url: str, **kwargs):
        self.get_calls.append((url, kwargs))
        if self._exc is not None:
            raise self._exc
        return self._response


def _patch_session(session: _FakeSession):
    return patch(
        "providers.ai_providers.comfyui.comfyui_status_client.aiohttp.ClientSession",
        return_value=session,
    )


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Tests — construction
# ---------------------------------------------------------------------------

class TestConstruction:
    def test_base_url_trailing_slash_stripped(self):
        client = ComfyUIStatusClient("http://comfyui.test:8188/")
        assert client.base_url == "http://comfyui.test:8188"

    def test_base_url_without_trailing_slash_unchanged(self):
        client = ComfyUIStatusClient("http://comfyui.test:8188")
        assert client.base_url == "http://comfyui.test:8188"


# ---------------------------------------------------------------------------
# Tests — fetch_queue_remaining happy paths
# ---------------------------------------------------------------------------

class TestFetchQueueRemaining:
    def test_returns_queue_remaining(self):
        client = ComfyUIStatusClient("http://comfyui.test:8188")
        session = _FakeSession(
            response=_FakeResponse(
                status=200, json_data={"exec_info": {"queue_remaining": 3}}
            )
        )

        with _patch_session(session):
            result = _run(client.fetch_queue_remaining())

        assert result == 3
        assert len(session.get_calls) == 1
        url, _ = session.get_calls[0]
        assert url == "http://comfyui.test:8188/prompt"

    def test_zero_queue(self):
        client = ComfyUIStatusClient("http://comfyui.test:8188")
        session = _FakeSession(
            response=_FakeResponse(
                status=200, json_data={"exec_info": {"queue_remaining": 0}}
            )
        )

        with _patch_session(session):
            assert _run(client.fetch_queue_remaining()) == 0

    def test_string_value_coerced_to_int(self):
        client = ComfyUIStatusClient("http://comfyui.test:8188")
        session = _FakeSession(
            response=_FakeResponse(
                status=200, json_data={"exec_info": {"queue_remaining": "3"}}
            )
        )

        with _patch_session(session):
            assert _run(client.fetch_queue_remaining()) == 3

    def test_timeout_config_passed_to_session(self):
        client = ComfyUIStatusClient("http://comfyui.test:8188", timeout_s=5)
        session = _FakeSession(
            response=_FakeResponse(
                status=200, json_data={"exec_info": {"queue_remaining": 1}}
            )
        )

        with _patch_session(session) as mock_session_cls:
            _run(client.fetch_queue_remaining())

        timeout = mock_session_cls.call_args.kwargs["timeout"]
        assert isinstance(timeout, aiohttp.ClientTimeout)
        assert timeout.total == 5


# ---------------------------------------------------------------------------
# Tests — HTTP / transport errors
# ---------------------------------------------------------------------------

class TestTransportErrors:
    def test_non_200_raises_with_status(self):
        client = ComfyUIStatusClient("http://comfyui.test:8188")
        session = _FakeSession(response=_FakeResponse(status=500))

        with _patch_session(session):
            with pytest.raises(ComfyUIStatusError, match="HTTP 500"):
                _run(client.fetch_queue_remaining())

    def test_non_200_not_double_wrapped(self):
        """The HTTP-status error must re-raise as-is, not get re-wrapped
        into the generic 'request failed' message."""
        client = ComfyUIStatusClient("http://comfyui.test:8188")
        session = _FakeSession(response=_FakeResponse(status=503))

        with _patch_session(session):
            with pytest.raises(ComfyUIStatusError) as excinfo:
                _run(client.fetch_queue_remaining())

        assert "request failed" not in str(excinfo.value)
        assert "HTTP 503" in str(excinfo.value)

    def test_connection_error_wrapped(self):
        client = ComfyUIStatusClient("http://comfyui.test:8188")
        session = _FakeSession(exc=aiohttp.ClientError("connection refused"))

        with _patch_session(session):
            with pytest.raises(ComfyUIStatusError, match="request failed"):
                _run(client.fetch_queue_remaining())

    def test_timeout_wrapped(self):
        client = ComfyUIStatusClient("http://comfyui.test:8188")
        session = _FakeSession(exc=asyncio.TimeoutError())

        with _patch_session(session):
            with pytest.raises(ComfyUIStatusError, match="request failed"):
                _run(client.fetch_queue_remaining())

    def test_invalid_json_wrapped(self):
        """A body that fails to parse as JSON raises inside the try block and
        is wrapped as a request failure."""
        client = ComfyUIStatusClient("http://comfyui.test:8188")
        session = _FakeSession(
            response=_FakeResponse(status=200, json_exc=ValueError("not json"))
        )

        with _patch_session(session):
            with pytest.raises(ComfyUIStatusError, match="request failed"):
                _run(client.fetch_queue_remaining())


# ---------------------------------------------------------------------------
# Tests — malformed payloads
# ---------------------------------------------------------------------------

class TestMalformedPayload:
    def _assert_unexpected_shape(self, json_data: object) -> None:
        client = ComfyUIStatusClient("http://comfyui.test:8188")
        session = _FakeSession(response=_FakeResponse(status=200, json_data=json_data))

        with _patch_session(session):
            with pytest.raises(ComfyUIStatusError, match="unexpected payload shape"):
                _run(client.fetch_queue_remaining())

    def test_missing_exec_info(self):
        self._assert_unexpected_shape({"something_else": 1})

    def test_missing_queue_remaining(self):
        self._assert_unexpected_shape({"exec_info": {}})

    def test_payload_not_a_dict(self):
        self._assert_unexpected_shape(["not", "a", "dict"])

    def test_payload_none(self):
        self._assert_unexpected_shape(None)

    def test_exec_info_not_a_dict(self):
        self._assert_unexpected_shape({"exec_info": "busy"})

    def test_queue_remaining_not_coercible(self):
        self._assert_unexpected_shape({"exec_info": {"queue_remaining": "lots"}})
