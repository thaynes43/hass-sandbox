"""Tests for DashboardNotify schedule checking logic."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

# Mock hassapi before importing the app
mock_hass = MagicMock()
mock_hass.Hass = type("_MockHass", (), {"__init__": lambda self, *a, **kw: None})
sys.modules["hassapi"] = mock_hass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))

from dashboard_notify.dashboard_notify_app import DashboardNotify


def _make_app() -> DashboardNotify:
    """Create a minimal DashboardNotify with schedule checking available."""
    ad = MagicMock()
    app = DashboardNotify(ad, MagicMock())
    return app


def _ts(hour: int, minute: int, weekday: str = "mon") -> float:
    """Create a timestamp for a given hour:minute on a specific day.

    weekday: mon=0, tue=1, ... sun=6
    We use a known Monday (2026-03-09) as baseline.
    """
    day_offsets = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
    # 2026-03-09 is a Monday
    base_day = 9 + day_offsets.get(weekday, 0)
    dt = datetime(2026, 3, base_day, hour, minute, 0)
    return dt.timestamp()


class TestBasicSchedule:
    def test_within_window(self):
        app = _make_app()
        schedule = {"start": "08:00", "end": "09:00"}
        assert app._is_schedule_active(schedule, _ts(8, 30)) is True

    def test_before_window(self):
        app = _make_app()
        schedule = {"start": "08:00", "end": "09:00"}
        assert app._is_schedule_active(schedule, _ts(7, 59)) is False

    def test_at_start(self):
        app = _make_app()
        schedule = {"start": "08:00", "end": "09:00"}
        assert app._is_schedule_active(schedule, _ts(8, 0)) is True

    def test_at_end(self):
        app = _make_app()
        schedule = {"start": "08:00", "end": "09:00"}
        # End is exclusive
        assert app._is_schedule_active(schedule, _ts(9, 0)) is False

    def test_after_window(self):
        app = _make_app()
        schedule = {"start": "08:00", "end": "09:00"}
        assert app._is_schedule_active(schedule, _ts(10, 0)) is False


class TestOvernightWindow:
    def test_overnight_before_midnight(self):
        app = _make_app()
        schedule = {"start": "21:30", "end": "01:00"}
        assert app._is_schedule_active(schedule, _ts(22, 0)) is True

    def test_overnight_after_midnight(self):
        app = _make_app()
        schedule = {"start": "21:30", "end": "01:00"}
        assert app._is_schedule_active(schedule, _ts(0, 30)) is True

    def test_overnight_outside_before(self):
        app = _make_app()
        schedule = {"start": "21:30", "end": "01:00"}
        assert app._is_schedule_active(schedule, _ts(15, 0)) is False

    def test_overnight_outside_after(self):
        app = _make_app()
        schedule = {"start": "21:30", "end": "01:00"}
        assert app._is_schedule_active(schedule, _ts(2, 0)) is False


class TestDayFiltering:
    def test_correct_day(self):
        app = _make_app()
        schedule = {"start": "08:00", "end": "09:00", "days": ["mon", "tue"]}
        assert app._is_schedule_active(schedule, _ts(8, 30, "mon")) is True

    def test_wrong_day(self):
        app = _make_app()
        schedule = {"start": "08:00", "end": "09:00", "days": ["mon", "tue"]}
        assert app._is_schedule_active(schedule, _ts(8, 30, "fri")) is False

    def test_no_days_filter(self):
        app = _make_app()
        schedule = {"start": "08:00", "end": "09:00"}
        # Should be active on any day
        assert app._is_schedule_active(schedule, _ts(8, 30, "sat")) is True

    def test_weekdays_only(self):
        app = _make_app()
        schedule = {
            "start": "07:30", "end": "08:30",
            "days": ["mon", "tue", "wed", "thu", "fri"]
        }
        assert app._is_schedule_active(schedule, _ts(8, 0, "wed")) is True
        assert app._is_schedule_active(schedule, _ts(8, 0, "sat")) is False


class TestNoSchedule:
    def test_none_schedule(self):
        app = _make_app()
        assert app._is_schedule_active(None, _ts(12, 0)) is False

    def test_empty_schedule(self):
        app = _make_app()
        assert app._is_schedule_active({}, _ts(12, 0)) is False
