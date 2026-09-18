"""Tests for providers.ha_provisioner.local_file_check.

The helper is a "is HA serving this file yet?" probe used by
``photo_frame_viewer`` to verify a staged generation.  Its contract:

* HTTP 200 -> True; anything else (404, redirect, error, timeout) -> False
* it never raises
* it never sends an ``Authorization`` header (``/local/...`` is
  unauthenticated static content — security policy S3/S6)
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

import aiohttp
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers.ha_provisioner.local_file_check import (  # noqa: E402
    build_local_url,
    local_file_exists,
)


def _run(coro):
    return asyncio.run(coro)


def _session(status: int | None = None, *, error: Exception | None = None) -> MagicMock:
    """A fake aiohttp session whose ``head()`` is an async context manager."""
    session = MagicMock()
    if error is not None:
        session.head = MagicMock(side_effect=error)
        return session
    resp = MagicMock()
    resp.status = status
    session.head.return_value.__aenter__.return_value = resp
    return session


class TestBuildLocalUrl:
    def test_joins_base_and_path(self):
        assert (
            build_local_url("http://ha.test:8123/", "/local/photo-frame/live/7/a.jpg")
            == "http://ha.test:8123/local/photo-frame/live/7/a.jpg"
        )

    def test_adds_missing_leading_slash(self):
        assert build_local_url("http://ha.test:8123", "local/x.jpg") == (
            "http://ha.test:8123/local/x.jpg"
        )

    def test_empty_inputs_yield_empty_string(self):
        assert build_local_url("", "/local/x.jpg") == ""
        assert build_local_url("http://ha.test:8123", "") == ""


class TestLocalFileExists:
    def test_200_is_true(self):
        session = _session(200)
        assert _run(
            local_file_exists("http://ha.test:8123", "/local/p/1/a.jpg", session=session)
        ) is True
        session.head.assert_called_once()

    def test_404_is_false(self):
        session = _session(404)
        assert _run(
            local_file_exists("http://ha.test:8123", "/local/p/1/a.jpg", session=session)
        ) is False

    @pytest.mark.parametrize("status", [204, 301, 302, 403, 500])
    def test_non_200_statuses_are_false(self, status):
        session = _session(status)
        assert _run(
            local_file_exists("http://ha.test:8123", "/local/p/1/a.jpg", session=session)
        ) is False

    def test_connection_error_is_false_and_does_not_raise(self):
        session = _session(error=aiohttp.ClientConnectorError(MagicMock(), OSError("boom")))
        assert _run(
            local_file_exists("http://ha.test:8123", "/local/p/1/a.jpg", session=session)
        ) is False

    def test_timeout_is_false_and_does_not_raise(self):
        session = _session(error=asyncio.TimeoutError())
        assert _run(
            local_file_exists("http://ha.test:8123", "/local/p/1/a.jpg", session=session)
        ) is False

    def test_unexpected_exception_is_false(self):
        session = _session(error=RuntimeError("unexpected"))
        assert _run(
            local_file_exists("http://ha.test:8123", "/local/p/1/a.jpg", session=session)
        ) is False

    def test_no_ha_url_is_false_without_touching_the_session(self):
        session = _session(200)
        assert _run(local_file_exists("", "/local/p/1/a.jpg", session=session)) is False
        session.head.assert_not_called()

    def test_sends_no_authorization_header(self):
        """The HA token must never ride along on a /local/... request."""
        session = _session(200)
        _run(local_file_exists("http://ha.test:8123", "/local/p/1/a.jpg", session=session))

        kwargs = session.head.call_args.kwargs
        assert "headers" not in kwargs, (
            f"local_file_exists must send no headers at all; got {kwargs.get('headers')!r}"
        )
        rendered = repr(session.head.call_args)
        assert "Authorization" not in rendered
        assert "Bearer" not in rendered

    def test_does_not_follow_redirects(self):
        """A 302 to the HA login page must not be mistaken for a served file."""
        session = _session(302)
        _run(local_file_exists("http://ha.test:8123", "/local/p/1/a.jpg", session=session))
        assert session.head.call_args.kwargs.get("allow_redirects") is False

    def test_timeout_seconds_are_applied(self):
        session = _session(200)
        _run(
            local_file_exists(
                "http://ha.test:8123", "/local/p/1/a.jpg", timeout_s=2.5, session=session
            )
        )
        timeout = session.head.call_args.kwargs.get("timeout")
        assert isinstance(timeout, aiohttp.ClientTimeout)
        assert timeout.total == 2.5

    @pytest.mark.parametrize("bad", [0, -1, None, "nope"])
    def test_bad_timeout_falls_back_to_default(self, bad):
        session = _session(200)
        _run(
            local_file_exists(
                "http://ha.test:8123", "/local/p/1/a.jpg", timeout_s=bad, session=session
            )
        )
        assert session.head.call_args.kwargs["timeout"].total == 5.0
