"""MovieGlu data fetcher — discovers nearby cinemas and fetches showtimes.

Pure-Python — no AppDaemon dependency.  Wraps ``MovieGluClient`` with
higher-level cinema-discovery and showtime-aggregation logic.
"""

from __future__ import annotations

import datetime
import logging
from typing import Dict, List, Optional

from .movieglu_client import MovieGluClient
from .types import CinemaInfo, ShowtimeCache, ShowtimeEntry

logger = logging.getLogger(__name__)


class MoviegluFetcher:
    """Fetches cinema showtimes from the MovieGlu API via RapidAPI.

    On first call to ``discover_cinemas()`` the fetcher queries nearby
    cinemas and matches them against the configured ``theater_names`` list
    using case-insensitive substring matching.  Matched cinema IDs are
    cached so that subsequent ``fetch_showtimes()`` calls do not need to
    re-discover.
    """

    def __init__(
        self,
        api_key_env: str,
        client_id_env: str,
        lat: str,
        lng: str,
        theater_names: List[str],
    ) -> None:
        from providers.secrets import resolve_secret

        self._api_key = resolve_secret(api_key_env)
        self._client_id = resolve_secret(client_id_env)
        self._lat = lat
        self._lng = lng
        # Store lowercased for case-insensitive matching
        self._theater_names: List[str] = [t.lower() for t in theater_names]
        # Maps matched (lowercased) theater name -> cinema_id
        self._cinema_ids: Dict[str, str] = {}

    # -------------------------------------------------------------------------
    # Factory
    # -------------------------------------------------------------------------

    def _create_client(self) -> MovieGluClient:
        """Create a new MovieGluClient for use within an async context."""
        return MovieGluClient(
            api_key=self._api_key,
            client_id=self._client_id,
            lat=self._lat,
            lng=self._lng,
        )

    # -------------------------------------------------------------------------
    # Cinema discovery
    # -------------------------------------------------------------------------

    async def discover_cinemas(self) -> List[CinemaInfo]:
        """Discover and return cinemas matching the configured theater names.

        Fetches up to 20 nearby cinemas from the API, attempts to match
        each against ``theater_names`` via ``_match_cinema_name()``, caches
        matched IDs in ``self._cinema_ids``, and logs which theaters matched
        and which didn't.
        """
        try:
            async with self._create_client() as client:
                raw_cinemas = await client.get_cinemas_nearby(n=20)
        except Exception as exc:
            logger.error(
                "MoviegluFetcher.discover_cinemas failed: %s", exc, exc_info=True
            )
            return []

        matched: List[CinemaInfo] = []
        matched_names: List[str] = []

        for cinema in raw_cinemas:
            api_name: str = cinema.get("cinema_name", "")
            cinema_id: str = str(cinema.get("cinema_id", ""))
            distance: float = float(cinema.get("distance_mi", cinema.get("distance", 0.0)))

            configured_name = self._match_cinema_name(api_name)
            if configured_name is not None:
                self._cinema_ids[configured_name] = cinema_id
                matched.append(
                    CinemaInfo(
                        cinema_id=cinema_id,
                        cinema_name=api_name,
                        distance=distance,
                    )
                )
                matched_names.append(configured_name)
                logger.info(
                    "MoviegluFetcher.discover_cinemas: matched '%s' -> cinema_id=%s",
                    api_name,
                    cinema_id,
                )

        unmatched = [n for n in self._theater_names if n not in matched_names]
        if unmatched:
            logger.warning(
                "MoviegluFetcher.discover_cinemas: no match found for configured theaters: %s",
                unmatched,
            )

        logger.info(
            "MoviegluFetcher.discover_cinemas: %d/%d configured theaters matched",
            len(matched),
            len(self._theater_names),
        )
        return matched

    # -------------------------------------------------------------------------
    # Showtime fetching
    # -------------------------------------------------------------------------

    async def fetch_showtimes(
        self, cinema_ids: Optional[Dict[str, str]] = None
    ) -> ShowtimeCache:
        """Fetch showtimes for all configured (or provided) cinemas.

        One API call is made per cinema.  Per-cinema errors are caught and
        logged as warnings; remaining cinemas are still fetched.  On total
        failure an empty ``ShowtimeCache`` is returned.

        Args:
            cinema_ids: Override mapping of theater name -> cinema_id.
                        Falls back to ``self._cinema_ids`` when ``None``.

        Returns:
            A ``ShowtimeCache`` with today's date and aggregated showtimes.
        """
        ids_to_use = cinema_ids if cinema_ids is not None else self._cinema_ids

        today = datetime.date.today().isoformat()
        cache = ShowtimeCache(date=today)

        if not ids_to_use:
            logger.warning(
                "MoviegluFetcher.fetch_showtimes: no cinema IDs available — "
                "call discover_cinemas() first"
            )
            return cache

        try:
            async with self._create_client() as client:
                for theater_name, cinema_id in ids_to_use.items():
                    try:
                        raw = await client.get_showtimes_by_cinema(cinema_id, today)
                        cinema_name = (
                            raw.get("cinema", {}).get("cinema_name", theater_name)
                        )
                        film_showtimes = self._parse_showtime_response(
                            cinema_name, raw
                        )
                        # Merge into cache: each film title -> list of ShowtimeEntry
                        for film_title, entries in film_showtimes.items():
                            if film_title not in cache.films:
                                cache.films[film_title] = []
                            cache.films[film_title].extend(entries)
                    except Exception as exc:
                        logger.warning(
                            "MoviegluFetcher.fetch_showtimes: failed for cinema_id=%s (%s): %s",
                            cinema_id,
                            theater_name,
                            exc,
                        )
        except Exception as exc:
            logger.error(
                "MoviegluFetcher.fetch_showtimes: total failure: %s", exc, exc_info=True
            )
            return ShowtimeCache(date=today)

        total_films = len(cache.films)
        logger.info(
            "MoviegluFetcher.fetch_showtimes: fetched showtimes for %d films across %d cinemas",
            total_films,
            len(ids_to_use),
        )
        return cache

    # -------------------------------------------------------------------------
    # Name matching
    # -------------------------------------------------------------------------

    def _match_cinema_name(self, api_name: str) -> Optional[str]:
        """Try to match an API cinema name against configured theater names.

        Uses case-insensitive substring matching in both directions: the
        configured name is a substring of the API name, or vice versa.

        Returns:
            The matching configured theater name (lowercased), or ``None``.
        """
        api_lower = api_name.lower()
        for configured in self._theater_names:
            if configured in api_lower or api_lower in configured:
                return configured
        return None

    # -------------------------------------------------------------------------
    # Response parsing
    # -------------------------------------------------------------------------

    def _parse_showtime_response(
        self, cinema_name: str, raw: dict
    ) -> Dict[str, List[ShowtimeEntry]]:
        """Parse a MovieGlu cinemaShowTimes response into film -> ShowtimeEntry.

        MovieGlu response structure::

            {
                "films": [
                    {
                        "film_id": 12345,
                        "film_name": "Thunderbolts*",
                        "showings": {
                            "Standard": {
                                "times": [
                                    {"display_showtime": "13:30"},
                                    ...
                                ]
                            },
                            "IMAX": {
                                "times": [{"display_showtime": "19:00"}]
                            }
                        }
                    }
                ]
            }

        All format types are flattened into a single list of time strings,
        keyed by lowercased film title.

        Args:
            cinema_name: Human-readable cinema name for the ShowtimeEntry.
            raw: The full JSON response dict from ``get_showtimes_by_cinema``.

        Returns:
            Mapping of lowercase film title -> list containing one
            ``ShowtimeEntry`` for ``cinema_name`` with all collected times.
        """
        result: Dict[str, List[ShowtimeEntry]] = {}
        films = raw.get("films", [])

        for film in films:
            film_name: str = film.get("film_name", "")
            if not film_name:
                continue

            film_key = film_name.lower()
            showings: dict = film.get("showings", {})

            times: List[str] = []
            for _format_type, format_data in showings.items():
                for time_entry in format_data.get("times", []):
                    display = time_entry.get("display_showtime", "")
                    if display:
                        times.append(display)

            entry = ShowtimeEntry(cinema_name=cinema_name, times=times)
            if film_key not in result:
                result[film_key] = []
            result[film_key].append(entry)

        return result
