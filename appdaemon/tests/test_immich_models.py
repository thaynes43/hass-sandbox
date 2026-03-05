"""Unit tests for immich_fetcher.models – validation rules."""

from __future__ import annotations

import pytest

from apps.immich_fetcher.models import FetcherConfig
from providers.photo_providers.types import LocationAlias, PhotoFilter


# ---------------------------------------------------------------------------
# LocationAlias
# ---------------------------------------------------------------------------

class TestLocationAlias:
    def test_valid_city_only(self):
        a = LocationAlias(city="Orlando")
        a.validate()  # should not raise

    def test_valid_full(self):
        a = LocationAlias(city="Orlando", state="Florida", country="US")
        a.validate()

    def test_empty_raises(self):
        a = LocationAlias()
        with pytest.raises(ValueError, match="at least one"):
            a.validate()

    def test_to_dict_omits_none(self):
        a = LocationAlias(city="Orlando", state="Florida")
        assert a.to_dict() == {"city": "Orlando", "state": "Florida"}


# ---------------------------------------------------------------------------
# PhotoFilter – happy paths
# ---------------------------------------------------------------------------

class TestPhotoFilterValid:
    def test_all_photos_minimal(self):
        pf = PhotoFilter(name="All", selection="all_photos")
        pf.validate()

    def test_search_minimal(self):
        pf = PhotoFilter(
            name="Beaches", selection="search", search_query="beach sunset"
        )
        pf.validate()

    def test_search_non_random(self):
        pf = PhotoFilter(
            name="Top", selection="search", randomize=False,
            search_query="Birthday"
        )
        pf.validate()

    def test_album_minimal(self):
        pf = PhotoFilter(
            name="Family", selection="album", album_name="Family 2024"
        )
        pf.validate()

    def test_album_non_random(self):
        pf = PhotoFilter(
            name="Family", selection="album", randomize=False,
            album_name="Family 2024"
        )
        pf.validate()

    def test_full_criteria(self):
        pf = PhotoFilter(
            name="Complex",
            selection="all_photos",
            people=["Alice", "Bob"],
            location="Disney World",
            taken_after="2023-01-01",
            taken_before="2024-12-31",
            favorites_only=True,
            rating=3,
        )
        pf.validate()

    def test_iso_date_with_time(self):
        pf = PhotoFilter(
            name="Dated", selection="all_photos",
            taken_after="2023-06-15T08:30:00"
        )
        pf.validate()

    def test_iso_date_with_tz(self):
        pf = PhotoFilter(
            name="TZ", selection="all_photos",
            taken_after="2023-06-15T08:30:00+05:30"
        )
        pf.validate()


# ---------------------------------------------------------------------------
# PhotoFilter – validation errors
# ---------------------------------------------------------------------------

class TestPhotoFilterInvalid:
    def test_empty_name(self):
        pf = PhotoFilter(name="", selection="all_photos")
        with pytest.raises(ValueError, match="name must not be empty"):
            pf.validate()

    def test_bad_selection(self):
        pf = PhotoFilter(name="X", selection="magic")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="Invalid selection type"):
            pf.validate()

    def test_all_photos_randomize_false(self):
        pf = PhotoFilter(name="X", selection="all_photos", randomize=False)
        with pytest.raises(ValueError, match="randomize=True"):
            pf.validate()

    def test_search_without_query(self):
        pf = PhotoFilter(name="X", selection="search")
        with pytest.raises(ValueError, match="search_query is required"):
            pf.validate()

    def test_search_query_on_all_photos(self):
        pf = PhotoFilter(
            name="X", selection="all_photos", search_query="oops"
        )
        with pytest.raises(ValueError, match="search_query should only"):
            pf.validate()

    def test_search_pool_size_too_low(self):
        pf = PhotoFilter(
            name="X", selection="search", search_query="q",
            search_pool_size=0
        )
        with pytest.raises(ValueError, match="search_pool_size"):
            pf.validate()

    def test_search_pool_size_too_high(self):
        pf = PhotoFilter(
            name="X", selection="search", search_query="q",
            search_pool_size=1001
        )
        with pytest.raises(ValueError, match="search_pool_size"):
            pf.validate()

    def test_album_without_name(self):
        pf = PhotoFilter(name="X", selection="album")
        with pytest.raises(ValueError, match="album_name is required"):
            pf.validate()

    def test_album_name_on_search(self):
        pf = PhotoFilter(
            name="X", selection="search", search_query="q",
            album_name="oops"
        )
        with pytest.raises(ValueError, match="album_name should only"):
            pf.validate()

    def test_bad_date_format(self):
        pf = PhotoFilter(
            name="X", selection="all_photos", taken_after="June 2023"
        )
        with pytest.raises(ValueError, match="ISO-format date"):
            pf.validate()

    def test_rating_too_low(self):
        pf = PhotoFilter(name="X", selection="all_photos", rating=0)
        with pytest.raises(ValueError, match="rating must be between"):
            pf.validate()

    def test_rating_too_high(self):
        pf = PhotoFilter(name="X", selection="all_photos", rating=6)
        with pytest.raises(ValueError, match="rating must be between"):
            pf.validate()


