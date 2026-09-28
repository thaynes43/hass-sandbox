"""Unit tests for SchoolLunchApp.

Mocks AppDaemon methods, HAProvisioner, and SchoolMenuClient — no real
network or HA access required.
"""

from __future__ import annotations

import asyncio
import calendar
import datetime
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import closing_create_task

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
    mask_sid,
    sid_from_menu_url,
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
    "menu_url_env": "SCHOOL_LUNCH",
    "menus": [
        {"name": "Elementary", "download_id": "853700"},
        {"name": "Middle School", "download_id": "854234"},
        {"name": "High School", "download_id": "854323"},
    ],
    "default_selected": ["Elementary", "Middle School"],
}


# The district site id is a secret in prod: the app reads the menu URL from
# the env var named by ``menu_url_env`` and parses ``sid`` out of it.
TEST_SID = "test-sid-1234"
TEST_MENU_URL = (
    f"https://www.schoolnutritionandfitness.com/index.php?sid={TEST_SID}&page=menus"
)


@pytest.fixture(autouse=True)
def _school_lunch_env(monkeypatch):
    monkeypatch.setenv("SCHOOL_LUNCH", TEST_MENU_URL)
    monkeypatch.delenv("SCHOOL_LUNCH_MISSING", raising=False)


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
    app.create_task = closing_create_task()

    return app


