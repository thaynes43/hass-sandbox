"""Unit tests for ImmichClient methods that contain non-trivial logic.

Currently exercises ``get_album_assets`` because it owns the pagination
loop introduced for Immich v3.0.0 (PR immich-app/immich#27835 removed
``AlbumResponseDto.assets``; we now call ``POST /api/search/assets``).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from providers.photo_providers.immich_client import ImmichClient


def _make_client() -> ImmichClient:
    """Build a client without opening a real session."""
    return ImmichClient(base_url="http://immich.test", api_key="test-key")


class TestGetAlbumAssets:
    @pytest.mark.asyncio
    async def test_single_page_returns_items(self):
        client = _make_client()
        client._json_request = AsyncMock(
            return_value={
                "assets": {
                    "items": [{"id": "a"}, {"id": "b"}],
                    "nextPage": None,
                }
            }
        )

        assets = await client.get_album_assets("alb-1")

        assert assets == [{"id": "a"}, {"id": "b"}]
        client._json_request.assert_awaited_once_with(
            "POST",
            "/api/search/assets",
            json={"albumIds": ["alb-1"], "page": 1, "size": 1000},
        )

    @pytest.mark.asyncio
    async def test_paginates_until_nextpage_none(self):
        client = _make_client()
        client._json_request = AsyncMock(
            side_effect=[
                {"assets": {"items": [{"id": "a"}, {"id": "b"}], "nextPage": "2"}},
                {"assets": {"items": [{"id": "c"}], "nextPage": None}},
            ]
        )

        assets = await client.get_album_assets("alb-2")

        assert assets == [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        assert client._json_request.await_count == 2
        # Second call should request page=2
        second_call = client._json_request.await_args_list[1]
        assert second_call.kwargs["json"]["page"] == 2

    @pytest.mark.asyncio
    async def test_invalid_nextpage_breaks_loop(self):
        client = _make_client()
        client._json_request = AsyncMock(
            return_value={
                "assets": {
                    "items": [{"id": "a"}],
                    "nextPage": "not-a-number",
                }
            }
        )

        assets = await client.get_album_assets("alb-3")

        # We collect what we got on the first page and stop instead of looping.
        assert assets == [{"id": "a"}]
        client._json_request.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_missing_assets_key_returns_empty(self):
        client = _make_client()
        client._json_request = AsyncMock(return_value={})

        assets = await client.get_album_assets("alb-4")

        assert assets == []

    @pytest.mark.asyncio
    async def test_non_dict_response_returns_empty(self):
        client = _make_client()
        client._json_request = AsyncMock(return_value=["unexpected"])

        assets = await client.get_album_assets("alb-5")

        assert assets == []
