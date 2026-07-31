"""Unit tests for photo_providers.types."""

from __future__ import annotations

import pytest

from providers.photo_providers.types import (
    LocationAlias,
    PhotoAlbum,
    PhotoFilter,
    PhotoMetadata,
    PhotoPerson,
    PhotoProviderName,
)


# ---------------------------------------------------------------------------
# LocationAlias
# ---------------------------------------------------------------------------

class TestLocationAlias:
    def test_valid_city_only(self):
        a = LocationAlias(city="Orlando")
        a.validate()

    def test_valid_state_only(self):
        a = LocationAlias(state="Florida")
        a.validate()

    def test_valid_country_only(self):
        a = LocationAlias(country="US")
        a.validate()

    def test_empty_raises(self):
        a = LocationAlias()
        with pytest.raises(ValueError, match="at least one"):
            a.validate()

    def test_to_dict_omits_none(self):
        a = LocationAlias(city="Orlando", state="Florida")
        assert a.to_dict() == {"city": "Orlando", "state": "Florida"}


# ---------------------------------------------------------------------------
# PhotoFilter – validation
# ---------------------------------------------------------------------------

class TestPhotoFilterValidation:
    def test_all_photos_valid(self):
        pf = PhotoFilter(name="All", selection="all_photos")
        pf.validate()

    def test_search_valid(self):
        pf = PhotoFilter(
            name="Beaches", selection="search", search_query="beach"
        )
        pf.validate()

    def test_album_valid(self):
        pf = PhotoFilter(
            name="Family", selection="album", album_name="Family 2024"
        )
        pf.validate()

    def test_empty_name_raises(self):
        pf = PhotoFilter(name="", selection="all_photos")
        with pytest.raises(ValueError, match="name must not be empty"):
            pf.validate()

    def test_invalid_selection_raises(self):
        pf = PhotoFilter(name="X", selection="magic")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="Invalid selection type"):
            pf.validate()

    def test_all_photos_randomize_false_raises(self):
        pf = PhotoFilter(name="X", selection="all_photos", randomize=False)
        with pytest.raises(ValueError, match="randomize=True"):
            pf.validate()

    def test_search_without_query_raises(self):
        pf = PhotoFilter(name="X", selection="search")
        with pytest.raises(ValueError, match="search_query is required"):
            pf.validate()

    def test_album_without_name_raises(self):
        pf = PhotoFilter(name="X", selection="album")
        with pytest.raises(ValueError, match="album_name is required"):
            pf.validate()

    def test_invalid_date_format_raises(self):
        pf = PhotoFilter(
            name="X", selection="all_photos",
            taken_after="not-a-date"
        )
        with pytest.raises(ValueError, match="ISO-format"):
            pf.validate()

    def test_rating_out_of_bounds_raises(self):
        pf = PhotoFilter(name="X", selection="all_photos", rating=6)
        with pytest.raises(ValueError, match="rating must be between 1 and 5"):
            pf.validate()

    def test_search_pool_size_out_of_range_raises(self):
        pf = PhotoFilter(
            name="X", selection="search",
            search_query="q",
            search_pool_size=0,
        )
        with pytest.raises(ValueError, match="search_pool_size"):
            pf.validate()


# ---------------------------------------------------------------------------
# PhotoFilter – serialisation
# ---------------------------------------------------------------------------

class TestPhotoFilterSerialisation:
    def test_to_dict_from_dict_round_trip(self):
        pf = PhotoFilter(
            name="Test",
            selection="search",
            search_query="sunset",
            randomize=False,
            search_pool_size=100,
        )
        d = pf.to_dict()
        restored = PhotoFilter.from_dict(d)
        assert restored.name == pf.name
        assert restored.selection == pf.selection
        assert restored.search_query == pf.search_query
        assert restored.randomize == pf.randomize
        assert restored.search_pool_size == pf.search_pool_size

    def test_to_dict_minimal(self):
        pf = PhotoFilter(name="Min", selection="all_photos")
        d = pf.to_dict()
        assert d["name"] == "Min"
        assert d["selection"] == "all_photos"
        assert "randomize" not in d or d["randomize"] is True

    def test_paused_omitted_when_false(self):
        pf = PhotoFilter(name="Min", selection="all_photos")
        assert "paused" not in pf.to_dict()

    def test_paused_round_trip(self):
        pf = PhotoFilter(name="Paused", selection="all_photos", paused=True)
        d = pf.to_dict()
        assert d["paused"] is True
        restored = PhotoFilter.from_dict(d)
        assert restored.paused is True

    def test_paused_defaults_false_from_dict(self):
        restored = PhotoFilter.from_dict({"name": "X", "selection": "all_photos"})
        assert restored.paused is False

    def test_paused_filter_still_validates(self):
        pf = PhotoFilter(
            name="Paused", selection="search", search_query="q", paused=True
        )
        pf.validate()


# ---------------------------------------------------------------------------
# PhotoMetadata
# ---------------------------------------------------------------------------

class TestPhotoMetadata:
    def test_construct_empty_lists(self):
        m = PhotoMetadata(people=[], albums=[])
        assert m.people == []
        assert m.albums == []

    def test_construct_with_data(self):
        m = PhotoMetadata(
            people=[PhotoPerson("Alice", 10), PhotoPerson("Bob", 5)],
            albums=[PhotoAlbum("Vacation", "alb-1", 20)],
        )
        assert len(m.people) == 2
        assert m.people[0].name == "Alice"
        assert m.people[0].asset_count == 10
        assert len(m.albums) == 1
        assert m.albums[0].name == "Vacation"
        assert m.albums[0].id == "alb-1"


# ---------------------------------------------------------------------------
# PhotoProviderName
# ---------------------------------------------------------------------------

class TestPhotoProviderName:
    def test_parse_immich(self):
        assert PhotoProviderName.parse("immich") == PhotoProviderName.IMMICH
        assert PhotoProviderName.parse("IMMICH") == PhotoProviderName.IMMICH

    def test_parse_invalid_raises(self):
        with pytest.raises(ValueError, match="Unsupported photo provider"):
            PhotoProviderName.parse("unknown")

    def test_parse_none_raises(self):
        with pytest.raises(ValueError, match="Unsupported photo provider"):
            PhotoProviderName.parse(None)
