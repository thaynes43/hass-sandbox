"""Async HTTP client wrapping the MDbList API.

Pure-Python — no AppDaemon dependency.  Uses ``aiohttp`` for all I/O.

MDbList aggregates ratings from IMDb, Rotten Tomatoes (critics + audience),
and Metacritic for a given TMDb ID.  The free tier supports individual lookups
only; bulk POST requires a paid tier, so this client uses sequential GETs with
a 1.0s delay between requests to respect rate limits.

API reference: https://mdblist.com/api/
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)


class MdbListClient:
    """Thin async wrapper around the MDbList API.

    Callers are responsible for opening / closing the underlying
    ``aiohttp.ClientSession`` (or using this class as an async context
    manager).

    Args:
        api_key: MDbList API key (resolved from env var by the caller).
        session: Optional existing ``aiohttp.ClientSession``.  If not provided
            a new session is created and owned by this client.
    """

    BASE_URL = "https://mdblist.com/api"

    def __init__(
        self,
        api_key: str,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> None:
        self._api_key = api_key
        self._owns_session = session is None
        self._session = session

    # -- Context manager -------------------------------------------------------

    async def __aenter__(self) -> "MdbListClient":
        if self._session is None:
            # Disable keep-alive: the 1s rate-limit sleep between requests
            # lets Cloudflare close idle connections silently, causing aiohttp
            # to hang when it tries to reuse a dead socket.
            connector = aiohttp.TCPConnector(force_close=True)
            timeout = aiohttp.ClientTimeout(total=30)
            self._session = aiohttp.ClientSession(
                connector=connector, timeout=timeout,
            )
            self._owns_session = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()

    # -- Internal helpers ------------------------------------------------------

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError(
                "MdbListClient has no active session — "
                "use 'async with MdbListClient(...)' or pass a session"
            )
        return self._session

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> dict:
        """Core GET helper.  Appends ``apikey`` to params automatically.

        Applies a 1.0s rate-limit delay before each request to stay within
        MDbList's free-tier limits.

        Args:
            path: API path (e.g. ``/tmdb/movie/123``).
            params: Optional extra query parameters.

        Returns:
            Parsed JSON response as a dict.

        Raises:
            aiohttp.ClientResponseError: on non-2xx HTTP status.
        """
        merged: Dict[str, Any] = dict(params or {})
        merged["apikey"] = self._api_key

        url = f"{self.BASE_URL}{path}"
        logger.debug("MdbListClient GET %s  params=%s", path, {k: v for k, v in merged.items() if k != "apikey"})

        # Rate-limit: free tier allows ~1 req/s
        await asyncio.sleep(1.0)

        async with self.session.get(url, params=merged) as resp:
            if resp.status == 404:
                logger.debug("MdbListClient GET %s -> 404 (item not found)", path)
                return {}
            data = await resp.json()

            # MDbList returns 200 with {"response": false, "error": "Not Found"}
            # for items not in its database — treat as empty, don't log at INFO
            if data.get("response") is False:
                logger.debug("MdbListClient GET %s -> not found in MDbList", path)
                return {}

            body_len = len(str(data))
            logger.info(
                "MdbListClient GET %s -> %d  (%d chars)",
                path, resp.status, body_len,
            )
            resp.raise_for_status()
            return data  # type: ignore[return-value]

    # -- Public API methods ----------------------------------------------------

    async def get_ratings(self, tmdb_id: int, media_type: str = "movie") -> dict:
        """Fetch ratings for a single item by TMDb ID.

        Calls ``GET /?tm={tmdb_id}&m={media_type}&apikey=...``.

        Args:
            tmdb_id: The TMDb numeric ID of the movie or show.
            media_type: ``"movie"`` or ``"show"`` (default ``"movie"``).

        Returns:
            MDbList response dict containing a ``ratings`` list.
        """
        return await self._get("/", {"tm": tmdb_id, "m": media_type})

    async def get_ratings_bulk(
        self, tmdb_ids: List[int], media_type: str = "movie"
    ) -> List[dict]:
        """Fetch ratings for multiple items via sequential GETs.

        MDbList bulk POST requires a paid tier.  This method uses sequential
        individual GETs with a 1.0s rate-limit delay (enforced in ``_get``).

        Args:
            tmdb_ids: List of TMDb numeric IDs (max 200 per call).
            media_type: ``"movie"`` or ``"show"`` (default ``"movie"``).

        Returns:
            List of MDbList response dicts in the same order as ``tmdb_ids``.
            Items that fail are omitted from the result.

        Raises:
            ValueError: if ``tmdb_ids`` has more than 200 entries.
        """
        if len(tmdb_ids) > 200:
            raise ValueError(
                f"get_ratings_bulk: too many IDs ({len(tmdb_ids)}); max is 200"
            )

        results: List[dict] = []
        for tmdb_id in tmdb_ids:
            try:
                data = await self.get_ratings(tmdb_id, media_type=media_type)
                results.append(data)
            except Exception as exc:
                logger.warning(
                    "MdbListClient.get_ratings_bulk: failed for tmdb_id=%d: %s",
                    tmdb_id,
                    exc,
                )
        return results

    # -- Parsing helpers -------------------------------------------------------

    @staticmethod
    def extract_ratings(data: dict) -> dict:
        """Parse an MDbList response dict into a flat ratings dict.

        Reads the ``ratings`` list from the response and maps known sources
        to standard keys.

        Args:
            data: A single MDbList item response dict (as returned by
                ``get_ratings``).

        Returns:
            Dict with zero or more of the following keys (only keys with
            non-None values are included):
            - ``imdb_rating`` (float): IMDb score (0-10)
            - ``rt_critics`` (int): Rotten Tomatoes Tomatometer (0-100)
            - ``rt_audience`` (int): RT Audience Score (0-100)
            - ``metacritic`` (int): Metacritic score (0-100)
        """
        ratings = data.get("ratings", [])
        result: dict = {}
        for r in ratings:
            src = r.get("source", "")
            val = r.get("value")
            if val is None:
                continue
            if src == "imdb":
                result["imdb_rating"] = float(val)
            elif src == "tomatoes":
                result["rt_critics"] = int(val)
            elif src == "tomatoesaudience":
                result["rt_audience"] = int(val)
            elif src == "metacritic":
                result["metacritic"] = int(val)
        return result
