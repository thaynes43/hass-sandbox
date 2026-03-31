"""Unit tests for SchoolLunchApp.

Mocks AppDaemon methods, HAProvisioner, and SchoolMenuClient — no real
network or HA access required.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Mock hassapi before importing the app
# ---------------------------------------------------------------------------
mock_hass = MagicMock()
mock_hass.Hass = type("_MockHass", (), {"__init__": lambda self, *a, **kw: None})
sys.modules["hassapi"] = mock_hass

_repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo_root / "apps"))
sys.path.insert(0, str(_repo_root))

from school_lunch_app.school_lunch_app import (
    SchoolLunchApp,
    SENSOR_ENTITY_ID,
    SELECTION_ENTITY_ID,
)
from providers.school_menu.types import MenuDay, MenuItem, MenuMonth


# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

def _make_menu_month(
    *,
    name: str = "Elementary",
    month: int = 2,  # 0-indexed = March → display_month = 3
    year: int = 2026,
    days: int = 3,
    prev_id: str = "prev-abc",
    next_id: str = "next-def",
) -> MenuMonth:
    """Build a minimal MenuMonth for testing."""
    menu_days = []
    for d in range(1, days + 1):
        menu_days.append(MenuDay(
            day=d,
            month=month,
            year=year,
            items=[
                MenuItem(name="Chicken Nuggets", category="Entrees", is_ancillary=False),
                MenuItem(name="Milk Choice", category="Milk", is_ancillary=True),
            ],
        ))
    return MenuMonth(
        menu_id=f"mongo-{name.lower().replace(' ', '-')}",
        menu_type_name="Lunch",
        month=month,
        year=year,
        days=menu_days,
        previous_month_id=prev_id,
        next_month_id=next_id,
    )


DEFAULT_ARGS: Dict[str, Any] = {
    "ha_url": "http://ha:8123",
    "ha_token_env": "TOKEN",
    "sid": "0802121850414637",
    "menus": [
        {"name": "Elementary", "download_id": "853700"},
        {"name": "Middle School", "download_id": "854234"},
        {"name": "High School", "download_id": "854323"},
    ],
    "default_selected": ["Elementary", "Middle School"],
}


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def _make_app(extra_args: dict | None = None) -> SchoolLunchApp:
    """Create a SchoolLunchApp with mocked AppDaemon methods."""
    ad = MagicMock()
    config = MagicMock()
    app = SchoolLunchApp(ad, config)

    args = dict(DEFAULT_ARGS)
    if extra_args:
        args.update(extra_args)
    app.args = args

    app.get_state = MagicMock(return_value=None)
    app.set_state = MagicMock()
    app.call_service = MagicMock()
    app.listen_state = MagicMock()
    app.listen_event = MagicMock()
    app.fire_event = MagicMock()
    app.run_in = MagicMock()
    app.run_daily = MagicMock()
    app.cancel_timer = MagicMock()
    app.log = MagicMock()
    app.create_task = MagicMock()

    return app


def _run(coro):
    """Run a coroutine in a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _startup(app: SchoolLunchApp, mock_prov: MagicMock, mock_client: MagicMock) -> None:
    """Initialize the app and run the async startup coroutine."""
    app.initialize()
    with patch("providers.ha_provisioner.HAProvisioner", return_value=mock_prov), \
         patch("school_lunch_app.school_lunch_app.SchoolMenuClient", return_value=mock_client):
        _run(app._async_startup())


# ---------------------------------------------------------------------------
# Mock provisioner and client factories
# ---------------------------------------------------------------------------

def _make_mock_provisioner() -> MagicMock:
    prov = MagicMock()
    prov.ensure_helper = AsyncMock(return_value=False)
    prov.ensure_script = AsyncMock(return_value=False)
    return prov


