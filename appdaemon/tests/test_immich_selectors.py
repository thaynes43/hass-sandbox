"""Unit tests for immich_fetcher.selectors – request body construction.

All aiohttp responses are mocked; no real HTTP calls are made.
"""

from __future__ import annotations

import random as _random
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from apps.immich_fetcher.immich_client import ImmichClient
from apps.immich_fetcher.models import LocationAlias, PhotoFilter
from apps.immich_fetcher.selectors import (
    AllPhotosSelector,
    AlbumSelector,
    SearchSelector,
    create_selector,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

PEOPLE_MAP: Dict[str, str] = {
    "Alice": "uuid-alice",
    "Bob": "uuid-bob",
}

LOCATION_ALIASES: Dict[str, LocationAlias] = {
    "Disney World": LocationAlias(city="Celebration", state="Florida"),
}


def _asset_list(ids: List[str]) -> List[Dict[str, Any]]:
    """Create a minimal asset list for mocking."""
    return [{"id": aid, "originalFileName": f"{aid}.jpg"} for aid in ids]


@pytest_asyncio.fixture
async def client():
    """Create an ImmichClient with a mocked session (never hits the network)."""
    c = ImmichClient(base_url="https://immich.example.com", api_key="test-key")
    c._session = AsyncMock()
    c._owns_session = False
    return c


# ---------------------------------------------------------------------------
# AllPhotosSelector
# ---------------------------------------------------------------------------

class TestAllPhotosSelector:
    @pytest.mark.asyncio
    async def test_basic_request_body(self, client: ImmichClient):
        pf = PhotoFilter(name="Random All", selection="all_photos")
        sel = AllPhotosSelector(client, pf)
        body = sel.build_request_body(10)
        assert body == {"type": "IMAGE", "size": 10}

    @pytest.mark.asyncio
    async def test_with_people(self, client: ImmichClient):
        pf = PhotoFilter(
            name="People", selection="all_photos", people=["Alice", "Bob"]
        )
        sel = AllPhotosSelector(client, pf, people_map=PEOPLE_MAP)
        body = sel.build_request_body(5)
        assert body["personIds"] == ["uuid-alice", "uuid-bob"]

    @pytest.mark.asyncio
    async def test_with_location_alias(self, client: ImmichClient):
        pf = PhotoFilter(
            name="Disney", selection="all_photos", location="Disney World"
        )
        sel = AllPhotosSelector(
            client, pf, location_aliases=LOCATION_ALIASES
        )
        body = sel.build_request_body(5)
        assert body["city"] == "Celebration"
        assert body["state"] == "Florida"

    @pytest.mark.asyncio
    async def test_with_raw_location(self, client: ImmichClient):
        pf = PhotoFilter(
            name="Raw", selection="all_photos", location="Springfield"
        )
        sel = AllPhotosSelector(client, pf)
        body = sel.build_request_body(5)
        assert body["city"] == "Springfield"

    @pytest.mark.asyncio
    async def test_with_dates(self, client: ImmichClient):
        pf = PhotoFilter(
            name="Dated", selection="all_photos",
            taken_after="2023-01-01", taken_before="2024-01-01",
        )
        sel = AllPhotosSelector(client, pf)
        body = sel.build_request_body(5)
        assert body["takenAfter"] == "2023-01-01"
        assert body["takenBefore"] == "2024-01-01"

    @pytest.mark.asyncio
    async def test_with_favorites(self, client: ImmichClient):
        pf = PhotoFilter(
            name="Favs", selection="all_photos", favorites_only=True
        )
        sel = AllPhotosSelector(client, pf)
        body = sel.build_request_body(5)
        assert body["isFavorite"] is True

    @pytest.mark.asyncio
    async def test_with_rating(self, client: ImmichClient):
        pf = PhotoFilter(name="Rated", selection="all_photos", rating=3)
        sel = AllPhotosSelector(client, pf)
        body = sel.build_request_body(5)
        assert body["rating"] == 3

    @pytest.mark.asyncio
    async def test_full_criteria(self, client: ImmichClient):
        pf = PhotoFilter(
            name="Full", selection="all_photos",
            people=["Alice"],
            location="Disney World",
            taken_after="2023-06-01",
            favorites_only=True,
            rating=4,
        )
        sel = AllPhotosSelector(
            client, pf,
            people_map=PEOPLE_MAP,
            location_aliases=LOCATION_ALIASES,
        )
        body = sel.build_request_body(10)
        assert body == {
            "type": "IMAGE",
            "size": 10,
            "personIds": ["uuid-alice"],
            "city": "Celebration",
            "state": "Florida",
            "takenAfter": "2023-06-01",
            "isFavorite": True,
            "rating": 4,
        }

    @pytest.mark.asyncio
    async def test_get_asset_ids(self, client: ImmichClient):
        pf = PhotoFilter(name="R", selection="all_photos")
        sel = AllPhotosSelector(client, pf)
        client.search_random = AsyncMock(
            return_value=_asset_list(["a1", "a2", "a3"])
        )
        ids = await sel.get_asset_ids(3)
        assert ids == ["a1", "a2", "a3"]
        client.search_random.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_missing_person_skipped(self, client: ImmichClient):
        pf = PhotoFilter(
            name="Missing", selection="all_photos",
            people=["Alice", "Unknown"],
        )
        sel = AllPhotosSelector(client, pf, people_map=PEOPLE_MAP)
        body = sel.build_request_body(5)
        assert body["personIds"] == ["uuid-alice"]


# ---------------------------------------------------------------------------
# SearchSelector
# ---------------------------------------------------------------------------

class TestSearchSelector:
    @pytest.mark.asyncio
    async def test_non_random_body(self, client: ImmichClient):
        pf = PhotoFilter(
            name="Top", selection="search", randomize=False,
            search_query="sunset beach",
        )
        sel = SearchSelector(client, pf)
        body = sel.build_request_body(10)
        assert body == {
            "type": "IMAGE",
            "query": "sunset beach",
            "size": 10,
        }

    @pytest.mark.asyncio
    async def test_random_body_uses_pool_size(self, client: ImmichClient):
        pf = PhotoFilter(
            name="RndSearch", selection="search", randomize=True,
            search_query="cats", search_pool_size=500,
        )
        sel = SearchSelector(client, pf)
        body = sel.build_request_body(500)
        assert body["size"] == 500
        assert body["query"] == "cats"

    @pytest.mark.asyncio
    async def test_with_criteria(self, client: ImmichClient):
        pf = PhotoFilter(
            name="SearchCrit", selection="search", randomize=False,
            search_query="birthday", people=["Alice"],
            taken_after="2023-01-01", favorites_only=True,
        )
        sel = SearchSelector(client, pf, people_map=PEOPLE_MAP)
        body = sel.build_request_body(20)
        assert body["personIds"] == ["uuid-alice"]
        assert body["takenAfter"] == "2023-01-01"
        assert body["isFavorite"] is True
        assert body["query"] == "birthday"

    @pytest.mark.asyncio
    async def test_get_asset_ids_non_random(self, client: ImmichClient):
        pf = PhotoFilter(
            name="Top5", selection="search", randomize=False,
            search_query="test",
        )
        sel = SearchSelector(client, pf)
        client.search_smart = AsyncMock(
            return_value=_asset_list(["s1", "s2", "s3", "s4", "s5"])
        )
        ids = await sel.get_asset_ids(5)
        assert ids == ["s1", "s2", "s3", "s4", "s5"]

    @pytest.mark.asyncio
    async def test_get_asset_ids_non_random_truncates(self, client: ImmichClient):
        pf = PhotoFilter(
            name="Top2", selection="search", randomize=False,
            search_query="test",
        )
        sel = SearchSelector(client, pf)
        client.search_smart = AsyncMock(
            return_value=_asset_list(["s1", "s2", "s3", "s4", "s5"])
        )
        ids = await sel.get_asset_ids(2)
        assert ids == ["s1", "s2"]

    @pytest.mark.asyncio
    async def test_get_asset_ids_randomized(self, client: ImmichClient):
        pf = PhotoFilter(
            name="RndSrch", selection="search", randomize=True,
            search_query="cats", search_pool_size=100,
        )
        sel = SearchSelector(client, pf)
        pool = _asset_list([f"id-{i}" for i in range(50)])
        client.search_smart = AsyncMock(return_value=pool)
        with patch.object(_random, "sample", wraps=_random.sample) as mock_sample:
            ids = await sel.get_asset_ids(10)
            mock_sample.assert_called_once()
            assert len(ids) == 10

    @pytest.mark.asyncio
    async def test_randomized_fewer_than_count(self, client: ImmichClient):
        pf = PhotoFilter(
            name="Few", selection="search", randomize=True,
            search_query="rare", search_pool_size=100,
        )
        sel = SearchSelector(client, pf)
        pool = _asset_list(["r1", "r2"])
        client.search_smart = AsyncMock(return_value=pool)
        ids = await sel.get_asset_ids(10)
        assert set(ids) == {"r1", "r2"}


# ---------------------------------------------------------------------------
# AlbumSelector
# ---------------------------------------------------------------------------

class TestAlbumSelector:
    @pytest.mark.asyncio
    async def test_non_random(self, client: ImmichClient):
        pf = PhotoFilter(
            name="Album", selection="album", randomize=False,
            album_name="Family"
        )
        sel = AlbumSelector(client, pf, album_id="album-uuid-123")
        client.get_album_assets = AsyncMock(
            return_value=_asset_list(["a1", "a2", "a3", "a4", "a5"])
        )
        ids = await sel.get_asset_ids(3)
        assert ids == ["a1", "a2", "a3"]
        client.get_album_assets.assert_awaited_once_with("album-uuid-123")

    @pytest.mark.asyncio
    async def test_randomized(self, client: ImmichClient):
        pf = PhotoFilter(
            name="Album", selection="album", randomize=True,
            album_name="Vacation"
        )
        sel = AlbumSelector(client, pf, album_id="alb-456")
        client.get_album_assets = AsyncMock(
            return_value=_asset_list([f"v{i}" for i in range(20)])
        )
        with patch.object(_random, "sample", wraps=_random.sample):
            ids = await sel.get_asset_ids(5)
            assert len(ids) == 5

    @pytest.mark.asyncio
    async def test_fewer_than_count(self, client: ImmichClient):
        pf = PhotoFilter(
            name="Small", selection="album", randomize=True,
            album_name="Tiny"
        )
        sel = AlbumSelector(client, pf, album_id="alb-tiny")
        client.get_album_assets = AsyncMock(
            return_value=_asset_list(["t1"])
        )
        ids = await sel.get_asset_ids(10)
        assert ids == ["t1"]


# ---------------------------------------------------------------------------
# create_selector factory
# ---------------------------------------------------------------------------

class TestCreateSelector:
    def test_all_photos(self, client: ImmichClient):
        pf = PhotoFilter(name="R", selection="all_photos")
        sel = create_selector(client, pf)
        assert isinstance(sel, AllPhotosSelector)

    def test_search(self, client: ImmichClient):
        pf = PhotoFilter(name="S", selection="search", search_query="q")
        sel = create_selector(client, pf)
        assert isinstance(sel, SearchSelector)

    def test_album_with_id(self, client: ImmichClient):
        pf = PhotoFilter(name="A", selection="album", album_name="X")
        sel = create_selector(client, pf, album_id="uuid")
        assert isinstance(sel, AlbumSelector)

    def test_album_without_id_raises(self, client: ImmichClient):
        pf = PhotoFilter(name="A", selection="album", album_name="X")
        with pytest.raises(ValueError, match="album_id must be provided"):
            create_selector(client, pf)

    def test_unknown_selection_raises(self, client: ImmichClient):
        pf = PhotoFilter(name="?", selection="all_photos")
        pf.selection = "magic"  # type: ignore[assignment]
        with pytest.raises(ValueError, match="Unknown selection"):
            create_selector(client, pf)
