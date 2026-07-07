"""Async HTTP client wrapping the Immich API.

Pure-Python – no AppDaemon dependency.  Uses ``aiohttp`` for all I/O.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)


class ImmichClient:
    """Thin async wrapper around the Immich REST API.

    Callers are responsible for opening / closing the underlying
    ``aiohttp.ClientSession`` (or using this class as an async context
    manager).
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._owns_session = session is None
        self._session = session

    # -- Context manager -----------------------------------------------------

    async def __aenter__(self) -> "ImmichClient":
        if self._session is None:
            self._session = aiohttp.ClientSession(
                headers=self._default_headers(),
            )
            self._owns_session = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()

    # -- Internal helpers ----------------------------------------------------

    def _default_headers(self) -> Dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "Accept": "application/json",
        }

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError(
                "ImmichClient has no active session – "
                "use 'async with ImmichClient(...)' or pass a session"
            )
        return self._session

    async def _json_request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        if json:
            logger.info("%s %s  body=%s", method, path, json)
        elif params:
            logger.info("%s %s  params=%s", method, path, params)
        else:
            logger.info("%s %s", method, path)

        async with self.session.request(
            method, url, json=json, params=params, headers=self._default_headers()
        ) as resp:
            data = await resp.json()
            # Log response: status always, body only when small
            body_len = len(str(data))
            if resp.status != 200 or body_len <= 500:
                logger.info(
                    "%s %s -> %d  (%d chars) %s",
                    method, path, resp.status, body_len, data,
                )
            else:
                logger.info(
                    "%s %s -> %d  (%d chars)",
                    method, path, resp.status, body_len,
                )
            resp.raise_for_status()
            return data

    # -- People --------------------------------------------------------------

    async def get_people(self) -> List[Dict[str, Any]]:
        """Return the full people list from ``GET /api/people``.

        Each element has at least ``id``, ``name``, and ``assetCount``.
        Fetches without query params first (works on all Immich versions),
        then paginates only if the response indicates more pages.
        """
        data = await self._json_request("GET", "/api/people")
        all_people: List[Dict[str, Any]] = data.get("people", [])

        page = 1
        while data.get("hasNextPage", False):
            page += 1
            data = await self._json_request(
                "GET", "/api/people", params={"page": page}
            )
            all_people.extend(data.get("people", []))

        logger.info("Retrieved %d people from Immich (%d page(s))", len(all_people), page)
        return all_people

    async def resolve_people_names(
        self, names: List[str], people_cache: Optional[Dict[str, str]] = None
    ) -> List[str]:
        """Resolve human-readable names to Immich person UUIDs.

        *people_cache* maps ``name -> uuid``; if not supplied the people
        list is fetched from the API.  Unknown names are logged as warnings
        and skipped.
        """
        if people_cache is None:
            raw = await self.get_people()
            people_cache = {p["name"]: p["id"] for p in raw if p.get("name")}

        ids: List[str] = []
        for name in names:
            uid = people_cache.get(name)
            if uid:
                ids.append(uid)
            else:
                logger.warning("Person '%s' not found in Immich – skipping", name)
        return ids

    # -- Albums --------------------------------------------------------------

    async def get_albums(self) -> List[Dict[str, Any]]:
        """Return all albums via ``GET /api/albums``.

        Each element has at least ``id``, ``albumName``, and ``assetCount``.
        """
        albums = await self._json_request("GET", "/api/albums")
        logger.info("Retrieved %d albums from Immich", len(albums))
        return albums

    async def resolve_album_name(self, album_name: str) -> Optional[str]:
        """Resolve an album display name to its UUID.  Returns *None* if
        not found (logged as a warning)."""
        albums = await self.get_albums()
        for album in albums:
            if album.get("albumName") == album_name:
                return album["id"]
        logger.warning("Album '%s' not found in Immich", album_name)
        return None

    async def get_album_assets(self, album_id: str) -> List[Dict[str, Any]]:
        """Return the asset list for a single album.

        Tries the legacy ``GET /api/albums/{id}`` shape first because it
        is the canonical path on Immich v2.x and avoids an extra
        round-trip.  If the response omits ``assets`` (Immich v3.0.0
        removed the field per PR immich-app/immich#27835) or the
        endpoint itself returns 404, falls back to the paginated
        ``POST /api/search/metadata`` form with an ``albumIds`` filter
        (v3.0.0 also removed ``POST /api/search/assets``; metadata search
        returns the same ``assets.items``/``assets.nextPage`` shape).
        """
        try:
            legacy = await self._json_request("GET", f"/api/albums/{album_id}")
        except aiohttp.ClientResponseError as exc:
            if exc.status != 404:
                raise
            legacy = None

        if isinstance(legacy, dict) and isinstance(legacy.get("assets"), list):
            items = legacy["assets"]
            logger.info(
                "Retrieved %d assets from album %s (legacy)", len(items), album_id
            )
            return items

        logger.info(
            "Album %s: legacy response missing 'assets' (Immich v3?), "
            "falling back to POST /api/search/metadata",
            album_id,
        )
        all_assets: List[Dict[str, Any]] = []
        page = 1
        while True:
            body = {"albumIds": [album_id], "page": page, "size": 1000}
            data = await self._json_request("POST", "/api/search/metadata", json=body)
            assets_section = data.get("assets", {}) if isinstance(data, dict) else {}
            items = assets_section.get("items", []) if isinstance(assets_section, dict) else []
            all_assets.extend(items)
            next_page = assets_section.get("nextPage") if isinstance(assets_section, dict) else None
            if next_page is None:
                break
            try:
                page = int(next_page)
            except (TypeError, ValueError):
                break
        logger.info(
            "Retrieved %d assets from album %s (search)", len(all_assets), album_id
        )
        return all_assets

    # -- Search endpoints ----------------------------------------------------

    async def search_random(self, body: dict) -> List[Dict[str, Any]]:
        """``POST /api/search/random`` – returns random assets matching *body*."""
        data = await self._json_request("POST", "/api/search/random", json=body)
        # API returns a flat list
        if isinstance(data, list):
            return data
        return data.get("assets", data.get("items", []))

    async def search_smart(self, body: dict) -> List[Dict[str, Any]]:
        """``POST /api/search/smart`` – returns assets ranked by relevance."""
        data = await self._json_request("POST", "/api/search/smart", json=body)
        assets_section = data.get("assets", {})
        return assets_section.get("items", [])

    # -- Asset download ------------------------------------------------------

    async def download_preview(self, asset_id: str) -> bytes:
        """Download a preview JPEG for *asset_id* via ``GET /api/assets/{id}/thumbnail?size=preview``."""
        url = f"{self.base_url}/api/assets/{asset_id}/thumbnail"
        async with self.session.get(
            url, params={"size": "preview"}, headers=self._default_headers()
        ) as resp:
            resp.raise_for_status()
            data = await resp.read()
            logger.debug(
                "Downloaded preview for %s (%d bytes)", asset_id, len(data)
            )
            return data