def _make_mock_client(
    resolve_results: Dict[str, Dict[str, str]] | None = None,
) -> MagicMock:
    """Build a mock SchoolMenuClient with default resolve + fetch behavior."""
    client = MagicMock()

    # Default: resolve each download_id to a predictable mongo_id
    default_resolve = {
        "853700": {"id": "mongo-elementary", "site_code": "100"},
        "854234": {"id": "mongo-middle", "site_code": "101"},
        "854323": {"id": "mongo-high", "site_code": "102"},
    }
    resolve_map = resolve_results or default_resolve

    async def _resolve(download_id: str) -> Dict[str, str]:
        result = resolve_map.get(download_id)
        if result is None:
            raise ValueError(f"No mock result for download_id={download_id}")
        return result

    client.resolve_menu_id = AsyncMock(side_effect=_resolve)

    # Default: return a minimal MenuMonth per mongo_id
    default_menus = {
        "mongo-elementary": _make_menu_month(name="Elementary"),
        "mongo-middle": _make_menu_month(name="Middle School"),
        "mongo-high": _make_menu_month(name="High School"),
    }

    async def _fetch(menu_id: str) -> MenuMonth:
        result = default_menus.get(menu_id)
        if result is None:
            raise ValueError(f"No mock menu for menu_id={menu_id}")
        return result

    client.fetch_menu = AsyncMock(side_effect=_fetch)

    # Support async context manager
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)

    return client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestInitialize:
    def test_initialize_calls_startup(self):
        """initialize() should register a run_in callback."""
        app = _make_app()
        app.initialize()
        app.run_in.assert_called_once()
        # First arg is the callback, second is the delay (0)
        callback = app.run_in.call_args[0][0]
        assert callable(callback)
        delay = app.run_in.call_args[0][1]
        assert delay == 0

    def test_initialize_stores_config(self):
        """initialize() stores sid, menus, and default_selected from args."""
        app = _make_app()
        app.initialize()
        assert app._sid == "0802121850414637"
        assert len(app._menus) == 3
        assert app._default_selected == ["Elementary", "Middle School"]


class TestProvisionEntities:
    def test_provision_entities(self):
        """Startup calls ensure_helper for input_text and ensure_script for relay."""
        app = _make_app()
        mock_prov = _make_mock_provisioner()
        mock_client = _make_mock_client()

        _startup(app, mock_prov, mock_client)

        mock_prov.ensure_helper.assert_called_once()
        call_args = mock_prov.ensure_helper.call_args
        assert call_args[0][0] == "input_text"
        assert call_args[0][1] == "School Lunch Selected Schools"
        assert call_args[1].get("max") == 255

        mock_prov.ensure_script.assert_called_once()
        script_id = mock_prov.ensure_script.call_args[0][0]
        assert script_id == "school_lunch_relay"

    def test_provision_entities_skipped_without_url(self):
        """If ha_url is missing, provisioning is skipped (no crash)."""
        app = _make_app(extra_args={"ha_url": None})
        app.initialize()
        mock_client = _make_mock_client()

        with patch("school_lunch_app.school_lunch_app.SchoolMenuClient", return_value=mock_client):
            _run(app._async_startup())

        app.log.assert_any_call(
            "ha_url / ha_token_env not configured — skipping provisioning",
            level="WARNING",
        )

    def test_relay_script_fires_school_lunch_command_event(self):
        """The relay script sequence fires the 'school_lunch_command' event."""
        app = _make_app()
        mock_prov = _make_mock_provisioner()
        mock_client = _make_mock_client()

        _startup(app, mock_prov, mock_client)

        script_config = mock_prov.ensure_script.call_args[0][1]
        assert script_config["sequence"][0]["event"] == "school_lunch_command"

    def test_provision_helper_initial_value_is_default_selected(self):
        """The input_text helper is provisioned with the default_selected JSON."""
        app = _make_app()
        mock_prov = _make_mock_provisioner()
        mock_client = _make_mock_client()

        _startup(app, mock_prov, mock_client)

        call_kwargs = mock_prov.ensure_helper.call_args[1]
        initial = call_kwargs.get("initial")
        assert initial == json.dumps(["Elementary", "Middle School"])


