"""Yearless photo dates: calendar boundaries, complete scans and intersections."""

from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from providers.photo_providers.date_filters import matches_capture_dates
from providers.photo_providers.immich_client import ImmichClient
from providers.photo_providers.immich_selectors import create_selector
from providers.photo_providers.types import (
    LocationAlias, PhotoFilter, PhotoProviderError, RecurringDateRange,
)


def _filter(**overrides):
    return PhotoFilter.from_dict({
        "name": "Birthday", "selection": "all_photos",
        "recurring_date_ranges": [{"start": "07-04"}], **overrides,
    })


def _asset(asset_id, captured, **overrides):
    return {"id": asset_id, "localDateTime": captured, **overrides}


def _page(assets, next_page=None):
    return {"assets": {"items": assets, "nextPage": next_page}}


def _client(*pages):
    client = ImmichClient(base_url="https://immich.test", api_key="test-key")
    client._json_request = AsyncMock(side_effect=pages)
    return client


@pytest.mark.parametrize("invalid", [
    None, "07-04", {}, [{"end": "07-04"}], [{"start": "7-04"}],
    [{"start": "00-01"}], [{"start": "13-01"}], [{"start": "02-30"}],
    [{"start": "04-31"}], [{"start": "12-25", "end": ""}],
    [{"start": "12-25", "end": "2024-12-25"}], [{"start": 704}],
    [{"start": "07-04", "extra": True}], ["07-04"],
])
def test_malformed_recurring_ranges_rejected(invalid):
    with pytest.raises(ValueError):
        _filter(recurring_date_ranges=invalid)


def test_ranges_round_trip_and_default_single_day():
    original = _filter(recurring_date_ranges=[
        {"start": "07-04"}, {"start": "12-24", "end": "12-25"},
    ])
    original.validate()
    serialized = original.to_dict()
    assert serialized["recurring_date_ranges"] == [
        {"start": "07-04", "end": "07-04"},
        {"start": "12-24", "end": "12-25"},
    ]
    assert PhotoFilter.from_dict(serialized).to_dict() == serialized
    assert "recurring_date_ranges" not in PhotoFilter(name="Old", selection="all_photos").to_dict()


@pytest.mark.parametrize("captured, expected", [
    ("1990-07-04T00:00:00", True), ("2026-07-04T23:59:59.999Z", True),
    ("2025-07-03T23:59:59Z", False), ("2025-07-05T00:00:00Z", False),
    ("2024-12-24T00:00:00Z", True), ("2020-12-25T23:59:59Z", True),
    ("2024-12-26T00:00:00Z", False),
])
def test_multiple_ranges_are_inclusive_or_across_years(captured, expected):
    pf = _filter(recurring_date_ranges=[
        {"start": "07-04"}, {"start": "12-24", "end": "12-25"},
    ])
    assert matches_capture_dates(_asset("a", captured), pf) is expected


@pytest.mark.parametrize("captured, expected", [
    ("2024-12-30", True), ("2025-01-01", True), ("2025-01-02", True),
    ("2024-12-29", False), ("2025-01-03", False), ("2024-07-04", False),
])
def test_wrapping_range(captured, expected):
    pf = _filter(recurring_date_ranges=[{"start": "12-30", "end": "01-02"}])
    assert matches_capture_dates(_asset("a", captured), pf) is expected


@pytest.mark.parametrize("captured, expected", [
    ("2000-02-29", True), ("2024-02-29", True), ("1900-02-28", False),
    ("2023-03-01", False), ("2023-02-29", False), ("1900-02-29", False),
])
def test_leap_day_is_never_shifted(captured, expected):
    pf = _filter(recurring_date_ranges=[{"start": "02-29"}])
    pf.validate()
    assert matches_capture_dates(_asset("a", captured), pf) is expected


