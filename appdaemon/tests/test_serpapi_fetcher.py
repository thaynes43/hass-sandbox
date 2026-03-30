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

SAMPLE_SHOWTIMES_RESPONSE = {
    "search_metadata": {"status": "Success"},
    "showtimes_results": [
        {
            "title": "Thunderbolts*",
            "theaters": [
                {
                    "name": "AMC CLASSIC Tyngsborough 12",
                    "link": "https://example.com/amc",
                    "showing": [
                        {
                            "time": ["1:30 PM", "4:15 PM", "7:00 PM", "9:45 PM"]
                        }
                    ],
                },
                {
                    "name": "Showcase Cinema de Lux Lowell",
                    "link": "https://example.com/showcase",
                    "showing": [
                        {
                            "time": ["1:00 PM", "3:45 PM", "6:30 PM"]
                        }
                    ],
                },
                {
                    "name": "Regal Fenway",
                    "link": "https://example.com/regal",
                    "showing": [
                        {
                            "time": ["2:00 PM", "5:00 PM"]
                        }
                    ],
                },
            ],
        },
        {
            "title": "A Minecraft Movie",
            "theaters": [
                {
                    "name": "AMC CLASSIC Tyngsborough 12",
                    "link": "https://example.com/amc",
                    "showing": [
                        {
                            "time": ["11:00 AM", "2:00 PM", "5:00 PM", "8:00 PM"]
                        }
                    ],
                },
            ],
        },
    ],
}

