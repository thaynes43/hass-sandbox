"""Unit tests for RepairableDeviceChecker."""

from __future__ import annotations

import asyncio
import datetime
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

from health_checks.checker_apps.device_checker.repairable_device_checker import (
    RepairableDeviceChecker,
    REPAIR_FAILED,
    REPAIR_IDLE,
    REPAIR_IN_PROGRESS,
    REPAIR_PENDING,
    REPAIR_SUCCESS,
)


DEFAULT_ARGS: Dict[str, Any] = {
    "ha_url": "http://ha:8123",
    "ha_token_env": "TOKEN",
    "checker_id": "printer",
    "checker_name": "Printer",
    "ping_host": "192.168.0.211",
    "ping_check_name": "Ping",
    "check_interval_s": 180,
    "repair_switch": "switch.downstairs_study_printer_switch",
    "repair_recovery_wait_s": 10,
    "repair_off_duration_s": 2,
    "auto_repair_enabled_default": False,
    "auto_repair_delay_min_default": 5,
    "entities": [
        {"entity_id": "sensor.brother_mfc_l3780cdw_series", "name": "Status"},
    ],
}


def _make_app(extra_args: dict | None = None) -> RepairableDeviceChecker:
    ad = MagicMock()
    config = MagicMock()
    app = RepairableDeviceChecker(ad, config)

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


def _make_mock_provisioner() -> MagicMock:
    prov = MagicMock()
    prov.ensure_helper = AsyncMock(return_value=False)
    return prov


def _startup(app: RepairableDeviceChecker, mock_prov: MagicMock | None = None) -> None:
    if mock_prov is None:
        mock_prov = _make_mock_provisioner()
    app.initialize()
    with patch(
        "health_checks.checker_apps.device_checker.repairable_device_checker.HAProvisioner",
        return_value=mock_prov,
    ):
        _run(app._async_startup())


def _init_only(app: RepairableDeviceChecker) -> None:
    app.initialize()


class TestLifecycle:
    def test_registers_with_supports_repair(self):
        app = _make_app()
        _startup(app)
        register_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "register_checker"
        ]
        payload = json.loads(register_calls[0][1]["payload"])
        assert payload["supports_repair"] is True

    def test_provisions_repair_helpers(self):
        app = _make_app()
        mock_prov = _make_mock_provisioner()
        _startup(app, mock_prov)
        assert mock_prov.ensure_helper.call_count == 2

    def test_listens_for_repair_events(self):
        app = _make_app()
        _startup(app)
        event_names = [c[0][1] for c in app.listen_event.call_args_list]
        assert "health_check_repair_printer" in event_names

    def test_inherits_check_names(self):
        app = _make_app()
        _startup(app)
        register_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "register_checker"
        ]
        payload = json.loads(register_calls[0][1]["payload"])
        assert "Ping" in payload["check_names"]
        assert "Status" in payload["check_names"]


class TestChecksInherited:
    def test_entity_no_healthy_state_ok(self):
        """Entity with no healthy_state is ok if not unavailable/unknown."""
        app = _make_app()
        _init_only(app)
        app.get_state = AsyncMock(return_value="idle")
        result = _run(app._check_entity_state(app._entities[0]))
        assert result["status"] == "ok"

    def test_entity_unavailable_critical(self):
        app = _make_app()
        _init_only(app)
        app.get_state = AsyncMock(return_value="unavailable")
        result = _run(app._check_entity_state(app._entities[0]))
        assert result["status"] == "critical"


