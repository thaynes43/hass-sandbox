"""Async HTTP client wrapping the TMDb v3 REST API.

Pure-Python – no AppDaemon dependency.  Uses ``aiohttp`` for all I/O.

Callers own the session lifecycle; use this class as an async context manager
or pass an existing ``aiohttp.ClientSession`` for test injection.

Image downloads use the separate TMDb image CDN (``image.tmdb.org``) and do
NOT require the API key.
"""

from __future__ import annotations

import logging
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.themoviedb.org"
_IMAGE_BASE_URL = "https://image.tmdb.org/t/p"


class TmdbClient:
    """Thin async wrapper around the TMDb v3 REST API.

    Callers are responsible for opening / closing the underlying
    ``aiohttp.ClientSession`` (or using this class as an async context
    manager).
    """

    def __init__(
        self,
        api_key: str,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> None:
        self._api_key = api_key
        self._owns_session = session is None
        self._session = session

    # -- Context manager -----------------------------------------------------

    async def __aenter__(self) -> "TmdbClient":
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
                "TmdbClient has no active session – "
                "use 'async with TmdbClient(...)' or pass a session"
            )
        return self._session

    async def _get(self, path: str, params: Optional[dict] = None) -> dict:
        """Core GET helper. Appends ``api_key`` to *params* automatically.

        Args:
            path: API path (e.g. ``/3/movie/now_playing``).
            params: Optional query parameters.  ``api_key`` is always injected.

        Returns:
            Parsed JSON response as a dict.
        """
        merged: dict = dict(params or {})
        merged["api_key"] = self._api_key

        url = f"{_BASE_URL}{path}"
        logger.debug("GET %s  params=%s", path, {k: v for k, v in merged.items() if k != "api_key"})

        async with self.session.get(url, params=merged) as resp:
            data = await resp.json()
            body_len = len(str(data))
            if resp.status != 200 or body_len <= 500:
                logger.info(
                    "GET %s -> %d  (%d chars) %s",
                    path, resp.status, body_len, data,
                )
            else:
                logger.info(
                    "GET %s -> %d  (%d chars)",
                    path, resp.status, body_len,
                )
            resp.raise_for_status()
            return data

    # -- Movie endpoints -----------------------------------------------------

    async def get_now_playing(self, region: str = "US", page: int = 1) -> dict:
        """``GET /3/movie/now_playing`` – movies currently in theaters.

        Args:
            region: ISO 3166-1 country code to filter release dates (default ``"US"``).
            page: Result page number (default 1).

        Returns:
            TMDb response dict with ``results``, ``page``, ``total_pages``, etc.
        """
        return await self._get(
            "/3/movie/now_playing",
            params={"region": region, "page": page},
        )

    async def get_upcoming(self, region: str = "US", page: int = 1) -> dict:
        """``GET /3/movie/upcoming`` – movies with upcoming release dates.

        Args:
            region: ISO 3166-1 country code to filter release dates (default ``"US"``).
            page: Result page number (default 1).

        Returns:
            TMDb response dict with ``results``, ``page``, ``total_pages``, etc.
        """
        return await self._get(
            "/3/movie/upcoming",
            params={"region": region, "page": page},
        )

    async def get_trending(
        self,
        media_type: str = "movie",
        time_window: str = "week",
    ) -> dict:
        """``GET /3/trending/{media_type}/{time_window}`` – trending media.

        Args:
            media_type: One of ``"movie"``, ``"tv"``, or ``"all"`` (default ``"movie"``).
            time_window: One of ``"day"`` or ``"week"`` (default ``"week"``).

        Returns:
            TMDb response dict with ``results``, ``page``, ``total_pages``, etc.
        """
        return await self._get(f"/3/trending/{media_type}/{time_window}")

    async def get_discover_movies(self, **params) -> dict:
        """``GET /3/discover/movie`` – discover movies with arbitrary filters.

        Keyword arguments are forwarded directly as query parameters.  Common
        filters include ``with_release_type``, ``primary_release_date.gte``,
        ``primary_release_date.lte``, ``vote_count.gte``, etc.

        Returns:
            TMDb response dict with ``results``, ``page``, ``total_pages``, etc.
        """
        return await self._get("/3/discover/movie", params=dict(params))

    async def get_movie_detail(self, movie_id: int) -> dict:
        """``GET /3/movie/{movie_id}`` – full movie metadata.

        Args:
            movie_id: TMDb numeric movie ID.

        Returns:
            TMDb movie detail dict.
        """
        return await self._get(f"/3/movie/{movie_id}")

    async def get_tv_detail(self, tv_id: int) -> dict:
        """``GET /3/tv/{tv_id}`` – full TV series metadata.

        Args:
            tv_id: TMDb numeric TV series ID.

        Returns:
            TMDb TV detail dict.
        """
        return await self._get(f"/3/tv/{tv_id}")

    # -- Image download ------------------------------------------------------

    async def download_poster(
        self,
        poster_path: str,
        size: str = "w342",
    ) -> bytes:
        """Download a poster image from the TMDb image CDN.

        The image CDN (``image.tmdb.org``) does not require the API key.

        Args:
            poster_path: The ``poster_path`` value from a TMDb response (e.g.
                ``"/abc123.jpg"``).  Leading slash is accepted.
            size: TMDb image size string (e.g. ``"w185"``, ``"w342"``,
                ``"w500"``, ``"original"``).  Default is ``"w342"``.

        Returns:
            Raw image bytes.
        """
        url = f"{_IMAGE_BASE_URL}/{size}{poster_path}"
        logger.debug("Downloading poster: %s", url)

        async with self.session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.read()
            logger.debug(
                "Downloaded poster %s  size=%s  (%d bytes)",
                poster_path, size, len(data),
            )
            return data
