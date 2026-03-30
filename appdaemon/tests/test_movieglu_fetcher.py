"""Unit tests for MoviegluFetcher."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from providers.media_providers.movieglu_fetcher import MoviegluFetcher
from providers.media_providers.types import CinemaInfo, ShowtimeCache, ShowtimeEntry


# ---------------------------------------------------------------------------
# Shared sample data
# ---------------------------------------------------------------------------

SAMPLE_CINEMAS_RESPONSE = [
    {"cinema_id": "7890", "cinema_name": "AMC CLASSIC Tyngsborough 12", "distance_mi": 5.2},
    {"cinema_id": "7891", "cinema_name": "Showcase Cinema de Lux Lowell", "distance_mi": 7.1},
    {"cinema_id": "7892", "cinema_name": "Regal Some Theater", "distance_mi": 15.0},
]

SAMPLE_SHOWTIMES_RESPONSE = {
    "cinema": {"cinema_id": "7890", "cinema_name": "AMC CLASSIC Tyngsborough 12"},
    "films": [
        {
            "film_id": 12345,
            "film_name": "Thunderbolts*",
            "showings": {
                "Standard": {
                    "times": [
                        {"display_showtime": "13:30"},
                        {"display_showtime": "16:15"},
                    ]
                },
                "IMAX": {
                    "times": [
                        {"display_showtime": "19:00"},
                    ]
                },
            },
        }
    ],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_client() -> MagicMock:
    """Create a mock MovieGluClient that works as async context manager."""
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


def _make_fetcher(theater_names=None) -> MoviegluFetcher:
    """Create a MoviegluFetcher with test secrets, bypassing env resolution."""
    if theater_names is None:
        theater_names = ["AMC Tyngsboro 12", "Showcase Cinema de Lux Lowell"]
    with patch("providers.secrets.resolve_secret", return_value="test-key"):
        return MoviegluFetcher(
            api_key_env="MOVIEGLU_API_KEY",
            client_id_env="MOVIEGLU_CLIENT_ID",
            lat="42.67",
            lng="-71.49",
            theater_names=theater_names,
        )


# ---------------------------------------------------------------------------
# TestMatchCinemaName
# ---------------------------------------------------------------------------

class TestMatchCinemaName:
    def test_exact_substring_match(self):
        """Documents the pure-substring boundary: different spellings (Tyngsboro vs
        Tyngsborough) do not match because neither is a substring of the other.
        Theater names in apps-dev.yaml should be chosen to match the API spelling
        closely enough for substring inclusion to work.
        """
        fetcher = _make_fetcher(["AMC Tyngsboro 12"])
        result = fetcher._match_cinema_name("AMC CLASSIC Tyngsborough 12")
        # "amc tyngsboro 12" is NOT a substring of "amc classic tyngsborough 12"
        # and vice versa — pure substring match cannot bridge different spellings.
        assert result is None

    def test_case_insensitive(self):
        """Lowercase configured name matches an all-caps API name."""
        fetcher = _make_fetcher(["showcase cinema de lux lowell"])
        result = fetcher._match_cinema_name("Showcase Cinema de Lux Lowell")
        assert result == "showcase cinema de lux lowell"

    def test_no_match(self):
        """A theater name not present in the API response returns None."""
        fetcher = _make_fetcher(["AMC Tyngsboro 12", "Showcase Cinema de Lux Lowell"])
        result = fetcher._match_cinema_name("Regal Fenway 13")
        assert result is None

    def test_partial_match_configured_in_api(self):
        """Configured name that is a substring of the full API name matches."""
        fetcher = _make_fetcher(["Showcase"])
        result = fetcher._match_cinema_name("Showcase Cinema de Lux Lowell")
        assert result == "showcase"

    def test_api_name_in_configured(self):
        """API name that is a substring of the configured name also matches."""
        fetcher = _make_fetcher(["AMC Methuen 20 The Loop"])
        result = fetcher._match_cinema_name("AMC Methuen")
        assert result == "amc methuen 20 the loop"

    def test_case_insensitive_mixed(self):
        """Match is case-insensitive regardless of capitalisation."""
        fetcher = _make_fetcher(["amc tyngsboro"])
        result = fetcher._match_cinema_name("AMC CLASSIC Tyngsborough 12")
        # "amc tyngsboro" not in "amc classic tyngsborough 12" and vice versa
        assert result is None


# ---------------------------------------------------------------------------
# TestDiscoverCinemas
# ---------------------------------------------------------------------------

class TestDiscoverCinemas:
    @pytest.mark.asyncio
    async def test_discovers_matching_cinemas(self):
        """Returns CinemaInfo for each cinema that matches a configured name."""
        mock_client = _make_mock_client()
        mock_client.get_cinemas_nearby = AsyncMock(return_value=SAMPLE_CINEMAS_RESPONSE)

        fetcher = _make_fetcher(["Showcase Cinema de Lux Lowell", "Regal Some Theater"])

        with patch(
            "providers.media_providers.movieglu_fetcher.MovieGluClient",
            return_value=mock_client,
        ):
            results = await fetcher.discover_cinemas()

        assert len(results) == 2
        cinema_ids = {c.cinema_id for c in results}
        assert "7891" in cinema_ids  # Showcase
        assert "7892" in cinema_ids  # Regal

    @pytest.mark.asyncio
    async def test_caches_cinema_ids(self):
        """After discovery, self._cinema_ids is populated with matched entries."""
        mock_client = _make_mock_client()
        mock_client.get_cinemas_nearby = AsyncMock(return_value=SAMPLE_CINEMAS_RESPONSE)

        fetcher = _make_fetcher(["Showcase Cinema de Lux Lowell"])

        with patch(
            "providers.media_providers.movieglu_fetcher.MovieGluClient",
            return_value=mock_client,
        ):
            await fetcher.discover_cinemas()

        assert "showcase cinema de lux lowell" in fetcher._cinema_ids
        assert fetcher._cinema_ids["showcase cinema de lux lowell"] == "7891"

    @pytest.mark.asyncio
    async def test_handles_no_matches(self):
        """Returns empty list when no configured theaters match the API response."""
        mock_client = _make_mock_client()
        mock_client.get_cinemas_nearby = AsyncMock(return_value=SAMPLE_CINEMAS_RESPONSE)

        fetcher = _make_fetcher(["Regal Fenway", "Alamo Drafthouse"])

        with patch(
            "providers.media_providers.movieglu_fetcher.MovieGluClient",
            return_value=mock_client,
        ):
            results = await fetcher.discover_cinemas()

        assert results == []
        assert fetcher._cinema_ids == {}

    @pytest.mark.asyncio
    async def test_handles_api_error(self):
        """Returns empty list when the API call raises an exception."""
        mock_client = _make_mock_client()
        mock_client.get_cinemas_nearby = AsyncMock(
            side_effect=RuntimeError("connection refused")
        )

        fetcher = _make_fetcher(["Showcase Cinema de Lux Lowell"])

        with patch(
            "providers.media_providers.movieglu_fetcher.MovieGluClient",
            return_value=mock_client,
        ):
            results = await fetcher.discover_cinemas()

        assert results == []


# ---------------------------------------------------------------------------
# TestParseShowtimeResponse
# ---------------------------------------------------------------------------

class TestParseShowtimeResponse:
    def test_parses_standard_and_imax(self):
        """All showtimes from both Standard and IMAX are collected."""
        fetcher = _make_fetcher()
        result = fetcher._parse_showtime_response(
            "AMC CLASSIC Tyngsborough 12", SAMPLE_SHOWTIMES_RESPONSE
        )

        assert "thunderbolts*" in result
        entries = result["thunderbolts*"]
        assert len(entries) == 1
        entry = entries[0]
        assert entry.cinema_name == "AMC CLASSIC Tyngsborough 12"
        assert set(entry.times) == {"13:30", "16:15", "19:00"}

    def test_handles_empty_films(self):
        """Response with no films returns an empty dict."""
        fetcher = _make_fetcher()
        result = fetcher._parse_showtime_response("Theater", {"films": []})
        assert result == {}

    def test_handles_missing_films_key(self):
        """Response without a films key returns an empty dict."""
        fetcher = _make_fetcher()
        result = fetcher._parse_showtime_response("Theater", {})
        assert result == {}

    def test_handles_missing_showings(self):
        """Film with no showings key produces an entry with empty times list."""
        raw = {
            "films": [
                {"film_id": 99, "film_name": "Silent Movie", "showings": {}}
            ]
        }
        fetcher = _make_fetcher()
        result = fetcher._parse_showtime_response("Theater", raw)
        assert "silent movie" in result
        assert result["silent movie"][0].times == []

    def test_handles_film_without_name(self):
        """Films missing a name are silently skipped."""
        raw = {
            "films": [
                {
                    "film_id": 1,
                    "showings": {"Standard": {"times": [{"display_showtime": "10:00"}]}},
                }
            ]
        }
        fetcher = _make_fetcher()
        result = fetcher._parse_showtime_response("Theater", raw)
        assert result == {}

    def test_multiple_films(self):
        """Multiple films each get their own entry."""
        raw = {
            "films": [
                {
                    "film_name": "Film A",
                    "showings": {
                        "Standard": {"times": [{"display_showtime": "10:00"}]}
                    },
                },
                {
                    "film_name": "Film B",
                    "showings": {
                        "Standard": {"times": [{"display_showtime": "14:00"}, {"display_showtime": "17:00"}]}
                    },
                },
            ]
        }
        fetcher = _make_fetcher()
        result = fetcher._parse_showtime_response("Theater", raw)
        assert "film a" in result
        assert "film b" in result
        assert result["film a"][0].times == ["10:00"]
        assert result["film b"][0].times == ["14:00", "17:00"]

    def test_film_title_lowercased_as_key(self):
        """Film titles are stored as lowercase keys."""
        raw = {
            "films": [
                {
                    "film_name": "Spider-Man: No Way Home",
                    "showings": {
                        "Standard": {"times": [{"display_showtime": "12:00"}]}
                    },
                }
            ]
        }
        fetcher = _make_fetcher()
        result = fetcher._parse_showtime_response("Theater", raw)
        assert "spider-man: no way home" in result
        assert "Spider-Man: No Way Home" not in result


# ---------------------------------------------------------------------------
# TestFetchShowtimes
# ---------------------------------------------------------------------------

class TestFetchShowtimes:
    @pytest.mark.asyncio
    async def test_fetches_all_configured_theaters(self):
        """One API call is made per cinema_id in the provided mapping."""
        mock_client = _make_mock_client()
        mock_client.get_showtimes_by_cinema = AsyncMock(
            return_value=SAMPLE_SHOWTIMES_RESPONSE
        )

        fetcher = _make_fetcher()
        cinema_ids = {"theater-a": "7890", "theater-b": "7891"}

        with patch(
            "providers.media_providers.movieglu_fetcher.MovieGluClient",
            return_value=mock_client,
        ):
            cache = await fetcher.fetch_showtimes(cinema_ids=cinema_ids)

        assert mock_client.get_showtimes_by_cinema.call_count == 2

    @pytest.mark.asyncio
    async def test_handles_partial_theater_failure(self):
        """When one cinema fails the others still succeed."""
        mock_client = _make_mock_client()
        call_count = 0

        async def _side_effect(cinema_id, date):
            nonlocal call_count
            call_count += 1
            if cinema_id == "7890":
                raise RuntimeError("timeout")
            return SAMPLE_SHOWTIMES_RESPONSE

        mock_client.get_showtimes_by_cinema = AsyncMock(side_effect=_side_effect)

        fetcher = _make_fetcher()
        cinema_ids = {"theater-fail": "7890", "theater-ok": "7891"}

        with patch(
            "providers.media_providers.movieglu_fetcher.MovieGluClient",
            return_value=mock_client,
        ):
            cache = await fetcher.fetch_showtimes(cinema_ids=cinema_ids)

        # 7891 succeeded so we should have film data
        assert call_count == 2
        assert len(cache.films) > 0

    @pytest.mark.asyncio
    async def test_returns_empty_cache_on_total_failure(self):
        """If the context manager itself raises, an empty cache is returned."""
        mock_client = _make_mock_client()
        mock_client.__aenter__ = AsyncMock(side_effect=RuntimeError("no connection"))

        fetcher = _make_fetcher()

        with patch(
            "providers.media_providers.movieglu_fetcher.MovieGluClient",
            return_value=mock_client,
        ):
            cache = await fetcher.fetch_showtimes(cinema_ids={"t": "1"})

        assert cache.films == {}

    @pytest.mark.asyncio
    async def test_sets_cache_date(self):
        """ShowtimeCache.date is set to today's ISO date."""
        import datetime

        mock_client = _make_mock_client()
        mock_client.get_showtimes_by_cinema = AsyncMock(
            return_value=SAMPLE_SHOWTIMES_RESPONSE
        )

        fetcher = _make_fetcher()
        today = datetime.date.today().isoformat()

        with patch(
            "providers.media_providers.movieglu_fetcher.MovieGluClient",
            return_value=mock_client,
        ):
            cache = await fetcher.fetch_showtimes(cinema_ids={"t": "7890"})

        assert cache.date == today

    @pytest.mark.asyncio
    async def test_uses_cached_cinema_ids_when_none_provided(self):
        """Uses self._cinema_ids when no cinema_ids argument is passed."""
        mock_client = _make_mock_client()
        mock_client.get_showtimes_by_cinema = AsyncMock(
            return_value=SAMPLE_SHOWTIMES_RESPONSE
        )

        fetcher = _make_fetcher()
        fetcher._cinema_ids = {"showcase cinema de lux lowell": "7891"}

        with patch(
            "providers.media_providers.movieglu_fetcher.MovieGluClient",
            return_value=mock_client,
        ):
            cache = await fetcher.fetch_showtimes()

        mock_client.get_showtimes_by_cinema.assert_awaited_once()
        call_args = mock_client.get_showtimes_by_cinema.call_args
        assert call_args[0][0] == "7891"

    @pytest.mark.asyncio
    async def test_returns_empty_cache_when_no_cinema_ids(self):
        """Returns empty ShowtimeCache immediately when no IDs are available."""
        mock_client = _make_mock_client()
        mock_client.get_showtimes_by_cinema = AsyncMock(
            return_value=SAMPLE_SHOWTIMES_RESPONSE
        )

        fetcher = _make_fetcher()
        # No discovery has been run, _cinema_ids is empty

        with patch(
            "providers.media_providers.movieglu_fetcher.MovieGluClient",
            return_value=mock_client,
        ):
            cache = await fetcher.fetch_showtimes()

        assert cache.films == {}
        mock_client.get_showtimes_by_cinema.assert_not_called()

    @pytest.mark.asyncio
    async def test_merges_showtimes_across_cinemas(self):
        """Films appearing at multiple cinemas are merged into a list of entries."""
        response_cinema_1 = {
            "cinema": {"cinema_name": "Theater One"},
            "films": [
                {
                    "film_name": "Big Movie",
                    "showings": {
                        "Standard": {"times": [{"display_showtime": "14:00"}]}
                    },
                }
            ],
        }
        response_cinema_2 = {
            "cinema": {"cinema_name": "Theater Two"},
            "films": [
                {
                    "film_name": "Big Movie",
                    "showings": {
                        "Standard": {"times": [{"display_showtime": "16:30"}]}
                    },
                }
            ],
        }

        responses = {"c1": response_cinema_1, "c2": response_cinema_2}

        async def _by_id(cinema_id, date):
            return responses[cinema_id]

        mock_client = _make_mock_client()
        mock_client.get_showtimes_by_cinema = AsyncMock(side_effect=_by_id)

        fetcher = _make_fetcher()

        with patch(
            "providers.media_providers.movieglu_fetcher.MovieGluClient",
            return_value=mock_client,
        ):
            cache = await fetcher.fetch_showtimes(cinema_ids={"t1": "c1", "t2": "c2"})

        assert "big movie" in cache.films
        entries = cache.films["big movie"]
        assert len(entries) == 2
        cinema_names = {e.cinema_name for e in entries}
        assert "Theater One" in cinema_names
        assert "Theater Two" in cinema_names
