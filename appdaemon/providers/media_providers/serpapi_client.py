"""Async HTTP client wrapping the SerpApi Google Showtimes endpoint.

Pure-Python — no AppDaemon dependency.  Uses ``aiohttp`` for all I/O.

SerpApi provides access to Google search results in structured JSON,
including the ``showtimes_results`` field returned when searching for
movie showtimes near a location.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import aiohttp

logger = logging.getLogger(__name__)


class SerpApiClient:
    """Thin async wrapper around the SerpApi search endpoint.

    Callers are responsible for opening / closing the underlying
    ``aiohttp.ClientSession`` (or using this class as an async context
    manager).
    """

    BASE_URL = "https://serpapi.com"

    def __init__(
        self,
        api_key: str,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> None:
        self._api_key = api_key
        self._owns_session = session is None
        self._session = session

    # -- Context manager -------------------------------------------------------

    async def __aenter__(self) -> "SerpApiClient":
        if self._session is None:
            self._session = aiohttp.ClientSession()
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
                "SerpApiClient has no active session — "
                "use 'async with SerpApiClient(...)' or pass a session"
            )
        return self._session

    # -- Public API methods ----------------------------------------------------

    async def get_showtimes(
        self,
        query: str,
        location: str = "",
    ) -> Dict[str, Any]:
        """Fetch Google showtimes results via SerpApi.

        Calls ``GET /search?engine=google&q={query}&location={location}&api_key={key}``.

        The response includes a ``showtimes_results`` list with structured
        data about movies and their theaters.  On non-2xx responses the
        method raises ``aiohttp.ClientResponseError``.

        Args:
            query: Search query string, e.g. ``"showtimes near 01886"``.
            location: Optional SerpApi location string.  Must be a value
                      from SerpApi's location database (e.g. ``"Boston,
                      Massachusetts, United States"``).  If the location
                      is not in their database the request returns 400,
                      so prefer embedding the location in *query* instead
                      (e.g. ``"showtimes near Westford, MA"``).

        Returns:
            Full SerpApi JSON response dict.
        """
        params: Dict[str, str] = {
            "engine": "google",
            "q": query,
            "api_key": self._api_key,
        }
        if location:
            params["location"] = location

        url = f"{self.BASE_URL}/search"
        logger.info("SerpApiClient.get_showtimes: GET /search  q=%r  location=%r", query, location)

        async with self.session.get(url, params=params) as resp:
            data = await resp.json()
            body_len = len(str(data))
            results_count = len(data.get("showtimes_results", []))
            if resp.status != 200 or body_len <= 500:
                logger.info(
                    "SerpApiClient GET /search -> %d  (%d chars) %s",
                    resp.status, body_len, data,
                )
            else:
                logger.info(
                    "SerpApiClient GET /search -> %d  (%d chars, %d showtime results)",
                    resp.status, body_len, results_count,
                )
            resp.raise_for_status()
            return data  # type: ignore[return-value]
