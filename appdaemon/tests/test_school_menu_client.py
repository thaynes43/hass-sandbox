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
