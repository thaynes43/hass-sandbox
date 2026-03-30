"""Tautulli data fetcher — normalizes raw Tautulli API responses into MediaItem objects.

Pure-Python; no AppDaemon dependency.  Wraps TautulliClient and owns the
poster-download lifecycle including skip-if-recent logic.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, List, Optional

from .tautulli_client import TautulliClient
from .types import FetchResult, MediaItem

logger = logging.getLogger(__name__)

# Skip re-downloading a poster if the file is younger than this many seconds.
_POSTER_MAX_AGE_SECONDS = 24 * 3600


class TautulliFetcher:
    """Fetch recently-added media from Tautulli and download poster art.

    Args:
        base_url: The base URL of the Tautulli instance (e.g. ``"http://tautulli:8181"``).
        api_key_env: Name of the environment variable that holds the Tautulli API key.
        poster_dir: Local directory where poster images will be saved.
        poster_width: Poster width in pixels (height derived at 1.5× ratio).
    """

    def __init__(
        self,
        base_url: str,
        api_key_env: str,
        poster_dir: str,
        poster_width: int = 342,
    ) -> None:
        from providers.secrets import resolve_secret

        self._base_url = base_url
        self._api_key = resolve_secret(api_key_env)
        self._poster_dir = poster_dir
        self._poster_width = poster_width

    # ------------------------------------------------------------------
    # Client factory
    # ------------------------------------------------------------------

    def _create_client(self) -> TautulliClient:
        """Create a fresh TautulliClient for a single async-with block."""
        return TautulliClient(
            base_url=self._base_url,
            api_key=self._api_key,
        )

    # ------------------------------------------------------------------
    # Public fetch methods
    # ------------------------------------------------------------------

    async def fetch_recently_added(self, count: int = 50) -> FetchResult:
        """Fetch recently-added movies and TV shows from Tautulli.

        Makes two API calls: one for movies and one for shows.  Each raw item
        is normalized into a :class:`MediaItem`.

        Args:
            count: Maximum number of items to request per media type.

        Returns:
            :class:`FetchResult` with ``status="ok"`` on success or
            ``status="error"`` on failure (with ``error_message`` populated).
        """
        try:
            async with self._create_client() as client:
                raw_movies = await client.get_recently_added(count, media_type="movie")
                raw_shows = await client.get_recently_added(count, media_type="show")

            items: List[MediaItem] = []
            for raw in raw_movies:
                items.append(self._normalize_item(raw))
            for raw in raw_shows:
                items.append(self._normalize_item(raw))

            logger.info(
                "TautulliFetcher.fetch_recently_added: %d movies + %d shows = %d total items",
                len(raw_movies),
                len(raw_shows),
                len(items),
            )
            return FetchResult(items=items, status="ok")

        except Exception as exc:
            logger.error(
                "TautulliFetcher.fetch_recently_added failed: %s",
                exc,
                exc_info=True,
            )
            return FetchResult(status="error", error_message=str(exc))

    async def fetch_popular(self) -> list[dict]:
        """Fetch popular movies and TV stats from Tautulli home stats.

        Used for cross-referencing against recently-added items to surface
        content the family already watches.

        Returns:
            Combined list of raw stat rows for ``popular_movies`` and
            ``popular_tv``.
        """
        try:
            async with self._create_client() as client:
                popular_movies = await client.get_home_stats("popular_movies")
                popular_tv = await client.get_home_stats("popular_tv")

            combined = popular_movies + popular_tv
            logger.info(
                "TautulliFetcher.fetch_popular: %d popular_movies + %d popular_tv = %d rows",
                len(popular_movies),
                len(popular_tv),
                len(combined),
            )
            return combined

        except Exception as exc:
            logger.error(
                "TautulliFetcher.fetch_popular failed: %s",
                exc,
                exc_info=True,
            )
            return []

    async def download_posters(self, items: List[MediaItem]) -> int:
        """Download poster art for each item, skipping recent cached files.

        Writes files to ``{poster_dir}/{item.local_poster}``.  A file is
        considered fresh if it is less than 24 hours old, in which case the
        download is skipped.

        Individual download failures are logged as warnings and do not abort
        the overall operation.

        Args:
            items: List of :class:`MediaItem` objects to download posters for.

        Returns:
            The number of posters actually downloaded (cache hits excluded).
        """
        os.makedirs(self._poster_dir, exist_ok=True)
        downloaded = 0

        for item in items:
            rating_key = item.id.removeprefix("plex-")
            filename = f"plex-{rating_key}.jpg"
            item.local_poster = filename
            dest_path = os.path.join(self._poster_dir, filename)

            if _is_recent_file(dest_path):
                logger.debug(
                    "TautulliFetcher.download_posters: skipping recent poster %s",
                    filename,
                )
                continue

            try:
                async with self._create_client() as client:
                    data = await client.download_poster(rating_key, width=self._poster_width)

                with open(dest_path, "wb") as fh:
                    fh.write(data)

                downloaded += 1
                logger.debug(
                    "TautulliFetcher.download_posters: saved %s (%d bytes)",
                    dest_path,
                    len(data),
                )

            except Exception as exc:
                logger.warning(
                    "TautulliFetcher.download_posters: failed to download poster for %s: %s",
                    item.id,
                    exc,
                )

        logger.info(
            "TautulliFetcher.download_posters: downloaded %d / %d posters",
            downloaded,
            len(items),
        )
        return downloaded

    # ------------------------------------------------------------------
    # Normalisation helpers
    # ------------------------------------------------------------------

    def _normalize_item(self, raw: dict) -> MediaItem:
        """Map a raw Tautulli recently-added dict into a :class:`MediaItem`.

        Args:
            raw: A single entry from the ``recently_added`` list returned by
                Tautulli's ``get_recently_added`` API command.

        Returns:
            A fully-populated :class:`MediaItem`.
        """
        rating_key = str(raw.get("rating_key", ""))
        item_id = f"plex-{rating_key}"

        media_type_raw = raw.get("media_type", "movie")
        media_type = "tv" if media_type_raw == "show" else "movie"

        # Duration: Tautulli reports milliseconds for movies/episodes.
        duration_ms = int(raw.get("duration", 0))
        runtime_min = duration_ms // 60000

        # Genres: can be a list of strings or a comma-joined string.
        genres_raw = raw.get("genres", "")
        if isinstance(genres_raw, list):
            genres = ", ".join(genres_raw)
        else:
            genres = str(genres_raw)

        # added_at: unix timestamp (int or string) → ISO 8601 string.
        added_at_raw = raw.get("added_at", "")
        added_at = _unix_to_iso(added_at_raw)

        # subtitle: "Added X days ago"
        subtitle = _days_ago_subtitle(added_at_raw)

        tmdb_id = self._parse_guids(raw.get("guids"))

        return MediaItem(
            id=item_id,
            title=raw.get("title", ""),
            year=int(raw.get("year", 0) or 0),
            media_type=media_type,
            rating=raw.get("content_rating", ""),
            runtime_min=runtime_min,
            genres=genres,
            summary=raw.get("summary", ""),
            source="tautulli",
            added_at=added_at,
            subtitle=subtitle,
            tmdb_id=tmdb_id,
            local_poster=f"plex-{rating_key}.jpg",
        )

    def _parse_guids(self, guids: Any) -> Optional[int]:
        """Extract a TMDb integer ID from Plex ``guids`` metadata.

        Plex can return ``guids`` in two formats:

        * Dict list: ``[{"id": "tmdb://12345"}, {"id": "imdb://tt1234567"}]``
        * String list: ``["com.plexapp.agents.tmdb://12345?lang=en", ...]``

        Args:
            guids: The raw ``guids`` value from the Tautulli API response.

        Returns:
            The TMDb ID as an integer, or ``None`` if not found or not parseable.
        """
        if not guids:
            return None

        for entry in guids:
            # Dict format: {"id": "tmdb://12345"} or {"id": "tmdb://12345?lang=en"}
            if isinstance(entry, dict):
                guid_str = entry.get("id", "")
            elif isinstance(entry, str):
                guid_str = entry
            else:
                continue

            tmdb_id = _extract_tmdb_id_from_string(guid_str)
            if tmdb_id is not None:
                return tmdb_id

        return None


# ---------------------------------------------------------------------------
# Module-level helpers (no class dependency)
# ---------------------------------------------------------------------------

def _extract_tmdb_id_from_string(s: str) -> Optional[int]:
    """Parse a TMDb ID from a guid string.

    Handles both ``"tmdb://12345"`` and
    ``"com.plexapp.agents.tmdb://12345?lang=en"`` forms.

    Args:
        s: A single guid string.

    Returns:
        The TMDb ID as an integer, or ``None`` if the string does not contain
        a recognisable TMDb reference.
    """
    # Look for "tmdb://<digits>" (preceded by anything, possibly followed by
    # a query string or path separator).
    marker = "tmdb://"
    idx = s.find(marker)
    if idx == -1:
        return None

    # Grab everything after the marker, up to the first non-digit character.
    rest = s[idx + len(marker):]
    digits = ""
    for ch in rest:
        if ch.isdigit():
            digits += ch
        else:
            break

    if not digits:
        return None

    try:
        return int(digits)
    except ValueError:
        return None


def _unix_to_iso(value: Any) -> str:
    """Convert a unix timestamp to an ISO 8601 UTC string.

    If *value* is already a non-numeric string (or empty), return it unchanged.

    Args:
        value: A unix timestamp (int, float, or digit string), or an existing
               ISO string.

    Returns:
        An ISO 8601 string (e.g. ``"2026-03-01T12:00:00+00:00"``) or the
        original string if conversion was not possible.
    """
    if not value and value != 0:
        return ""
    try:
        ts = float(value)
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (ValueError, TypeError, OSError):
        return str(value)


def _days_ago_subtitle(added_at_raw: Any) -> str:
    """Build a human-readable 'Added N days ago' subtitle.

    Args:
        added_at_raw: A unix timestamp (int, float, or digit string).

    Returns:
        A string like ``"Added 3 days ago"``, ``"Added today"``, or ``""`` if
        the timestamp could not be parsed.
    """
    if not added_at_raw and added_at_raw != 0:
        return ""
    try:
        ts = float(added_at_raw)
    except (ValueError, TypeError):
        return ""

    now = time.time()
    delta_days = int((now - ts) / 86400)

    if delta_days == 0:
        return "Added today"
    if delta_days == 1:
        return "Added yesterday"
    return f"Added {delta_days} days ago"


def _is_recent_file(path: str) -> bool:
    """Return True if *path* exists and was modified less than 24 hours ago.

    Args:
        path: Absolute or relative filesystem path to check.

    Returns:
        ``True`` if the file is present and fresh, ``False`` otherwise.
    """
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return False
    return (time.time() - mtime) < _POSTER_MAX_AGE_SECONDS