class TestResolveMenuIds:
    def test_resolve_menu_ids_on_startup(self):
        """Startup calls resolve_menu_id for each configured menu."""
        app = _make_app()
        mock_prov = _make_mock_provisioner()
        mock_client = _make_mock_client()

        _startup(app, mock_prov, mock_client)

        assert mock_client.resolve_menu_id.call_count == 3
        call_ids = {c[0][0] for c in mock_client.resolve_menu_id.call_args_list}
        assert call_ids == {"853700", "854234", "854323"}

    def test_resolve_stores_mongo_ids(self):
        """After startup, _resolved_menus maps school name to mongo_id."""
        app = _make_app()
        mock_prov = _make_mock_provisioner()
        mock_client = _make_mock_client()

        _startup(app, mock_prov, mock_client)

        assert "Elementary" in app._resolved_menus
        assert app._resolved_menus["Elementary"]["mongo_id"] == "mongo-elementary"
        assert app._resolved_menus["Middle School"]["mongo_id"] == "mongo-middle"
        assert app._resolved_menus["High School"]["mongo_id"] == "mongo-high"

    def test_resolve_partial_failure(self):
        """If one school fails to resolve, the others still work."""
        app = _make_app()
        mock_prov = _make_mock_provisioner()

        # "854234" (Middle School) is absent from resolve_map — will raise
        resolve_map = {
            "853700": {"id": "mongo-elementary", "site_code": "100"},
            "854323": {"id": "mongo-high", "site_code": "102"},
        }
        mock_client = _make_mock_client(resolve_results=resolve_map)
        # Provide only menus for schools that resolved
        resolved_menus = {
            "mongo-elementary": _make_menu_month(name="Elementary"),
            "mongo-high": _make_menu_month(name="High School"),
        }
        mock_client.fetch_menu = AsyncMock(
            side_effect=lambda mid: resolved_menus[mid]
        )

        _startup(app, mock_prov, mock_client)

        assert "Elementary" in app._resolved_menus
        assert "High School" in app._resolved_menus
        assert "Middle School" not in app._resolved_menus

        error_calls = [c for c in app.log.call_args_list if c[1].get("level") == "ERROR"]
        assert any("Middle School" in str(c) for c in error_calls)


class TestFetchAllMenus:
    def test_fetch_all_menus(self):
        """Fetches menu for each resolved school and builds school data list."""
        app = _make_app()
        mock_prov = _make_mock_provisioner()
        mock_client = _make_mock_client()

        _startup(app, mock_prov, mock_client)

        assert len(app._school_data) == 3
        names = {s["name"] for s in app._school_data}
        assert names == {"Elementary", "Middle School", "High School"}

    def test_fetch_partial_failure(self):
        """If one school fetch fails, the others still publish."""
        app = _make_app()
        mock_prov = _make_mock_provisioner()
        mock_client = _make_mock_client()

        async def _flaky_fetch(menu_id: str) -> MenuMonth:
            if menu_id == "mongo-middle":
                raise ValueError("Network error")
            menus = {
                "mongo-elementary": _make_menu_month(name="Elementary"),
                "mongo-high": _make_menu_month(name="High School"),
            }
            return menus[menu_id]

        mock_client.fetch_menu = AsyncMock(side_effect=_flaky_fetch)

        _startup(app, mock_prov, mock_client)

        names = {s["name"] for s in app._school_data}
        assert "Elementary" in names
        assert "High School" in names
        assert "Middle School" not in names

        warn_calls = [c for c in app.log.call_args_list if c[1].get("level") == "WARNING"]
        assert any("Middle School" in str(c) for c in warn_calls)


class TestSensorState:
    def test_sensor_state_format(self):
        """Published sensor state has correct structure."""
        app = _make_app()
        mock_prov = _make_mock_provisioner()
        mock_client = _make_mock_client()

        _startup(app, mock_prov, mock_client)

        app.set_state.assert_called()
        last_call = app.set_state.call_args
        entity_id = last_call[0][0]
        state = last_call[1]["state"]
        attrs = last_call[1]["attributes"]

        assert entity_id == SENSOR_ENTITY_ID
        assert state == "ok"
        assert "schools" in attrs
        assert "last_updated" in attrs
        assert isinstance(attrs["schools"], list)

        for school in attrs["schools"]:
            assert "name" in school
            assert "month" in school
            assert "year" in school
            assert "days" in school
            assert "prev_month_id" in school
            assert "next_month_id" in school

    def test_sensor_uses_display_month(self):
        """Month values in sensor attributes are 1-indexed (display_month)."""
        app = _make_app()
        mock_prov = _make_mock_provisioner()
        mock_client = _make_mock_client()

        # month=2 (0-indexed March) → display_month = 3
        month2_menus = {
            "mongo-elementary": _make_menu_month(name="Elementary", month=2),
            "mongo-middle": _make_menu_month(name="Middle School", month=2),
            "mongo-high": _make_menu_month(name="High School", month=2),
        }
        mock_client.fetch_menu = AsyncMock(side_effect=lambda mid: month2_menus[mid])

        _startup(app, mock_prov, mock_client)

        last_call = app.set_state.call_args
        attrs = last_call[1]["attributes"]
        for school in attrs["schools"]:
            assert school["month"] == 3, (
                f"Expected 1-indexed month=3 but got {school['month']} "
                f"for school {school['name']}"
            )

    def test_sensor_day_items_structure(self):
        """Each day's items have name and role fields."""
        app = _make_app()
        mock_prov = _make_mock_provisioner()
        mock_client = _make_mock_client()

        _startup(app, mock_prov, mock_client)

        last_call = app.set_state.call_args
        attrs = last_call[1]["attributes"]

        school = next(s for s in attrs["schools"] if s["name"] == "Elementary")
        assert len(school["days"]) > 0
        first_day = school["days"][0]
        assert "day" in first_day
        assert "month" in first_day
        assert "year" in first_day
        assert first_day["month"] == 3  # 1-indexed (0-indexed month=2 → display 3)
        assert first_day["year"] == 2026
        assert "items" in first_day
        for item in first_day["items"]:
            assert "name" in item
            assert "role" in item
            assert item["role"] in ("option", "includes")


