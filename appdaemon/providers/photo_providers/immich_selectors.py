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
from .date_filters import build_date_scan_body, matches_capture_dates
from .types import LocationAlias, PhotoFilter, PhotoProviderError

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
        people_map = people_map or {}
        unknown = [n for n in pf.people if n not in people_map]
        if unknown:
            raise PhotoProviderError(f"Unknown people: {', '.join(unknown)}")
        body["personIds"] = [people_map[n] for n in pf.people]

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


async def _sample_matching_assets(
    client: ImmichClient, pf: PhotoFilter, body: dict, count: int,
    *, randomize: bool = True,
) -> List[str]:
    """Choose uniformly from all matches with a fixed-size reservoir."""
    body = {**build_date_scan_body(pf, body), "size": 1000}
    ids: List[str] = []
    seen: set[str] = set()
    async for asset in client.iter_search_assets(body):
        if asset["id"] in seen or not matches_capture_dates(asset, pf):
            continue
        seen.add(asset["id"])
        if len(ids) < count:
            ids.append(asset["id"])
        elif randomize:
            position = random.randrange(len(seen))
            if position < count:
                ids[position] = asset["id"]
        if not randomize and len(ids) == count:
            break
    if randomize:
        random.shuffle(ids)
    logger.info(
        "Date-filtered metadata search: filter=%r matches=%d selected=%d",
        pf.name, len(seen), len(ids),
    )
    return ids


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
        if any((self.photo_filter.recurring_date_ranges,
                self.photo_filter.taken_after, self.photo_filter.taken_before)):
            return await _sample_matching_assets(self.client, self.photo_filter, body, count)
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
        if any((self.photo_filter.recurring_date_ranges,
                self.photo_filter.taken_after, self.photo_filter.taken_before)):
            limit = self.photo_filter.search_pool_size if self.photo_filter.randomize else count
            body = build_date_scan_body(self.photo_filter, self.build_request_body(1000))
            ids: List[str] = []
            seen: set[str] = set()
            async for asset in self.client.iter_search_assets(body, smart=True):
                if asset["id"] in seen or not matches_capture_dates(asset, self.photo_filter):
                    continue
                seen.add(asset["id"])
                ids.append(asset["id"])
                if len(ids) >= limit:
                    break
            if self.photo_filter.randomize:
                random.shuffle(ids)
            logger.info("Date-filtered smart search: filter=%r selected=%d", self.photo_filter.name, len(ids))
            return ids
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
# AlbumSelector  (GET /api/albums/{id} on v2; POST /api/search/assets fallback on v3)
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
        *,
        people_map: Optional[Dict[str, str]] = None,
        location_aliases: Optional[Dict[str, LocationAlias]] = None,
    ) -> None:
        self.client = client
        self.photo_filter = photo_filter
        self.album_id = album_id
        self.people_map = people_map
        self.location_aliases = location_aliases

    async def get_asset_ids(self, count: int) -> List[str]:
        pf = self.photo_filter
        if any((pf.people, pf.location, pf.taken_after, pf.taken_before,
                pf.recurring_date_ranges, pf.favorites_only, pf.rating is not None)):
            body = _build_common_filters(
                pf, people_map=self.people_map, location_aliases=self.location_aliases,
            )
            body["albumIds"] = [self.album_id]
            return await _sample_matching_assets(
                self.client, pf, body, count, randomize=pf.randomize,
            )
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
        return AlbumSelector(
            client, photo_filter, album_id,
            people_map=people_map, location_aliases=location_aliases,
        )
    raise ValueError(f"Unknown selection type: {sel}")