# ---------------------------------------------------------------------------
# PhotoFilter – serialisation round-trip
# ---------------------------------------------------------------------------

class TestPhotoFilterSerde:
    def test_round_trip_all_photos(self):
        pf = PhotoFilter(
            name="Random", selection="all_photos",
            people=["Alice"], location="Home", favorites_only=True, rating=4,
            taken_after="2023-01-01",
        )
        d = pf.to_dict()
        pf2 = PhotoFilter.from_dict(d)
        assert pf2.name == pf.name
        assert pf2.selection == pf.selection
        assert pf2.people == pf.people
        assert pf2.location == pf.location
        assert pf2.favorites_only == pf.favorites_only
        assert pf2.rating == pf.rating

    def test_round_trip_search(self):
        pf = PhotoFilter(
            name="Search", selection="search", randomize=True,
            search_query="cats", search_pool_size=500,
        )
        d = pf.to_dict()
        pf2 = PhotoFilter.from_dict(d)
        assert pf2.search_query == "cats"
        assert pf2.search_pool_size == 500
        assert pf2.randomize is True

    def test_round_trip_album(self):
        pf = PhotoFilter(
            name="Album", selection="album", randomize=False,
            album_name="Vacation 2024",
        )
        d = pf.to_dict()
        pf2 = PhotoFilter.from_dict(d)
        assert pf2.album_name == "Vacation 2024"
        assert pf2.randomize is False


# ---------------------------------------------------------------------------
# FetcherConfig
# ---------------------------------------------------------------------------

class TestFetcherConfig:
    def _make_filter(self, **kw) -> PhotoFilter:
        defaults = dict(name="Default", selection="all_photos")
        defaults.update(kw)
        return PhotoFilter(**defaults)

    def test_valid_minimal(self):
        cfg = FetcherConfig(filters=[self._make_filter()])
        cfg.validate()

    def test_empty_filters(self):
        cfg = FetcherConfig(filters=[])
        with pytest.raises(ValueError, match="At least one filter"):
            cfg.validate()

    def test_duplicate_filter_names(self):
        cfg = FetcherConfig(
            filters=[self._make_filter(), self._make_filter()]
        )
        with pytest.raises(ValueError, match="Duplicate filter name"):
            cfg.validate()

    def test_bad_num_photos(self):
        cfg = FetcherConfig(filters=[self._make_filter()], num_photos=0)
        with pytest.raises(ValueError, match="num_photos"):
            cfg.validate()

    def test_bad_interval(self):
        cfg = FetcherConfig(
            filters=[self._make_filter()], update_interval_minutes=0
        )
        with pytest.raises(ValueError, match="update_interval_minutes"):
            cfg.validate()

    def test_bad_quality(self):
        cfg = FetcherConfig(
            filters=[self._make_filter()], download_quality="ultra"
        )
        with pytest.raises(ValueError, match="download_quality"):
            cfg.validate()

    def test_invalid_alias_propagates(self):
        cfg = FetcherConfig(
            filters=[self._make_filter()],
            location_aliases={"Bad": LocationAlias()},
        )
        with pytest.raises(ValueError, match="at least one"):
            cfg.validate()

    def test_round_trip(self):
        cfg = FetcherConfig(
            filters=[
                PhotoFilter(name="A", selection="all_photos"),
                PhotoFilter(
                    name="B", selection="search", search_query="dogs",
                    search_pool_size=300, randomize=True,
                ),
            ],
            num_photos=20,
            update_interval_minutes=30,
            download_quality="fullsize",
            location_aliases={
                "Disney": LocationAlias(city="Celebration", state="Florida"),
            },
        )
        d = cfg.to_dict()
        cfg2 = FetcherConfig.from_dict(d)
        cfg2.validate()
        assert len(cfg2.filters) == 2
        assert cfg2.num_photos == 20
        assert cfg2.download_quality == "fullsize"
        assert "Disney" in cfg2.location_aliases
        assert cfg2.location_aliases["Disney"].city == "Celebration"