class TestCommandHandling:
    def _setup_running_app(self) -> SchoolLunchApp:
        """Create and start an app, populating _resolved_menus and _school_data."""
        app = _make_app()
        mock_prov = _make_mock_provisioner()
        mock_client = _make_mock_client()
        _startup(app, mock_prov, mock_client)
        # Reset set_state so we can track subsequent calls cleanly
        app.set_state.reset_mock()
        app.call_service.reset_mock()
        app.log.reset_mock()
        return app

    def test_command_select_schools(self):
        """select_schools command updates the input_text entity."""
        app = self._setup_running_app()

        app._on_command(
            "school_lunch_command",
            {
                "command": "select_schools",
                "payload": json.dumps({"schools": ["Elementary", "High School"]}),
            },
            {},
        )

        app.call_service.assert_called_once_with(
            "input_text/set_value",
            entity_id=SELECTION_ENTITY_ID,
            value=json.dumps(["Elementary", "High School"]),
        )

    def test_command_select_schools_min_one(self):
        """select_schools rejects empty school list with WARNING log."""
        app = self._setup_running_app()

        app._on_command(
            "school_lunch_command",
            {
                "command": "select_schools",
                "payload": json.dumps({"schools": []}),
            },
            {},
        )

        app.call_service.assert_not_called()
        warn_calls = [c for c in app.log.call_args_list if c[1].get("level") == "WARNING"]
        assert len(warn_calls) > 0

    def test_command_fetch_month(self):
        """fetch_month command creates an async task to fetch and update sensor."""
        app = self._setup_running_app()

        app._on_command(
            "school_lunch_command",
            {
                "command": "fetch_month",
                "payload": json.dumps({
                    "school": "Elementary",
                    "menu_id": "some-mongo-id",
                }),
            },
            {},
        )

        # create_task should have been called with the coroutine
        app.create_task.assert_called_once()

    def test_command_fetch_month_updates_sensor(self):
        """_do_fetch_month fetches a specific month and updates the sensor."""
        app = self._setup_running_app()

        # 0-indexed month=3 → display_month=4
        new_month = _make_menu_month(name="Elementary", month=3, year=2026)

        # Directly replace the app's client with a mock that returns the new month
        mock_client = _make_mock_client()
        mock_client.fetch_menu = AsyncMock(return_value=new_month)
        app._client = mock_client

        _run(app._do_fetch_month("Elementary", "some-mongo-id"))

        app.set_state.assert_called()
        last_call = app.set_state.call_args
        attrs = last_call[1]["attributes"]
        elem_school = next(
            (s for s in attrs["schools"] if s["name"] == "Elementary"), None
        )
        assert elem_school is not None
        assert elem_school["month"] == 4  # display_month is 1-indexed

    def test_command_unknown(self):
        """Unknown command logs a WARNING."""
        app = self._setup_running_app()

        app._on_command(
            "school_lunch_command",
            {"command": "do_something_unknown", "payload": "{}"},
            {},
        )

        warn_calls = [c for c in app.log.call_args_list if c[1].get("level") == "WARNING"]
        assert any("do_something_unknown" in str(c) for c in warn_calls)

    def test_command_invalid_payload(self):
        """Invalid JSON payload logs a WARNING and does not crash."""
        app = self._setup_running_app()

        app._on_command(
            "school_lunch_command",
            {"command": "select_schools", "payload": "NOT JSON {{{"},
            {},
        )

        warn_calls = [c for c in app.log.call_args_list if c[1].get("level") == "WARNING"]
        assert len(warn_calls) > 0
        app.call_service.assert_not_called()


