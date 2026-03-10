"""Unit tests for event_generator module."""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

_APPS_DIR = Path(__file__).resolve().parents[1] / "apps"
if str(_APPS_DIR) not in sys.path:
    sys.path.insert(0, str(_APPS_DIR))

from calendar_from_schedule_app.event_generator import (
    EventInstance,
    generate_events,
    _add_months,
    _build_description,
)
from calendar_from_schedule_app.schedule_parser import (
    ConsumableConfig,
    Schedule,
    ScheduleTask,
    TaskWindow,
)


def _make_schedule(tasks, **kwargs) -> Schedule:
    defaults = {
        "name": "Test",
        "plan_start": date(2026, 1, 1),
        "plan_end": None,
        "tasks": tasks,
        "procedures": {},
        "context": {},
        "consumables": {},
    }
    defaults.update(kwargs)
    return Schedule(**defaults)


def _recurring_days_task(**overrides) -> ScheduleTask:
    defaults = {
        "id": "weekly_test",
        "title": "Weekly Test",
        "type": "recurring_days",
        "severity": "HIGH",
        "frequency_days": 7,
        "first_due": date(2026, 1, 8),
        "window": TaskWindow(before_days=2, after_days=2),
    }
    defaults.update(overrides)
    return ScheduleTask(**defaults)


class TestRecurringDays:
    def test_basic_generation(self):
        task = _recurring_days_task()
        schedule = _make_schedule([task])
        events = generate_events(schedule, date(2026, 1, 31))

        # Jan 8, 15, 22, 29 = 4 events
        assert len(events) == 4
        assert events[0].task_id == "weekly_test"
        assert events[0].title == "Weekly Test"

    def test_single_day_event_on_due_date(self):
        task = _recurring_days_task()
        schedule = _make_schedule([task])
        events = generate_events(schedule, date(2026, 1, 15))

        # Event lands on the due date as a single-day event
        assert events[0].start_date == date(2026, 1, 8)
        assert events[0].end_date == date(2026, 1, 9)  # exclusive single-day

    def test_plan_start_filter(self):
        # first_due before plan_start should be skipped
        task = _recurring_days_task(first_due=date(2025, 12, 25))
        schedule = _make_schedule([task], plan_start=date(2026, 1, 1))
        events = generate_events(schedule, date(2026, 1, 15))

        # Dec 25 < plan_start, but Jan 1 >= plan_start
        for e in events:
            assert e.start_date >= date(2026, 1, 1)


class TestRecurringMonths:
    def test_basic_monthly(self):
        task = ScheduleTask(
            id="monthly_shock",
            title="Monthly Shock",
            type="recurring_months",
            frequency_months=1,
            first_due=date(2026, 1, 5),
            window=TaskWindow(before_days=7, after_days=7),
        )
        schedule = _make_schedule([task])
        events = generate_events(schedule, date(2026, 4, 30))

        # Jan, Feb, Mar, Apr = 4 events
        assert len(events) == 4

    def test_end_of_month_clamping(self):
        # Jan 31 + 1 month should become Feb 28
        result = _add_months(date(2026, 1, 31), 1)
        assert result == date(2026, 2, 28)


class TestFixedDate:
    def test_fixed_date_single_day_on_due_date(self):
        task = ScheduleTask(
            id="drain_refill_1",
            title="Drain & Refill",
            type="fixed_date",
            due_date=date(2026, 6, 5),
            window=TaskWindow(start=date(2026, 5, 22), end=date(2026, 6, 19)),
            severity="HIGH",
            procedure_ref="drain_refill",
        )
        procedures = {
            "drain_refill": {
                "title": "Drain Procedure",
                "steps": ["Step 1", "Step 2"],
            }
        }
        schedule = _make_schedule([task], procedures=procedures)
        events = generate_events(schedule, date(2026, 12, 31))

        assert len(events) == 1
        assert events[0].start_date == date(2026, 6, 5)   # due date
        assert events[0].end_date == date(2026, 6, 6)     # single-day

    def test_fixed_date_outside_horizon(self):
        task = ScheduleTask(
            id="future_task",
            title="Future",
            type="fixed_date",
            due_date=date(2027, 6, 1),
            severity="MED",
        )
        schedule = _make_schedule([task])
        events = generate_events(schedule, date(2026, 12, 31))
        assert len(events) == 0