EMPTY_RESPONSE: dict = {
    "search_metadata": {"status": "Success"},
    "showtimes_results": [],
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
    def test_configured_name_in_api_name(self):
        """Configured name that is a substring of the full API name matches."""
        fetcher = _make_fetcher(["Showcase Cinema de Lux Lowell"])
        result = fetcher._match_theater("Showcase Cinema de Lux Lowell")
        assert result == "showcase cinema de lux lowell"

    def test_case_insensitive(self):
        """Match is case-insensitive."""
        fetcher = _make_fetcher(["showcase cinema de lux lowell"])
        result = fetcher._match_theater("Showcase Cinema de Lux Lowell")
        assert result == "showcase cinema de lux lowell"

    def test_partial_configured_in_api(self):
        """Shorter configured name matches a longer API name."""
        fetcher = _make_fetcher(["Showcase"])
        result = fetcher._match_theater("Showcase Cinema de Lux Lowell")
        assert result == "showcase"

    def test_api_name_in_configured(self):
        """API name that is a substring of the configured name also matches."""
        fetcher = _make_fetcher(["AMC Methuen 20 The Loop"])
        result = fetcher._match_theater("AMC Methuen")
        assert result == "amc methuen 20 the loop"

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


# ---------------------------------------------------------------------------
# TestParseShowtimesResponse
# ---------------------------------------------------------------------------

class TestParseShowtimesResponse:
    def test_filters_to_configured_theaters(self):
        """Only theaters matching configured names are included."""
        # Only Showcase and AMC Tyngsboro configured; Regal should be excluded
        fetcher = _make_fetcher(["Showcase Cinema de Lux Lowell", "AMC Classic Tyngsborough"])
        cache = fetcher._parse_showtimes_response(SAMPLE_SHOWTIMES_RESPONSE)

        assert "thunderbolts*" in cache.films
        entries = cache.films["thunderbolts*"]
        cinema_names = {e.cinema_name for e in entries}
        # Regal is not configured — should not appear
        assert "Regal Fenway" not in cinema_names
        # Both configured theaters should appear
        assert "AMC CLASSIC Tyngsborough 12" in cinema_names
        assert "Showcase Cinema de Lux Lowell" in cinema_names

    def test_collects_all_times(self):
        """All times from the showing list are collected."""
        fetcher = _make_fetcher(["AMC Classic Tyngsborough"])
        cache = fetcher._parse_showtimes_response(SAMPLE_SHOWTIMES_RESPONSE)

        assert "thunderbolts*" in cache.films
        amc_entry = next(
            e for e in cache.films["thunderbolts*"]
            if "AMC" in e.cinema_name
        )
        assert set(amc_entry.times) == {"1:30 PM", "4:15 PM", "7:00 PM", "9:45 PM"}

    def test_multiple_movies(self):
        """Multiple movies are each parsed into separate film entries."""
        fetcher = _make_fetcher(["AMC Classic Tyngsborough"])
        cache = fetcher._parse_showtimes_response(SAMPLE_SHOWTIMES_RESPONSE)

        assert "thunderbolts*" in cache.films
        assert "a minecraft movie" in cache.films

    def test_film_title_lowercased(self):
        """Film titles are stored as lowercase keys."""
        fetcher = _make_fetcher(["AMC Classic Tyngsborough"])
        cache = fetcher._parse_showtimes_response(SAMPLE_SHOWTIMES_RESPONSE)

        assert "thunderbolts*" in cache.films
        assert "Thunderbolts*" not in cache.films

    def test_empty_results(self):
        """Empty showtimes_results returns empty ShowtimeCache."""
        fetcher = _make_fetcher()
        cache = fetcher._parse_showtimes_response(EMPTY_RESPONSE)
        assert cache.films == {}

    def test_missing_showtimes_results_key(self):
        """Response without showtimes_results key returns empty ShowtimeCache."""
        fetcher = _make_fetcher()
        cache = fetcher._parse_showtimes_response(NO_KEY_RESPONSE)
        assert cache.films == {}

    def test_movie_without_title_skipped(self):
        """Movies missing a title are silently skipped."""
        raw = {
            "showtimes_results": [
                {
                    "theaters": [
                        {
                            "name": "AMC CLASSIC Tyngsborough 12",
                            "showing": [{"time": ["2:00 PM"]}],
                        }
                    ]
                }
            ]
        }
        fetcher = _make_fetcher(["AMC Classic Tyngsborough"])
        cache = fetcher._parse_showtimes_response(raw)
        assert cache.films == {}

    def test_no_configured_theaters_match(self):
        """If no theaters match, films dict is empty."""
        fetcher = _make_fetcher(["Alamo Drafthouse"])
        cache = fetcher._parse_showtimes_response(SAMPLE_SHOWTIMES_RESPONSE)
        assert cache.films == {}

    def test_multiple_showings_times_collected(self):
        """Times from multiple showing entries are all collected."""
        raw = {
            "showtimes_results": [
                {
                    "title": "Test Film",
                    "theaters": [
                        {
                            "name": "AMC CLASSIC Tyngsborough 12",
                            "showing": [
                                {"time": ["10:00 AM", "1:00 PM"]},
                                {"time": ["4:00 PM", "7:00 PM"]},
                            ],
                        }
                    ],
                }
            ]
        }
        fetcher = _make_fetcher(["AMC Classic Tyngsborough"])
        cache = fetcher._parse_showtimes_response(raw)

        assert "test film" in cache.films
        entry = cache.films["test film"][0]
        assert set(entry.times) == {"10:00 AM", "1:00 PM", "4:00 PM", "7:00 PM"}

    def test_same_film_multiple_configured_theaters(self):
        """One film showing at multiple configured theaters creates multiple entries."""
        fetcher = _make_fetcher([
            "AMC Classic Tyngsborough",
            "Showcase Cinema de Lux Lowell",
        ])
        cache = fetcher._parse_showtimes_response(SAMPLE_SHOWTIMES_RESPONSE)

        assert "thunderbolts*" in cache.films
        entries = cache.films["thunderbolts*"]
        assert len(entries) == 2
        names = {e.cinema_name for e in entries}
        assert "AMC CLASSIC Tyngsborough 12" in names
        assert "Showcase Cinema de Lux Lowell" in names


# ---------------------------------------------------------------------------
# TestFetchShowtimes
# ---------------------------------------------------------------------------

class TestFetchShowtimes:
    @pytest.mark.asyncio
    async def test_returns_showtime_cache(self):
        """fetch_showtimes returns a populated ShowtimeCache on success."""
        mock_client = _make_mock_client()
        mock_client.get_showtimes = AsyncMock(return_value=SAMPLE_SHOWTIMES_RESPONSE)

        fetcher = _make_fetcher(["AMC Classic Tyngsborough", "Showcase Cinema de Lux Lowell"])

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
        mock_client.get_showtimes = AsyncMock(return_value=SAMPLE_SHOWTIMES_RESPONSE)

        fetcher = _make_fetcher(["AMC Classic Tyngsborough"])
        today = datetime.date.today().isoformat()

        with patch(
            "providers.media_providers.serpapi_fetcher.SerpApiClient",
            return_value=mock_client,
        ):
            cache = await fetcher.fetch_showtimes()

        assert cache.date == today

    @pytest.mark.asyncio
    async def test_uses_location_in_query(self):
        """get_showtimes is called with a query containing the configured location."""
        mock_client = _make_mock_client()
        mock_client.get_showtimes = AsyncMock(return_value=EMPTY_RESPONSE)

        fetcher = _make_fetcher()

        with patch(
            "providers.media_providers.serpapi_fetcher.SerpApiClient",
            return_value=mock_client,
        ):
            await fetcher.fetch_showtimes()

        mock_client.get_showtimes.assert_awaited_once()
        call_kwargs = mock_client.get_showtimes.call_args
        # query should contain the location
        query_arg = call_kwargs[1].get("query") or call_kwargs[0][0]
        assert "Westford, MA" in query_arg

    @pytest.mark.asyncio
    async def test_returns_empty_cache_on_api_failure(self):
        """Returns an empty ShowtimeCache when the API call raises an exception."""
        mock_client = _make_mock_client()
        mock_client.get_showtimes = AsyncMock(
            side_effect=RuntimeError("connection refused")
        )

        fetcher = _make_fetcher()

        with patch(
            "providers.media_providers.serpapi_fetcher.SerpApiClient",
            return_value=mock_client,
        ):
            cache = await fetcher.fetch_showtimes()

        assert cache.films == {}

    @pytest.mark.asyncio
    async def test_returns_empty_cache_on_context_manager_failure(self):
        """Returns an empty cache when the context manager itself raises."""
        mock_client = _make_mock_client()
        mock_client.__aenter__ = AsyncMock(side_effect=RuntimeError("no connection"))

        fetcher = _make_fetcher()

        with patch(
            "providers.media_providers.serpapi_fetcher.SerpApiClient",
            return_value=mock_client,
        ):
            cache = await fetcher.fetch_showtimes()

        assert cache.films == {}

    @pytest.mark.asyncio
    async def test_filters_non_configured_theaters(self):
        """fetch_showtimes excludes theaters not in the configured list."""
        mock_client = _make_mock_client()
        mock_client.get_showtimes = AsyncMock(return_value=SAMPLE_SHOWTIMES_RESPONSE)

        # Only configure Showcase — Regal and AMC should not appear
        fetcher = _make_fetcher(["Showcase Cinema de Lux Lowell"])

        with patch(
            "providers.media_providers.serpapi_fetcher.SerpApiClient",
            return_value=mock_client,
        ):
            cache = await fetcher.fetch_showtimes()

        for film_key, entries in cache.films.items():
            for entry in entries:
                assert "Showcase" in entry.cinema_name or "showcase" in entry.cinema_name.lower()
