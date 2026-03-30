"""Unit tests for TmdbFetcher."""

from __future__ import annotations

import os
import time
from datetime import date, timedelta
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from providers.media_providers.tmdb_fetcher import TmdbFetcher
from providers.media_providers.types import FetchResult, MediaItem


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_client() -> MagicMock:
    """Create a mock TmdbClient that works as an async context manager."""
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


def _make_fetcher(
    popularity_threshold: float = 10.0,
    vote_count_threshold: int = 50,
    genre_filter: List[str] | None = None,
) -> TmdbFetcher:
    """Create a TmdbFetcher with a test API key (secrets patched at call site)."""
    with patch("providers.secrets.resolve_secret", return_value="test-key"):
        return TmdbFetcher(
            api_key_env="TMDB_API_KEY",
            poster_dir="/tmp/test-posters",
            popularity_threshold=popularity_threshold,
            vote_count_threshold=vote_count_threshold,
            genre_filter=genre_filter,
        )


def _movie(
    tmdb_id: int = 1,
    title: str = "Test Movie",
    popularity: float = 20.0,
    vote_count: int = 100,
    vote_average: float = 7.5,
    release_date: str = "2026-05-15",
    genre_ids: List[int] | None = None,
    poster_path: str = "/poster.jpg",
) -> dict:
    """Build a minimal TMDb movie result dict."""
    return {
        "id": tmdb_id,
        "title": title,
        "popularity": popularity,
        "vote_count": vote_count,
        "vote_average": vote_average,
        "release_date": release_date,
        "genre_ids": genre_ids or [28],
        "poster_path": poster_path,
        "overview": f"Overview of {title}",
    }


# ---------------------------------------------------------------------------
# TestApplyFilters
# ---------------------------------------------------------------------------

class TestApplyFilters:
    def test_filters_by_popularity(self):
        fetcher = _make_fetcher(popularity_threshold=15.0)
        items = [
            fetcher._normalize_movie(_movie(tmdb_id=1, popularity=20.0)),
            fetcher._normalize_movie(_movie(tmdb_id=2, popularity=5.0)),
            fetcher._normalize_movie(_movie(tmdb_id=3, popularity=15.0)),
        ]
        result = fetcher._apply_filters(items)
        ids = [i.tmdb_id for i in result]
        assert 2 not in ids
        assert 1 in ids
        assert 3 in ids

    def test_filters_by_vote_count(self):
        fetcher = _make_fetcher(vote_count_threshold=100)
        items = [
            fetcher._normalize_movie(_movie(tmdb_id=1, vote_count=200)),
            fetcher._normalize_movie(_movie(tmdb_id=2, vote_count=50)),
            fetcher._normalize_movie(_movie(tmdb_id=3, vote_count=100)),
        ]
        result = fetcher._apply_filters(items)
        ids = [i.tmdb_id for i in result]
        assert 2 not in ids
        assert 1 in ids
        assert 3 in ids

    def test_genre_filter_whitelist(self):
        fetcher = _make_fetcher(genre_filter=["Action", "Comedy"])
        # Manually build items with genre strings already set
        item_action = fetcher._normalize_movie(_movie(tmdb_id=1, genre_ids=[28]))
        item_action.genres = "Action"
        item_drama = fetcher._normalize_movie(_movie(tmdb_id=2, genre_ids=[18]))
        item_drama.genres = "Drama"
        item_comedy = fetcher._normalize_movie(_movie(tmdb_id=3, genre_ids=[35]))
        item_comedy.genres = "Comedy"

        result = fetcher._apply_filters([item_action, item_drama, item_comedy])
        ids = [i.tmdb_id for i in result]
        assert 1 in ids
        assert 2 not in ids
        assert 3 in ids

    def test_empty_genre_filter_allows_all(self):
        fetcher = _make_fetcher(genre_filter=[])
        items = [
            fetcher._normalize_movie(_movie(tmdb_id=1, genre_ids=[28])),
            fetcher._normalize_movie(_movie(tmdb_id=2, genre_ids=[18])),
        ]
        result = fetcher._apply_filters(items)
        assert len(result) == 2

    def test_sorts_by_popularity_descending(self):
        fetcher = _make_fetcher()
        items = [
            fetcher._normalize_movie(_movie(tmdb_id=1, popularity=15.0)),
            fetcher._normalize_movie(_movie(tmdb_id=2, popularity=50.0)),
            fetcher._normalize_movie(_movie(tmdb_id=3, popularity=25.0)),
        ]
        result = fetcher._apply_filters(items)
        assert result[0].tmdb_id == 2
        assert result[1].tmdb_id == 3
        assert result[2].tmdb_id == 1


