"""Unit tests for TautulliFetcher."""

from __future__ import annotations

import os
import time
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

import pytest

from providers.media_providers.tautulli_fetcher import TautulliFetcher
from providers.media_providers.types import FetchResult, MediaItem


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_client() -> MagicMock:
    """Create a mock TautulliClient usable as an async context manager."""
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


def _make_fetcher(poster_dir: str = "/tmp/posters") -> TautulliFetcher:
    """Construct a TautulliFetcher with patched resolve_secret."""
    with patch("providers.secrets.resolve_secret", return_value="test-key"):
        return TautulliFetcher(
            base_url="http://tautulli.test:8181",
            api_key_env="TAUTULLI_API_KEY",
            poster_dir=poster_dir,
        )


def _raw_movie(**overrides) -> dict:
    """Return a minimal raw Tautulli movie dict."""
    base = {
        "rating_key": "1001",
        "title": "Test Movie",
        "year": "2024",
        "media_type": "movie",
        "content_rating": "PG-13",
        "duration": "7200000",  # 120 min in ms
        "genres": ["Action", "Sci-Fi"],
        "summary": "A test movie summary.",
        "added_at": "1711670400",  # 2024-03-28 12:00:00 UTC (fixed past date)
        "guids": [{"id": "tmdb://12345"}, {"id": "imdb://tt9876543"}],
    }
    base.update(overrides)
    return base