def test_local_day_wins_over_utc_and_invalid_local_falls_back():
    pf = _filter()
    assert matches_capture_dates(_asset(
        "local", "2024-07-04T23:30:00Z", fileCreatedAt="2024-07-05T05:30:00Z",
    ), pf)
    assert not matches_capture_dates(_asset(
        "other", "2024-07-03T23:30:00Z", fileCreatedAt="2024-07-04T05:30:00Z",
    ), pf)
    assert matches_capture_dates(_asset(
        "fallback", "invalid", fileCreatedAt="2024-07-04T12:00:00Z",
    ), pf)
    assert not matches_capture_dates({"id": "missing"}, pf)


def test_absolute_days_intersect_recurring_ranges_and_include_last_day():
    pf = _filter(taken_after="2023-01-01", taken_before="2024-07-04")
    assert not matches_capture_dates(_asset("old", "2022-07-04T12:00:00Z"), pf)
    assert matches_capture_dates(_asset("end", "2024-07-04T23:59:59Z"), pf)
    assert not matches_capture_dates(_asset("new", "2025-07-04T12:00:00Z"), pf)
    assert not matches_capture_dates(_asset("wrong-day", "2024-07-03T12:00:00Z"), pf)


def test_timestamp_bounds_preserve_offsets_and_naive_local_times():
    pf = _filter(taken_after="2024-07-04T10:00:00-05:00")
    assert not matches_capture_dates(_asset(
        "before", "2024-07-04T12:00:00Z", fileCreatedAt="2024-07-04T14:59:59Z",
    ), pf)
    assert matches_capture_dates(_asset(
        "at", "2024-07-04T10:00:00Z", fileCreatedAt="2024-07-04T15:00:00Z",
    ), pf)
    pf.taken_before = "2024-07-04T22:00:00"
    assert not matches_capture_dates(_asset(
        "late", "2024-07-04T22:00:01Z", fileCreatedAt="2024-07-05T03:00:01Z",
    ), pf)


@pytest.mark.parametrize("overrides", [
    {"taken_after": "2023-02-29"}, {"taken_before": "2024-04-31"},
    {"taken_after": "2025-01-01", "taken_before": "2024-12-31"},
    {"taken_after": "2024-07-04T99:00:00Z"}, {"taken_after": 2024},
    {"people": "Alice"}, {"people": [None]}, {"favorites_only": "false"},
    {"search_pool_size": True}, {"name": "   "}, {"paused": 1},
])
def test_malformed_filter_criteria_rejected(overrides):
    with pytest.raises(ValueError):
        _filter(**overrides).validate()


def test_mixed_date_and_time_same_day_bounds_are_valid():
    pf = _filter(taken_after="2024-07-04T12:00:00", taken_before="2024-07-04")
    pf.validate()
    assert matches_capture_dates(_asset("end", "2024-07-04T23:30:00"), pf)


@pytest.mark.asyncio
async def test_all_photos_scans_later_pages_before_sampling_and_intersects_criteria():
    client = _client(
        _page([_asset("miss", "2024-07-03"), _asset("first", "2023-07-04")], "2"),
        _page([_asset("last", "2024-07-04T23:30:00Z", fileCreatedAt="2024-07-05T04:30:00Z")]),
    )
    pf = _filter(
        people=["Alice"], location="Home", favorites_only=True,
        taken_after="2023-01-01", taken_before="2024-07-04",
    )
    selector = create_selector(
        client, pf, people_map={"Alice": "uuid-alice"},
        location_aliases={"Home": LocationAlias(city="Boston", state="Massachusetts")},
    )
    with patch("providers.photo_providers.immich_selectors.random.randrange", return_value=0):
        result = await selector.get_asset_ids(1)
    assert result == ["last"]
    assert client._json_request.await_count == 2
    for request in client._json_request.await_args_list:
        assert request.args == ("POST", "/api/search/metadata")
        body = request.kwargs["json"]
        assert body["personIds"] == ["uuid-alice"]
        assert body["city"] == "Boston" and body["state"] == "Massachusetts"
        assert body["isFavorite"] is True and body["type"] == "IMAGE"
    assert client._json_request.await_args_list[1].kwargs["json"]["page"] == 2


