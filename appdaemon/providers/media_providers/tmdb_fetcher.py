"""TMDb data fetcher — normalises movie/TV data into MediaItem objects.

Wraps TmdbClient to provide higher-level operations:
- fetch_in_theaters: now_playing + trending, deduplicated, filtered
- fetch_coming_soon: upcoming + discover(digital/streaming), merged
- fetch_detail: full metadata for a single item
- download_posters: cache poster images to local disk

Pure Python — no AppDaemon dependency.
"""

from __future__ import annotations

import logging
import os
from datetime import date, timedelta
from typing import Dict, List, Optional

from .tmdb_client import TmdbClient
from .types import FetchResult, MediaItem

logger = logging.getLogger(__name__)


class TmdbFetcher:
    """Fetches and normalises movie/TV data from TMDb.

    Args:
        api_key_env: Name of the environment variable holding the TMDb API key.
        poster_dir: Absolute filesystem path where poster images are cached.
        poster_width: TMDb image width bucket (e.g. 342, 500). Default 342.
        popularity_threshold: Minimum TMDb popularity score. Default 10.0.
        vote_count_threshold: Minimum vote count to include. Default 50.
        genre_filter: Optional list of genre strings; empty list = allow all.
    """

    def __init__(
        self,
        api_key_env: str,
        poster_dir: str,
        poster_width: int = 342,
        popularity_threshold: float = 10.0,
        vote_count_threshold: int = 50,
        genre_filter: Optional[List[str]] = None,
    ) -> None:
        from providers.secrets import resolve_secret

        self._api_key = resolve_secret(api_key_env)
        self._poster_dir = poster_dir
        self._poster_size = f"w{poster_width}"
        self._popularity_threshold = popularity_threshold
        self._vote_count_threshold = vote_count_threshold
        self._genre_filter: List[str] = genre_filter or []
        self._genre_map: Dict[int, str] = {}

    # -- Client factory -------------------------------------------------------

    def _create_client(self) -> TmdbClient:
        """Create a new TmdbClient for use within an async context."""
        return TmdbClient(api_key=self._api_key)

    async def _ensure_genre_map(self) -> None:
        """Populate _genre_map with {id: name} from TMDb if not already loaded.

        Called at the start of fetch_in_theaters() and fetch_coming_soon() so
        that _normalize_movie() can resolve genre IDs to human-readable names.
        No-op if the map is already populated.
        """
        if self._genre_map:
            return
        try:
            async with self._create_client() as client:
                data = await client.get_genre_list()
            genres = data.get("genres", [])
            self._genre_map = {g["id"]: g["name"] for g in genres if "id" in g and "name" in g}
            logger.info("TmdbFetcher._ensure_genre_map: loaded %d genres", len(self._genre_map))
        except Exception as exc:
            logger.warning("TmdbFetcher._ensure_genre_map: failed to load genre map: %s", exc)

    # -- Public fetch methods -------------------------------------------------

    async def fetch_in_theaters(self) -> FetchResult:
        """Fetch movies currently in theaters.

        Merges results from /3/movie/now_playing and /3/trending/movie/week,
        deduplicates by TMDb ID, applies popularity/vote filters, and returns
        items sorted by popularity descending.

        Returns:
            FetchResult with status="ok" on success, "error" on failure.
        """
        await self._ensure_genre_map()

        try:
            async with self._create_client() as client:
                now_playing_resp = await client.get_now_playing()
                trending_resp = await client.get_trending("movie", "week")
        except Exception as exc:
            logger.error(
                "TmdbFetcher.fetch_in_theaters failed: %s", exc, exc_info=True
            )
            return FetchResult(status="error", error_message=str(exc))

        now_playing = now_playing_resp.get("results", [])
        trending = trending_resp.get("results", [])

        # Merge and deduplicate by TMDb numeric ID
        seen: set[int] = set()
        merged: List[MediaItem] = []
        for raw in now_playing + trending:
            tmdb_id = raw.get("id")
            if tmdb_id is None or tmdb_id in seen:
                continue
            seen.add(tmdb_id)
            item = self._normalize_movie(raw)
            item.release_type = "in_theaters"
            merged.append(item)

        filtered = self._apply_filters(merged)
        logger.info(
            "TmdbFetcher.fetch_in_theaters: %d raw -> %d after filters",
            len(merged),
            len(filtered),
        )
        return FetchResult(items=filtered)

    async def fetch_coming_soon(self) -> FetchResult:
        """Fetch upcoming theatrical and digital/streaming releases.

        Merges /3/movie/upcoming (theatrical) with /3/discover/movie filtered
        for digital releases in the next 90 days, deduplicates by TMDb ID,
        applies filters, and returns items sorted by release_date ascending.

        Returns:
            FetchResult with status="ok" on success, "error" on failure.
        """
        await self._ensure_genre_map()

        today = date.today()
        date_lte = today + timedelta(days=90)

        try:
            async with self._create_client() as client:
                upcoming_resp = await client.get_upcoming(region="US")
                discover_resp = await client.get_discover_movies(
                    with_release_type="4",
                    **{
                        "primary_release_date.gte": today.isoformat(),
                        "primary_release_date.lte": date_lte.isoformat(),
                    },
                    sort_by="popularity.desc",
                )
        except Exception as exc:
            logger.error(
                "TmdbFetcher.fetch_coming_soon failed: %s", exc, exc_info=True
            )
            return FetchResult(status="error", error_message=str(exc))

        upcoming = upcoming_resp.get("results", [])
        discover = discover_resp.get("results", [])

        # Merge: upcoming = theatrical, discover = digital
        seen: set[int] = set()
        merged: List[MediaItem] = []
        for raw in upcoming:
            tmdb_id = raw.get("id")
            if tmdb_id is None or tmdb_id in seen:
                continue
            seen.add(tmdb_id)
            item = self._normalize_movie(raw)
            item.release_type = "theatrical"
            # Subtitle: "Opens May 2" style
            if item.release_date:
                try:
                    d = date.fromisoformat(item.release_date)
                    item.subtitle = d.strftime("Opens %b %-d")
                except ValueError:
                    pass
            merged.append(item)

        for raw in discover:
            tmdb_id = raw.get("id")
            if tmdb_id is None or tmdb_id in seen:
                continue
            seen.add(tmdb_id)
            item = self._normalize_movie(raw)
            item.release_type = "digital"
            # Subtitle: "May 2" style
            if item.release_date:
                try:
                    d = date.fromisoformat(item.release_date)
                    item.subtitle = d.strftime("%b %-d")
                except ValueError:
                    pass
            merged.append(item)

        # For coming soon, only require future release date and popularity threshold.
        # Skip vote_count filter — unreleased movies have few votes by definition.
        today_str = today.isoformat()
        filtered = []
        for item in merged:
            if not item.release_date or item.release_date < today_str:
                continue  # Past releases don't belong in Coming Soon
            if item.tmdb_popularity < self._popularity_threshold:
                continue  # Still require minimum popularity
            if self._genre_filter:
                item_genres = {g.strip() for g in item.genres.split(",")} if item.genres else set()
                if not item_genres.intersection(self._genre_filter):
                    continue
            filtered.append(item)

        # Sort by release_date ascending (soonest first)
        filtered.sort(key=lambda x: x.release_date)

        logger.info(
            "TmdbFetcher.fetch_coming_soon: %d raw -> %d future+filtered",
            len(merged),
            len(filtered),
        )
        return FetchResult(items=filtered)

    async def fetch_detail(
        self, tmdb_id: int, media_type: str = "movie"
    ) -> MediaItem:
        """Fetch full metadata for a single movie or TV show.

        Args:
            tmdb_id: TMDb numeric ID.
            media_type: "movie" or "tv".

        Returns:
            Fully-populated MediaItem with summary field set.
        """
        async with self._create_client() as client:
            if media_type == "tv":
                raw = await client.get_tv_detail(tmdb_id)
                item = self._normalize_tv(raw)
            else:
                raw = await client.get_movie_detail(
                    tmdb_id, append_to_response="release_dates,credits"
                )
                item = self._normalize_movie(raw)

        # Detail endpoint returns genre objects with name; overwrite genres field
        genre_objs = raw.get("genres", [])
        if genre_objs:
            item.genres = ", ".join(g.get("name", "") for g in genre_objs if g.get("name"))

        # Runtime from detail endpoint
        if media_type != "tv":
            item.runtime_min = raw.get("runtime") or 0

        # Extract revenue and tagline
        item.revenue = raw.get("revenue", 0) or 0
        item.tagline = raw.get("tagline", "") or ""

        # Extract US MPAA certification from release_dates
        if media_type != "tv":
            for country in raw.get("release_dates", {}).get("results", []):
                if country.get("iso_3166_1") == "US":
                    for rd in country.get("release_dates", []):
                        cert = rd.get("certification", "")
                        if cert:
                            item.certification = cert
                            item.rating = cert  # Fix the MPAA rating bug
                            break
                    break

        # Extract director from credits crew
        crew = raw.get("credits", {}).get("crew", [])
        directors = [c["name"] for c in crew if c.get("job") == "Director"]
        if directors:
            item.director = ", ".join(directors[:2])  # Cap at 2 directors

        logger.debug(
            "TmdbFetcher.fetch_detail: tmdb_id=%d title=%s "
            "cert=%r director=%r revenue=%d tagline=%r",
            tmdb_id,
            item.title,
            item.certification,
            item.director,
            item.revenue,
            item.tagline,
        )
        return item

    async def download_posters(self, items: List[MediaItem]) -> int:
        """Download poster images to disk for each item that has a poster_path.

        Files are saved to ``{poster_dir}/tmdb-{id}.jpg``.  Existing files
        younger than 24 hours are skipped.  Failures per-item are logged and
        do not abort the batch.

        Args:
            items: List of MediaItem objects.  ``item.local_poster`` is set on
                   each item after a successful download.

        Returns:
            Count of posters actually downloaded (not skipped).
        """
        os.makedirs(self._poster_dir, exist_ok=True)
        downloaded = 0

        async with self._create_client() as client:
            for item in items:
                if not item.poster_path:
                    continue

                fname = f"tmdb-{item.tmdb_id}.jpg"
                dest = os.path.join(self._poster_dir, fname)

                # Skip if file exists and is < 24h old
                if os.path.exists(dest):
                    age_s = _file_age_seconds(dest)
                    if age_s < 86400:
                        item.local_poster = fname
                        logger.debug(
                            "TmdbFetcher.download_posters: skip %s (age %.0fs)",
                            fname,
                            age_s,
                        )
                        continue

                try:
                    data = await client.download_poster(
                        item.poster_path, size=self._poster_size
                    )
                    with open(dest, "wb") as fh:
                        fh.write(data)
                    item.local_poster = fname
                    downloaded += 1
                    logger.debug(
                        "TmdbFetcher.download_posters: downloaded %s (%d bytes)",
                        fname,
                        len(data),
                    )
                except Exception as exc:
                    logger.warning(
                        "TmdbFetcher.download_posters: failed for %s: %s",
                        fname,
                        exc,
                    )

        logger.info(
            "TmdbFetcher.download_posters: %d downloaded, %d items processed",
            downloaded,
            len(items),
        )
        return downloaded

    # -- Filtering & sorting --------------------------------------------------

    def _apply_filters(self, items: List[MediaItem]) -> List[MediaItem]:
        """Filter and sort a list of MediaItems.

        Applies:
        1. Popularity threshold (tmdb_popularity >= threshold).
        2. Vote count threshold (vote_count >= threshold).
        3. Genre whitelist if genre_filter is non-empty.

        Items are sorted by tmdb_popularity descending.

        Args:
            items: Input list of MediaItems.

        Returns:
            Filtered and sorted list.
        """
        result = []
        for item in items:
            if item.tmdb_popularity < self._popularity_threshold:
                continue
            if item.vote_count < self._vote_count_threshold:
                continue
            if self._genre_filter:
                # genres is stored as comma-separated string; check for any match
                item_genres = [g.strip() for g in item.genres.split(",") if g.strip()]
                if not any(g in self._genre_filter for g in item_genres):
                    continue
            result.append(item)

        result.sort(key=lambda x: x.tmdb_popularity, reverse=True)
        return result

    # -- Normalization helpers -------------------------------------------------

    def _normalize_movie(self, raw: dict) -> MediaItem:
        """Map a TMDb movie response dict to a MediaItem.

        Args:
            raw: Raw dict from TMDb movie response (now_playing, upcoming,
                 trending, discover, or detail).

        Returns:
            Populated MediaItem (release_type and subtitle set to default "").
        """
        tmdb_id = raw.get("id")
        release_date = raw.get("release_date", "")
        year = 0
        if release_date and len(release_date) >= 4:
            try:
                year = int(release_date[:4])
            except ValueError:
                pass

        # genre_ids list (from list endpoints) → resolve via genre_map when available
        # Full genre names come from the detail endpoint or genre_map
        genre_ids = raw.get("genre_ids", [])
        genres_str = ""
        if genre_ids and self._genre_map:
            genres_str = ", ".join(self._genre_map.get(gid, str(gid)) for gid in genre_ids)
        elif genre_ids:
            genres_str = ", ".join(str(g) for g in genre_ids)
        # Detail endpoint may provide genre objects instead
        genre_objs = raw.get("genres", [])
        if genre_objs:
            genres_str = ", ".join(g.get("name", "") for g in genre_objs if g.get("name"))

        return MediaItem(
            id=f"tmdb-{tmdb_id}",
            title=raw.get("title", ""),
            year=year,
            poster_path=raw.get("poster_path") or "",
            local_poster=f"tmdb-{tmdb_id}.jpg" if tmdb_id is not None else "",
            media_type="movie",
            summary=raw.get("overview", ""),
            release_date=release_date,
            tmdb_score=raw.get("vote_average", 0.0),
            tmdb_popularity=raw.get("popularity", 0.0),
            vote_count=raw.get("vote_count", 0),
            genres=genres_str,
            source="tmdb",
            tmdb_id=tmdb_id,
        )

    def _normalize_tv(self, raw: dict) -> MediaItem:
        """Map a TMDb TV series response dict to a MediaItem.

        Args:
            raw: Raw dict from TMDb TV response (trending or detail).

        Returns:
            Populated MediaItem with media_type="tv".
        """
        tmdb_id = raw.get("id")
        release_date = raw.get("first_air_date", "")
        year = 0
        if release_date and len(release_date) >= 4:
            try:
                year = int(release_date[:4])
            except ValueError:
                pass

        genre_ids = raw.get("genre_ids", [])
        genres_str = ""
        if genre_ids:
            genres_str = ", ".join(str(g) for g in genre_ids)
        genre_objs = raw.get("genres", [])
        if genre_objs:
            genres_str = ", ".join(g.get("name", "") for g in genre_objs if g.get("name"))

        return MediaItem(
            id=f"tmdb-{tmdb_id}",
            title=raw.get("name", ""),
            year=year,
            poster_path=raw.get("poster_path") or "",
            local_poster=f"tmdb-{tmdb_id}.jpg" if tmdb_id is not None else "",
            media_type="tv",
            summary=raw.get("overview", ""),
            release_date=release_date,
            tmdb_score=raw.get("vote_average", 0.0),
            tmdb_popularity=raw.get("popularity", 0.0),
            vote_count=raw.get("vote_count", 0),
            genres=genres_str,
            source="tmdb",
            tmdb_id=tmdb_id,
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _file_age_seconds(path: str) -> float:
    """Return age of a file in seconds since last modification."""
    import time

    return time.time() - os.path.getmtime(path)
