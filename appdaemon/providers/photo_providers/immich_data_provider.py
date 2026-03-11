"""Immich implementation of the PhotoProvider protocol."""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from .immich_client import ImmichClient
from .immich_selectors import create_selector
from .types import (
    LocationAlias,
    PhotoAlbum,
    PhotoFilter,
    PhotoMetadata,
    PhotoPerson,
)

logger = logging.getLogger(__name__)


class ImmichDataProvider:
    """Concrete PhotoProvider implementation for Immich."""

    def __init__(self, base_url: str, api_key_env: str) -> None:
        """base_url is the resolved Immich URL; api_key stays env-backed to avoid UI exposure."""
        from providers.secrets import resolve_secret

        self._base_url = base_url
        self._api_key = resolve_secret(api_key_env)
        self._people_map_cache: Dict[str, str] = {}

    def _create_client(self) -> ImmichClient:
        """Create a new ImmichClient for use within an async context."""
        return ImmichClient(base_url=self._base_url, api_key=self._api_key)

    async def refresh_metadata(self) -> PhotoMetadata:
        """Fetch and return current people and albums from Immich."""
        try:
            async with self._create_client() as client:
                raw_people = await client.get_people()
                raw_albums = await client.get_albums()
        except Exception as exc:
            logger.error(
                "ImmichDataProvider.refresh_metadata failed: %s",
                exc,
                exc_info=True,
            )
            raise

        has_asset_count = bool(
            raw_people and "assetCount" in raw_people[0]
        )
        named = [p for p in raw_people if p.get("name")]
        if has_asset_count:
            named.sort(key=lambda p: p.get("assetCount", 0), reverse=True)
        self._people_map_cache = {p["name"]: p["id"] for p in named}

        people = [
            PhotoPerson(
                name=p["name"],
                asset_count=p.get("assetCount", 0),
            )
            for p in named
        ]
        albums_raw = sorted(
            [a for a in raw_albums if a.get("albumName")],
            key=lambda x: x.get("assetCount", 0),
            reverse=True,
        )
        albums = [
            PhotoAlbum(
                name=a.get("albumName", ""),
                id=a["id"],
                asset_count=a.get("assetCount", 0),
            )
            for a in albums_raw
        ]

        logger.info(
            "ImmichDataProvider.refresh_metadata: retrieved %d people, %d albums",
            len(people),
            len(albums),
        )
        return PhotoMetadata(people=people, albums=albums)

    async def fetch_photo_ids(
        self,
        photo_filter: PhotoFilter,
        count: int,
        *,
        location_aliases: Optional[Dict[str, LocationAlias]] = None,
    ) -> List[str]:
        """Return up to count photo IDs matching the filter."""
        album_id: Optional[str] = None
        if photo_filter.selection == "album" and photo_filter.album_name:
            try:
                async with self._create_client() as client:
                    album_id = await client.resolve_album_name(photo_filter.album_name)
            except Exception as exc:
                logger.warning(
                    "ImmichDataProvider.fetch_photo_ids: failed to resolve album '%s': %s",
                    photo_filter.album_name,
                    exc,
                )
                return []
            if album_id is None:
                logger.warning(
                    "ImmichDataProvider.fetch_photo_ids: album '%s' not found",
                    photo_filter.album_name,
                )
                return []

        try:
            async with self._create_client() as client:
                selector = create_selector(
                    client,
                    photo_filter,
                    people_map=self._people_map_cache,
                    location_aliases=location_aliases,
                    album_id=album_id,
                )
                ids = await selector.get_asset_ids(count)
        except Exception as exc:
            logger.error(
                "ImmichDataProvider.fetch_photo_ids failed for filter '%s': %s",
                photo_filter.name,
                exc,
                exc_info=True,
            )
            raise

        logger.info(
            "ImmichDataProvider.fetch_photo_ids: filter='%s' selection=%s returned %d IDs",
            photo_filter.name,
            photo_filter.selection,
            len(ids),
        )
        return ids

    async def download_photo(self, photo_id: str) -> bytes:
        """Download raw bytes for a single photo."""
        try:
            async with self._create_client() as client:
                data = await client.download_preview(photo_id)
        except Exception as exc:
            logger.warning(
                "ImmichDataProvider.download_photo failed for asset %s: %s",
                photo_id,
                exc,
            )
            raise

        logger.debug(
            "ImmichDataProvider.download_photo: asset %s (%d bytes)",
            photo_id,
            len(data),
        )
        return data
