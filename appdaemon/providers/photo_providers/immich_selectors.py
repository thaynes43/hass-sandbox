"""Asset selection strategies for the Immich fetcher.

Each selector takes a :class:`PhotoFilter`, builds the correct Immich API
request body, and returns a list of asset IDs.  Selectors are pure Python
with no AppDaemon dependency.
"""

from __future__ import annotations

import logging
import random
from abc import ABC, abstractmethod
from typing import Dict, List, Optional

from .immich_client import ImmichClient
from .types import LocationAlias, PhotoFilter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_common_filters(
    pf: PhotoFilter,
    *,
    people_map: Optional[Dict[str, str]] = None,
    location_aliases: Optional[Dict[str, LocationAlias]] = None,
) -> dict:
    """Build the subset of the Immich API body shared by all endpoints.

    Returns a dict ready to be merged into the final request body.
    """
    body: dict = {"type": "IMAGE"}

    # People -> personIds
    if pf.people:
        if people_map is None:
            people_map = {}
        ids = [people_map[n] for n in pf.people if n in people_map]
        if ids:
            body["personIds"] = ids
        for name in pf.people:
            if name not in people_map:
                logger.warning("Person '%s' not in people_map – skipped", name)

    # Location (alias or raw city)
    if pf.location:
        if location_aliases and pf.location in location_aliases:
            alias = location_aliases[pf.location]
            body.update(alias.to_dict())
        else:
            body["city"] = pf.location

    # Date range
    if pf.taken_after:
        body["takenAfter"] = pf.taken_after
    if pf.taken_before:
        body["takenBefore"] = pf.taken_before

    # Favourites / rating
    if pf.favorites_only:
        body["isFavorite"] = True
    if pf.rating is not None:
        body["rating"] = pf.rating

    return body


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------

class AssetSelector(ABC):
    """Abstract base for asset selection strategies."""

    @abstractmethod
    async def get_asset_ids(self, count: int) -> List[str]:
        """Return up to *count* asset IDs."""


# ---------------------------------------------------------------------------
# AllPhotosSelector  (POST /api/search/random)
# ---------------------------------------------------------------------------

class AllPhotosSelector(AssetSelector):
    """Random photos from the entire library, filtered by criteria."""

    def __init__(
        self,
        client: ImmichClient,
        photo_filter: PhotoFilter,
        *,
        people_map: Optional[Dict[str, str]] = None,
        location_aliases: Optional[Dict[str, LocationAlias]] = None,
    ) -> None:
        self.client = client
        self.photo_filter = photo_filter
        self.people_map = people_map
        self.location_aliases = location_aliases

    def build_request_body(self, count: int) -> dict:
        body = _build_common_filters(
            self.photo_filter,
            people_map=self.people_map,
            location_aliases=self.location_aliases,
        )
        body["size"] = count
        return body

    async def get_asset_ids(self, count: int) -> List[str]:
        body = self.build_request_body(count)
        assets = await self.client.search_random(body)
        ids = [a["id"] for a in assets]
        logger.info("AllPhotosSelector returned %d assets", len(ids))
        return ids


# ---------------------------------------------------------------------------
# SearchSelector  (POST /api/search/smart)
# ---------------------------------------------------------------------------

class SearchSelector(AssetSelector):
    """Semantic / CLIP search.

    When ``randomize=False`` returns the top-N most relevant results.
    When ``randomize=True`` fetches the full pool (search_pool_size) so
    the viewer can cycle through all of them for maximum variety.
    """

    def __init__(
        self,
        client: ImmichClient,
        photo_filter: PhotoFilter,
        *,
        people_map: Optional[Dict[str, str]] = None,
        location_aliases: Optional[Dict[str, LocationAlias]] = None,
    ) -> None:
        self.client = client
        self.photo_filter = photo_filter
        self.people_map = people_map
        self.location_aliases = location_aliases

    def build_request_body(self, size: int) -> dict:
        body = _build_common_filters(
            self.photo_filter,
            people_map=self.people_map,
            location_aliases=self.location_aliases,
        )
        body["query"] = self.photo_filter.search_query
        body["size"] = size
        return body

    async def get_asset_ids(self, count: int) -> List[str]:
        if self.photo_filter.randomize:
            pool_size = self.photo_filter.search_pool_size
            body = self.build_request_body(pool_size)
            assets = await self.client.search_smart(body)
            ids = list(dict.fromkeys(a["id"] for a in assets))
            random.shuffle(ids)
            logger.info(
                "SearchSelector (pool) returning %d assets (pool_size=%d)",
                len(ids),
                pool_size,
            )
            return ids
        else:
            body = self.build_request_body(count)
            assets = await self.client.search_smart(body)
            ids = [a["id"] for a in assets][:count]
            logger.info("SearchSelector returned %d assets", len(ids))
            return ids


# ---------------------------------------------------------------------------
# AlbumSelector  (GET /api/albums/{id}/assets)
# ---------------------------------------------------------------------------

class AlbumSelector(AssetSelector):
    """Photos from a specific Immich album.

    *album_id* must be resolved before constructing this selector
    (the fetcher app resolves ``album_name → album_id`` via the client).
    """

    def __init__(
        self,
        client: ImmichClient,
        photo_filter: PhotoFilter,
        album_id: str,
    ) -> None:
        self.client = client
        self.photo_filter = photo_filter
        self.album_id = album_id

    async def get_asset_ids(self, count: int) -> List[str]:
        assets = await self.client.get_album_assets(self.album_id)
        ids = [a["id"] for a in assets]
        if self.photo_filter.randomize:
            if len(ids) > count:
                ids = random.sample(ids, count)
            logger.info(
                "AlbumSelector (randomized) returned %d assets from album %s",
                len(ids),
                self.album_id,
            )
        else:
            ids = ids[:count]
            logger.info(
                "AlbumSelector returned %d assets from album %s",
                len(ids),
                self.album_id,
            )
        return ids


# ---------------------------------------------------------------------------
# Factory helper
# ---------------------------------------------------------------------------

def create_selector(
    client: ImmichClient,
    photo_filter: PhotoFilter,
    *,
    people_map: Optional[Dict[str, str]] = None,
    location_aliases: Optional[Dict[str, LocationAlias]] = None,
    album_id: Optional[str] = None,
) -> AssetSelector:
    """Build the appropriate selector for *photo_filter*."""
    sel = photo_filter.selection
    if sel == "all_photos":
        return AllPhotosSelector(
            client, photo_filter,
            people_map=people_map, location_aliases=location_aliases,
        )
    if sel == "search":
        return SearchSelector(
            client, photo_filter,
            people_map=people_map, location_aliases=location_aliases,
        )
    if sel == "album":
        if album_id is None:
            raise ValueError(
                f"album_id must be provided for album selection "
                f"(filter '{photo_filter.name}')"
            )
        return AlbumSelector(client, photo_filter, album_id)
    raise ValueError(f"Unknown selection type: {sel}")