# ---------------------------------------------------------------------------
# TestNormalizeMovie
# ---------------------------------------------------------------------------

class TestNormalizeMovie:
    def test_normalizes_standard_movie(self):
        fetcher = _make_fetcher()
        raw = _movie(
            tmdb_id=999,
            title="Big Film",
            popularity=30.0,
            vote_count=500,
            vote_average=8.0,
            release_date="2026-07-04",
            genre_ids=[28, 12],
            poster_path="/abc123.jpg",
        )
        item = fetcher._normalize_movie(raw)

        assert item.id == "tmdb-999"
        assert item.title == "Big Film"
        assert item.year == 2026
        assert item.poster_path == "/abc123.jpg"
        assert item.tmdb_score == 8.0
        assert item.tmdb_popularity == 30.0
        assert item.vote_count == 500
        assert item.release_date == "2026-07-04"
        assert item.source == "tmdb"
        assert item.tmdb_id == 999
        assert item.media_type == "movie"
        assert item.summary == "Overview of Big Film"
        assert item.local_poster == "tmdb-999.jpg"

    def test_handles_missing_poster_path(self):
        fetcher = _make_fetcher()
        raw = _movie(poster_path=None)
        item = fetcher._normalize_movie(raw)
        assert item.poster_path == ""

    def test_extracts_year_from_release_date(self):
        fetcher = _make_fetcher()
        raw = _movie(release_date="2026-05-02")
        item = fetcher._normalize_movie(raw)
        assert item.year == 2026

    def test_handles_empty_release_date(self):
        fetcher = _make_fetcher()
        raw = _movie(release_date="")
        item = fetcher._normalize_movie(raw)
        assert item.year == 0

    def test_stores_genre_ids_as_comma_separated_string(self):
        fetcher = _make_fetcher()
        raw = _movie(genre_ids=[28, 12, 878])
        item = fetcher._normalize_movie(raw)
        # genre_ids stored as comma-separated ID strings
        assert "28" in item.genres
        assert "12" in item.genres
        assert "878" in item.genres


# ---------------------------------------------------------------------------
# TestNormalizeTV
# ---------------------------------------------------------------------------

class TestNormalizeTV:
    def test_normalizes_tv_show(self):
        fetcher = _make_fetcher()
        raw = {
            "id": 42,
            "name": "Cool Show",
            "popularity": 25.0,
            "vote_count": 300,
            "vote_average": 7.8,
            "first_air_date": "2025-01-10",
            "genre_ids": [18, 10765],
            "poster_path": "/show.jpg",
            "overview": "A cool drama.",
        }
        item = fetcher._normalize_tv(raw)

        assert item.id == "tmdb-42"
        assert item.title == "Cool Show"
        assert item.year == 2025
        assert item.media_type == "tv"
        assert item.release_date == "2025-01-10"
        assert item.source == "tmdb"
        assert item.tmdb_id == 42

    def test_handles_missing_name(self):
        fetcher = _make_fetcher()
        raw = {"id": 7, "popularity": 20.0, "vote_count": 100}
        item = fetcher._normalize_tv(raw)
        assert item.title == ""
        assert item.year == 0


# ---------------------------------------------------------------------------
# TestFetchInTheaters
# ---------------------------------------------------------------------------

