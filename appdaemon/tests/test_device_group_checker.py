"""Unit tests for DeviceGroupChecker.

Mocks AppDaemon methods and check_utils — no real HA access required.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict
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

from health_checks.checker_apps.device_group_checker.device_group_checker import (
    DeviceGroupChecker,
)


# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

SAMPLE_DEVICES = [
    {
        "name": "Movie Room",
        "ip": "192.168.50.102",
        "entities": [
            {
                "entity_id": "binary_sensor.movie_room_breeze_status",
                "healthy_state": "on",
                "name": "Status",
            }
        ],
    },
    {
        "name": "Rumpus Room",
        "ip": "192.168.50.192",
        "entities": [
            {
                "entity_id": "binary_sensor.rumpus_room_breeze_status",
                "healthy_state": "on",
                "name": "Status",
            }
        ],
    },
]

DEFAULT_ARGS: Dict[str, Any] = {
    "checker_id": "cielo",
    "checker_name": "Cielo Home",
    "check_interval_s": 180,
    "devices": SAMPLE_DEVICES,
}


def _make_app(extra_args: dict | None = None) -> DeviceGroupChecker:
    ad = MagicMock()
    config = MagicMock()
    app = DeviceGroupChecker(ad, config)

    args = dict(DEFAULT_ARGS)
    if extra_args:
        args.update(extra_args)
    app.args = args

    app.get_state = MagicMock(return_value=None)
    app.set_state = MagicMock()
    app.call_service = MagicMock()
    app.listen_event = MagicMock()
    app.fire_event = MagicMock()
    app.run_in = MagicMock()
    app.run_every = MagicMock()
    app.log = MagicMock()
    app.create_task = MagicMock()

    return app


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _startup(app: DeviceGroupChecker) -> None:
    app.initialize()
    _run(app._async_startup())


def _init_only(app: DeviceGroupChecker) -> None:
    app.initialize()


# ---------------------------------------------------------------------------
# Tests — Lifecycle
# ---------------------------------------------------------------------------

class TestLifecycle:
    def test_initialize_calls_run_in(self):
        app = _make_app()
        app.initialize()
        app.run_in.assert_called_once()

    def test_registers_correct_check_names(self):
        app = _make_app()
        _startup(app)
        register_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "register_checker"
        ]
        assert len(register_calls) == 1
        payload = json.loads(register_calls[0][1]["payload"])
        names = payload["check_names"]
        # device_name + entity name + ping per device
        assert "Movie Room Status" in names
        assert "Movie Room Ping" in names
        assert "Rumpus Room Status" in names
        assert "Rumpus Room Ping" in names
        assert len(names) == 4

    def test_registers_without_ping_when_ip_omitted(self):
        devices_no_ip = [
            {
                "name": "Movie Room",
                "entities": [
                    {
                        "entity_id": "binary_sensor.movie_room_breeze_status",
                        "healthy_state": "on",
                        "name": "Status",
                    }
                ],
            }
        ]
        app = _make_app({"devices": devices_no_ip})
        _startup(app)
        register_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "register_checker"
        ]
        payload = json.loads(register_calls[0][1]["payload"])
        assert "Movie Room Ping" not in payload["check_names"]
        assert "Movie Room Status" in payload["check_names"]
        assert len(payload["check_names"]) == 1

    def test_no_repair_state_in_registration(self):
        app = _make_app()
        _startup(app)
        register_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "register_checker"
        ]
        payload = json.loads(register_calls[0][1]["payload"])
        assert "supports_repair" not in payload
        assert "repair_state" not in payload

    def test_registers_event_listeners(self):
        app = _make_app()
        _startup(app)
        event_names = [c[0][1] for c in app.listen_event.call_args_list]
        assert "health_check_controller_ready" in event_names
        assert "health_check_recheck" in event_names

    def test_multiple_entities_per_device(self):
        devices_multi = [
            {
                "name": "Movie Room",
                "ip": "192.168.50.102",
                "entities": [
                    {
                        "entity_id": "binary_sensor.movie_room_breeze_status",
                        "healthy_state": "on",
                        "name": "Status",
                    },
                    {
                        "entity_id": "sensor.movie_room_temperature",
                        "name": "Temperature",
                    },
                ],
            }
        ]
        app = _make_app({"devices": devices_multi})
        _startup(app)
        register_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "register_checker"
        ]
        payload = json.loads(register_calls[0][1]["payload"])
        names = payload["check_names"]
        assert "Movie Room Status" in names
        assert "Movie Room Temperature" in names
        assert "Movie Room Ping" in names
        assert len(names) == 3


# ---------------------------------------------------------------------------
# Tests — Entity checks
# ---------------------------------------------------------------------------

class TestEntityChecks:
    def test_entity_ok_specific_state(self):
        app = _make_app()
        _init_only(app)
        app.get_state = AsyncMock(return_value="on")
        entity_conf = app._devices[0]["entities"][0]
        result = _run(app._check_entity_state("Movie Room Status", entity_conf))
        assert result["status"] == "ok"
        assert result["name"] == "Movie Room Status"

    def test_entity_wrong_state(self):
        app = _make_app()
        _init_only(app)
        app.get_state = AsyncMock(return_value="off")
        entity_conf = app._devices[0]["entities"][0]
        result = _run(app._check_entity_state("Movie Room Status", entity_conf))
        assert result["status"] == "critical"
        assert "Expected 'on'" in result["detail"]

    def test_entity_unavailable(self):
        app = _make_app()
        _init_only(app)
        app.get_state = AsyncMock(return_value="unavailable")
        entity_conf = app._devices[0]["entities"][0]
        result = _run(app._check_entity_state("Movie Room Status", entity_conf))
        assert result["status"] == "critical"
        assert "unavailable" in result["detail"]

    def test_entity_unknown(self):
        app = _make_app()
        _init_only(app)
        app.get_state = AsyncMock(return_value="unknown")
        entity_conf = app._devices[0]["entities"][0]
        result = _run(app._check_entity_state("Movie Room Status", entity_conf))
        assert result["status"] == "critical"

    def test_entity_not_found(self):
        app = _make_app()
        _init_only(app)
        app.get_state = AsyncMock(return_value=None)
        entity_conf = app._devices[0]["entities"][0]
        result = _run(app._check_entity_state("Movie Room Status", entity_conf))
        assert result["status"] == "critical"

    def test_entity_no_healthy_state_any_non_unavailable_is_ok(self):
        """When healthy_state is omitted, any state except unavailable/unknown is ok."""
        devices = [
            {
                "name": "Sensor",
                "entities": [
                    {
                        "entity_id": "sensor.some_sensor",
                        "name": "Reading",
                        # no healthy_state
                    }
                ],
            }
        ]
        app = _make_app({"devices": devices})
        _init_only(app)
        app.get_state = AsyncMock(return_value="42.5")
        entity_conf = app._devices[0]["entities"][0]
        assert entity_conf["healthy_state"] == ""
        result = _run(app._check_entity_state("Sensor Reading", entity_conf))
        assert result["status"] == "ok"

    def test_yaml_bool_coercion_true_becomes_on(self):
        """YAML coerces 'on' to True — should be reversed to 'on'."""
        devices = [
            {
                "name": "Device",
                "entities": [
                    {
                        "entity_id": "binary_sensor.test",
                        "healthy_state": True,
                        "name": "Status",
                    }
                ],
            }
        ]
        app = _make_app({"devices": devices})
        _init_only(app)
        assert app._devices[0]["entities"][0]["healthy_state"] == "on"

    def test_yaml_bool_coercion_false_becomes_off(self):
        """YAML coerces 'off' to False — should be reversed to 'off'."""
        devices = [
            {
                "name": "Device",
                "entities": [
                    {
                        "entity_id": "binary_sensor.test",
                        "healthy_state": False,
                        "name": "Status",
                    }
                ],
            }
        ]
        app = _make_app({"devices": devices})
        _init_only(app)
        assert app._devices[0]["entities"][0]["healthy_state"] == "off"


# ---------------------------------------------------------------------------
# Tests — Ping checks
# ---------------------------------------------------------------------------

class TestPingChecks:
    def test_ping_ok(self):
        app = _make_app()
        _init_only(app)
        with patch(
            "health_checks.checker_apps.device_group_checker"
            ".device_group_checker.ping_check",
            new_callable=AsyncMock,
            return_value={"status": "ok", "detail": "3ms"},
        ):
            result = _run(app._check_device_ping(SAMPLE_DEVICES[0]))
        assert result["status"] == "ok"
        assert result["name"] == "Movie Room Ping"

    def test_ping_critical(self):
        app = _make_app()
        _init_only(app)
        with patch(
            "health_checks.checker_apps.device_group_checker"
            ".device_group_checker.ping_check",
            new_callable=AsyncMock,
            return_value={"status": "critical", "detail": "timeout"},
        ):
            result = _run(app._check_device_ping(SAMPLE_DEVICES[0]))
        assert result["status"] == "critical"
        assert result["name"] == "Movie Room Ping"


# ---------------------------------------------------------------------------
# Tests — Run checks reporting
# ---------------------------------------------------------------------------

class TestRunChecks:
    def test_reports_all_results(self):
        app = _make_app()
        _init_only(app)
        app.get_state = AsyncMock(side_effect=["on", "on"])

        with patch(
            "health_checks.checker_apps.device_group_checker"
            ".device_group_checker.ping_check",
            new_callable=AsyncMock,
            return_value={"status": "ok", "detail": "3ms"},
        ):
            _run(app._run_checks())

        report_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
        ]
        assert len(report_calls) == 1
        payload = json.loads(report_calls[0][1]["payload"])
        # 2 devices x (1 entity + 1 ping) = 4 results
        assert len(payload["results"]) == 4
        assert "repair_state" not in payload

    def test_no_ping_skips_ping_check(self):
        devices_no_ip = [
            {
                "name": "Movie Room",
                "entities": [
                    {
                        "entity_id": "binary_sensor.movie_room_breeze_status",
                        "healthy_state": "on",
                        "name": "Status",
                    }
                ],
            }
        ]
        app = _make_app({"devices": devices_no_ip})
        _init_only(app)
        app.get_state = AsyncMock(return_value="on")

        _run(app._run_checks())

        report_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
        ]
        payload = json.loads(report_calls[0][1]["payload"])
        assert len(payload["results"]) == 1
        assert payload["results"][0]["name"] == "Movie Room Status"