class TestConsumableTrigger:
    def test_generates_order_event(self):
        replace_task = ScheduleTask(
            id="filter_replace_min_12_16_weeks",
            title="Replace Filters",
            type="recurring_days",
            frequency_days=98,
            first_due=date(2026, 6, 11),
            window=TaskWindow(before_days=7, after_days=7),
            severity="MED",
        )
        order_task = ScheduleTask(
            id="order_filters_when_one_left",
            title="Order Filters",
            type="trigger",
            trigger="inventory_threshold",
            threshold=ConsumableConfig(item="filters", remaining_filters_lte=1),
            severity="LOW",
        )
        consumables = {
            "filters": {
                "pack_size": 4,
                "starting_inventory_packs": 1,
            }
        }
        schedule = _make_schedule(
            [replace_task, order_task],
            consumables=consumables,
        )
        events = generate_events(schedule, date(2027, 6, 30))

        order_events = [e for e in events if e.task_id == "order_filters_when_one_left"]
        assert len(order_events) >= 1

    def test_no_order_if_enough_inventory(self):
        replace_task = ScheduleTask(
            id="filter_replace_min_12_16_weeks",
            title="Replace Filters",
            type="recurring_days",
            frequency_days=98,
            first_due=date(2026, 6, 11),
            window=TaskWindow(before_days=7, after_days=7),
            severity="MED",
        )
        order_task = ScheduleTask(
            id="order_filters_when_one_left",
            title="Order Filters",
            type="trigger",
            trigger="inventory_threshold",
            threshold=ConsumableConfig(item="filters", remaining_filters_lte=1),
            severity="LOW",
        )
        consumables = {
            "filters": {
                "pack_size": 4,
                "starting_inventory_packs": 100,  # plenty
            }
        }
        schedule = _make_schedule(
            [replace_task, order_task],
            consumables=consumables,
        )
        events = generate_events(schedule, date(2026, 12, 31))

        order_events = [e for e in events if e.task_id == "order_filters_when_one_left"]
        assert len(order_events) == 0


class TestDescriptionContent:
    def test_includes_severity_and_checklist(self):
        task = _recurring_days_task(checklist=["Step 1", "Step 2"])
        desc = _build_description(task, {}, window_before=2, window_after=2)
        assert "Severity: HIGH" in desc
        assert "Step 1" in desc
        assert "Step 2" in desc

    def test_includes_window_info(self):
        task = _recurring_days_task()
        desc = _build_description(task, {}, window_before=2, window_after=3)
        assert "2 days before" in desc
        assert "3 days after" in desc

    def test_includes_explicit_window_dates(self):
        task = _recurring_days_task()
        desc = _build_description(
            task, {},
            window_start=date(2026, 5, 22), window_end=date(2026, 6, 19),
        )
        assert "2026-05-22" in desc
        assert "2026-06-19" in desc

    def test_includes_procedure(self):
        task = _recurring_days_task(procedure_ref="my_proc")
        procedures = {"my_proc": {"title": "My Procedure", "steps": ["Do thing"]}}
        desc = _build_description(task, procedures)
        assert "My Procedure" in desc
        assert "Do thing" in desc


class TestSyncKeyFormat:
    def test_sync_key_format(self):
        task = _recurring_days_task()
        schedule = _make_schedule([task])
        events = generate_events(schedule, date(2026, 1, 15))

        assert events[0].sync_key == "weekly_test_2026-01-08"