@pytest.mark.asyncio
async def test_absolute_only_filter_includes_entire_end_day():
    client = _client(_page([
        _asset("midnight", "2024-07-04T00:00:00"),
        _asset("late", "2024-07-04T23:59:59"),
        _asset("after", "2024-07-05T00:00:00"),
    ]))
    pf = _filter(recurring_date_ranges=[], taken_after="2024-07-04", taken_before="2024-07-04")
    assert set(await create_selector(client, pf).get_asset_ids(10)) == {"midnight", "late"}


@pytest.mark.asyncio
async def test_overlapping_ranges_and_repeated_asset_do_not_duplicate():
    client = _client(
        _page([_asset("same", "2024-12-25"), _asset("eve", "2024-12-24")], "2"),
        _page([_asset("same", "2024-12-25")]),
    )
    pf = _filter(recurring_date_ranges=[
        {"start": "12-24", "end": "12-26"}, {"start": "12-25"},
    ])
    assert set(await create_selector(client, pf).get_asset_ids(10)) == {"same", "eve"}


@pytest.mark.asyncio
async def test_smart_search_pages_to_matching_top_results_and_stops_at_limit():
    client = _client(
        _page([_asset("unrelated", "2024-05-01"), _asset("top", "2020-07-04")], "2"),
        _page([_asset("next", "2024-07-04"), _asset("extra", "2023-07-04")], "3"),
    )
    pf = _filter(selection="search", search_query="birthday", randomize=False, people=["Jackson"])
    assert await create_selector(client, pf, people_map={"Jackson": "uuid-jackson"}).get_asset_ids(2) == ["top", "next"]
    assert client._json_request.await_count == 2
    for request in client._json_request.await_args_list:
        assert request.args == ("POST", "/api/search/smart")
        assert request.kwargs["json"]["query"] == "birthday"
        assert request.kwargs["json"]["personIds"] == ["uuid-jackson"]


@pytest.mark.asyncio
async def test_smart_search_random_pool_is_built_after_date_matching():
    client = _client(
        _page([_asset("unrelated", "2024-05-01"), _asset("one", "2020-07-04")], "2"),
        _page([_asset("two", "2024-07-04"), _asset("three", "2023-07-04"), _asset("extra", "2022-07-04")], "3"),
    )
    pf = _filter(selection="search", search_query="birthday", search_pool_size=3)
    assert set(await create_selector(client, pf).get_asset_ids(1)) == {"one", "two", "three"}
    assert client._json_request.await_count == 2


@pytest.mark.asyncio
async def test_filtered_album_applies_all_common_criteria_and_dates():
    client = _client(_page([
        _asset("miss", "2024-07-03"), _asset("match", "2024-07-04"),
    ]))
    pf = _filter(selection="album", album_name="Family", randomize=False,
                 people=["Alice"], location="Boston", favorites_only=True, rating=5)
    result = await create_selector(
        client, pf, album_id="album-family", people_map={"Alice": "uuid-alice"},
    ).get_asset_ids(1)
    assert result == ["match"]
    body = client._json_request.await_args.kwargs["json"]
    assert body["albumIds"] == ["album-family"]
    assert body["personIds"] == ["uuid-alice"]
    assert body["city"] == "Boston" and body["isFavorite"] and body["rating"] == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("next_page", ["bogus", "1", "0", -2, True])
async def test_bad_pagination_fails_without_incomplete_success(next_page):
    client = _client(_page([_asset("a", "2024-07-04")], next_page))
    with pytest.raises(PhotoProviderError):
        await create_selector(client, _filter()).get_asset_ids(1)
    assert client._json_request.await_count == 1


@pytest.mark.asyncio
async def test_unknown_person_never_broadens_search():
    client = _client()
    with pytest.raises(PhotoProviderError, match="Unknown people"):
        await create_selector(client, _filter(people=["Unknown"]), people_map={}).get_asset_ids(1)
    client._json_request.assert_not_awaited()