class TestDailyFetch:
    def test_daily_fetch_scheduled(self):
        """run_daily is called during startup with 5 AM time."""
        app = _make_app()
        mock_prov = _make_mock_provisioner()
        mock_client = _make_mock_client()

        _startup(app, mock_prov, mock_client)

        app.run_daily.assert_called_once()
        call_args = app.run_daily.call_args
        callback = call_args[0][0]
        schedule_time = call_args[0][1]

        assert callable(callback)
        assert isinstance(schedule_time, datetime.time)
        assert schedule_time.hour == 5
        assert schedule_time.minute == 0

    def test_daily_fetch_updates_sensor(self):
        """Daily fetch refreshes all school menus and updates the sensor."""
        app = _make_app()
        mock_prov = _make_mock_provisioner()
        mock_client = _make_mock_client()

        _startup(app, mock_prov, mock_client)
        app.set_state.reset_mock()

        with patch("school_lunch_app.school_lunch_app.SchoolMenuClient", return_value=mock_client):
            _run(app._do_daily_fetch())

        app.set_state.assert_called()
        last_call = app.set_state.call_args
        assert last_call[1]["state"] == "ok"
        assert len(last_call[1]["attributes"]["schools"]) == 3

    def test_daily_fetch_keeps_stale_data_on_failure(self):
        """When daily fetch fails for a school, stale data is preserved."""
        app = _make_app()
        mock_prov = _make_mock_provisioner()
        mock_client = _make_mock_client()

        _startup(app, mock_prov, mock_client)

        # Verify initial data is present
        assert any(s["name"] == "Middle School" for s in app._school_data)

        # Daily fetch: Middle School fails
        async def _flaky_fetch(menu_id: str) -> MenuMonth:
            if menu_id == "mongo-middle":
                raise ValueError("Network error")
            menus = {
                "mongo-elementary": _make_menu_month(name="Elementary"),
                "mongo-high": _make_menu_month(name="High School"),
            }
            return menus[menu_id]

        mock_client.fetch_menu = AsyncMock(side_effect=_flaky_fetch)

        with patch("school_lunch_app.school_lunch_app.SchoolMenuClient", return_value=mock_client):
            _run(app._do_daily_fetch())

        names = {s["name"] for s in app._school_data}
        assert "Elementary" in names
        assert "High School" in names
        assert "Middle School" in names  # stale data preserved from initial fetch

    def test_daily_fetch_sets_error_state_on_total_failure(self):
        """When all fetches fail, sensor state is set to 'error'."""
        app = _make_app()
        mock_prov = _make_mock_provisioner()
        mock_client = _make_mock_client()

        _startup(app, mock_prov, mock_client)
        app.set_state.reset_mock()

        mock_client.fetch_menu = AsyncMock(side_effect=ValueError("All gone"))

        with patch("school_lunch_app.school_lunch_app.SchoolMenuClient", return_value=mock_client):
            _run(app._do_daily_fetch())

        last_call = app.set_state.call_args
        assert last_call[1]["state"] == "error"