class TestFetchInTheaters:
    @pytest.mark.asyncio
    async def test_merges_now_playing_and_trending(self):
        mock_client = _make_mock_client()
        mock_client.get_now_playing = AsyncMock(
            return_value={"results": [_movie(tmdb_id=1, popularity=50.0)]}
        )
        mock_client.get_trending = AsyncMock(
            return_value={"results": [_movie(tmdb_id=2, popularity=40.0)]}
        )

        with patch(
            "providers.media_providers.tmdb_fetcher.TmdbClient",
            return_value=mock_client,
        ), patch("providers.secrets.resolve_secret", return_value="test-key"):
            fetcher = TmdbFetcher(
                api_key_env="TMDB_API_KEY",
                poster_dir="/tmp/test",
            )
            result = await fetcher.fetch_in_theaters()

        assert result.status == "ok"
        ids = [i.tmdb_id for i in result.items]
        assert 1 in ids
        assert 2 in ids

    @pytest.mark.asyncio
    async def test_deduplicates_by_tmdb_id(self):
        mock_client = _make_mock_client()
        shared_movie = _movie(tmdb_id=5, title="Same Movie", popularity=30.0)
        mock_client.get_now_playing = AsyncMock(
            return_value={"results": [shared_movie]}
        )
        mock_client.get_trending = AsyncMock(
            return_value={"results": [shared_movie]}
        )

        with patch(
            "providers.media_providers.tmdb_fetcher.TmdbClient",
            return_value=mock_client,
        ), patch("providers.secrets.resolve_secret", return_value="test-key"):
            fetcher = TmdbFetcher(
                api_key_env="TMDB_API_KEY",
                poster_dir="/tmp/test",
            )
            result = await fetcher.fetch_in_theaters()

        assert result.status == "ok"
        assert len([i for i in result.items if i.tmdb_id == 5]) == 1

    @pytest.mark.asyncio
    async def test_sets_release_type_in_theaters(self):
        mock_client = _make_mock_client()
        mock_client.get_now_playing = AsyncMock(
            return_value={"results": [_movie(tmdb_id=1, popularity=50.0)]}
        )
        mock_client.get_trending = AsyncMock(
            return_value={"results": []}
        )

        with patch(
            "providers.media_providers.tmdb_fetcher.TmdbClient",
            return_value=mock_client,
        ), patch("providers.secrets.resolve_secret", return_value="test-key"):
            fetcher = TmdbFetcher(
                api_key_env="TMDB_API_KEY",
                poster_dir="/tmp/test",
            )
            result = await fetcher.fetch_in_theaters()

        assert result.items[0].release_type == "in_theaters"

    @pytest.mark.asyncio
    async def test_returns_error_on_failure(self):
        mock_client = _make_mock_client()
        mock_client.get_now_playing = AsyncMock(side_effect=Exception("network error"))

        with patch(
            "providers.media_providers.tmdb_fetcher.TmdbClient",
            return_value=mock_client,
        ), patch("providers.secrets.resolve_secret", return_value="test-key"):
            fetcher = TmdbFetcher(
                api_key_env="TMDB_API_KEY",
                poster_dir="/tmp/test",
            )
            result = await fetcher.fetch_in_theaters()

        assert result.status == "error"
        assert "network error" in result.error_message
        assert result.items == []


# ---------------------------------------------------------------------------
# TestFetchComingSoon
# ---------------------------------------------------------------------------