def _run(coro):
    """Run a coroutine in a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# Freeze time to March 15 2026 so _advance_to_current_month() is a no-op
# for the default test data (month=2, 0-indexed March → display_month=3).
_FROZEN_NOW = datetime.datetime(2026, 3, 15, 12, 0, 0)


def _startup(
    app: SchoolLunchApp,
    mock_prov: MagicMock,
    mock_client: MagicMock,
    now: datetime.datetime = _FROZEN_NOW,
) -> None:
    """Initialize the app and run the async startup coroutine."""
    app.initialize()
    with patch("providers.ha_provisioner.HAProvisioner", return_value=mock_prov), \
         patch("school_lunch_app.school_lunch_app.SchoolMenuClient", return_value=mock_client), \
         patch("school_lunch_app.school_lunch_app.datetime") as mock_dt:
        mock_dt.datetime.now.return_value = now
        mock_dt.time = datetime.time
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
        assert app._sid == TEST_SID
        assert len(app._menus) == 3
        assert app._default_selected == ["Elementary", "Middle School"]


class TestSidResolution:
    """The district site id comes from the menu URL env var, never the repo."""

    @pytest.mark.parametrize(
        "url, expected",
        [
            (TEST_MENU_URL, TEST_SID),
            # sid not the first parameter, and surrounding whitespace
            ("  https://x.example/index.php?page=menus&sid=99&x=1  ", "99"),
            # no sid parameter at all
            ("https://x.example/index.php?page=menus", ""),
            # empty sid value
            ("https://x.example/index.php?sid=&page=menus", ""),
            ("", ""),
        ],
    )
    def test_sid_from_menu_url(self, url, expected):
        assert sid_from_menu_url(url) == expected

    def test_mask_sid(self):
        assert mask_sid("0123456789") == "****6789"
        assert mask_sid("") == "<unset>"

    def test_sid_parsed_from_env_menu_url(self):
        app = _make_app()
        app.initialize()
        assert app._sid == TEST_SID

    def test_initialize_log_masks_sid(self):
        """The startup log line must not leak the full site id (S6)."""
        app = _make_app()
        app.initialize()
        logged = " ".join(str(c.args[0]) for c in app.log.call_args_list)
        assert TEST_SID not in logged
        assert "****1234" in logged

    def test_direct_sid_fallback(self):
        """A plain ``sid`` arg still works when no menu_url is configured."""
        args = dict(DEFAULT_ARGS)
        del args["menu_url_env"]
        args["sid"] = "direct-sid"
        app = _make_app()
        app.args = args
        app.initialize()
        assert app._sid == "direct-sid"

    def test_menu_url_wins_over_direct_sid(self):
        app = _make_app({"sid": "ignored-sid"})
        app.initialize()
        assert app._sid == TEST_SID

    def test_missing_env_var_logs_error(self):
        app = _make_app({"menu_url_env": "SCHOOL_LUNCH_MISSING"})
        app.initialize()
        assert app._sid == ""
        errors = [c for c in app.log.call_args_list if c.kwargs.get("level") == "ERROR"]
        assert errors and "SCHOOL_LUNCH_MISSING" in str(errors[0].args[0])

    def test_menu_url_without_sid_logs_error(self, monkeypatch):
        monkeypatch.setenv("SCHOOL_LUNCH", "https://x.example/index.php?page=menus")
        app = _make_app()
        app.initialize()
        assert app._sid == ""
        errors = [c for c in app.log.call_args_list if c.kwargs.get("level") == "ERROR"]
        assert errors and "sid" in str(errors[0].args[0])

    def test_startup_skips_fetch_without_sid(self):
        """Without a site id, startup provisions entities but never hits the API."""
        app = _make_app({"menu_url_env": "SCHOOL_LUNCH_MISSING"})
        mock_prov = _make_mock_provisioner()
        mock_client = _make_mock_client()
        _startup(app, mock_prov, mock_client)
        mock_prov.ensure_script.assert_called_once()
        mock_client.resolve_menu_id.assert_not_called()
        app.listen_event.assert_not_called()
        app.set_state.assert_not_called()


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

        with patch("school_lunch_app.school_lunch_app.SchoolMenuClient", return_value=mock_client), \
             patch("school_lunch_app.school_lunch_app.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _FROZEN_NOW
            mock_dt.time = datetime.time
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

        with patch("school_lunch_app.school_lunch_app.SchoolMenuClient", return_value=mock_client), \
             patch("school_lunch_app.school_lunch_app.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _FROZEN_NOW
            mock_dt.time = datetime.time
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

        with patch("school_lunch_app.school_lunch_app.SchoolMenuClient", return_value=mock_client), \
             patch("school_lunch_app.school_lunch_app.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _FROZEN_NOW
            mock_dt.time = datetime.time
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


# ---------------------------------------------------------------------------
# fetch_month keeps the refresh window (today's/tomorrow's lunch)
# ---------------------------------------------------------------------------

# A Tuesday in October: the daily refresh window is October + November.
_OCT_NOW = datetime.datetime(2026, 10, 6, 9, 0, 0)
_ONE_SCHOOL = {"menus": [{"name": "Elementary", "download_id": "853700"}]}


def _month_id(year: int, month: int) -> str:
    return f"id-{year}-{month:02d}"


def _neighbour(year: int, month: int, step: int) -> tuple:
    index = year * 12 + (month - 1) + step
    return index // 12, index % 12 + 1


def _weekday_menu(year: int, month: int, *, tag: str = "") -> MenuMonth:
    """One month of weekday lunches; month is 1-indexed.

    Each day's entree names its own date (made-up items only) so a test can
    tell which fetch a published day came from; ``tag`` marks a re-fetch.
    """
    days = [
        MenuDay(
            day=d,
            month=month - 1,  # the API's months are 0-indexed
            year=year,
            items=[
                MenuItem(name=f"Mock Entree {month}/{d}{tag}", category="Entrees"),
                MenuItem(name="Mock Milk", category="Milk", is_ancillary=True),
            ],
        )
        for d in range(1, calendar.monthrange(year, month)[1] + 1)
        if datetime.date(year, month, d).weekday() < 5
    ]
    prev_year, prev_month = _neighbour(year, month, -1)
    next_year, next_month = _neighbour(year, month, 1)
    return MenuMonth(
        menu_id=_month_id(year, month),
        menu_type_name="Lunch",
        month=month - 1,
        year=year,
        days=days,
        previous_month_id=_month_id(prev_year, prev_month),
        next_month_id=_month_id(next_year, next_month),
    )


def _month_client(
    now: datetime.datetime,
    *,
    last_published: tuple | None = None,
    resolve_results: Dict[str, Dict[str, str]] | None = None,
) -> MagicMock:
    """A client whose download id resolves to a month; any month's id fetches.

    The download id resolves to ``now``'s month, or to ``last_published``
    (year, month) when the district has not published anything newer yet —
    that month then has no ``nextMonthPublished``, as in summer.
    """
    year, month = last_published or (now.year, now.month)
    client = _make_mock_client(resolve_results=resolve_results or {
        "853700": {"id": _month_id(year, month), "site_code": "100"},
    })

    async def _fetch(menu_id: str) -> MenuMonth:
        _, y, m = menu_id.split("-")
        menu = _weekday_menu(int(y), int(m))
        if last_published and (int(y), int(m)) == last_published:
            menu.next_month_id = None
        return menu

    client.fetch_menu = AsyncMock(side_effect=_fetch)
    return client


def _published_school(app: SchoolLunchApp, name: str = "Elementary") -> Dict[str, Any]:
    attrs = app.set_state.call_args[1]["attributes"]
    return next(s for s in attrs["schools"] if s["name"] == name)


def _find_day(school: Dict[str, Any], year: int, month: int, day: int):
    """Look a day up the way both cards and the voice script do: by its own fields."""
    return next(
        (
            d for d in school["days"]
            if d["day"] == day and d["month"] == month and d["year"] == year
        ),
        None,
    )


def _entree(day: Dict[str, Any]) -> str:
    return next(i["name"] for i in day["items"] if i["role"] == "option")


def _day_keys(school: Dict[str, Any]) -> List[tuple]:
    return [(d["year"], d["month"], d["day"]) for d in school["days"]]


def _months_held(school: Dict[str, Any]) -> set:
    return {(d["year"], d["month"]) for d in school["days"]}


class TestFetchMonthKeepsRefreshWindow:
    """Browsing months in the detail card must not evict today's lunch."""

    def _running_app(
        self,
        now: datetime.datetime = _OCT_NOW,
        last_published: tuple | None = None,
    ):
        app = _make_app(_ONE_SCHOOL)
        client = _month_client(now, last_published=last_published)
        _startup(app, _make_mock_provisioner(), client, now=now)
        return app, client

    def _browse(
        self,
        app: SchoolLunchApp,
        menu_id: str,
        now: datetime.datetime = _OCT_NOW,
        school: str = "Elementary",
    ) -> None:
        with patch("school_lunch_app.school_lunch_app.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = now
            _run(app._do_fetch_month(school, menu_id))

    def test_refresh_holds_current_and_next_month(self):
        """Baseline the fix relies on: the refresh publishes October + November."""
        app, _ = self._running_app()
        school = _published_school(app)
        assert (school["month"], school["year"]) == (10, 2026)
        assert _months_held(school) == {(2026, 10), (2026, 11)}
        assert app._refresh_windows == {"Elementary": {(2026, 10), (2026, 11)}}

    def test_browse_next_month_keeps_todays_lunch(self):
        app, _ = self._running_app()
        self._browse(app, _month_id(2026, 11))
        school = _published_school(app)

        # The calendar tab shows and navigates the browsed month.
        assert (school["month"], school["year"]) == (11, 2026)
        assert school["prev_month_id"] == _month_id(2026, 10)
        assert school["next_month_id"] == _month_id(2026, 12)

        # Today's and tomorrow's lunch are still on the sensor.
        today = _find_day(school, 2026, 10, 6)
        assert today is not None
        assert _entree(today) == "Mock Entree 10/6"
        assert _find_day(school, 2026, 10, 7) is not None
        assert _find_day(school, 2026, 11, 2) is not None
        assert _months_held(school) == {(2026, 10), (2026, 11)}
        assert len(_day_keys(school)) == len(set(_day_keys(school)))

    def test_browse_past_month_keeps_window_and_points_calendar_at_it(self):
        app, _ = self._running_app()
        self._browse(app, _month_id(2026, 9))
        school = _published_school(app)

        assert (school["month"], school["year"]) == (9, 2026)
        assert school["prev_month_id"] == _month_id(2026, 8)
        assert school["next_month_id"] == _month_id(2026, 10)

        assert _find_day(school, 2026, 9, 15) is not None
        assert _entree(_find_day(school, 2026, 10, 6)) == "Mock Entree 10/6"
        assert _find_day(school, 2026, 11, 30) is not None
        assert _months_held(school) == {(2026, 9), (2026, 10), (2026, 11)}
        # Merged days come out in date order.
        assert _day_keys(school) == sorted(_day_keys(school))

    def test_browse_same_month_twice_does_not_duplicate_days(self):
        app, _ = self._running_app()
        self._browse(app, _month_id(2026, 9))
        count_after_first = len(_published_school(app)["days"])
        self._browse(app, _month_id(2026, 9))
        school = _published_school(app)

        assert len(school["days"]) == count_after_first
        assert len(_day_keys(school)) == len(set(_day_keys(school)))

    def test_browsing_a_window_month_replaces_its_days(self):
        """A re-fetched window month replaces that month's days, never doubles them."""
        app, client = self._running_app()
        client.fetch_menu = AsyncMock(
            side_effect=lambda menu_id: _weekday_menu(2026, 11, tag=" (revised)"),
        )
        self._browse(app, _month_id(2026, 11))
        school = _published_school(app)

        november = [d for d in school["days"] if d["month"] == 11]
        assert november and all(_entree(d).endswith("(revised)") for d in november)
        assert len(_day_keys(school)) == len(set(_day_keys(school)))
        assert _entree(_find_day(school, 2026, 10, 6)) == "Mock Entree 10/6"

    def test_day_dropped_from_a_refetched_month_does_not_linger(self):
        """The browsed fetch is the whole truth for its month."""
        app, client = self._running_app()
        revised = _weekday_menu(2026, 11)
        revised.days = [d for d in revised.days if d.day != 2]
        client.fetch_menu = AsyncMock(return_value=revised)

        self._browse(app, _month_id(2026, 11))
        school = _published_school(app)

        assert _find_day(school, 2026, 11, 2) is None
        assert _find_day(school, 2026, 11, 3) is not None

    def test_browsed_month_carrying_a_neighbour_day_is_not_doubled(self):
        """A browsed menu that lists a day of a window month keeps one copy of it."""
        app, client = self._running_app()
        september = _weekday_menu(2026, 9)
        september.days.append(MenuDay(
            day=1, month=9, year=2026,  # 0-indexed month 9 = October 1st
            items=[MenuItem(name="Mock Spillover Entree", category="Entrees")],
        ))
        client.fetch_menu = AsyncMock(return_value=september)

        self._browse(app, _month_id(2026, 9))
        school = _published_school(app)

        assert _day_keys(school).count((2026, 10, 1)) == 1
        assert len(_day_keys(school)) == len(set(_day_keys(school)))

    def test_browsing_many_months_never_exceeds_window_plus_one(self):
        app, _ = self._running_app()
        window = {(2026, 10), (2026, 11)}
        for year, month in [
            (2026, 9), (2026, 8), (2026, 7), (2026, 8), (2026, 9),
            (2026, 10), (2026, 11), (2026, 12), (2027, 1), (2026, 12),
        ]:
            self._browse(app, _month_id(year, month))
            school = _published_school(app)
            assert (school["month"], school["year"]) == (month, year)
            assert _months_held(school) == window | {(year, month)}
            assert len(_day_keys(school)) == len(set(_day_keys(school)))
            assert _find_day(school, 2026, 10, 6) is not None
        # Browsing never moves the recorded window; only a refresh does.
        assert app._refresh_windows["Elementary"] == window

    def test_summer_browse_keeps_the_last_published_months(self):
        """Refresh stuck behind the clock (no nextMonthPublished): a browse keeps its days."""
        july_now = datetime.datetime(2026, 7, 15, 9, 0, 0)
        app, _ = self._running_app(now=july_now, last_published=(2026, 6))
        school = _published_school(app)
        assert _months_held(school) == {(2026, 6)}
        assert app._refresh_windows["Elementary"] == {(2026, 6)}

        self._browse(app, _month_id(2026, 5), now=july_now)
        school = _published_school(app)

        assert (school["month"], school["year"]) == (5, 2026)
        assert school["next_month_id"] == _month_id(2026, 6)
        assert _months_held(school) == {(2026, 5), (2026, 6)}
        assert _entree(_find_day(school, 2026, 6, 15)) == "Mock Entree 6/15"

    def test_summer_browsing_many_months_stays_bounded(self):
        july_now = datetime.datetime(2026, 7, 15, 9, 0, 0)
        app, _ = self._running_app(now=july_now, last_published=(2026, 6))
        for year, month in [(2026, 5), (2026, 4), (2026, 5), (2026, 6), (2026, 3)]:
            self._browse(app, _month_id(year, month), now=july_now)
            school = _published_school(app)
            assert _months_held(school) == {(2026, 6), (year, month)}
            assert len(_day_keys(school)) == len(set(_day_keys(school)))

    def test_no_recorded_window_falls_back_to_the_calendar(self):
        """A school only ever added by a browse keeps the current + next calendar month."""
        app, _ = self._running_app()
        self._browse(app, _month_id(2026, 10), school="Visiting School")
        assert "Visiting School" not in app._refresh_windows

        for year, month, held in [
            (2026, 11, {(2026, 10), (2026, 11)}),
            (2026, 9, {(2026, 9), (2026, 10), (2026, 11)}),
            (2026, 8, {(2026, 8), (2026, 10), (2026, 11)}),
        ]:
            self._browse(app, _month_id(year, month), school="Visiting School")
            visiting = _published_school(app, "Visiting School")
            assert (visiting["month"], visiting["year"]) == (month, year)
            assert _months_held(visiting) == held
            assert _find_day(visiting, 2026, 10, 6) is not None

    def test_calendar_fallback_wraps_into_january(self):
        dec_now = datetime.datetime(2026, 12, 8, 9, 0, 0)
        app, _ = self._running_app(now=dec_now)
        self._browse(app, _month_id(2027, 1), now=dec_now, school="Visiting School")
        self._browse(app, _month_id(2026, 11), now=dec_now, school="Visiting School")

        visiting = _published_school(app, "Visiting School")
        assert _months_held(visiting) == {(2026, 11), (2027, 1)}
        assert _find_day(visiting, 2027, 1, 4) is not None

    def test_refreshed_month_without_days_stays_in_the_window(self):
        """The refresh's own month counts even if it published no days yet."""
        app = _make_app(_ONE_SCHOOL)
        client = _month_client(_OCT_NOW)
        full_fetch = client.fetch_menu.side_effect

        async def _october_empty(menu_id: str) -> MenuMonth:
            menu = await full_fetch(menu_id)
            if menu_id == _month_id(2026, 10):
                menu.days = []
            return menu

        client.fetch_menu = AsyncMock(side_effect=_october_empty)
        _startup(app, _make_mock_provisioner(), client, now=_OCT_NOW)
        assert _months_held(_published_school(app)) == {(2026, 11)}
        assert app._refresh_windows["Elementary"] == {(2026, 10), (2026, 11)}

        # October's lunches appear later; browsing it and then away keeps them.
        client.fetch_menu = AsyncMock(side_effect=full_fetch)
        self._browse(app, _month_id(2026, 10))
        self._browse(app, _month_id(2026, 9))

        school = _published_school(app)
        assert _months_held(school) == {(2026, 9), (2026, 10), (2026, 11)}
        assert _find_day(school, 2026, 10, 6) is not None

    def test_failed_refresh_keeps_the_schools_recorded_window(self):
        """Stale entry kept by _merge_school_data keeps the window it was published with."""
        two_schools = {"menus": [
            {"name": "Elementary", "download_id": "853700"},
            {"name": "Middle School", "download_id": "854234"},
        ]}
        june_now = datetime.datetime(2026, 6, 15, 9, 0, 0)
        july_now = datetime.datetime(2026, 7, 10, 9, 0, 0)
        app = _make_app(two_schools)
        client = _month_client(june_now, resolve_results={
            "853700": {"id": _month_id(2026, 6), "site_code": "100"},
            "854234": {"id": "mid-2026-06", "site_code": "101"},
        })
        _startup(app, _make_mock_provisioner(), client, now=june_now)
        assert app._refresh_windows == {
            "Elementary": {(2026, 6), (2026, 7)},
            "Middle School": {(2026, 6), (2026, 7)},
        }

        # July refresh: Elementary advances, Middle School's fetch fails.
        healthy_fetch = client.fetch_menu.side_effect

        async def _middle_down(menu_id: str) -> MenuMonth:
            if menu_id.startswith("mid-"):
                raise ValueError("network error")
            return await healthy_fetch(menu_id)

        client.fetch_menu = AsyncMock(side_effect=_middle_down)
        with patch("school_lunch_app.school_lunch_app.SchoolMenuClient", return_value=client), \
             patch("school_lunch_app.school_lunch_app.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = july_now
            mock_dt.time = datetime.time
            _run(app._do_daily_fetch())

        assert app._refresh_windows == {
            "Elementary": {(2026, 7), (2026, 8)},
            "Middle School": {(2026, 6), (2026, 7)},
        }
        assert _months_held(_published_school(app, "Middle School")) == {(2026, 6), (2026, 7)}

        # Browsing the stale school keeps the months it still publishes.
        client.fetch_menu = AsyncMock(side_effect=healthy_fetch)
        self._browse(app, _month_id(2026, 5), now=july_now, school="Middle School")
        middle = _published_school(app, "Middle School")
        assert _months_held(middle) == {(2026, 5), (2026, 6), (2026, 7)}
        assert _find_day(middle, 2026, 6, 15) is not None

    def test_window_wraps_into_january_in_december(self):
        """In December the window is December + January of the next year."""
        dec_now = datetime.datetime(2026, 12, 8, 9, 0, 0)
        app, _ = self._running_app(now=dec_now)
        assert _months_held(_published_school(app)) == {(2026, 12), (2027, 1)}

        self._browse(app, _month_id(2026, 11), now=dec_now)
        school = _published_school(app)

        assert (school["month"], school["year"]) == (11, 2026)
        assert _months_held(school) == {(2026, 11), (2026, 12), (2027, 1)}
        assert _find_day(school, 2027, 1, 4) is not None

    def test_browse_school_not_loaded_yet_is_appended(self):
        """A school the refresh never loaded gets the browsed month as-is."""
        app, _ = self._running_app()
        self._browse(app, _month_id(2026, 9), school="Visiting School")

        attrs = app.set_state.call_args[1]["attributes"]
        assert [s["name"] for s in attrs["schools"]] == ["Elementary", "Visiting School"]
        visiting = attrs["schools"][1]
        assert (visiting["month"], visiting["year"]) == (9, 2026)
        assert _months_held(visiting) == {(2026, 9)}
        # The loaded school is untouched.
        assert _months_held(attrs["schools"][0]) == {(2026, 10), (2026, 11)}

    def test_daily_refresh_replaces_browsed_data(self):
        app, client = self._running_app()
        self._browse(app, _month_id(2026, 8))
        assert (2026, 8) in _months_held(_published_school(app))

        with patch("school_lunch_app.school_lunch_app.SchoolMenuClient", return_value=client), \
             patch("school_lunch_app.school_lunch_app.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _OCT_NOW
            mock_dt.time = datetime.time
            _run(app._do_daily_fetch())

        school = _published_school(app)
        assert (school["month"], school["year"]) == (10, 2026)
        assert school["prev_month_id"] == _month_id(2026, 9)
        assert _months_held(school) == {(2026, 10), (2026, 11)}

    def test_failed_browse_leaves_sensor_alone(self):
        app, client = self._running_app()
        app.set_state.reset_mock()
        client.fetch_menu = AsyncMock(side_effect=ValueError("network down"))

        self._browse(app, _month_id(2026, 9))

        app.set_state.assert_not_called()
        assert _months_held(app._school_data[0]) == {(2026, 10), (2026, 11)}


# ---------------------------------------------------------------------------
# Client use is serialised (the client shares one session)
# ---------------------------------------------------------------------------

class _SessionFakeClient:
    """A stand-in with SchoolMenuClient's session lifecycle, so overlap is real.

    ``__aenter__`` opens a session only when none is open (a second, nested
    entry reuses it); ``__aexit__`` closes and nulls it, as
    ``SchoolMenuClient.close`` does. A call with no open session, or whose
    session was closed while it was in flight, raises. ``gate(menu_id)`` makes
    that month's fetch wait on an Event so a test can hold it mid-request.
    """

    def __init__(self, resolve_to: str) -> None:
        self._session: object | None = None
        self._resolve_to = resolve_to
        self._gates: Dict[str, asyncio.Event] = {}
        self.waiting: set = set()

    def gate(self, menu_id: str) -> asyncio.Event:
        self._gates[menu_id] = asyncio.Event()
        return self._gates[menu_id]

    async def __aenter__(self) -> "_SessionFakeClient":
        if self._session is None:
            self._session = object()
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._session = None

    def _open_session(self) -> object:
        if self._session is None:
            raise RuntimeError("SchoolMenuClient has no active session")
        return self._session

    async def resolve_menu_id(self, download_id: str) -> Dict[str, str]:
        self._open_session()
        await asyncio.sleep(0)
        return {"id": self._resolve_to, "site_code": "100"}

    async def fetch_menu(self, menu_id: str) -> MenuMonth:
        session = self._open_session()
        gate = self._gates.get(menu_id)
        if gate is not None:
            self.waiting.add(menu_id)
            await gate.wait()
            self.waiting.discard(menu_id)
        else:
            await asyncio.sleep(0)
        if self._session is not session:
            raise RuntimeError("Session is closed")
        _, year, month = menu_id.split("-")
        return _weekday_menu(int(year), int(month))


async def _until(condition) -> None:
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


async def _let_others_run() -> None:
    for _ in range(50):
        await asyncio.sleep(0)


class TestClientSerialisation:
    """Two quick month taps, or a tap during the refresh, must both land."""

    def _fake_app(self):
        app = _make_app(_ONE_SCHOOL)
        client = _SessionFakeClient(resolve_to=_month_id(2026, 10))
        _startup(app, _make_mock_provisioner(), client, now=_OCT_NOW)
        assert _months_held(_published_school(app)) == {(2026, 10), (2026, 11)}
        app.set_state.reset_mock()
        app.log.reset_mock()
        return app, client

    @staticmethod
    def _published_months(app: SchoolLunchApp) -> List[tuple]:
        """(state, month, year) of Elementary for every publish, in order."""
        out = []
        for c in app.set_state.call_args_list:
            school = next(
                s for s in c.kwargs["attributes"]["schools"] if s["name"] == "Elementary"
            )
            out.append((c.kwargs["state"], school["month"], school["year"]))
        return out

    @staticmethod
    def _problems(app: SchoolLunchApp) -> List[str]:
        return [
            str(c.args[0]) for c in app.log.call_args_list
            if c.kwargs.get("level") in ("WARNING", "ERROR")
        ]

    def test_two_overlapping_browses_both_apply(self):
        app, client = self._fake_app()
        november, december = _month_id(2026, 11), _month_id(2026, 12)

        async def scenario():
            gate = client.gate(november)
            first = asyncio.ensure_future(app._do_fetch_month("Elementary", november))
            await _until(lambda: november in client.waiting)
            second = asyncio.ensure_future(app._do_fetch_month("Elementary", december))
            await _let_others_run()
            gate.set()
            await asyncio.gather(first, second)

        _run(scenario())

        assert self._problems(app) == []
        assert self._published_months(app) == [("ok", 11, 2026), ("ok", 12, 2026)]
        school = _published_school(app)
        assert school["next_month_id"] == _month_id(2027, 1)
        assert _months_held(school) == {(2026, 10), (2026, 11), (2026, 12)}
        assert _find_day(school, 2026, 10, 6) is not None

    def test_browse_during_refresh_runs_after_it(self):
        app, client = self._fake_app()
        october, september = _month_id(2026, 10), _month_id(2026, 9)

        async def scenario():
            gate = client.gate(october)  # the refresh's current-month fetch
            refresh = asyncio.ensure_future(app._do_daily_fetch())
            await _until(lambda: october in client.waiting)
            browse = asyncio.ensure_future(app._do_fetch_month("Elementary", september))
            await _let_others_run()
            gate.set()
            await asyncio.gather(refresh, browse)

        with patch("school_lunch_app.school_lunch_app.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _OCT_NOW
            mock_dt.time = datetime.time
            _run(scenario())

        assert self._problems(app) == []
        assert self._published_months(app) == [("ok", 10, 2026), ("ok", 9, 2026)]
        school = _published_school(app)
        assert _months_held(school) == {(2026, 9), (2026, 10), (2026, 11)}
        assert _find_day(school, 2026, 10, 6) is not None

    def test_refresh_during_browse_runs_after_it(self):
        app, client = self._fake_app()
        november = _month_id(2026, 11)

        async def scenario():
            gate = client.gate(november)
            browse = asyncio.ensure_future(app._do_fetch_month("Elementary", november))
            await _until(lambda: november in client.waiting)
            refresh = asyncio.ensure_future(app._do_daily_fetch())
            await _let_others_run()
            gate.set()  # stays set, so the refresh's own November pre-fetch passes too
            await asyncio.gather(browse, refresh)

        with patch("school_lunch_app.school_lunch_app.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = _OCT_NOW
            mock_dt.time = datetime.time
            _run(scenario())

        assert self._problems(app) == []
        assert self._published_months(app) == [("ok", 11, 2026), ("ok", 10, 2026)]
        assert _months_held(_published_school(app)) == {(2026, 10), (2026, 11)}
