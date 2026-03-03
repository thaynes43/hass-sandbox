"""Unit tests for ImmichDataProvider."""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from photo_providers.immich_data_provider import ImmichDataProvider
from photo_providers.types import LocationAlias, PhotoAlbum, PhotoFilter, PhotoMetadata, PhotoPerson


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_client() -> MagicMock:
    """Create a mock ImmichClient that works as async context manager."""
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


# ---------------------------------------------------------------------------
# refresh_metadata
# ---------------------------------------------------------------------------

class TestRefreshMetadata:
    @pytest.mark.asyncio
    async def test_builds_photo_metadata_ranked_by_asset_count(self):
        raw_people = [
            {"id": "uuid-bob", "name": "Bob", "assetCount": 5},
            {"id": "uuid-alice", "name": "Alice", "assetCount": 50},
            {"id": "uuid-charlie", "name": "Charlie", "assetCount": 200},
        ]
        raw_albums = [
            {"id": "alb-1", "albumName": "Small", "assetCount": 10},
            {"id": "alb-2", "albumName": "Big", "assetCount": 100},
        ]
        mock_client = _make_mock_client()
        mock_client.get_people = AsyncMock(return_value=raw_people)
        mock_client.get_albums = AsyncMock(return_value=raw_albums)

        with patch(
            "photo_providers.immich_data_provider.ImmichClient",
            return_value=mock_client,
        ):
            provider = ImmichDataProvider("https://immich.test", "key")
            metadata = await provider.refresh_metadata()

        assert len(metadata.people) == 3
        assert metadata.people[0].name == "Charlie"
        assert metadata.people[0].asset_count == 200
        assert metadata.people[1].name == "Alice"
        assert metadata.people[1].asset_count == 50
        assert metadata.people[2].name == "Bob"
        assert metadata.people[2].asset_count == 5

        assert len(metadata.albums) == 2
        assert metadata.albums[0].name == "Big"
        assert metadata.albums[0].asset_count == 100
        assert metadata.albums[1].name == "Small"
        assert metadata.albums[1].asset_count == 10

    @pytest.mark.asyncio
    async def test_populates_people_map_cache(self):
        raw_people = [
            {"id": "uuid-alice", "name": "Alice", "assetCount": 10},
            {"id": "uuid-bob", "name": "Bob", "assetCount": 5},
        ]
        mock_client = _make_mock_client()
        mock_client.get_people = AsyncMock(return_value=raw_people)
        mock_client.get_albums = AsyncMock(return_value=[])

        with patch(
            "photo_providers.immich_data_provider.ImmichClient",
            return_value=mock_client,
        ):
            provider = ImmichDataProvider("https://immich.test", "key")
            await provider.refresh_metadata()

        assert provider._people_map_cache == {"Alice": "uuid-alice", "Bob": "uuid-bob"}


# ---------------------------------------------------------------------------
# fetch_photo_ids
# ---------------------------------------------------------------------------

