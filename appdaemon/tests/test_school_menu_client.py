"""Unit tests for the school menu provider client."""

from __future__ import annotations

import pytest

from providers.school_menu.types import MenuDay, MenuItem, MenuMonth
from providers.school_menu.client import SchoolMenuClient


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

class TestMenuDay:
    def test_basic_construction(self):
        item = MenuItem(name="Pizza", category="Entrees")
        day = MenuDay(day=5, month=2, year=2026, items=[item])
        assert day.day == 5
        assert len(day.items) == 1
        assert day.items[0].name == "Pizza"

    def test_default_items(self):
        day = MenuDay(day=1, month=0, year=2026)
        assert day.items == []


class TestMenuMonth:
    def test_display_month(self):
        menu = MenuMonth(
            menu_id="abc123",
            menu_type_name="Elementary Lunch",
            month=2,  # 0-indexed March
            year=2026,
        )
        assert menu.display_month == 3

    def test_display_month_january(self):
        menu = MenuMonth(
            menu_id="abc",
            menu_type_name="Test",
            month=0,
            year=2026,
        )
        assert menu.display_month == 1


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

class TestParseMenu:
    """Tests for SchoolMenuClient._parse_menu static method."""

    def _sample_graphql_response(self):
        return {
            "id": "69652cd259d4d077766cfd3b",
            "month": 2,
            "year": 2026,
            "menuType": {
                "id": "58ebb898eabc88eb0c8b457a",
                "name": "Elementary Lunch",
            },
            "items": [
                {
                    "day": 3,
                    "month": 2,
                    "year": 2026,
                    "product": {
                        "name": "Chicken Tenders",
                        "category": "Entrees",
                        "hide_on_calendars": "",
                        "is_ancillary": False,
                    },
                },
                {
                    "day": 3,
                    "month": 2,
                    "year": 2026,
                    "product": {
                        "name": "Milk Choice",
                        "category": "Milk",
                        "hide_on_calendars": "",
                        "is_ancillary": False,
                    },
                },
                {
                    "day": 5,
                    "month": 2,
                    "year": 2026,
                    "product": {
                        "name": "Pizza Day",
                        "category": "Entrees",
                        "hide_on_calendars": "",
                        "is_ancillary": False,
                    },
                },
            ],
            "previousMonthPublished": {"id": "prev-id-123"},
            "nextMonthPublished": {"id": "next-id-456"},
        }

    def test_parses_basic_menu(self):
        data = self._sample_graphql_response()
        result = SchoolMenuClient._parse_menu(data)

        assert isinstance(result, MenuMonth)
        assert result.menu_id == "69652cd259d4d077766cfd3b"
        assert result.menu_type_name == "Elementary Lunch"
        assert result.month == 2
        assert result.year == 2026
        assert result.display_month == 3

    def test_groups_items_by_day(self):
        data = self._sample_graphql_response()
        result = SchoolMenuClient._parse_menu(data)

        assert len(result.days) == 2
        # Day 3 has 2 items
        day3 = result.days[0]
        assert day3.day == 3
        assert len(day3.items) == 2
        assert day3.items[0].name == "Chicken Tenders"
        assert day3.items[1].name == "Milk Choice"
        # Day 5 has 1 item
        day5 = result.days[1]
        assert day5.day == 5
        assert len(day5.items) == 1

    def test_days_sorted(self):
        data = self._sample_graphql_response()
        result = SchoolMenuClient._parse_menu(data)
        day_numbers = [d.day for d in result.days]
        assert day_numbers == sorted(day_numbers)

    def test_navigation_ids(self):
        data = self._sample_graphql_response()
        result = SchoolMenuClient._parse_menu(data)

        assert result.previous_month_id == "prev-id-123"
        assert result.next_month_id == "next-id-456"

    def test_null_navigation(self):
        data = self._sample_graphql_response()
        data["previousMonthPublished"] = None
        data["nextMonthPublished"] = None
        result = SchoolMenuClient._parse_menu(data)

        assert result.previous_month_id is None
        assert result.next_month_id is None

    def test_filters_hidden_items(self):
        data = self._sample_graphql_response()
        data["items"].append(
            {
                "day": 3,
                "month": 2,
                "year": 2026,
                "product": {
                    "name": "Hidden Item",
                    "category": "Entrees",
                    "hide_on_calendars": "1",
                    "is_ancillary": False,
                },
            }
        )
        result = SchoolMenuClient._parse_menu(data)
        day3 = result.days[0]
        names = [i.name for i in day3.items]
        assert "Hidden Item" not in names

    def test_skips_null_product(self):
        data = self._sample_graphql_response()
        data["items"].append(
            {
                "day": 3,
                "month": 2,
                "year": 2026,
                "product": None,
            }
        )
        result = SchoolMenuClient._parse_menu(data)
        day3 = result.days[0]
        # Should still have original 2 items, null product skipped
        assert len(day3.items) == 2

    def test_empty_items_list(self):
        data = self._sample_graphql_response()
        data["items"] = []
        result = SchoolMenuClient._parse_menu(data)
        assert result.days == []

    def test_preserves_category(self):
        data = self._sample_graphql_response()
        result = SchoolMenuClient._parse_menu(data)
        assert result.days[0].items[0].category == "Entrees"
        assert result.days[0].items[1].category == "Milk"

    def test_preserves_ancillary_flag(self):
        data = self._sample_graphql_response()
        data["items"][0]["product"]["is_ancillary"] = True
        result = SchoolMenuClient._parse_menu(data)
        assert result.days[0].items[0].is_ancillary is True


