"""SerpApi showtime fetcher — searches Google showtimes per theater.

Pure-Python — no AppDaemon dependency.  Wraps ``SerpApiClient`` with
higher-level showtime-aggregation logic.

Strategy: search for each configured theater by name (e.g.
``"AMC Tyngsboro 12 showtimes"``).  Google returns a ``showtimes``
array grouped by day, each day containing ``movies`` with showtime
data.  This is more reliable than a generic ``"movies"`` query because
it targets specific theaters and always triggers Google's showtimes
knowledge panel.

SerpApi response structure when searching a theater name::

    {
        "showtimes": [
            {
                "day": "Today",
                "date": "Mar 29",
                "movies": [
                    {
                        "name": "Thunderbolts*",
                        "link": "...",
                        "showing": [
                            {
                                "type": "Standard",
                                "time": ["1:30pm", "4:15pm", "7:00pm"]
                            },
                            {
                                "type": "IMAX",
                                "time": ["9:45pm"]
                            }
                        ]
                    }
                ]
            }
        ]
    }
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
    each configured theater and aggregates the results into a single
    ``ShowtimeCache`` keyed by lowercase film title.
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
        self._theater_names: List[str] = theater_names

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
        """Fetch showtimes by searching Google for each configured theater.

        Makes one SerpApi request per theater (e.g. ``"AMC Tyngsboro 12
        showtimes"``).  Per-theater errors are logged as warnings and do
        not abort the whole batch.

        Returns:
            A ``ShowtimeCache`` with today's date and aggregated showtimes,
            keyed by lowercase film title.  Returns an empty cache on
            total failure.
        """
        today = datetime.date.today().isoformat()
        cache = ShowtimeCache(date=today)

        if not self._theater_names:
            logger.warning("SerpApiFetcher: no theaters configured")
            return cache

        try:
            async with self._create_client() as client:
                for theater_name in self._theater_names:
                    await self._fetch_theater(client, theater_name, cache)
        except Exception as exc:
            logger.error(
                "SerpApiFetcher.fetch_showtimes failed: %s", exc, exc_info=True
            )

        total_films = len(cache.films)
        logger.info(
            "SerpApiFetcher.fetch_showtimes: %d theaters queried, %d films found",
            len(self._theater_names),
            total_films,
        )
        return cache

    async def _fetch_theater(
        self,
        client: SerpApiClient,
        theater_name: str,
        cache: ShowtimeCache,
    ) -> None:
        """Fetch showtimes for a single theater and merge into *cache*."""
        query = f"{theater_name} showtimes"
        try:
            raw = await client.get_showtimes(query=query)
        except Exception as exc:
            logger.warning(
                "SerpApiFetcher: failed to fetch showtimes for '%s': %s",
                theater_name,
                exc,
            )
            return

        showtimes_list = raw.get("showtimes") or []
        if not showtimes_list:
            top_keys = list(raw.keys())[:15]
            logger.warning(
                "SerpApiFetcher: no 'showtimes' key for theater '%s'.  "
                "Top-level keys: %s",
                theater_name,
                top_keys,
            )
            return

        # Only parse the first day (today).  SerpApi may return multiple days.
        today_block = showtimes_list[0] if showtimes_list else {}
        movies = today_block.get("movies") or []

        for movie in movies:
            film_title: str = movie.get("name", "")
            if not film_title:
                continue

            film_key = film_title.lower()

            # Collect all times across all format types (Standard, IMAX, etc.)
            times: List[str] = []
            for showing in movie.get("showing", []):
                for t in showing.get("time", []):
                    if t:
                        times.append(t)

            if not times:
                continue

            entry = ShowtimeEntry(cinema_name=theater_name, times=times)
            if film_key not in cache.films:
                cache.films[film_key] = []
            cache.films[film_key].append(entry)

        logger.info(
            "SerpApiFetcher: theater '%s' -> %d movies with showtimes",
            theater_name,
            len(movies),
        )

    # -------------------------------------------------------------------------
    # Theater name matching (used by app when cross-referencing with TMDb)
    # -------------------------------------------------------------------------

    def _match_theater(self, theater_name: str) -> Optional[str]:
        """Try to match an API theater name against configured theater names.

        Uses case-insensitive substring matching in both directions: the
        configured name is a substring of the API name, or vice versa.

        Args:
            theater_name: Theater name from an external source.

        Returns:
            The matching configured theater name, or ``None``.
        """
        if not theater_name:
            return None
        api_lower = theater_name.lower()
        for configured in self._theater_names:
            conf_lower = configured.lower()
            if conf_lower in api_lower or api_lower in conf_lower:
                return configured
        return None
