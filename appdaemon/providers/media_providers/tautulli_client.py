"""Async HTTP client wrapping the Tautulli REST API.

Pure-Python – no AppDaemon dependency.  Uses ``aiohttp`` for all I/O.

Tautulli wraps all API responses in an envelope::

    {"response": {"result": "success", "data": ...}}

The ``_api_request`` method unwraps this envelope automatically and raises
``RuntimeError`` when ``result != "success"``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)


class TautulliClient:
    """Thin async wrapper around the Tautulli REST API (``/api/v2``).

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

    async def __aenter__(self) -> "TautulliClient":
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()

    # -- Internal helpers ----------------------------------------------------

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError(
                "TautulliClient has no active session – "
                "use 'async with TautulliClient(...)' or pass a session"
            )
        return self._session

    async def _api_request(
        self,
        cmd: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Call ``GET {base_url}/api/v2`` and unwrap the Tautulli response envelope.

        Args:
            cmd: The Tautulli API command name (e.g. ``"get_recently_added"``).
            params: Optional additional query parameters to include.

        Returns:
            The ``data`` value from the unwrapped response envelope.

        Raises:
            RuntimeError: When Tautulli returns ``result != "success"``.
            aiohttp.ClientResponseError: On non-2xx HTTP status.
        """
        url = f"{self.base_url}/api/v2"
        query: Dict[str, Any] = {"apikey": self.api_key, "cmd": cmd}
        if params:
            query.update(params)

        # Log command (never log the full api_key)
        if params:
            logger.info("Tautulli cmd=%s  params=%s", cmd, params)
        else:
            logger.info("Tautulli cmd=%s", cmd)

        async with self.session.get(url, params=query) as resp:
            resp.raise_for_status()
            envelope = await resp.json()

        response_block = envelope.get("response", {})
        result = response_block.get("result", "error")
        data = response_block.get("data")

        logger.info(
            "Tautulli cmd=%s -> result=%s  data_type=%s",
            cmd,
            result,
            type(data).__name__,
        )

        if result != "success":
            message = response_block.get("message", "unknown error")
            raise RuntimeError(
                f"Tautulli API error for cmd={cmd!r}: {message}"
            )

        return data

    # -- Public API methods --------------------------------------------------

    async def get_recently_added(
        self,
        count: int = 25,
        media_type: str = "movie",
    ) -> List[Dict[str, Any]]:
        """Return the recently-added items from Tautulli.

        Args:
            count: Maximum number of items to return (passed to Tautulli).
            media_type: ``"movie"``, ``"show"``, or ``"episode"``.

        Returns:
            The ``recently_added`` list from the response data.
        """
        data = await self._api_request(
            "get_recently_added",
            {"count": count, "media_type": media_type},
        )
        items: List[Dict[str, Any]] = data.get("recently_added", [])
        logger.info(
            "get_recently_added(count=%d, media_type=%s) -> %d items",
            count,
            media_type,
            len(items),
        )
        return items

    async def get_home_stats(
        self,
        stat_id: str = "popular_movies",
        count: int = 10,
    ) -> List[Dict[str, Any]]:
        """Return the ``rows`` for a specific home-stats entry.

        Args:
            stat_id: The Tautulli stat identifier to filter for
                (e.g. ``"popular_movies"``, ``"top_movies"``).
            count: Number of rows to request via ``stats_count``.

        Returns:
            The ``rows`` list for the matching stat entry, or an empty list
            if the stat_id is not found in the response.
        """
        data = await self._api_request(
            "get_home_stats",
            {"stats_count": count},
        )
        stats: List[Dict[str, Any]] = data if isinstance(data, list) else []
        for entry in stats:
            if entry.get("stat_id") == stat_id:
                rows: List[Dict[str, Any]] = entry.get("rows", [])
                logger.info(
                    "get_home_stats(stat_id=%s, count=%d) -> %d rows",
                    stat_id,
                    count,
                    len(rows),
                )
                return rows

        logger.warning(
            "get_home_stats: stat_id=%r not found in response (available: %s)",
            stat_id,
            [e.get("stat_id") for e in stats],
        )
        return []

    async def download_poster(
        self,
        rating_key: str,
        width: int = 342,
    ) -> bytes:
        """Download poster image bytes via Tautulli's ``pms_image_proxy``.

        This endpoint returns raw binary image data, not JSON.

        Args:
            rating_key: The Plex rating key for the media item.
            width: Poster width in pixels; height is derived at 1.5× ratio.

        Returns:
            Raw image bytes.
        """
        url = f"{self.base_url}/api/v2"
        height = int(width * 1.5)
        params: Dict[str, Any] = {
            "apikey": self.api_key,
            "cmd": "pms_image_proxy",
            "rating_key": rating_key,
            "width": width,
            "height": height,
        }

        logger.info(
            "Downloading poster for rating_key=%s (%dx%d)",
            rating_key,
            width,
            height,
        )

        async with self.session.get(url, params=params) as resp:
            resp.raise_for_status()
            data = await resp.read()

        logger.info(
            "Downloaded poster for rating_key=%s (%d bytes)",
            rating_key,
            len(data),
        )
        return data
