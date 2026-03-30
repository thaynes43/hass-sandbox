"""Async HTTP client wrapping the MovieGlu API via RapidAPI.

Pure-Python — no AppDaemon dependency.  Uses ``aiohttp`` for all I/O.

MovieGlu provides cinema listings and showtimes.  This client accesses
the API through the RapidAPI gateway, which requires both the standard
RapidAPI headers (``x-rapidapi-key``, ``x-rapidapi-host``) and
MovieGlu-specific headers (``client``, ``x-api-key``, ``Authorization``,
``territory``, ``api-version``, ``geolocation``, ``device-datetime``).
"""

from __future__ import annotations

import datetime
import logging
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)


class MovieGluClient:
    """Thin async wrapper around the MovieGlu REST API via RapidAPI.

    Callers are responsible for opening / closing the underlying
    ``aiohttp.ClientSession`` (or using this class as an async context
    manager).
    """

    BASE_URL = "https://api-gate2.movieglu.com"

    def __init__(
        self,
        api_key: str,
        client_id: str,
        lat: str,
        lng: str,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> None:
        self._api_key = api_key
        self._client_id = client_id
        self._lat = lat
        self._lng = lng
        self._owns_session = session is None
        self._session = session

    # -- Context manager -------------------------------------------------------

    async def __aenter__(self) -> "MovieGluClient":
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

    def _build_headers(self) -> Dict[str, str]:
        """Build the full set of required MovieGlu / RapidAPI headers.

        ``device-datetime`` is generated fresh on each call so it reflects
        the actual time of the request.
        """
        return {
            "x-rapidapi-key": self._api_key,
            "x-rapidapi-host": "api-gate2.movieglu.com",
            "client": self._client_id,
            "x-api-key": self._api_key,
            "Authorization": self._client_id,
            "territory": "US",
            "api-version": "v200",
            "geolocation": f"{self._lat};{self._lng}",
            "device-datetime": datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat(),
        }

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError(
                "MovieGluClient has no active session – "
                "use 'async with MovieGluClient(...)' or pass a session"
            )
        return self._session

    async def _get(
        self, path: str, params: Optional[Dict[str, Any]] = None
    ) -> dict:
        """Core GET helper.  Builds MovieGlu headers, logs path and status,
        raises on non-2xx, and returns the parsed JSON body."""
        url = f"{self.BASE_URL}{path}"
        headers = self._build_headers()

        if params:
            logger.info("GET %s  params=%s", path, params)
        else:
            logger.info("GET %s", path)

        async with self.session.get(url, params=params, headers=headers) as resp:
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
            return data  # type: ignore[return-value]

    # -- Public API methods ----------------------------------------------------

    async def get_cinemas_nearby(self, n: int = 10) -> List[Dict[str, Any]]:
        """Return a list of cinemas near the configured lat/lng.

        Calls ``GET /cinemasNearby/?n={n}`` and returns the ``cinemas``
        list from the response.  Each element contains at least
        ``cinema_id``, ``cinema_name``, and ``distance_mi``.
        """
        data = await self._get("/cinemasNearby/", params={"n": n})
        cinemas: List[Dict[str, Any]] = data.get("cinemas", [])
        logger.info(
            "get_cinemas_nearby(n=%d) returned %d cinemas", n, len(cinemas)
        )
        return cinemas

    async def get_showtimes_by_cinema(
        self, cinema_id: str, date: str
    ) -> dict:
        """Return showtime data for a single cinema on the given date.

        Calls ``GET /cinemaShowTimes/?cinema_id={cinema_id}&date={date}``.
        *date* must be formatted as ``"YYYY-MM-DD"``.

        Returns the full response dict, which contains a ``films`` list.
        Each film entry includes nested showtime details.
        """
        params = {"cinema_id": cinema_id, "date": date}
        data = await self._get("/cinemaShowTimes/", params=params)
        film_count = len(data.get("films", []))
        logger.info(
            "get_showtimes_by_cinema(cinema_id=%s, date=%s) returned %d films",
            cinema_id, date, film_count,
        )
        return data
