"""Unit tests for schedule_parser module."""

from __future__ import annotations

import sys
import tempfile
from datetime import date
from pathlib import Path

import pytest
import yaml

# Ensure apps dir is importable
_APPS_DIR = Path(__file__).resolve().parents[1] / "apps"
if str(_APPS_DIR) not in sys.path:
    sys.path.insert(0, str(_APPS_DIR))

from calendar_from_schedule_app.schedule_parser import (
    compute_file_hash,
    load_schedule,
)


def _write_yaml(data: dict, path: Path) -> Path:
    with open(path, "w") as f:
        yaml.dump(data, f)
    return path


def _minimal_schedule(**overrides) -> dict:
    base = {
        "schedule": {"name": "Test Schedule", "plan_start": "2026-01-01"},
        "tasks": [
            {
                "id": "test_task",
                "title": "Test Task",
                "frequency_days": 7,
                "first_due": "2026-01-08",
                "severity": "HIGH",
            }
        ],
    }
    base.update(overrides)
    return base


class TestLoadSchedule:
    def test_valid_parse(self, tmp_path):
        data = _minimal_schedule()
        path = _write_yaml(data, tmp_path / "schedule.yaml")
        schedule = load_schedule(path)

        assert schedule.name == "Test Schedule"
        assert schedule.plan_start == date(2026, 1, 1)
        assert len(schedule.tasks) == 1
        assert schedule.tasks[0].id == "test_task"
        assert schedule.tasks[0].type == "recurring_days"

    def test_missing_name_raises(self, tmp_path):
        data = {"schedule": {}, "tasks": [{"id": "x", "title": "X", "frequency_days": 1, "first_due": "2026-01-01"}]}
        path = _write_yaml(data, tmp_path / "bad.yaml")
        with pytest.raises(ValueError, match="schedule.name"):
            load_schedule(path)

    def test_missing_tasks_raises(self, tmp_path):
        data = {"schedule": {"name": "X"}, "tasks": []}
        path = _write_yaml(data, tmp_path / "empty.yaml")
        with pytest.raises(ValueError, match="at least one task"):
            load_schedule(path)

    def test_recurring_days_type(self, tmp_path):
        data = _minimal_schedule()
        path = _write_yaml(data, tmp_path / "s.yaml")
        schedule = load_schedule(path)
        assert schedule.tasks[0].type == "recurring_days"
        assert schedule.tasks[0].frequency_days == 7

    def test_recurring_months_type(self, tmp_path):
        data = _minimal_schedule(tasks=[{
            "id": "monthly",
            "title": "Monthly Task",
            "frequency_months": 1,
            "first_due": "2026-02-01",
            "severity": "MED",
        }])
        path = _write_yaml(data, tmp_path / "s.yaml")
        schedule = load_schedule(path)
        assert schedule.tasks[0].type == "recurring_months"

    def test_fixed_date_type(self, tmp_path):
        data = _minimal_schedule(tasks=[{
            "id": "fixed",
            "title": "Fixed Task",
            "due_date": "2026-06-01",
            "severity": "HIGH",
            "window": {"start": "2026-05-25", "end": "2026-06-10"},
        }])
        path = _write_yaml(data, tmp_path / "s.yaml")
        schedule = load_schedule(path)
        assert schedule.tasks[0].type == "fixed_date"
        assert schedule.tasks[0].due_date == date(2026, 6, 1)
        assert schedule.tasks[0].window.start == date(2026, 5, 25)

    def test_trigger_type(self, tmp_path):
        data = _minimal_schedule(tasks=[{
            "id": "order_filters",
            "title": "Order Filters",
            "trigger": "inventory_threshold",
            "threshold": {"item": "filters", "remaining_filters_lte": 1},
            "severity": "LOW",
        }])
        path = _write_yaml(data, tmp_path / "s.yaml")
        schedule = load_schedule(path)
        assert schedule.tasks[0].type == "trigger"
        assert schedule.tasks[0].threshold.item == "filters"


class TestFileHash:
    def test_consistent_hash(self, tmp_path):
        path = tmp_path / "test.yaml"
        path.write_text("hello world")
        h1 = compute_file_hash(path)
        h2 = compute_file_hash(path)
        assert h1 == h2
        assert len(h1) == 64  # SHA256 hex

    def test_different_content_different_hash(self, tmp_path):
        p1 = tmp_path / "a.yaml"
        p2 = tmp_path / "b.yaml"
        p1.write_text("content a")
        p2.write_text("content b")
        assert compute_file_hash(p1) != compute_file_hash(p2)