class TestFetchComingSoon:
    @pytest.mark.asyncio
    async def test_merges_upcoming_and_discover(self):
        mock_client = _make_mock_client()
        mock_client.get_upcoming = AsyncMock(
            return_value={"results": [_movie(tmdb_id=10, popularity=30.0, release_date="2026-04-10")]}
        )
        mock_client.get_discover_movies = AsyncMock(
            return_value={"results": [_movie(tmdb_id=20, popularity=25.0, release_date="2026-04-20")]}
        )

        with patch(
            "providers.media_providers.tmdb_fetcher.TmdbClient",
            return_value=mock_client,
        ), patch("providers.secrets.resolve_secret", return_value="test-key"):
            fetcher = TmdbFetcher(
                api_key_env="TMDB_API_KEY",
                poster_dir="/tmp/test",
            )
            result = await fetcher.fetch_coming_soon()

        assert result.status == "ok"
        ids = [i.tmdb_id for i in result.items]
        assert 10 in ids
        assert 20 in ids

    @pytest.mark.asyncio
    async def test_deduplicates_across_sources(self):
        shared_movie = _movie(tmdb_id=99, popularity=40.0, release_date="2026-05-01")
        mock_client = _make_mock_client()
        mock_client.get_upcoming = AsyncMock(
            return_value={"results": [shared_movie]}
        )
        mock_client.get_discover_movies = AsyncMock(
            return_value={"results": [shared_movie]}
        )

        with patch(
            "providers.media_providers.tmdb_fetcher.TmdbClient",
            return_value=mock_client,
        ), patch("providers.secrets.resolve_secret", return_value="test-key"):
            fetcher = TmdbFetcher(
                api_key_env="TMDB_API_KEY",
                poster_dir="/tmp/test",
            )
            result = await fetcher.fetch_coming_soon()

        assert len([i for i in result.items if i.tmdb_id == 99]) == 1

    @pytest.mark.asyncio
    async def test_sets_release_types(self):
        mock_client = _make_mock_client()
        mock_client.get_upcoming = AsyncMock(
            return_value={"results": [_movie(tmdb_id=1, popularity=30.0, release_date="2026-05-01")]}
        )
        mock_client.get_discover_movies = AsyncMock(
            return_value={"results": [_movie(tmdb_id=2, popularity=25.0, release_date="2026-05-10")]}
        )

        with patch(
            "providers.media_providers.tmdb_fetcher.TmdbClient",
            return_value=mock_client,
        ), patch("providers.secrets.resolve_secret", return_value="test-key"):
            fetcher = TmdbFetcher(
                api_key_env="TMDB_API_KEY",
                poster_dir="/tmp/test",
            )
            result = await fetcher.fetch_coming_soon()

        item_by_id = {i.tmdb_id: i for i in result.items}
        assert item_by_id[1].release_type == "theatrical"
        assert item_by_id[2].release_type == "digital"

    @pytest.mark.asyncio
    async def test_sorts_by_release_date_ascending(self):
        mock_client = _make_mock_client()
        mock_client.get_upcoming = AsyncMock(
            return_value={
                "results": [
                    _movie(tmdb_id=1, popularity=30.0, release_date="2026-06-15"),
                    _movie(tmdb_id=2, popularity=25.0, release_date="2026-04-01"),
                ]
            }
        )
        mock_client.get_discover_movies = AsyncMock(return_value={"results": []})

        with patch(
            "providers.media_providers.tmdb_fetcher.TmdbClient",
            return_value=mock_client,
        ), patch("providers.secrets.resolve_secret", return_value="test-key"):
            fetcher = TmdbFetcher(
                api_key_env="TMDB_API_KEY",
                poster_dir="/tmp/test",
            )
            result = await fetcher.fetch_coming_soon()

        assert result.items[0].tmdb_id == 2   # earlier date first
        assert result.items[1].tmdb_id == 1

    @pytest.mark.asyncio
    async def test_returns_error_on_failure(self):
        mock_client = _make_mock_client()
        mock_client.get_upcoming = AsyncMock(side_effect=RuntimeError("timeout"))

        with patch(
            "providers.media_providers.tmdb_fetcher.TmdbClient",
            return_value=mock_client,
        ), patch("providers.secrets.resolve_secret", return_value="test-key"):
            fetcher = TmdbFetcher(
                api_key_env="TMDB_API_KEY",
                poster_dir="/tmp/test",
            )
            result = await fetcher.fetch_coming_soon()

        assert result.status == "error"
        assert "timeout" in result.error_message
        assert result.items == []


# ---------------------------------------------------------------------------
# TestDownloadPosters
# ---------------------------------------------------------------------------

