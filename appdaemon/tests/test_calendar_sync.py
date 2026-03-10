"""Unit tests for calendar_sync and sync_state modules."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_APPS_DIR = Path(__file__).resolve().parents[1] / "apps"
if str(_APPS_DIR) not in sys.path:
    sys.path.insert(0, str(_APPS_DIR))

from calendar_from_schedule_app.calendar_sync import sync_calendar
from calendar_from_schedule_app.event_generator import EventInstance
from calendar_from_schedule_app.sync_state import (
    SyncState,
    load_sync_state,
    save_sync_state,
)


def _make_event(task_id="task1", due="2026-01-08", **kwargs) -> EventInstance:
    defaults = {
        "task_id": task_id,
        "title": f"Task {task_id}",
        "start_date": date.fromisoformat(due),
        "end_date": date.fromisoformat(due),
        "description": "Test",
        "severity": "MED",
        "sync_key": f"{task_id}_{due}",
    }
    defaults.update(kwargs)
    return EventInstance(**defaults)


def _make_app():
    app = AsyncMock()
    app.call_service = AsyncMock()
    return app


def _make_ha_client(events=None):
    client = AsyncMock()
    client.get = AsyncMock(return_value=events or [])
    return client


class TestSyncCalendar:
    def test_empty_state_creates_all(self):
        app = _make_app()
        client = _make_ha_client()
        state = SyncState()
        desired = [_make_event("t1", "2026-01-08"), _make_event("t2", "2026-01-15")]

        new_state = asyncio.get_event_loop().run_until_complete(
            sync_calendar(app, "calendar.test", desired, state, client)
        )

        assert app.call_service.call_count == 2
        assert len(new_state.events) == 2

    def test_noop_when_unchanged(self):
        app = _make_app()
        existing_events = [
            {"uid": "uid-1", "summary": "Task t1", "start": {"date": "2026-01-08"}},
        ]
        client = _make_ha_client(existing_events)

        state = SyncState(events={"t1_2026-01-08": "uid-1"})
        desired = [_make_event("t1", "2026-01-08")]

        new_state = asyncio.get_event_loop().run_until_complete(
            sync_calendar(app, "calendar.test", desired, state, client)
        )

        # No creates or deletes
        assert app.call_service.call_count == 0

    def test_deletes_removed_events(self):
        app = _make_app()
        existing_events = [
            {"uid": "uid-old", "summary": "Old Task", "start": {"date": "2026-01-01"}},
        ]
        client = _make_ha_client(existing_events)

        state = SyncState(events={"old_task_2026-01-01": "uid-old"})
        desired = [_make_event("new_task", "2026-01-15")]

        new_state = asyncio.get_event_loop().run_until_complete(
            sync_calendar(app, "calendar.test", desired, state, client)
        )

        # 1 delete + 1 create
        assert app.call_service.call_count == 2
        assert "old_task_2026-01-01" not in new_state.events

    def test_adds_new_events(self):
        app = _make_app()
        existing_events = [
            {"uid": "uid-1", "summary": "Task t1", "start": {"date": "2026-01-08"}},
        ]
        client = _make_ha_client(existing_events)

        state = SyncState(events={"t1_2026-01-08": "uid-1"})
        desired = [
            _make_event("t1", "2026-01-08"),
            _make_event("t2", "2026-01-15"),
        ]

        new_state = asyncio.get_event_loop().run_until_complete(
            sync_calendar(app, "calendar.test", desired, state, client)
        )

        # Only 1 create (t2 is new)
        assert app.call_service.call_count == 1
        assert "t2_2026-01-15" in new_state.events

    def test_updates_changed_events(self):
        """Changed events are handled as delete + create."""
        app = _make_app()
        existing_events = [
            {"uid": "uid-old", "summary": "Old Version", "start": {"date": "2026-01-08"}},
        ]
        client = _make_ha_client(existing_events)

        # Old state has the event, but with a different sync_key (simulating a change)
        state = SyncState(events={"t1_old_2026-01-08": "uid-old"})
        desired = [_make_event("t1_new", "2026-01-08")]

        new_state = asyncio.get_event_loop().run_until_complete(
            sync_calendar(app, "calendar.test", desired, state, client)
        )

        # 1 delete (old) + 1 create (new)
        assert app.call_service.call_count == 2


class TestSyncStatePersistence:
    def test_round_trip(self, tmp_path):
        path = tmp_path / "state.json"
        state = SyncState(
            file_hash="abc123",
            horizon_end="2026-06-01",
            events={"key1": "uid1", "key2": "uid2"},
            last_sync_iso="2026-01-01T00:00:00+00:00",
        )
        save_sync_state(state, path)
        loaded = load_sync_state(path)

        assert loaded.file_hash == "abc123"
        assert loaded.horizon_end == "2026-06-01"
        assert loaded.events == {"key1": "uid1", "key2": "uid2"}
        assert loaded.last_sync_iso == "2026-01-01T00:00:00+00:00"

    def test_missing_file_returns_empty(self, tmp_path):
        state = load_sync_state(tmp_path / "nonexistent.json")
        assert state.file_hash == ""
        assert state.events == {}
