"""Unit tests for VestaboardClient."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers.vestaboard.vestaboard_client import VestaboardClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_GRID = [[0] * 22 for _ in range(6)]
_FAKE_IP = "192.168.1.100"
_FAKE_KEY = "test-key"


def _make_mock_response(status: int, json_data=None) -> MagicMock:
    """Build a mock aiohttp response usable as an async context manager."""
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _make_mock_session() -> MagicMock:
    """Build a mock aiohttp.ClientSession."""
    session = MagicMock()
    session.closed = False
    session.close = AsyncMock()
    return session


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------


class TestVestaboardClientInit:
    def test_base_url_built_from_ip(self):
        client = VestaboardClient(_FAKE_IP, _FAKE_KEY, session=MagicMock())
        assert client._base_url == f"http://{_FAKE_IP}:7000"

    def test_headers_contain_api_key(self):
        client = VestaboardClient(_FAKE_IP, _FAKE_KEY, session=MagicMock())
        headers = client._headers()
        assert headers["X-Vestaboard-Local-Api-Key"] == _FAKE_KEY
        assert headers["Content-Type"] == "application/json"

    def test_no_session_raises_on_property_access(self):
        client = VestaboardClient(_FAKE_IP, _FAKE_KEY, session=None)
        # Before entering context manager, accessing _active_session should raise
        with pytest.raises(RuntimeError, match="no active session"):
            _ = client._active_session


# ---------------------------------------------------------------------------
# write_frame
# ---------------------------------------------------------------------------


class TestWriteFrame:
    @pytest.mark.asyncio
    async def test_posts_to_correct_url(self):
        session = _make_mock_session()
        resp = _make_mock_response(200)
        session.post = MagicMock(return_value=resp)

        client = VestaboardClient(_FAKE_IP, _FAKE_KEY, session=session)
        await client.write_frame(_FAKE_GRID)

        call_args = session.post.call_args
        assert call_args[0][0] == f"http://{_FAKE_IP}:7000/local-api/message"

    @pytest.mark.asyncio
    async def test_sends_correct_headers(self):
        session = _make_mock_session()
        resp = _make_mock_response(200)
        session.post = MagicMock(return_value=resp)

        client = VestaboardClient(_FAKE_IP, _FAKE_KEY, session=session)
        await client.write_frame(_FAKE_GRID)

        headers = session.post.call_args[1]["headers"]
        assert headers["X-Vestaboard-Local-Api-Key"] == _FAKE_KEY

    @pytest.mark.asyncio
    async def test_sends_grid_as_json_body(self):
        session = _make_mock_session()
        resp = _make_mock_response(200)
        session.post = MagicMock(return_value=resp)

        client = VestaboardClient(_FAKE_IP, _FAKE_KEY, session=session)
        await client.write_frame(_FAKE_GRID)

        sent_json = session.post.call_args[1]["json"]
        assert sent_json == _FAKE_GRID

    @pytest.mark.asyncio
    async def test_returns_true_on_200(self):
        session = _make_mock_session()
        session.post = MagicMock(return_value=_make_mock_response(200))

        client = VestaboardClient(_FAKE_IP, _FAKE_KEY, session=session)
        result = await client.write_frame(_FAKE_GRID)

        assert result is True

    @pytest.mark.asyncio
    async def test_returns_true_on_204(self):
        session = _make_mock_session()
        session.post = MagicMock(return_value=_make_mock_response(204))

        client = VestaboardClient(_FAKE_IP, _FAKE_KEY, session=session)
        result = await client.write_frame(_FAKE_GRID)

        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_on_400(self):
        session = _make_mock_session()
        session.post = MagicMock(return_value=_make_mock_response(400))

        client = VestaboardClient(_FAKE_IP, _FAKE_KEY, session=session)
        result = await client.write_frame(_FAKE_GRID)

        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_500(self):
        session = _make_mock_session()
        session.post = MagicMock(return_value=_make_mock_response(500))

        client = VestaboardClient(_FAKE_IP, _FAKE_KEY, session=session)
        result = await client.write_frame(_FAKE_GRID)

        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_exception(self):
        session = _make_mock_session()
        session.post = MagicMock(side_effect=Exception("connection refused"))

        client = VestaboardClient(_FAKE_IP, _FAKE_KEY, session=session)
        result = await client.write_frame(_FAKE_GRID)

        assert result is False


# ---------------------------------------------------------------------------
# read_current
# ---------------------------------------------------------------------------


class TestReadCurrent:
    @pytest.mark.asyncio
    async def test_gets_correct_url(self):
        session = _make_mock_session()
        resp = _make_mock_response(200, json_data=_FAKE_GRID)
        session.get = MagicMock(return_value=resp)

        client = VestaboardClient(_FAKE_IP, _FAKE_KEY, session=session)
        await client.read_current()

        call_args = session.get.call_args
        assert call_args[0][0] == f"http://{_FAKE_IP}:7000/local-api/message"

    @pytest.mark.asyncio
    async def test_sends_correct_headers(self):
        session = _make_mock_session()
        resp = _make_mock_response(200, json_data=_FAKE_GRID)
        session.get = MagicMock(return_value=resp)

        client = VestaboardClient(_FAKE_IP, _FAKE_KEY, session=session)
        await client.read_current()

        headers = session.get.call_args[1]["headers"]
        assert headers["X-Vestaboard-Local-Api-Key"] == _FAKE_KEY

    @pytest.mark.asyncio
    async def test_returns_parsed_grid_on_200(self):
        session = _make_mock_session()
        resp = _make_mock_response(200, json_data=_FAKE_GRID)
        session.get = MagicMock(return_value=resp)

        client = VestaboardClient(_FAKE_IP, _FAKE_KEY, session=session)
        result = await client.read_current()

        assert result == _FAKE_GRID

    @pytest.mark.asyncio
    async def test_returns_none_on_404(self):
        session = _make_mock_session()
        session.get = MagicMock(return_value=_make_mock_response(404))

        client = VestaboardClient(_FAKE_IP, _FAKE_KEY, session=session)
        result = await client.read_current()

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_500(self):
        session = _make_mock_session()
        session.get = MagicMock(return_value=_make_mock_response(500))

        client = VestaboardClient(_FAKE_IP, _FAKE_KEY, session=session)
        result = await client.read_current()

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_exception(self):
        session = _make_mock_session()
        session.get = MagicMock(side_effect=Exception("timeout"))

        client = VestaboardClient(_FAKE_IP, _FAKE_KEY, session=session)
        result = await client.read_current()

        assert result is None


# ---------------------------------------------------------------------------
# Async context manager
# ---------------------------------------------------------------------------


class TestAsyncContextManager:
    @pytest.mark.asyncio
    async def test_creates_session_when_none_provided(self):
        mock_session = _make_mock_session()
        with patch(
            "providers.vestaboard.vestaboard_client.aiohttp.ClientSession",
            return_value=mock_session,
        ):
            client = VestaboardClient(_FAKE_IP, _FAKE_KEY)
            async with client:
                assert client._session is mock_session
                assert client._owns_session is True

    @pytest.mark.asyncio
    async def test_closes_owned_session_on_exit(self):
        mock_session = _make_mock_session()
        with patch(
            "providers.vestaboard.vestaboard_client.aiohttp.ClientSession",
            return_value=mock_session,
        ):
            client = VestaboardClient(_FAKE_IP, _FAKE_KEY)
            async with client:
                pass
            mock_session.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_does_not_close_injected_session(self):
        mock_session = _make_mock_session()
        client = VestaboardClient(_FAKE_IP, _FAKE_KEY, session=mock_session)
        async with client:
            pass
        mock_session.close.assert_not_awaited()