class TestFetchPhotoIds:
    @pytest.mark.asyncio
    async def test_all_photos_calls_search_random(self):
        mock_client = _make_mock_client()
        mock_client.search_random = AsyncMock(
            return_value=[{"id": "a1"}, {"id": "a2"}]
        )

        with patch(
            "photo_providers.immich_data_provider.ImmichClient",
            return_value=mock_client,
        ):
            provider = ImmichDataProvider("https://immich.test", "key")
            provider._people_map_cache = {}
            pf = PhotoFilter(name="All", selection="all_photos")
            ids = await provider.fetch_photo_ids(pf, 5)

        assert ids == ["a1", "a2"]
        mock_client.search_random.assert_called_once()
        call_body = mock_client.search_random.call_args[0][0]
        assert call_body["type"] == "IMAGE"
        assert call_body["size"] == 5

    @pytest.mark.asyncio
    async def test_search_calls_search_smart(self):
        mock_client = _make_mock_client()
        mock_client.search_smart = AsyncMock(
            return_value=[{"id": "s1"}, {"id": "s2"}]
        )

        with patch(
            "photo_providers.immich_data_provider.ImmichClient",
            return_value=mock_client,
        ):
            provider = ImmichDataProvider("https://immich.test", "key")
            provider._people_map_cache = {}
            pf = PhotoFilter(
                name="Search",
                selection="search",
                search_query="sunset",
                randomize=False,
            )
            ids = await provider.fetch_photo_ids(pf, 3)

        assert ids == ["s1", "s2"]
        mock_client.search_smart.assert_called_once()
        call_body = mock_client.search_smart.call_args[0][0]
        assert call_body["query"] == "sunset"
        assert call_body["size"] == 3

    @pytest.mark.asyncio
    async def test_album_resolves_name_then_calls_get_album_assets(self):
        mock_client = _make_mock_client()
        mock_client.resolve_album_name = AsyncMock(return_value="alb-123")
        mock_client.get_album_assets = AsyncMock(
            return_value=[{"id": "x1"}, {"id": "x2"}]
        )

        with patch(
            "photo_providers.immich_data_provider.ImmichClient",
            return_value=mock_client,
        ):
            provider = ImmichDataProvider("https://immich.test", "key")
            pf = PhotoFilter(
                name="Album",
                selection="album",
                album_name="Family 2024",
                randomize=False,
            )
            ids = await provider.fetch_photo_ids(pf, 5)

        assert ids == ["x1", "x2"]
        mock_client.resolve_album_name.assert_awaited_once_with("Family 2024")
        mock_client.get_album_assets.assert_awaited_once_with("alb-123")

    @pytest.mark.asyncio
    async def test_album_not_found_returns_empty(self):
        mock_client = _make_mock_client()
        mock_client.resolve_album_name = AsyncMock(return_value=None)

        with patch(
            "photo_providers.immich_data_provider.ImmichClient",
            return_value=mock_client,
        ):
            provider = ImmichDataProvider("https://immich.test", "key")
            pf = PhotoFilter(
                name="Missing",
                selection="album",
                album_name="Nonexistent",
            )
            ids = await provider.fetch_photo_ids(pf, 5)

        assert ids == []
        mock_client.get_album_assets.assert_not_called()

    @pytest.mark.asyncio
    async def test_passes_location_aliases_to_selector(self):
        mock_client = _make_mock_client()
        mock_client.search_random = AsyncMock(return_value=[{"id": "a1"}])

        alias = LocationAlias(city="Paris", country="France")
        location_aliases = {"Europe": alias}

        with patch(
            "photo_providers.immich_data_provider.ImmichClient",
            return_value=mock_client,
        ):
            provider = ImmichDataProvider("https://immich.test", "key")
            provider._people_map_cache = {}
            pf = PhotoFilter(
                name="Loc",
                selection="all_photos",
                location="Europe",
            )
            await provider.fetch_photo_ids(
                pf, 5, location_aliases=location_aliases
            )

        call_body = mock_client.search_random.call_args[0][0]
        assert call_body["city"] == "Paris"
        assert call_body["country"] == "France"

    @pytest.mark.asyncio
    async def test_people_map_cache_used_for_person_ids(self):
        mock_client = _make_mock_client()
        mock_client.search_random = AsyncMock(return_value=[{"id": "a1"}])

        with patch(
            "photo_providers.immich_data_provider.ImmichClient",
            return_value=mock_client,
        ):
            provider = ImmichDataProvider("https://immich.test", "key")
            provider._people_map_cache = {"Alice": "uuid-alice", "Bob": "uuid-bob"}
            pf = PhotoFilter(
                name="People",
                selection="all_photos",
                people=["Alice", "Bob"],
            )
            await provider.fetch_photo_ids(pf, 5)

        call_body = mock_client.search_random.call_args[0][0]
        assert call_body["personIds"] == ["uuid-alice", "uuid-bob"]


# ---------------------------------------------------------------------------
# download_photo
# ---------------------------------------------------------------------------

class TestDownloadPhoto:
    @pytest.mark.asyncio
    async def test_returns_bytes_from_download_preview(self):
        mock_client = _make_mock_client()
        mock_client.download_preview = AsyncMock(
            return_value=b"\xff\xd8\xff\xe0FAKE_JPEG"
        )

        with patch(
            "photo_providers.immich_data_provider.ImmichClient",
            return_value=mock_client,
        ):
            provider = ImmichDataProvider("https://immich.test", "key")
            data = await provider.download_photo("asset-123")

        assert data == b"\xff\xd8\xff\xe0FAKE_JPEG"
        mock_client.download_preview.assert_awaited_once_with("asset-123")
