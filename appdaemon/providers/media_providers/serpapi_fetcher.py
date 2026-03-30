"""SerpApi showtime fetcher — searches Google showtimes and parses results.

Pure-Python — no AppDaemon dependency.  Wraps ``SerpApiClient`` with
higher-level showtime-aggregation logic.

SerpApi response structure for showtimes_results::

    [
        {
            "title": "Thunderbolts*",
            "theaters": [
                {
                    "name": "AMC CLASSIC Tyngsborough 12",
                    "showing": [
                        {
                            "time": ["1:30 PM", "4:15 PM", "7:00 PM", "9:45 PM"]
                        }
                    ]
                },
                ...
            ]
        },
        ...
    ]

Each element in ``showtimes_results`` represents one movie; nested under
``theaters`` are the cinemas showing that movie with their times.
"""

from __future__ import annotations

import datetime
import logging
from typing import Dict, List, Optional

from .serpapi_client import SerpApiClient
from .types import ShowtimeCache, ShowtimeEntry

logger = logging.getLogger(__name__)


class SerpApiFetcher:
    """Fetches theater showtimes via SerpApi Google search.

    On each call to ``fetch_showtimes()``, the fetcher queries Google for
    showtimes near the configured location and filters the results to only
    the configured theater names using case-insensitive substring matching.
    """

    def __init__(
        self,
        api_key_env: str,
        location: str,
        theater_names: List[str],
    ) -> None:
        from providers.secrets import resolve_secret

        self._api_key = resolve_secret(api_key_env)
        self._location = location
        # Store lowercased for case-insensitive matching
        self._theater_names: List[str] = [t.lower() for t in theater_names]

    # -------------------------------------------------------------------------
    # Factory
    # -------------------------------------------------------------------------

    def _create_client(self) -> SerpApiClient:
        """Create a new SerpApiClient for use within an async context."""
        return SerpApiClient(api_key=self._api_key)

    # -------------------------------------------------------------------------
    # Showtime fetching
    # -------------------------------------------------------------------------

    async def fetch_showtimes(self) -> ShowtimeCache:
        """Fetch showtimes by searching Google for the configured location.

        Searches ``"showtimes near {location}"`` and parses the
        ``showtimes_results`` from the SerpApi response.  Only theaters
        matching the configured ``theater_names`` (case-insensitive substring)
        are included.

        Returns:
            A ``ShowtimeCache`` with today's date and aggregated showtimes,
            keyed by lowercase film title.  Returns an empty cache on failure.
        """
        query = f"showtimes near {self._location}"
        today = datetime.date.today().isoformat()
        cache = ShowtimeCache(date=today)

        try:
            async with self._create_client() as client:
                raw = await client.get_showtimes(
                    query=query,
                    location=self._location,
                )
        except Exception as exc:
            logger.error(
                "SerpApiFetcher.fetch_showtimes failed: %s", exc, exc_info=True
            )
            return cache

        cache = self._parse_showtimes_response(raw)
        cache.date = today

        total_films = len(cache.films)
        logger.info(
            "SerpApiFetcher.fetch_showtimes: parsed %d films from response",
            total_films,
        )
        return cache

    # -------------------------------------------------------------------------
    # Response parsing
    # -------------------------------------------------------------------------

    def _parse_showtimes_response(self, raw: dict) -> ShowtimeCache:
        """Parse a SerpApi showtimes response into a ShowtimeCache.

        Iterates over ``showtimes_results``, each of which represents one
        movie.  For each movie, iterates over its ``theaters`` list.  Only
        theaters matching the configured ``theater_names`` via
        ``_match_theater()`` are included.

        Args:
            raw: Full SerpApi JSON response dict.

        Returns:
            A ``ShowtimeCache`` with films keyed by lowercase title.
        """
        today = datetime.date.today().isoformat()
        cache = ShowtimeCache(date=today)

        showtime_results = raw.get("showtimes_results", [])
        if not showtime_results:
            logger.warning(
                "SerpApiFetcher._parse_showtimes_response: "
                "no showtimes_results in response"
            )
            return cache

        for movie in showtime_results:
            film_title: str = movie.get("title", "")
            if not film_title:
                continue

            film_key = film_title.lower()
            theaters = movie.get("theaters", [])

            for theater in theaters:
                theater_name: str = theater.get("name", "")
                matched = self._match_theater(theater_name)
                if matched is None:
                    continue

                # Collect all times from the showing list
                times: List[str] = []
                for showing in theater.get("showing", []):
                    for t in showing.get("time", []):
                        if t:
                            times.append(t)

                entry = ShowtimeEntry(cinema_name=theater_name, times=times)
                if film_key not in cache.films:
                    cache.films[film_key] = []
                cache.films[film_key].append(entry)

                logger.debug(
                    "SerpApiFetcher: matched theater '%s' for film '%s' (%d times)",
                    theater_name,
                    film_title,
                    len(times),
                )

        return cache

    # -------------------------------------------------------------------------
    # Theater name matching
    # -------------------------------------------------------------------------

    def _match_theater(self, theater_name: str) -> Optional[str]:
        """Try to match an API theater name against configured theater names.

        Uses case-insensitive substring matching in both directions: the
        configured name is a substring of the API name, or vice versa.

        Args:
            theater_name: Theater name from the SerpApi response.

        Returns:
            The matching configured theater name (lowercased), or ``None``.
        """
        if not theater_name:
            return None
        api_lower = theater_name.lower()
        for configured in self._theater_names:
            if configured in api_lower or api_lower in configured:
                return configured
        return None