def _raw_show(**overrides) -> dict:
    """Return a minimal raw Tautulli show dict."""
    base = {
        "rating_key": "2001",
        "title": "Test Show",
        "year": "2023",
        "media_type": "show",
        "content_rating": "TV-14",
        "duration": "0",
        "genres": "Drama",
        "summary": "A test show summary.",
        "added_at": "1711584000",  # one day earlier
        "guids": [{"id": "tmdb://99999"}],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# TestParseGuids
# ---------------------------------------------------------------------------

class TestParseGuids:
    def _fetcher(self) -> TautulliFetcher:
        return _make_fetcher()

    def test_extracts_tmdb_id_from_dict_format(self):
        fetcher = self._fetcher()
        result = fetcher._parse_guids([{"id": "tmdb://12345"}, {"id": "imdb://tt0000001"}])
        assert result == 12345

    def test_extracts_tmdb_id_from_string_format(self):
        fetcher = self._fetcher()
        result = fetcher._parse_guids(
            ["com.plexapp.agents.imdb://tt0000001?lang=en",
             "com.plexapp.agents.tmdb://12345?lang=en"]
        )
        assert result == 12345

    def test_returns_none_for_no_tmdb(self):
        fetcher = self._fetcher()
        result = fetcher._parse_guids([{"id": "imdb://tt1234567"}])
        assert result is None

    def test_returns_none_for_empty(self):
        fetcher = self._fetcher()
        result = fetcher._parse_guids([])
        assert result is None

    def test_returns_none_for_none(self):
        fetcher = self._fetcher()
        result = fetcher._parse_guids(None)
        assert result is None

    def test_returns_first_tmdb_id_when_multiple_present(self):
        fetcher = self._fetcher()
        result = fetcher._parse_guids([
            {"id": "tmdb://111"},
            {"id": "tmdb://222"},
        ])
        assert result == 111

    def test_handles_mixed_string_and_dict_entries(self):
        fetcher = self._fetcher()
        result = fetcher._parse_guids([
            "imdb://tt0000001",
            {"id": "tmdb://5678"},
        ])
        assert result == 5678


# ---------------------------------------------------------------------------
# TestNormalizeItem
# ---------------------------------------------------------------------------

class TestNormalizeItem:
    def _fetcher(self) -> TautulliFetcher:
        return _make_fetcher()

    def test_normalizes_movie_item(self):
        fetcher = self._fetcher()
        raw = _raw_movie()
        item = fetcher._normalize_item(raw)

        assert item.id == "plex-1001"
        assert item.title == "Test Movie"
        assert item.year == 2024
        assert item.media_type == "movie"
        assert item.rating == "PG-13"
        assert item.runtime_min == 120
        assert item.genres == "Action, Sci-Fi"
        assert item.summary == "A test movie summary."
        assert item.source == "tautulli"
        assert item.tmdb_id == 12345
        assert item.local_poster == "plex-1001.jpg"

    def test_normalizes_tv_show(self):
        fetcher = self._fetcher()
        raw = _raw_show()
        item = fetcher._normalize_item(raw)

        assert item.id == "plex-2001"
        assert item.media_type == "tv"
        assert item.tmdb_id == 99999

    def test_genres_as_string_passthrough(self):
        fetcher = self._fetcher()
        raw = _raw_show(genres="Drama, Thriller")
        item = fetcher._normalize_item(raw)
        assert item.genres == "Drama, Thriller"

    def test_genres_as_list_joined(self):
        fetcher = self._fetcher()
        raw = _raw_movie(genres=["Action", "Comedy"])
        item = fetcher._normalize_item(raw)
        assert item.genres == "Action, Comedy"

    def test_computes_subtitle_days_ago(self):
        fetcher = self._fetcher()
        # Use a timestamp 5 days in the past.
        past_ts = int(time.time()) - (5 * 86400 + 3600)
        raw = _raw_movie(added_at=str(past_ts))
        item = fetcher._normalize_item(raw)
        assert item.subtitle == "Added 5 days ago"

    def test_computes_subtitle_today(self):
        fetcher = self._fetcher()
        raw = _raw_movie(added_at=str(int(time.time()) - 60))
        item = fetcher._normalize_item(raw)
        assert item.subtitle == "Added today"

    def test_computes_subtitle_yesterday(self):
        fetcher = self._fetcher()
        past_ts = int(time.time()) - (1 * 86400 + 3600)
        raw = _raw_movie(added_at=str(past_ts))
        item = fetcher._normalize_item(raw)
        assert item.subtitle == "Added yesterday"

    def test_handles_missing_fields(self):
        fetcher = self._fetcher()
        # Minimal dict — should not crash.
        item = fetcher._normalize_item({})
        assert item.id == "plex-"
        assert item.title == ""
        assert item.year == 0
        assert item.runtime_min == 0
        assert item.tmdb_id is None
        assert item.source == "tautulli"

    def test_added_at_stored_as_iso(self):
        fetcher = self._fetcher()
        raw = _raw_movie(added_at="1711670400")
        item = fetcher._normalize_item(raw)
        # 1711670400 == 2024-03-29T00:00:00 UTC
        assert item.added_at.startswith("2024-03-29")

    def test_duration_zero_gives_zero_runtime(self):
        fetcher = self._fetcher()
        raw = _raw_movie(duration="0")
        item = fetcher._normalize_item(raw)
        assert item.runtime_min == 0


# ---------------------------------------------------------------------------
# TestFetchRecentlyAdded
# ---------------------------------------------------------------------------

class TestFetchRecentlyAdded:
    @pytest.mark.asyncio
    async def test_returns_items_on_success(self):
        mock_client = _make_mock_client()
        mock_client.get_recently_added = AsyncMock(side_effect=[
            [_raw_movie()],  # movies
            [_raw_show()],   # shows
        ])

        with patch(
            "providers.media_providers.tautulli_fetcher.TautulliClient",
            return_value=mock_client,
        ), patch("providers.secrets.resolve_secret", return_value="test-key"):
            fetcher = TautulliFetcher(
                base_url="http://tautulli.test:8181",
                api_key_env="TAUTULLI_API_KEY",
                poster_dir="/tmp/posters",
            )
            result = await fetcher.fetch_recently_added(count=25)

        assert isinstance(result, FetchResult)
        assert result.status == "ok"
        assert result.error_message == ""
        assert len(result.items) == 2
        assert result.items[0].id == "plex-1001"
        assert result.items[1].id == "plex-2001"

    @pytest.mark.asyncio
    async def test_returns_error_on_failure(self):
        mock_client = _make_mock_client()
        mock_client.get_recently_added = AsyncMock(
            side_effect=RuntimeError("Tautulli API unavailable")
        )

        with patch(
            "providers.media_providers.tautulli_fetcher.TautulliClient",
            return_value=mock_client,
        ), patch("providers.secrets.resolve_secret", return_value="test-key"):
            fetcher = TautulliFetcher(
                base_url="http://tautulli.test:8181",
                api_key_env="TAUTULLI_API_KEY",
                poster_dir="/tmp/posters",
            )
            result = await fetcher.fetch_recently_added()

        assert result.status == "error"
        assert "Tautulli API unavailable" in result.error_message
        assert result.items == []

    @pytest.mark.asyncio
    async def test_fetches_both_movies_and_shows(self):
        mock_client = _make_mock_client()
        mock_client.get_recently_added = AsyncMock(return_value=[])

        with patch(
            "providers.media_providers.tautulli_fetcher.TautulliClient",
            return_value=mock_client,
        ), patch("providers.secrets.resolve_secret", return_value="test-key"):
            fetcher = TautulliFetcher(
                base_url="http://tautulli.test:8181",
                api_key_env="TAUTULLI_API_KEY",
                poster_dir="/tmp/posters",
            )
            await fetcher.fetch_recently_added(count=10)

        assert mock_client.get_recently_added.call_count == 2
        calls = mock_client.get_recently_added.call_args_list
        media_types_called = {c.kwargs.get("media_type") or c.args[1] for c in calls}
        # Both "movie" and "show" must have been requested.
        assert "movie" in media_types_called
        assert "show" in media_types_called

    @pytest.mark.asyncio
    async def test_empty_response_returns_ok_with_no_items(self):
        mock_client = _make_mock_client()
        mock_client.get_recently_added = AsyncMock(return_value=[])

        with patch(
            "providers.media_providers.tautulli_fetcher.TautulliClient",
            return_value=mock_client,
        ), patch("providers.secrets.resolve_secret", return_value="test-key"):
            fetcher = TautulliFetcher(
                base_url="http://tautulli.test:8181",
                api_key_env="TAUTULLI_API_KEY",
                poster_dir="/tmp/posters",
            )
            result = await fetcher.fetch_recently_added()

        assert result.status == "ok"
        assert result.items == []


# ---------------------------------------------------------------------------
# TestDownloadPosters
# ---------------------------------------------------------------------------

class TestDownloadPosters:
    @pytest.mark.asyncio
    async def test_downloads_to_correct_path(self, tmp_path):
        mock_client = _make_mock_client()
        mock_client.download_poster = AsyncMock(return_value=b"\xff\xd8\xff\xe0FAKE")

        poster_dir = str(tmp_path / "posters")

        with patch(
            "providers.media_providers.tautulli_fetcher.TautulliClient",
            return_value=mock_client,
        ), patch("providers.secrets.resolve_secret", return_value="test-key"):
            fetcher = TautulliFetcher(
                base_url="http://tautulli.test:8181",
                api_key_env="TAUTULLI_API_KEY",
                poster_dir=poster_dir,
            )
            item = MediaItem(id="plex-1001", title="Test Movie")
            count = await fetcher.download_posters([item])

        assert count == 1
        expected_path = os.path.join(poster_dir, "plex-1001.jpg")
        assert os.path.exists(expected_path)
        with open(expected_path, "rb") as fh:
            assert fh.read() == b"\xff\xd8\xff\xe0FAKE"
        assert item.local_poster == "plex-1001.jpg"

    @pytest.mark.asyncio
    async def test_skips_existing_recent_file(self, tmp_path):
        mock_client = _make_mock_client()
        mock_client.download_poster = AsyncMock(return_value=b"NEW_DATA")

        poster_dir = str(tmp_path / "posters")
        os.makedirs(poster_dir)

        # Create a fresh file (modified just now).
        existing_path = os.path.join(poster_dir, "plex-1001.jpg")
        with open(existing_path, "wb") as fh:
            fh.write(b"CACHED")

        with patch(
            "providers.media_providers.tautulli_fetcher.TautulliClient",
            return_value=mock_client,
        ), patch("providers.secrets.resolve_secret", return_value="test-key"):
            fetcher = TautulliFetcher(
                base_url="http://tautulli.test:8181",
                api_key_env="TAUTULLI_API_KEY",
                poster_dir=poster_dir,
            )

        item = MediaItem(id="plex-1001", title="Test Movie")
        count = await fetcher.download_posters([item])

        # File was recent, no download should have happened.
        assert count == 0
        mock_client.download_poster.assert_not_called()
        # File contents unchanged.
        with open(existing_path, "rb") as fh:
            assert fh.read() == b"CACHED"

    @pytest.mark.asyncio
    async def test_handles_download_error(self, tmp_path):
        mock_client = _make_mock_client()
        # First item fails, second succeeds.
        mock_client.download_poster = AsyncMock(
            side_effect=[
                RuntimeError("connection refused"),
                b"\xff\xd8\xff\xe0GOOD",
            ]
        )

        poster_dir = str(tmp_path / "posters")

        with patch(
            "providers.media_providers.tautulli_fetcher.TautulliClient",
            return_value=mock_client,
        ), patch("providers.secrets.resolve_secret", return_value="test-key"):
            fetcher = TautulliFetcher(
                base_url="http://tautulli.test:8181",
                api_key_env="TAUTULLI_API_KEY",
                poster_dir=poster_dir,
            )
            item_fail = MediaItem(id="plex-1001", title="Failing Movie")
            item_ok = MediaItem(id="plex-2002", title="Good Movie")
            count = await fetcher.download_posters([item_fail, item_ok])

        # Only the successful one counts.
        assert count == 1
        # Good item's file should exist.
        assert os.path.exists(os.path.join(poster_dir, "plex-2002.jpg"))
        # Failed item's file should not exist.
        assert not os.path.exists(os.path.join(poster_dir, "plex-1001.jpg"))

    @pytest.mark.asyncio
    async def test_stale_file_is_redownloaded(self, tmp_path):
        mock_client = _make_mock_client()
        mock_client.download_poster = AsyncMock(return_value=b"FRESH")

        poster_dir = str(tmp_path / "posters")
        os.makedirs(poster_dir)

        # Create a file that is 25 hours old.
        stale_path = os.path.join(poster_dir, "plex-1001.jpg")
        with open(stale_path, "wb") as fh:
            fh.write(b"STALE")
        stale_mtime = time.time() - (25 * 3600)
        os.utime(stale_path, (stale_mtime, stale_mtime))

        with patch(
            "providers.media_providers.tautulli_fetcher.TautulliClient",
            return_value=mock_client,
        ), patch("providers.secrets.resolve_secret", return_value="test-key"):
            fetcher = TautulliFetcher(
                base_url="http://tautulli.test:8181",
                api_key_env="TAUTULLI_API_KEY",
                poster_dir=poster_dir,
            )
            item = MediaItem(id="plex-1001", title="Stale Movie")
            count = await fetcher.download_posters([item])

        assert count == 1
        mock_client.download_poster.assert_called_once()
        with open(stale_path, "rb") as fh:
            assert fh.read() == b"FRESH"

    @pytest.mark.asyncio
    async def test_sets_local_poster_on_items(self, tmp_path):
        mock_client = _make_mock_client()
        mock_client.download_poster = AsyncMock(return_value=b"DATA")

        poster_dir = str(tmp_path / "posters")

        with patch(
            "providers.media_providers.tautulli_fetcher.TautulliClient",
            return_value=mock_client,
        ), patch("providers.secrets.resolve_secret", return_value="test-key"):
            fetcher = TautulliFetcher(
                base_url="http://tautulli.test:8181",
                api_key_env="TAUTULLI_API_KEY",
                poster_dir=poster_dir,
            )
            item = MediaItem(id="plex-5555", title="Movie")
            await fetcher.download_posters([item])

        assert item.local_poster == "plex-5555.jpg"

    @pytest.mark.asyncio
    async def test_empty_list_returns_zero(self, tmp_path):
        poster_dir = str(tmp_path / "posters")

        with patch("providers.secrets.resolve_secret", return_value="test-key"):
            fetcher = TautulliFetcher(
                base_url="http://tautulli.test:8181",
                api_key_env="TAUTULLI_API_KEY",
                poster_dir=poster_dir,
            )

        count = await fetcher.download_posters([])
        assert count == 0


# ---------------------------------------------------------------------------
# TestFetchPopular (bonus coverage)
# ---------------------------------------------------------------------------

class TestFetchPopular:
    @pytest.mark.asyncio
    async def test_returns_combined_rows_on_success(self):
        mock_client = _make_mock_client()
        mock_client.get_home_stats = AsyncMock(side_effect=[
            [{"rating_key": "m1"}],   # popular_movies
            [{"rating_key": "t1"}, {"rating_key": "t2"}],  # popular_tv
        ])

        with patch(
            "providers.media_providers.tautulli_fetcher.TautulliClient",
            return_value=mock_client,
        ), patch("providers.secrets.resolve_secret", return_value="test-key"):
            fetcher = TautulliFetcher(
                base_url="http://tautulli.test:8181",
                api_key_env="TAUTULLI_API_KEY",
                poster_dir="/tmp/posters",
            )
            result = await fetcher.fetch_popular()

        assert len(result) == 3
        assert result[0] == {"rating_key": "m1"}
        assert result[2] == {"rating_key": "t2"}

    @pytest.mark.asyncio
    async def test_returns_empty_list_on_failure(self):
        mock_client = _make_mock_client()
        mock_client.get_home_stats = AsyncMock(
            side_effect=RuntimeError("stats unavailable")
        )

        with patch(
            "providers.media_providers.tautulli_fetcher.TautulliClient",
            return_value=mock_client,
        ), patch("providers.secrets.resolve_secret", return_value="test-key"):
            fetcher = TautulliFetcher(
                base_url="http://tautulli.test:8181",
                api_key_env="TAUTULLI_API_KEY",
                poster_dir="/tmp/posters",
            )
            result = await fetcher.fetch_popular()

        assert result == []
