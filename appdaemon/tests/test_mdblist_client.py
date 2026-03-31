"""Unit tests for MdbListClient.

Tests cover the static extract_ratings() parser and the bulk-limit guard.
No real HTTP calls are made.
"""

from __future__ import annotations

import asyncio
import pytest

from providers.media_providers.mdblist_client import MdbListClient


# ---------------------------------------------------------------------------
# TestExtractRatings
# ---------------------------------------------------------------------------

class TestExtractRatings:
    def test_extract_ratings_all_present(self):
        """All four known rating sources map to the correct output keys."""
        data = {
            "ratings": [
                {"source": "imdb", "value": 8.1},
                {"source": "tomatoes", "value": 85},
                {"source": "tomatoesaudience", "value": 92},
                {"source": "metacritic", "value": 78},
            ]
        }
        result = MdbListClient.extract_ratings(data)
        assert result["imdb_rating"] == 8.1
        assert result["rt_critics"] == 85
        assert result["rt_audience"] == 92
        assert result["metacritic"] == 78

    def test_extract_ratings_partial(self):
        """Only IMDb present — other keys absent from result."""
        data = {"ratings": [{"source": "imdb", "value": 7.5}]}
        result = MdbListClient.extract_ratings(data)
        assert result["imdb_rating"] == 7.5
        assert "rt_critics" not in result
        assert "rt_audience" not in result
        assert "metacritic" not in result

    def test_extract_ratings_empty(self):
        """Empty ratings array produces an empty result dict."""
        data = {"ratings": []}
        result = MdbListClient.extract_ratings(data)
        assert result == {}

    def test_extract_ratings_missing_key(self):
        """Missing 'ratings' key in the response is treated as empty."""
        result = MdbListClient.extract_ratings({})
        assert result == {}

    def test_extract_ratings_null_values_skipped(self):
        """Ratings with value: None are skipped and do not appear in output."""
        data = {
            "ratings": [
                {"source": "imdb", "value": None},
                {"source": "tomatoes", "value": 70},
            ]
        }
        result = MdbListClient.extract_ratings(data)
        assert "imdb_rating" not in result
        assert result["rt_critics"] == 70

    def test_extract_ratings_unknown_source_ignored(self):
        """Unknown source keys are silently ignored."""
        data = {
            "ratings": [
                {"source": "unknown_site", "value": 99},
                {"source": "imdb", "value": 6.5},
            ]
        }
        result = MdbListClient.extract_ratings(data)
        assert "unknown_site" not in result
        assert result["imdb_rating"] == 6.5

    def test_extract_ratings_imdb_coerced_to_float(self):
        """imdb_rating is always a float even when source provides an int."""
        data = {"ratings": [{"source": "imdb", "value": 8}]}
        result = MdbListClient.extract_ratings(data)
        assert isinstance(result["imdb_rating"], float)
        assert result["imdb_rating"] == 8.0

    def test_extract_ratings_rt_critics_coerced_to_int(self):
        """rt_critics is always an int."""
        data = {"ratings": [{"source": "tomatoes", "value": 85.7}]}
        result = MdbListClient.extract_ratings(data)
        assert isinstance(result["rt_critics"], int)
        assert result["rt_critics"] == 85

    def test_extract_ratings_rt_audience_coerced_to_int(self):
        """rt_audience is always an int."""
        data = {"ratings": [{"source": "tomatoesaudience", "value": 92.3}]}
        result = MdbListClient.extract_ratings(data)
        assert isinstance(result["rt_audience"], int)
        assert result["rt_audience"] == 92

    def test_extract_ratings_metacritic_coerced_to_int(self):
        """metacritic is always an int."""
        data = {"ratings": [{"source": "metacritic", "value": 78.9}]}
        result = MdbListClient.extract_ratings(data)
        assert isinstance(result["metacritic"], int)
        assert result["metacritic"] == 78


# ---------------------------------------------------------------------------
# TestGetRatingsBulk
# ---------------------------------------------------------------------------

class TestGetRatingsBulk:
    def test_bulk_rejects_over_200(self):
        """get_ratings_bulk raises ValueError when more than 200 IDs are supplied."""
        client = MdbListClient(api_key="test-key")
        ids = list(range(201))  # 201 IDs
        loop = asyncio.new_event_loop()
        try:
            with pytest.raises(ValueError, match="200"):
                loop.run_until_complete(client.get_ratings_bulk(ids))
        finally:
            loop.close()

    def test_bulk_accepts_exactly_200(self):
        """get_ratings_bulk does not raise for exactly 200 IDs.

        We patch the underlying _get so no real HTTP call is made.
        The goal is to confirm the guard does not fire at the limit.
        """
        from unittest.mock import AsyncMock, patch

        client = MdbListClient(api_key="test-key")
        ids = list(range(200))  # exactly 200

        mock_data = {"ratings": [{"source": "imdb", "value": 7.0}]}
        with patch.object(client, "_get", new=AsyncMock(return_value=mock_data)):
            loop = asyncio.new_event_loop()
            try:
                results = loop.run_until_complete(client.get_ratings_bulk(ids))
            finally:
                loop.close()

        assert len(results) == 200

    def test_bulk_rejects_201_before_http(self):
        """ValueError is raised synchronously before any HTTP call is attempted."""
        from unittest.mock import AsyncMock, patch

        client = MdbListClient(api_key="test-key")
        ids = list(range(201))

        # If the guard fires first, _get should never be called
        with patch.object(client, "_get", new=AsyncMock()) as mock_get:
            loop = asyncio.new_event_loop()
            try:
                with pytest.raises(ValueError):
                    loop.run_until_complete(client.get_ratings_bulk(ids))
            finally:
                loop.close()
            mock_get.assert_not_called()