class TestMonthAdvance:
    """Tests for the auto-advance logic that follows nextMonthPublished."""

    def test_advance_when_menu_is_stale(self):
        """If fetched menu is behind the current month, advance via next_month_id."""
        app = _make_app()
        mock_prov = _make_mock_provisioner()

        # March menu (month=2, 0-indexed) with next_month_id pointing to April
        march_menu = _make_menu_month(name="Elementary", month=2, year=2026, next_id="april-mongo-id")
        april_menu = _make_menu_month(name="Elementary", month=3, year=2026, prev_id="march-mongo-id", next_id=None)
        april_menu.menu_id = "april-mongo-id"

        fetch_calls = []

        async def _tracking_fetch(menu_id: str) -> MenuMonth:
            fetch_calls.append(menu_id)
            if menu_id == "april-mongo-id":
                return april_menu
            # Default: return march menus for all initial IDs
            menus = {
                "mongo-elementary": march_menu,
                "mongo-middle": _make_menu_month(name="Middle School", month=3, year=2026, next_id=None),
                "mongo-high": _make_menu_month(name="High School", month=3, year=2026, next_id=None),
            }
            return menus[menu_id]

        mock_client = _make_mock_client()
        mock_client.fetch_menu = AsyncMock(side_effect=_tracking_fetch)

        _startup(app, mock_prov, mock_client)

        # Simulate April — the startup fetched March, should have advanced
        # We need to test _advance_to_current_month directly
        app._client = mock_client
        april_now = datetime.datetime(2026, 4, 15)
        result = _run(app._advance_to_current_month("Elementary", march_menu, april_now))

        assert result.display_month == 4
        assert result.menu_id == "april-mongo-id"
        assert "april-mongo-id" in fetch_calls

    def test_no_advance_when_current(self):
        """If menu already matches current month, no extra fetch happens."""
        app = _make_app()
        mock_prov = _make_mock_provisioner()
        mock_client = _make_mock_client()

        _startup(app, mock_prov, mock_client)

        april_menu = _make_menu_month(name="Elementary", month=3, year=2026)
        app._client = mock_client
        mock_client.fetch_menu.reset_mock()

        april_now = datetime.datetime(2026, 4, 15)
        result = _run(app._advance_to_current_month("Elementary", april_menu, april_now))

        assert result is april_menu
        mock_client.fetch_menu.assert_not_called()

    def test_advance_stops_when_no_next_month(self):
        """If nextMonthPublished is None, advance stops and returns what we have."""
        app = _make_app()
        mock_prov = _make_mock_provisioner()
        mock_client = _make_mock_client()

        _startup(app, mock_prov, mock_client)

        march_menu = _make_menu_month(name="Elementary", month=2, year=2026, next_id=None)
        app._client = mock_client
        mock_client.fetch_menu.reset_mock()

        april_now = datetime.datetime(2026, 4, 15)
        result = _run(app._advance_to_current_month("Elementary", march_menu, april_now))

        # Should return March since no next is available
        assert result.display_month == 3
        mock_client.fetch_menu.assert_not_called()
        # Should have logged a warning
        app.log.assert_any_call(
            "'Elementary' menu is 3/2026 but no nextMonthPublished "
            "available to advance to 4/2026",
            level="WARNING",
        )

    def test_advance_updates_resolved_mongo_id(self):
        """After advancing, _resolved_menus mongo_id is updated for next fetch."""
        app = _make_app()
        mock_prov = _make_mock_provisioner()

        march_menu = _make_menu_month(name="Elementary", month=2, year=2026, next_id="april-mongo-id")
        april_menu = _make_menu_month(name="Elementary", month=3, year=2026)
        april_menu.menu_id = "april-mongo-id"

        async def _fetch(menu_id: str) -> MenuMonth:
            if menu_id == "april-mongo-id":
                return april_menu
            menus = {
                "mongo-elementary": march_menu,
                "mongo-middle": _make_menu_month(name="Middle School", month=3, year=2026),
                "mongo-high": _make_menu_month(name="High School", month=3, year=2026),
            }
            return menus[menu_id]

        mock_client = _make_mock_client()
        mock_client.fetch_menu = AsyncMock(side_effect=_fetch)

        _startup(app, mock_prov, mock_client)

        # Now simulate a daily fetch in April
        april_now = datetime.datetime(2026, 4, 15)
        with patch("school_lunch_app.school_lunch_app.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = april_now
            mock_dt.time = datetime.time
            with patch("school_lunch_app.school_lunch_app.SchoolMenuClient", return_value=mock_client):
                _run(app._do_daily_fetch())

        # The Elementary mongo_id should have been updated
        assert app._resolved_menus["Elementary"]["mongo_id"] == "april-mongo-id"

    def test_advance_multi_month_gap(self):
        """Advance follows the chain through multiple months."""
        app = _make_app()
        mock_prov = _make_mock_provisioner()
        mock_client = _make_mock_client()

        _startup(app, mock_prov, mock_client)

        # Jan -> Feb -> March (current)
        jan_menu = _make_menu_month(name="Elementary", month=0, year=2026, next_id="feb-id")
        feb_menu = _make_menu_month(name="Elementary", month=1, year=2026, next_id="mar-id")
        mar_menu = _make_menu_month(name="Elementary", month=2, year=2026)
        mar_menu.menu_id = "mar-id"

        async def _chain_fetch(menu_id: str) -> MenuMonth:
            return {"feb-id": feb_menu, "mar-id": mar_menu}[menu_id]

        mock_client.fetch_menu = AsyncMock(side_effect=_chain_fetch)
        app._client = mock_client

        march_now = datetime.datetime(2026, 3, 15)
        result = _run(app._advance_to_current_month("Elementary", jan_menu, march_now))

        assert result.display_month == 3
        assert mock_client.fetch_menu.call_count == 2


class TestListenerRegistration:
    def test_event_listener_registered(self):
        """listen_event is called for school_lunch_command during startup."""
        app = _make_app()
        mock_prov = _make_mock_provisioner()
        mock_client = _make_mock_client()

        _startup(app, mock_prov, mock_client)

        app.listen_event.assert_called_once()
        event_name = app.listen_event.call_args[0][1]
        assert event_name == "school_lunch_command"
