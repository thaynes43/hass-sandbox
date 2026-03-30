"""Unit tests for SerpApiFetcher."""

from __future__ import annotations

import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from providers.media_providers.serpapi_fetcher import SerpApiFetcher
from providers.media_providers.types import ShowtimeCache, ShowtimeEntry


# ---------------------------------------------------------------------------
# Shared sample data
# ---------------------------------------------------------------------------

SAMPLE_THEATER_RESPONSE = {
    "search_metadata": {"status": "Success"},
    "showtimes": [
        {
            "day": "Today",
            "date": "Mar 29",
            "movies": [
                {
                    "name": "Thunderbolts*",
                    "link": "https://google.com/...",
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
                },
                {
                    "name": "A Minecraft Movie",
                    "showing": [
                        {
                            "type": "Standard",
                            "time": ["2:00pm", "5:30pm"]
                        }
                    ]
                }
            ]
        }
    ]
}

EMPTY_RESPONSE: dict = {
    "search_metadata": {"status": "Success"},
    "showtimes": [],
}

NO_KEY_RESPONSE: dict = {
    "search_metadata": {"status": "Success"},
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_client() -> MagicMock:
    """Create a mock SerpApiClient that works as async context manager."""
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


def _make_fetcher(theater_names=None) -> SerpApiFetcher:
    """Create a SerpApiFetcher with test secrets, bypassing env resolution."""
    if theater_names is None:
        theater_names = [
            "AMC Tyngsboro 12",
            "Showcase Cinema de Lux Lowell",
            "AMC Methuen 20",
        ]
    with patch("providers.secrets.resolve_secret", return_value="test-key"):
        return SerpApiFetcher(
            api_key_env="SERPAPI_KEY",
            location="Westford, MA",
            theater_names=theater_names,
        )


# ---------------------------------------------------------------------------
# TestMatchTheater
# ---------------------------------------------------------------------------

class TestMatchTheater:
    def test_exact_match_returns_original_case(self):
        """Exact match returns the configured name in its original case."""
        fetcher = _make_fetcher(["Showcase Cinema de Lux Lowell"])
        result = fetcher._match_theater("Showcase Cinema de Lux Lowell")
        assert result == "Showcase Cinema de Lux Lowell"

    def test_case_insensitive_returns_configured_case(self):
        """Match is case-insensitive but returns the configured name's casing."""
        fetcher = _make_fetcher(["Showcase Cinema de Lux Lowell"])
        result = fetcher._match_theater("showcase cinema de lux lowell")
        assert result == "Showcase Cinema de Lux Lowell"

    def test_partial_configured_in_api(self):
        """Shorter configured name matches a longer API name."""
        fetcher = _make_fetcher(["Showcase"])
        result = fetcher._match_theater("Showcase Cinema de Lux Lowell")
        assert result == "Showcase"

    def test_api_name_in_configured(self):
        """API name that is a substring of the configured name also matches."""
        fetcher = _make_fetcher(["AMC Methuen 20 The Loop"])
        result = fetcher._match_theater("AMC Methuen")
        assert result == "AMC Methuen 20 The Loop"

    def test_no_match_returns_none(self):
        """A theater not matching any configured name returns None."""
        fetcher = _make_fetcher(["AMC Tyngsboro 12", "Showcase Cinema de Lux Lowell"])
        result = fetcher._match_theater("Regal Fenway 13")
        assert result is None

    def test_empty_theater_name(self):
        """An empty theater name does not match anything."""
        fetcher = _make_fetcher(["AMC Tyngsboro 12"])
        result = fetcher._match_theater("")
        assert result is None

    def test_configured_names_preserve_original_case(self):
        """Configured theater names are stored in their original case."""
        fetcher = _make_fetcher(["AMC Tyngsboro 12", "Showcase Cinema de Lux Lowell"])
        assert "AMC Tyngsboro 12" in fetcher._theater_names
        assert "Showcase Cinema de Lux Lowell" in fetcher._theater_names


# ---------------------------------------------------------------------------
# TestFetchTheater
# ---------------------------------------------------------------------------

class TestFetchTheater:
    @pytest.mark.asyncio
    async def test_parses_movies_into_cache(self):
        """_fetch_theater parses the new theater-centric response into the cache."""
        mock_client = _make_mock_client()
        mock_client.get_showtimes = AsyncMock(return_value=SAMPLE_THEATER_RESPONSE)

        fetcher = _make_fetcher(["AMC Tyngsboro 12"])
        cache = ShowtimeCache(date=datetime.date.today().isoformat())

        await fetcher._fetch_theater(mock_client, "AMC Tyngsboro 12", cache)

        assert "thunderbolts*" in cache.films
        assert "a minecraft movie" in cache.films

    @pytest.mark.asyncio
    async def test_film_title_lowercased(self):
        """Film titles are stored as lowercase keys."""
        mock_client = _make_mock_client()
        mock_client.get_showtimes = AsyncMock(return_value=SAMPLE_THEATER_RESPONSE)

        fetcher = _make_fetcher(["AMC Tyngsboro 12"])
        cache = ShowtimeCache(date=datetime.date.today().isoformat())

        await fetcher._fetch_theater(mock_client, "AMC Tyngsboro 12", cache)

        assert "thunderbolts*" in cache.films
        assert "Thunderbolts*" not in cache.films

    @pytest.mark.asyncio
    async def test_collects_all_times_across_formats(self):
        """All times across all format types (Standard, IMAX, etc.) are collected."""
        mock_client = _make_mock_client()
        mock_client.get_showtimes = AsyncMock(return_value=SAMPLE_THEATER_RESPONSE)

        fetcher = _make_fetcher(["AMC Tyngsboro 12"])
        cache = ShowtimeCache(date=datetime.date.today().isoformat())

        await fetcher._fetch_theater(mock_client, "AMC Tyngsboro 12", cache)

        entry = cache.films["thunderbolts*"][0]
        assert set(entry.times) == {"1:30pm", "4:15pm", "7:00pm", "9:45pm"}

    @pytest.mark.asyncio
    async def test_cinema_name_set_to_theater_name(self):
        """ShowtimeEntry.cinema_name is set to the theater name passed in."""
        mock_client = _make_mock_client()
        mock_client.get_showtimes = AsyncMock(return_value=SAMPLE_THEATER_RESPONSE)

        fetcher = _make_fetcher(["AMC Tyngsboro 12"])
        cache = ShowtimeCache(date=datetime.date.today().isoformat())

        await fetcher._fetch_theater(mock_client, "AMC Tyngsboro 12", cache)

        entry = cache.films["thunderbolts*"][0]
        assert entry.cinema_name == "AMC Tyngsboro 12"

    @pytest.mark.asyncio
    async def test_query_format_is_theater_showtimes(self):
        """get_showtimes is called with '{theater_name} showtimes'."""
        mock_client = _make_mock_client()
        mock_client.get_showtimes = AsyncMock(return_value=EMPTY_RESPONSE)

        fetcher = _make_fetcher(["AMC Tyngsboro 12"])
        cache = ShowtimeCache(date=datetime.date.today().isoformat())

        await fetcher._fetch_theater(mock_client, "AMC Tyngsboro 12", cache)

        mock_client.get_showtimes.assert_awaited_once_with(
            query="AMC Tyngsboro 12 showtimes"
        )

    @pytest.mark.asyncio
    async def test_empty_showtimes_list_produces_no_films(self):
        """An empty 'showtimes' list in the response results in no films added."""
        mock_client = _make_mock_client()
        mock_client.get_showtimes = AsyncMock(return_value=EMPTY_RESPONSE)

        fetcher = _make_fetcher(["AMC Tyngsboro 12"])
        cache = ShowtimeCache(date=datetime.date.today().isoformat())

        await fetcher._fetch_theater(mock_client, "AMC Tyngsboro 12", cache)

        assert cache.films == {}

    @pytest.mark.asyncio
    async def test_missing_showtimes_key_produces_no_films(self):
        """A response without the 'showtimes' key results in no films added."""
        mock_client = _make_mock_client()
        mock_client.get_showtimes = AsyncMock(return_value=NO_KEY_RESPONSE)

        fetcher = _make_fetcher(["AMC Tyngsboro 12"])
        cache = ShowtimeCache(date=datetime.date.today().isoformat())

        await fetcher._fetch_theater(mock_client, "AMC Tyngsboro 12", cache)

        assert cache.films == {}

    @pytest.mark.asyncio
    async def test_movie_without_name_is_skipped(self):
        """Movies missing a 'name' field are silently skipped."""
        raw = {
            "showtimes": [
                {
                    "day": "Today",
                    "movies": [
                        {
                            "showing": [{"type": "Standard", "time": ["2:00pm"]}]
                        }
                    ]
                }
            ]
        }
        mock_client = _make_mock_client()
        mock_client.get_showtimes = AsyncMock(return_value=raw)

        fetcher = _make_fetcher(["AMC Tyngsboro 12"])
        cache = ShowtimeCache(date=datetime.date.today().isoformat())

        await fetcher._fetch_theater(mock_client, "AMC Tyngsboro 12", cache)

        assert cache.films == {}

    @pytest.mark.asyncio
    async def test_movie_without_times_is_skipped(self):
        """Movies with no showtime entries are silently skipped."""
        raw = {
            "showtimes": [
                {
                    "day": "Today",
                    "movies": [
                        {
                            "name": "Empty Film",
                            "showing": []
                        }
                    ]
                }
            ]
        }
        mock_client = _make_mock_client()
        mock_client.get_showtimes = AsyncMock(return_value=raw)

        fetcher = _make_fetcher(["AMC Tyngsboro 12"])
        cache = ShowtimeCache(date=datetime.date.today().isoformat())

        await fetcher._fetch_theater(mock_client, "AMC Tyngsboro 12", cache)

        assert cache.films == {}

    @pytest.mark.asyncio
    async def test_only_first_day_parsed(self):
        """Only the first day block (today) is parsed; subsequent days are ignored."""
        raw = {
            "showtimes": [
                {
                    "day": "Today",
                    "movies": [
                        {
                            "name": "Today Film",
                            "showing": [{"type": "Standard", "time": ["1:00pm"]}]
                        }
                    ]
                },
                {
                    "day": "Tomorrow",
                    "movies": [
                        {
                            "name": "Tomorrow Film",
                            "showing": [{"type": "Standard", "time": ["2:00pm"]}]
                        }
                    ]
                }
            ]
        }
        mock_client = _make_mock_client()
        mock_client.get_showtimes = AsyncMock(return_value=raw)

        fetcher = _make_fetcher(["AMC Tyngsboro 12"])
        cache = ShowtimeCache(date=datetime.date.today().isoformat())

        await fetcher._fetch_theater(mock_client, "AMC Tyngsboro 12", cache)

        assert "today film" in cache.films
        assert "tomorrow film" not in cache.films

    @pytest.mark.asyncio
    async def test_api_error_is_caught_gracefully(self):
        """An exception from get_showtimes is caught; cache remains unchanged."""
        mock_client = _make_mock_client()
        mock_client.get_showtimes = AsyncMock(
            side_effect=RuntimeError("connection refused")
        )

        fetcher = _make_fetcher(["AMC Tyngsboro 12"])
        cache = ShowtimeCache(date=datetime.date.today().isoformat())

        # Should not raise
        await fetcher._fetch_theater(mock_client, "AMC Tyngsboro 12", cache)

        assert cache.films == {}

    @pytest.mark.asyncio
    async def test_accumulates_into_existing_cache(self):
        """Multiple calls to _fetch_theater accumulate films into the same cache."""
        mock_client = _make_mock_client()
        mock_client.get_showtimes = AsyncMock(return_value=SAMPLE_THEATER_RESPONSE)

        fetcher = _make_fetcher(["AMC Tyngsboro 12", "Showcase Cinema de Lux Lowell"])
        cache = ShowtimeCache(date=datetime.date.today().isoformat())

        await fetcher._fetch_theater(mock_client, "AMC Tyngsboro 12", cache)
        await fetcher._fetch_theater(mock_client, "Showcase Cinema de Lux Lowell", cache)

        # Both theaters add entries for each film — 2 entries per film
        assert len(cache.films["thunderbolts*"]) == 2
        cinema_names = {e.cinema_name for e in cache.films["thunderbolts*"]}
        assert "AMC Tyngsboro 12" in cinema_names
        assert "Showcase Cinema de Lux Lowell" in cinema_names


# ---------------------------------------------------------------------------
# TestFetchShowtimes
# ---------------------------------------------------------------------------

class TestFetchShowtimes:
    @pytest.mark.asyncio
    async def test_returns_showtime_cache(self):
        """fetch_showtimes returns a populated ShowtimeCache on success."""
        mock_client = _make_mock_client()
        mock_client.get_showtimes = AsyncMock(return_value=SAMPLE_THEATER_RESPONSE)

        fetcher = _make_fetcher(["AMC Tyngsboro 12", "Showcase Cinema de Lux Lowell"])

        with patch(
            "providers.media_providers.serpapi_fetcher.SerpApiClient",
            return_value=mock_client,
        ):
            cache = await fetcher.fetch_showtimes()

        assert isinstance(cache, ShowtimeCache)
        assert len(cache.films) > 0

    @pytest.mark.asyncio
    async def test_sets_cache_date_to_today(self):
        """ShowtimeCache.date is set to today's ISO date."""
        mock_client = _make_mock_client()
        mock_client.get_showtimes = AsyncMock(return_value=SAMPLE_THEATER_RESPONSE)

        fetcher = _make_fetcher(["AMC Tyngsboro 12"])
        today = datetime.date.today().isoformat()

        with patch(
            "providers.media_providers.serpapi_fetcher.SerpApiClient",
            return_value=mock_client,
        ):
            cache = await fetcher.fetch_showtimes()

        assert cache.date == today

    @pytest.mark.asyncio
    async def test_makes_one_call_per_theater(self):
        """get_showtimes is called once per configured theater."""
        mock_client = _make_mock_client()
        mock_client.get_showtimes = AsyncMock(return_value=EMPTY_RESPONSE)

        theater_names = ["AMC Tyngsboro 12", "Showcase Cinema de Lux Lowell", "AMC Methuen 20"]
        fetcher = _make_fetcher(theater_names)

        with patch(
            "providers.media_providers.serpapi_fetcher.SerpApiClient",
            return_value=mock_client,
        ):
            await fetcher.fetch_showtimes()

        assert mock_client.get_showtimes.await_count == len(theater_names)

    @pytest.mark.asyncio
    async def test_query_uses_theater_name_not_location(self):
        """Each get_showtimes call uses '{theater_name} showtimes', not a location query."""
        mock_client = _make_mock_client()
        mock_client.get_showtimes = AsyncMock(return_value=EMPTY_RESPONSE)

        fetcher = _make_fetcher(["AMC Tyngsboro 12"])

        with patch(
            "providers.media_providers.serpapi_fetcher.SerpApiClient",
            return_value=mock_client,
        ):
            await fetcher.fetch_showtimes()

        call_kwargs = mock_client.get_showtimes.call_args
        query_arg = call_kwargs[1].get("query") or call_kwargs[0][0]
        assert query_arg == "AMC Tyngsboro 12 showtimes"

    @pytest.mark.asyncio
    async def test_location_not_used_in_query(self):
        """The configured location is not included in the per-theater query."""
        mock_client = _make_mock_client()
        mock_client.get_showtimes = AsyncMock(return_value=EMPTY_RESPONSE)

        fetcher = _make_fetcher(["AMC Tyngsboro 12"])

        with patch(
            "providers.media_providers.serpapi_fetcher.SerpApiClient",
            return_value=mock_client,
        ):
            await fetcher.fetch_showtimes()

        call_kwargs = mock_client.get_showtimes.call_args
        query_arg = call_kwargs[1].get("query") or call_kwargs[0][0]
        assert "Westford, MA" not in query_arg

    @pytest.mark.asyncio
    async def test_returns_empty_cache_on_api_failure(self):
        """Returns an empty ShowtimeCache when all API calls raise an exception."""
        mock_client = _make_mock_client()
        mock_client.get_showtimes = AsyncMock(
            side_effect=RuntimeError("connection refused")
        )

        fetcher = _make_fetcher(["AMC Tyngsboro 12"])

        with patch(
            "providers.media_providers.serpapi_fetcher.SerpApiClient",
            return_value=mock_client,
        ):
            cache = await fetcher.fetch_showtimes()

        assert cache.films == {}

    @pytest.mark.asyncio
    async def test_returns_empty_cache_on_context_manager_failure(self):
        """Returns an empty cache when the async context manager itself raises."""
        mock_client = _make_mock_client()
        mock_client.__aenter__ = AsyncMock(side_effect=RuntimeError("no connection"))

        fetcher = _make_fetcher(["AMC Tyngsboro 12"])

        with patch(
            "providers.media_providers.serpapi_fetcher.SerpApiClient",
            return_value=mock_client,
        ):
            cache = await fetcher.fetch_showtimes()

        assert cache.films == {}

    @pytest.mark.asyncio
    async def test_no_theaters_configured_returns_empty_cache(self):
        """fetch_showtimes with no theaters returns an empty cache without calling API."""
        mock_client = _make_mock_client()
        mock_client.get_showtimes = AsyncMock(return_value=SAMPLE_THEATER_RESPONSE)

        fetcher = _make_fetcher(theater_names=[])

        with patch(
            "providers.media_providers.serpapi_fetcher.SerpApiClient",
            return_value=mock_client,
        ):
            cache = await fetcher.fetch_showtimes()

        assert cache.films == {}
        mock_client.get_showtimes.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_one_theater_failure_does_not_abort_others(self):
        """A per-theater API failure is logged as a warning and does not abort remaining theaters."""
        mock_client = _make_mock_client()
        # First theater fails, second succeeds
        mock_client.get_showtimes = AsyncMock(
            side_effect=[RuntimeError("timeout"), SAMPLE_THEATER_RESPONSE]
        )

        fetcher = _make_fetcher(["AMC Tyngsboro 12", "Showcase Cinema de Lux Lowell"])

        with patch(
            "providers.media_providers.serpapi_fetcher.SerpApiClient",
            return_value=mock_client,
        ):
            cache = await fetcher.fetch_showtimes()

        # The second theater's films should still be present
        assert len(cache.films) > 0

    @pytest.mark.asyncio
    async def test_aggregates_films_across_theaters(self):
        """Films from all theaters are merged into a single cache."""
        amc_response = {
            "showtimes": [
                {
                    "day": "Today",
                    "movies": [
                        {
                            "name": "AMC Exclusive",
                            "showing": [{"type": "Standard", "time": ["1:00pm"]}]
                        }
                    ]
                }
            ]
        }
        showcase_response = {
            "showtimes": [
                {
                    "day": "Today",
                    "movies": [
                        {
                            "name": "Showcase Exclusive",
                            "showing": [{"type": "Standard", "time": ["2:00pm"]}]
                        }
                    ]
                }
            ]
        }

        mock_client = _make_mock_client()
        mock_client.get_showtimes = AsyncMock(
            side_effect=[amc_response, showcase_response]
        )

        fetcher = _make_fetcher(["AMC Tyngsboro 12", "Showcase Cinema de Lux Lowell"])

        with patch(
            "providers.media_providers.serpapi_fetcher.SerpApiClient",
            return_value=mock_client,
        ):
            cache = await fetcher.fetch_showtimes()

        assert "amc exclusive" in cache.films
        assert "showcase exclusive" in cache.films