class TestDownloadPosters:
    @pytest.mark.asyncio
    async def test_downloads_to_correct_path(self, tmp_path):
        mock_client = _make_mock_client()
        mock_client.download_poster = AsyncMock(return_value=b"FAKE_IMAGE_DATA")

        item = MediaItem(
            id="tmdb-42",
            title="Some Film",
            poster_path="/abc.jpg",
            tmdb_id=42,
        )

        with patch(
            "providers.media_providers.tmdb_fetcher.TmdbClient",
            return_value=mock_client,
        ), patch("providers.secrets.resolve_secret", return_value="test-key"):
            fetcher = TmdbFetcher(
                api_key_env="TMDB_API_KEY",
                poster_dir=str(tmp_path),
            )
            count = await fetcher.download_posters([item])

        assert count == 1
        expected_path = tmp_path / "tmdb-42.jpg"
        assert expected_path.exists()
        assert expected_path.read_bytes() == b"FAKE_IMAGE_DATA"
        assert item.local_poster == "tmdb-42.jpg"

    @pytest.mark.asyncio
    async def test_skips_existing_recent_file(self, tmp_path):
        mock_client = _make_mock_client()
        mock_client.download_poster = AsyncMock(return_value=b"NEW_DATA")

        # Create an existing file that's only 1 hour old
        existing = tmp_path / "tmdb-10.jpg"
        existing.write_bytes(b"OLD_DATA")
        recent_mtime = time.time() - 3600  # 1 hour ago
        os.utime(existing, (recent_mtime, recent_mtime))

        item = MediaItem(
            id="tmdb-10",
            title="Already Cached",
            poster_path="/cached.jpg",
            tmdb_id=10,
        )

        with patch(
            "providers.media_providers.tmdb_fetcher.TmdbClient",
            return_value=mock_client,
        ), patch("providers.secrets.resolve_secret", return_value="test-key"):
            fetcher = TmdbFetcher(
                api_key_env="TMDB_API_KEY",
                poster_dir=str(tmp_path),
            )
            count = await fetcher.download_posters([item])

        assert count == 0
        mock_client.download_poster.assert_not_called()
        assert item.local_poster == "tmdb-10.jpg"
        # File content should be unchanged
        assert existing.read_bytes() == b"OLD_DATA"

    @pytest.mark.asyncio
    async def test_downloads_when_file_is_stale(self, tmp_path):
        mock_client = _make_mock_client()
        mock_client.download_poster = AsyncMock(return_value=b"FRESH_DATA")

        # Create a stale file (25 hours old)
        existing = tmp_path / "tmdb-55.jpg"
        existing.write_bytes(b"STALE_DATA")
        stale_mtime = time.time() - 90000  # 25 hours ago
        os.utime(existing, (stale_mtime, stale_mtime))

        item = MediaItem(
            id="tmdb-55",
            title="Stale File",
            poster_path="/stale.jpg",
            tmdb_id=55,
        )

        with patch(
            "providers.media_providers.tmdb_fetcher.TmdbClient",
            return_value=mock_client,
        ), patch("providers.secrets.resolve_secret", return_value="test-key"):
            fetcher = TmdbFetcher(
                api_key_env="TMDB_API_KEY",
                poster_dir=str(tmp_path),
            )
            count = await fetcher.download_posters([item])

        assert count == 1
        assert existing.read_bytes() == b"FRESH_DATA"

    @pytest.mark.asyncio
    async def test_skips_items_without_poster_path(self, tmp_path):
        mock_client = _make_mock_client()
        mock_client.download_poster = AsyncMock(return_value=b"DATA")

        item = MediaItem(
            id="tmdb-77",
            title="No Poster",
            poster_path="",
            tmdb_id=77,
        )

        with patch(
            "providers.media_providers.tmdb_fetcher.TmdbClient",
            return_value=mock_client,
        ), patch("providers.secrets.resolve_secret", return_value="test-key"):
            fetcher = TmdbFetcher(
                api_key_env="TMDB_API_KEY",
                poster_dir=str(tmp_path),
            )
            count = await fetcher.download_posters([item])

        assert count == 0
        mock_client.download_poster.assert_not_called()
        assert item.local_poster == ""

    @pytest.mark.asyncio
    async def test_continues_on_per_item_error(self, tmp_path):
        mock_client = _make_mock_client()
        mock_client.download_poster = AsyncMock(
            side_effect=[Exception("CDN error"), b"GOOD_DATA"]
        )

        items = [
            MediaItem(id="tmdb-1", title="Bad", poster_path="/bad.jpg", tmdb_id=1),
            MediaItem(id="tmdb-2", title="Good", poster_path="/good.jpg", tmdb_id=2),
        ]

        with patch(
            "providers.media_providers.tmdb_fetcher.TmdbClient",
            return_value=mock_client,
        ), patch("providers.secrets.resolve_secret", return_value="test-key"):
            fetcher = TmdbFetcher(
                api_key_env="TMDB_API_KEY",
                poster_dir=str(tmp_path),
            )
            count = await fetcher.download_posters(items)

        # First failed, second succeeded
        assert count == 1
        assert items[0].local_poster == ""
        assert items[1].local_poster == "tmdb-2.jpg"