class TestRepairStateMachine:
    def test_initial_state_idle(self):
        app = _make_app()
        _init_only(app)
        assert app._repair_status == REPAIR_IDLE

    def test_unhealthy_with_auto_repair_goes_pending(self):
        app = _make_app()
        _init_only(app)
        app._cached_auto_repair_enabled = True
        app._cached_auto_repair_delay_min = 5

        results = [
            {"name": "Ping", "status": "critical", "detail": "timeout"},
            {"name": "Status", "status": "critical", "detail": "unavailable"},
        ]
        app._evaluate_auto_repair(results)
        assert app._repair_status == REPAIR_PENDING

    def test_all_ok_cancels_pending(self):
        app = _make_app()
        _init_only(app)
        app._repair_status = REPAIR_PENDING
        app._unhealthy_since = datetime.datetime.now()

        results = [
            {"name": "Ping", "status": "ok", "detail": "3ms"},
            {"name": "Status", "status": "ok", "detail": "idle"},
        ]
        app._evaluate_auto_repair(results)
        assert app._repair_status == REPAIR_IDLE

    def test_failed_no_auto_retry(self):
        app = _make_app()
        _init_only(app)
        app._repair_status = REPAIR_FAILED

        results = [
            {"name": "Ping", "status": "critical", "detail": "timeout"},
        ]
        app._evaluate_auto_repair(results)
        assert app._repair_status == REPAIR_FAILED

    def test_failed_clears_when_checks_recover(self):
        app = _make_app()
        _init_only(app)
        app._repair_status = REPAIR_FAILED
        app._repair_detail = "Did not recover after 300s"
        app._unhealthy_since = datetime.datetime.now()

        results = [
            {"name": "Ping", "status": "ok", "detail": "3ms"},
            {"name": "Status", "status": "ok", "detail": "idle"},
        ]
        app._evaluate_auto_repair(results)
        assert app._repair_status == REPAIR_IDLE
        assert app._repair_detail == ""
        assert app._unhealthy_since is None

    def test_manual_repair_from_failed(self):
        app = _make_app()
        _init_only(app)
        app._repair_status = REPAIR_FAILED
        app.create_task = MagicMock()

        app._start_repair()
        assert app._repair_status == REPAIR_IN_PROGRESS

    def test_no_repair_switch_fails(self):
        app = _make_app({"repair_switch": ""})
        _init_only(app)

        app._start_repair()
        assert app._repair_status == REPAIR_FAILED
        assert "No repair switch" in app._repair_detail


class TestRepairExecution:
    def test_successful_repair(self):
        app = _make_app({"repair_recovery_wait_s": 10, "repair_off_duration_s": 0})
        _init_only(app)
        app._repair_status = REPAIR_IN_PROGRESS

        app._run_checks_only = AsyncMock(return_value=[
            {"name": "Ping", "status": "ok", "detail": "3ms"},
            {"name": "Status", "status": "ok", "detail": "idle"},
        ])

        _run(app._execute_repair())

        assert app._repair_status == REPAIR_SUCCESS
        calls = [c[0][0] for c in app.call_service.call_args_list]
        assert "switch/turn_off" in calls
        assert "switch/turn_on" in calls

    def test_repair_timeout(self):
        app = _make_app({"repair_recovery_wait_s": 10, "repair_off_duration_s": 0})
        _init_only(app)
        app._repair_status = REPAIR_IN_PROGRESS

        app._run_checks_only = AsyncMock(return_value=[
            {"name": "Ping", "status": "critical", "detail": "timeout"},
        ])

        _run(app._execute_repair())
        assert app._repair_status == REPAIR_FAILED

    def test_repair_error(self):
        app = _make_app()
        _init_only(app)
        app._repair_status = REPAIR_IN_PROGRESS
        app.call_service = MagicMock(side_effect=Exception("fail"))

        _run(app._execute_repair())
        assert app._repair_status == REPAIR_FAILED


class TestReportPayload:
    def test_includes_repair_state(self):
        app = _make_app()
        _init_only(app)
        app._cached_auto_repair_enabled = False
        app._cached_auto_repair_delay_min = 5

        payload = app._build_report_payload([
            {"name": "Ping", "status": "ok", "detail": "3ms"},
        ])
        assert "repair_state" in payload
        assert payload["repair_state"]["status"] == REPAIR_IDLE


class TestRepairCommand:
    def test_start_repair_command(self):
        app = _make_app()
        _init_only(app)
        app.create_task = MagicMock()

        app._on_repair_command(
            "health_check_repair_printer",
            {"action": "start_repair"},
            {},
        )
        assert app._repair_status == REPAIR_IN_PROGRESS

    def test_update_config_command(self):
        app = _make_app()
        _init_only(app)

        app._on_repair_command(
            "health_check_repair_printer",
            {"action": "update_repair_config", "auto_repair_enabled": True},
            {},
        )
        calls = [c[0][0] for c in app.call_service.call_args_list]
        assert "input_boolean/turn_on" in calls
