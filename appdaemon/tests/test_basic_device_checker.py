"""Unit tests for BasicDeviceChecker."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

mock_hass = MagicMock()
mock_hass.Hass = type("_MockHass", (), {"__init__": lambda self, *a, **kw: None})
sys.modules["hassapi"] = mock_hass

_repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo_root / "apps"))
sys.path.insert(0, str(_repo_root))

from health_checks.checker_apps.device_checker.device_checker import (
    BasicDeviceChecker,
)


DEFAULT_ARGS: Dict[str, Any] = {
    "checker_id": "vestaboard",
    "checker_name": "Vestaboard",
    "ping_host": "192.168.50.159",
    "ping_check_name": "Ping",
    "check_interval_s": 180,
    "entities": [
        {
            "entity_id": "sensor.vestaboard_controller_status",
            "healthy_state": "active",
            "name": "Controller Status",
        },
        {
            "entity_id": "sensor.vestaboard_configuration_status",
            "healthy_state": "ok",
            "name": "Configuration Status",
        },
    ],
}


def _make_app(extra_args: dict | None = None) -> BasicDeviceChecker:
    ad = MagicMock()
    config = MagicMock()
    app = BasicDeviceChecker(ad, config)

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


def _startup(app: BasicDeviceChecker) -> None:
    app.initialize()
    _run(app._async_startup())


def _init_only(app: BasicDeviceChecker) -> None:
    app.initialize()


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
        payload = json.loads(register_calls[0][1]["payload"])
        names = payload["check_names"]
        assert names == ["Ping", "Controller Status", "Configuration Status"]
        assert "supports_repair" not in payload

    def test_registers_without_ping(self):
        app = _make_app({"ping_host": ""})
        _startup(app)
        register_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "register_checker"
        ]
        payload = json.loads(register_calls[0][1]["payload"])
        assert "Ping" not in payload["check_names"]
        assert len(payload["check_names"]) == 2

    def test_registers_event_listeners(self):
        app = _make_app()
        _startup(app)
        event_names = [c[0][1] for c in app.listen_event.call_args_list]
        assert "health_check_controller_ready" in event_names
        assert "health_check_recheck" in event_names


class TestEntityChecks:
    def test_entity_ok(self):
        app = _make_app()
        _init_only(app)
        app.get_state = AsyncMock(return_value="active")
        result = _run(app._check_entity_state(app._entities[0]))
        assert result["status"] == "ok"
        assert result["name"] == "Controller Status"

    def test_entity_wrong_state(self):
        app = _make_app()
        _init_only(app)
        app.get_state = AsyncMock(return_value="error")
        result = _run(app._check_entity_state(app._entities[0]))
        assert result["status"] == "critical"
        assert "Expected 'active'" in result["detail"]

    def test_entity_not_found(self):
        app = _make_app()
        _init_only(app)
        app.get_state = AsyncMock(return_value=None)
        result = _run(app._check_entity_state(app._entities[0]))
        assert result["status"] == "critical"
        assert "None" in result["detail"]

    def test_yaml_bool_coercion(self):
        """YAML coerces 'on' to True — should be reversed."""
        app = _make_app({
            "entities": [
                {"entity_id": "binary_sensor.test", "healthy_state": True, "name": "Test"},
            ],
        })
        _init_only(app)
        assert app._entities[0]["healthy_state"] == "on"


class TestPingCheck:
    def test_ping_ok(self):
        app = _make_app()
        _init_only(app)
        with patch(
            "health_checks.checker_apps.device_checker.device_checker.ping_check",
            new_callable=AsyncMock,
            return_value={"status": "ok", "detail": "3ms"},
        ):
            result = _run(app._check_ping())
        assert result["status"] == "ok"
        assert result["name"] == "Ping"

    def test_ping_critical(self):
        app = _make_app()
        _init_only(app)
        with patch(
            "health_checks.checker_apps.device_checker.device_checker.ping_check",
            new_callable=AsyncMock,
            return_value={"status": "critical", "detail": "timeout"},
        ):
            result = _run(app._check_ping())
        assert result["status"] == "critical"


class TestRunChecks:
    def test_reports_all_results(self):
        app = _make_app()
        _init_only(app)
        app.get_state = AsyncMock(side_effect=["active", "ok"])

        with patch(
            "health_checks.checker_apps.device_checker.device_checker.ping_check",
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
        assert len(payload["results"]) == 3  # ping + 2 entities
        assert "repair_state" not in payload

    def test_no_ping_skips_ping_check(self):
        app = _make_app({"ping_host": ""})
        _init_only(app)
        app.get_state = AsyncMock(side_effect=["active", "ok"])

        _run(app._run_checks())

        report_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
        ]
        payload = json.loads(report_calls[0][1]["payload"])
        assert len(payload["results"]) == 2


class TestPendingRepairEventsDrain:
    """BasicDeviceChecker itself never populates _pending_repair_events (it
    has no repair support), but it owns the drain logic that
    RepairableDeviceChecker relies on — verify it directly here."""

    def test_no_repair_events_key_when_buffer_empty(self):
        app = _make_app()
        _init_only(app)
        payload = app._build_report_payload([
            {"name": "Ping", "status": "ok", "detail": "3ms"},
        ])
        assert "repair_events" not in payload

    def test_drains_and_clears_pending_repair_events(self):
        app = _make_app()
        _init_only(app)
        app._pending_repair_events.append(
            {"result": "success", "duration_s": 12}
        )

        payload = app._build_report_payload([])
        assert payload["repair_events"] == [
            {"result": "success", "duration_s": 12}
        ]
        assert app._pending_repair_events == []

        # Next payload build must not repeat the drained event.
        payload2 = app._build_report_payload([])
        assert "repair_events" not in payload2
