"""Unit tests for ImmichClient methods that contain non-trivial logic.

Currently exercises ``get_album_assets`` because it owns the version
fallback added for Immich v3.0.0.  On v2.x we read ``assets`` directly
from ``GET /api/albums/{id}``; on v3 (PR immich-app/immich#27835 removed
``AlbumResponseDto.assets``) we fall back to paginated
``POST /api/search/metadata``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import aiohttp
import pytest

from providers.photo_providers.immich_client import ImmichClient


def _make_client() -> ImmichClient:
    """Build a client without opening a real session."""
    return ImmichClient(base_url="http://immich.test", api_key="test-key")


def _legacy_album(items):
    return {"id": "alb", "albumName": "x", "assets": items}


def _search_page(items, next_page=None):
    return {"assets": {"items": items, "nextPage": next_page}}


def _http_error(status: int) -> aiohttp.ClientResponseError:
    return aiohttp.ClientResponseError(
        request_info=None, history=(), status=status, message="err"
    )


class TestGetAlbumAssets:
    @pytest.mark.asyncio
    async def test_legacy_v2_returns_assets_field(self):
        """Immich v2: GET /api/albums/{id} returns assets inline; no fallback."""
        client = _make_client()
        client._json_request = AsyncMock(
            return_value=_legacy_album([{"id": "a"}, {"id": "b"}])
        )

        assets = await client.get_album_assets("alb-1")

        assert assets == [{"id": "a"}, {"id": "b"}]
        client._json_request.assert_awaited_once_with("GET", "/api/albums/alb-1")

    @pytest.mark.asyncio
    async def test_v3_falls_back_to_search_when_assets_missing(self):
        """Immich v3: legacy GET returns 200 but no `assets` field — use search."""
        client = _make_client()
        client._json_request = AsyncMock(
            side_effect=[
                {"id": "alb", "albumName": "x"},  # v3 shape: no `assets`
                _search_page([{"id": "a"}, {"id": "b"}], next_page=None),
            ]
        )

        assets = await client.get_album_assets("alb-2")

        assert assets == [{"id": "a"}, {"id": "b"}]
        assert client._json_request.await_count == 2
        first, second = client._json_request.await_args_list
        assert first.args == ("GET", "/api/albums/alb-2")
        assert second.args == ("POST", "/api/search/metadata")
        assert second.kwargs["json"] == {
            "albumIds": ["alb-2"],
            "page": 1,
            "size": 1000,
        }

    @pytest.mark.asyncio
    async def test_legacy_404_falls_back_to_search(self):
        """If the legacy endpoint returns 404, fall back instead of raising."""
        client = _make_client()
        client._json_request = AsyncMock(
            side_effect=[
                _http_error(404),
                _search_page([{"id": "z"}], next_page=None),
            ]
        )

        assets = await client.get_album_assets("alb-3")

        assert assets == [{"id": "z"}]
        assert client._json_request.await_count == 2

    @pytest.mark.asyncio
    async def test_legacy_500_propagates(self):
        """Non-404 errors from the legacy endpoint must bubble up."""
        client = _make_client()
        client._json_request = AsyncMock(side_effect=_http_error(500))

        with pytest.raises(aiohttp.ClientResponseError):
            await client.get_album_assets("alb-4")

    @pytest.mark.asyncio
    async def test_search_paginates_until_nextpage_none(self):
        """Pagination of the search fallback collects items across pages."""
        client = _make_client()
        client._json_request = AsyncMock(
            side_effect=[
                {"id": "alb"},  # legacy returns no assets -> fallback
                _search_page([{"id": "a"}, {"id": "b"}], next_page="2"),
                _search_page([{"id": "c"}], next_page=None),
            ]
        )

        assets = await client.get_album_assets("alb-5")

        assert assets == [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        assert client._json_request.await_count == 3
        third = client._json_request.await_args_list[2]
        assert third.kwargs["json"]["page"] == 2

    @pytest.mark.asyncio
    async def test_search_invalid_nextpage_breaks_loop(self):
        client = _make_client()
        client._json_request = AsyncMock(
            side_effect=[
                {"id": "alb"},
                _search_page([{"id": "a"}], next_page="not-a-number"),
            ]
        )

        assets = await client.get_album_assets("alb-6")

        assert assets == [{"id": "a"}]
        assert client._json_request.await_count == 2

    @pytest.mark.asyncio
    async def test_search_missing_assets_key_returns_empty(self):
        client = _make_client()
        client._json_request = AsyncMock(
            side_effect=[
                {"id": "alb"},
                {},
            ]
        )

        assets = await client.get_album_assets("alb-7")

        assert assets == []

    @pytest.mark.asyncio
    async def test_search_non_dict_response_returns_empty(self):
        client = _make_client()
        client._json_request = AsyncMock(
            side_effect=[
                {"id": "alb"},
                ["unexpected"],
            ]
        )

        assets = await client.get_album_assets("alb-8")

        assert assets == []