# ---------------------------------------------------------------------------
# Notices (content overlay boxes placed on the calendar design)
# ---------------------------------------------------------------------------

def _school_days(year, month0, skip=()):
    """MenuDays with one item for every weekday of the month except ``skip``."""
    import calendar as _cal

    days = []
    for week in _cal.monthcalendar(year, month0 + 1):
        for d in week[0:5]:
            if d and d not in skip:
                days.append(
                    MenuDay(
                        day=d, month=month0, year=year,
                        items=[MenuItem(name="Pizza", category="Entrees")],
                    )
                )
    return days


def _box(text, left, top, width="8.4%"):
    box = {"html": f"<p>{text}</p>", "left": left, "top": top}
    if width is not None:
        box["width"] = width
    return box


def _notices(menu):
    return {d.day: d.notice for d in menu.days if d.notice}


EARLY = "EARLY RELEASE GRAB AND GO BREAKFAST AND LUNCH WILL BE SERVED"


class TestMergeNotices:
    """SchoolMenuClient._merge_notices: each notice lands on the day under it.

    Coordinates are the district's real boxes (percent of the design page).
    """

    def test_october_2026_early_release_stays_on_tuesday(self):
        # Mon 12 has no menu and no box (holiday); the box sits over Tue 13.
        # The old left-to-right hand-out put it on Monday 12.
        menu = MenuMonth(
            menu_id="oct", menu_type_name="Elementary", month=9, year=2026,
            days=_school_days(2026, 9, skip=(12, 13)),
        )
        SchoolMenuClient._merge_notices(
            menu, [_box(EARLY, "22.131068781750727%", "48.18023149577523%", "8.40077%")]
        )
        assert _notices(menu) == {13: EARLY}
        assert 12 not in {d.day for d in menu.days}
        tue = next(d for d in menu.days if d.day == 13)
        assert tue.month == 9 and tue.year == 2026
        assert [i.name for i in tue.items] == ["Grab and Go Breakfast & Lunch"]

    def test_june_2025_early_release_stays_on_friday(self):
        # Thu 19 (Juneteenth) has no box; the early release box is over Fri 20.
        menu = MenuMonth(
            menu_id="jun", menu_type_name="Elementary", month=5, year=2025,
            days=_school_days(2025, 5, skip=(19, 20)),
        )
        # Friday column, third of five rows (June 2025 opens on a Sunday)
        SchoolMenuClient._merge_notices(menu, [_box(EARLY, "68.0%", "48.0%", "9.9%")])
        assert _notices(menu) == {20: EARLY}

    def test_september_2026_two_notices_in_one_week(self):
        menu = MenuMonth(
            menu_id="sep", menu_type_name="Elementary", month=8, year=2026,
            days=_school_days(2026, 8, skip=(21, 24)),
        )
        SchoolMenuClient._merge_notices(menu, [
            _box(EARLY, "53.6%", "65.2%", "9.9177%"),
            _box("NO SCHOOL IN OBSERVANCE OF YOM KIPPUR", "6.9%", "66.2%", "8.147%"),
        ])
        assert _notices(menu) == {
            21: "NO SCHOOL IN OBSERVANCE OF YOM KIPPUR",
            24: EARLY,
        }
        # A no-school notice gets no synthetic grab-and-go item
        mon = next(d for d in menu.days if d.day == 21)
        assert mon.items == []

    def test_days_stay_sorted(self):
        menu = MenuMonth(
            menu_id="oct", menu_type_name="Elementary", month=9, year=2026,
            days=_school_days(2026, 9, skip=(12, 13)),
        )
        SchoolMenuClient._merge_notices(menu, [_box(EARLY, "22.1%", "48.2%")])
        assert [d.day for d in menu.days] == sorted(d.day for d in menu.days)

    def test_notice_over_a_day_with_a_menu_is_not_moved(self):
        # Nov 2025: the box sits over Wed 26, which has a menu; Thu 27
        # (Thanksgiving) is the week's only missing day. Old code put it on 27.
        menu = MenuMonth(
            menu_id="nov", menu_type_name="Middle School", month=10, year=2025,
            days=_school_days(2025, 10, skip=(27, 28)),
        )
        SchoolMenuClient._merge_notices(menu, [_box(EARLY, "38.2%", "78.6%")])
        assert _notices(menu) == {}
        assert {27, 28}.isdisjoint(d.day for d in menu.days)

    def test_sidebar_and_header_text_is_ignored(self):
        menu = MenuMonth(
            menu_id="oct", menu_type_name="Elementary", month=9, year=2026,
            days=_school_days(2026, 9, skip=(12, 13)),
        )
        SchoolMenuClient._merge_notices(menu, [
            _box("Kitchen CLOSED for cleaning on the weekend", "83.2%", "40.0%", "12.9%"),
            _box("HOLIDAY menu below", "34.0%", "2.8%", "18.6%"),
        ])
        assert _notices(menu) == {}

    def test_notice_over_a_cell_outside_the_month_is_dropped(self):
        # Oct 2026 starts on a Thursday: Monday of the first row is September.
        menu = MenuMonth(
            menu_id="oct", menu_type_name="Elementary", month=9, year=2026,
            days=_school_days(2026, 9, skip=(1,)),
        )
        SchoolMenuClient._merge_notices(menu, [_box(EARLY, "6.9%", "12.0%")])
        assert _notices(menu) == {}

    def test_box_without_width_uses_its_left_edge(self):
        menu = MenuMonth(
            menu_id="oct", menu_type_name="Elementary", month=9, year=2026,
            days=_school_days(2026, 9, skip=(12, 13)),
        )
        SchoolMenuClient._merge_notices(menu, [_box(EARLY, "22.1%", "48.2%", width=None)])
        assert _notices(menu) == {13: EARLY}

    def test_wide_banner_lands_on_its_first_day(self):
        # A box over Mon-Tue (28% wide) starts in the Monday column; its own
        # centre (20.9%) would fall in Tuesday.
        menu = MenuMonth(
            menu_id="oct", menu_type_name="Elementary", month=9, year=2026,
            days=_school_days(2026, 9, skip=(12, 13)),
        )
        SchoolMenuClient._merge_notices(
            menu, [_box("NO SCHOOL COLUMBUS DAY", "6.9%", "48.2%", "28%")]
        )
        assert _notices(menu) == {12: "NO SCHOOL COLUMBUS DAY"}

    def test_dropped_notices_are_logged_as_warnings(self, caplog):
        menu = MenuMonth(
            menu_id="oct", menu_type_name="Elementary", month=9, year=2026,
            days=_school_days(2026, 9, skip=(13,)),
        )
        with caplog.at_level("WARNING", logger="providers.school_menu.client"):
            SchoolMenuClient._merge_notices(menu, [
                _box(EARLY, "auto", "48.2%"),
                _box("Kitchen CLOSED on the weekend", "83.2%", "40.0%", "12.9%"),
            ])
        warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert any("unreadable box position" in m and "auto" in m for m in warnings)
        assert any("outside the calendar grid" in m and "Kitchen CLOSED" in m for m in warnings)
        assert _notices(menu) == {}

    def test_unparseable_position_is_skipped(self):
        menu = MenuMonth(
            menu_id="oct", menu_type_name="Elementary", month=9, year=2026,
            days=_school_days(2026, 9, skip=(13,)),
        )
        SchoolMenuClient._merge_notices(menu, [_box(EARLY, "auto", "48.2%")])
        assert _notices(menu) == {}
