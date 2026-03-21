"""Unit tests for FanHealthChecker.

Mocks AppDaemon methods, HAProvisioner, and check_utils — no real HA access required.
"""

from __future__ import annotations

import asyncio
import datetime
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

from health_checks.checker_apps.fan_health_checker.fan_health_checker import (
    FanHealthChecker,
    REPAIR_FAILED,
    REPAIR_IDLE,
    REPAIR_IN_PROGRESS,
    REPAIR_PENDING,
    REPAIR_SUCCESS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_FANS = [
    {
        "name": "Pink Room",
        "entity_id": "fan.pink_room_fan_fan",
        "ip": "192.168.50.112",
        "power_switch": "switch.upstairs_pink_room_scene_controller",
        "relay_control": "select.upstairs_pink_room_scene_controller_relay_control",
        "scene_control": "select.upstairs_pink_room_scene_controller_scene_control_relay",
    },
    {
        "name": "Blue Room",
        "entity_id": "fan.blue_room_fan_fan",
        "ip": "192.168.50.134",
        "power_switch": "switch.upstairs_blue_room_scene_controller",
        "relay_control": "select.upstairs_blue_room_scene_controller_relay_control",
        "scene_control": "select.upstairs_blue_room_scene_controller_scene_control_relay",
    },
]

DEFAULT_ARGS: Dict[str, Any] = {
    "ha_url": "http://ha:8123",
    "ha_token_env": "TOKEN",
    "checker_id": "fans",
    "checker_name": "Fans",
    "check_interval_s": 180,
    "repair_recovery_wait_s": 10,
    "auto_repair_enabled_default": False,
    "auto_repair_delay_min_default": 5,
    "repair_script": "script.zen32_hard_reset",
    "fans": SAMPLE_FANS,
}


def _make_app(extra_args: dict | None = None) -> FanHealthChecker:
    ad = MagicMock()
    config = MagicMock()
    app = FanHealthChecker(ad, config)

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
    app.run_every = MagicMock()
    app.cancel_timer = MagicMock()
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


def _startup(app: FanHealthChecker, mock_prov: MagicMock | None = None) -> None:
    if mock_prov is None:
        mock_prov = _make_mock_provisioner()
    app.initialize()
    with patch(
        "health_checks.checker_apps.fan_health_checker.fan_health_checker.HAProvisioner",
        return_value=mock_prov,
    ):
        _run(app._async_startup())


def _init_only(app: FanHealthChecker) -> None:
    app.initialize()


# ---------------------------------------------------------------------------
# Tests — Lifecycle
# ---------------------------------------------------------------------------

class TestLifecycle:
    def test_initialize_calls_run_in(self):
        app = _make_app()
        app.initialize()
        app.run_in.assert_called_once()

    def test_startup_provisions_helpers(self):
        app = _make_app()
        mock_prov = _make_mock_provisioner()
        _startup(app, mock_prov)
        assert mock_prov.ensure_helper.call_count == 2

    def test_startup_registers_with_supports_repair(self):
        app = _make_app()
        _startup(app)
        register_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "register_checker"
        ]
        assert len(register_calls) == 1
        payload = json.loads(register_calls[0][1]["payload"])
        assert payload["supports_repair"] is True
        assert payload["checker_id"] == "fans"

    def test_registers_correct_check_names(self):
        app = _make_app()
        _startup(app)
        register_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "register_checker"
        ]
        payload = json.loads(register_calls[0][1]["payload"])
        names = payload["check_names"]
        assert len(names) == 4  # 2 fans x 2 checks
        assert "Pink Room State" in names
        assert "Pink Room Ping" in names
        assert "Blue Room State" in names
        assert "Blue Room Ping" in names

    def test_startup_registers_event_listeners(self):
        app = _make_app()
        _startup(app)
        event_names = [c[0][1] for c in app.listen_event.call_args_list]
        assert "health_check_controller_ready" in event_names
        assert "health_check_recheck" in event_names
        assert "health_check_repair_fans" in event_names


# ---------------------------------------------------------------------------
# Tests — Check execution
# ---------------------------------------------------------------------------

class TestChecks:
    def test_fan_entity_ok(self):
        app = _make_app()
        _init_only(app)
        app.get_state = AsyncMock(return_value="on")
        result = _run(app._check_fan_entity(SAMPLE_FANS[0]))
        assert result["status"] == "ok"
        assert result["name"] == "Pink Room State"

    def test_fan_entity_unavailable(self):
        app = _make_app()
        _init_only(app)
        app.get_state = AsyncMock(return_value="unavailable")
        result = _run(app._check_fan_entity(SAMPLE_FANS[0]))
        assert result["status"] == "critical"

    def test_fan_entity_unknown(self):
        app = _make_app()
        _init_only(app)
        app.get_state = AsyncMock(return_value="unknown")
        result = _run(app._check_fan_entity(SAMPLE_FANS[0]))
        assert result["status"] == "critical"

    def test_fan_entity_none(self):
        app = _make_app()
        _init_only(app)
        app.get_state = AsyncMock(return_value=None)
        result = _run(app._check_fan_entity(SAMPLE_FANS[0]))
        assert result["status"] == "critical"

    def test_fan_entity_off_is_ok(self):
        """A fan that is 'off' is still healthy — just not running."""
        app = _make_app()
        _init_only(app)
        app.get_state = AsyncMock(return_value="off")
        result = _run(app._check_fan_entity(SAMPLE_FANS[0]))
        assert result["status"] == "ok"

    def test_fan_ping_ok(self):
        app = _make_app()
        _init_only(app)
        with patch(
            "health_checks.checker_apps.fan_health_checker.fan_health_checker.ping_check",
            new_callable=AsyncMock,
            return_value={"status": "ok", "detail": "3ms"},
        ):
            result = _run(app._check_fan_ping(SAMPLE_FANS[0]))
        assert result["status"] == "ok"
        assert result["name"] == "Pink Room Ping"

    def test_fan_ping_critical(self):
        app = _make_app()
        _init_only(app)
        with patch(
            "health_checks.checker_apps.fan_health_checker.fan_health_checker.ping_check",
            new_callable=AsyncMock,
            return_value={"status": "critical", "detail": "timeout"},
        ):
            result = _run(app._check_fan_ping(SAMPLE_FANS[0]))
        assert result["status"] == "critical"


# ---------------------------------------------------------------------------
# Tests — Per-fan repair state
# ---------------------------------------------------------------------------

class TestPerFanRepairState:
    def test_initial_state_all_idle(self):
        app = _make_app()
        _init_only(app)
        for fr in app._fan_repair_states.values():
            assert fr["status"] == REPAIR_IDLE

    def test_recovered_fan_resets_failed_state(self):
        app = _make_app()
        _init_only(app)
        app._fan_repair_states["Pink Room"]["status"] = REPAIR_FAILED
        app._fan_repair_states["Pink Room"]["detail"] = "Did not recover"

        results = [
            {"name": "Pink Room State", "status": "ok", "detail": "on"},
            {"name": "Pink Room Ping", "status": "ok", "detail": "3ms"},
            {"name": "Blue Room State", "status": "ok", "detail": "off"},
            {"name": "Blue Room Ping", "status": "ok", "detail": "5ms"},
        ]
        app._reset_recovered_fans(results)

        assert app._fan_repair_states["Pink Room"]["status"] == REPAIR_IDLE

    def test_still_failing_fan_keeps_failed_state(self):
        app = _make_app()
        _init_only(app)
        app._fan_repair_states["Pink Room"]["status"] = REPAIR_FAILED

        results = [
            {"name": "Pink Room State", "status": "critical", "detail": "unavailable"},
            {"name": "Pink Room Ping", "status": "critical", "detail": "timeout"},
            {"name": "Blue Room State", "status": "ok", "detail": "off"},
            {"name": "Blue Room Ping", "status": "ok", "detail": "5ms"},
        ]
        app._reset_recovered_fans(results)

        assert app._fan_repair_states["Pink Room"]["status"] == REPAIR_FAILED

    def test_aggregate_status_in_progress(self):
        app = _make_app()
        _init_only(app)
        app._fan_repair_states["Pink Room"]["status"] = REPAIR_IN_PROGRESS
        app._fan_repair_states["Blue Room"]["status"] = REPAIR_IDLE
        assert app._aggregate_repair_status() == REPAIR_IN_PROGRESS

    def test_aggregate_status_failed(self):
        app = _make_app()
        _init_only(app)
        app._fan_repair_states["Pink Room"]["status"] = REPAIR_FAILED
        app._fan_repair_states["Blue Room"]["status"] = REPAIR_IDLE
        assert app._aggregate_repair_status() == REPAIR_FAILED

    def test_aggregate_status_all_idle(self):
        app = _make_app()
        _init_only(app)
        assert app._aggregate_repair_status() == REPAIR_IDLE


# ---------------------------------------------------------------------------
# Tests — Auto-repair
# ---------------------------------------------------------------------------

class TestAutoRepair:
    def test_auto_repair_triggers_for_first_failing_fan(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(side_effect=["on", "5"])
        app._unhealthy_since = datetime.datetime.now() - datetime.timedelta(minutes=10)
        app.create_task = MagicMock()

        results = [
            {"name": "Pink Room State", "status": "critical", "detail": "unavailable"},
            {"name": "Pink Room Ping", "status": "critical", "detail": "timeout"},
            {"name": "Blue Room State", "status": "ok", "detail": "off"},
            {"name": "Blue Room Ping", "status": "ok", "detail": "5ms"},
        ]
        app._evaluate_auto_repair(results)

        # Should have started repair (create_task called)
        assert app._fan_repair_states["Pink Room"]["status"] == REPAIR_IN_PROGRESS

    def test_auto_repair_skips_already_failed_fan(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(side_effect=["on", "5"])
        app._unhealthy_since = datetime.datetime.now() - datetime.timedelta(minutes=10)
        app._fan_repair_states["Pink Room"]["status"] = REPAIR_FAILED
        app.create_task = MagicMock()

        results = [
            {"name": "Pink Room State", "status": "critical", "detail": "unavailable"},
            {"name": "Pink Room Ping", "status": "critical", "detail": "timeout"},
            {"name": "Blue Room State", "status": "critical", "detail": "unavailable"},
            {"name": "Blue Room Ping", "status": "critical", "detail": "timeout"},
        ]
        app._evaluate_auto_repair(results)

        # Pink Room already failed, should repair Blue Room
        assert app._fan_repair_states["Pink Room"]["status"] == REPAIR_FAILED
        assert app._fan_repair_states["Blue Room"]["status"] == REPAIR_IN_PROGRESS

    def test_auto_repair_disabled_stays_idle(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(side_effect=["off", "5"])

        results = [
            {"name": "Pink Room State", "status": "critical", "detail": "unavailable"},
            {"name": "Pink Room Ping", "status": "critical", "detail": "timeout"},
            {"name": "Blue Room State", "status": "ok", "detail": "off"},
            {"name": "Blue Room Ping", "status": "ok", "detail": "5ms"},
        ]
        app._evaluate_auto_repair(results)

        assert app._fan_repair_states["Pink Room"]["status"] == REPAIR_IDLE
        assert app._unhealthy_since is not None

    def test_all_ok_cancels_pending(self):
        app = _make_app()
        _init_only(app)
        app._repair_status = REPAIR_PENDING
        app._unhealthy_since = datetime.datetime.now()
        app._auto_repair_deadline = datetime.datetime.now()

        results = [
            {"name": "Pink Room State", "status": "ok", "detail": "on"},
            {"name": "Pink Room Ping", "status": "ok", "detail": "3ms"},
            {"name": "Blue Room State", "status": "ok", "detail": "off"},
            {"name": "Blue Room Ping", "status": "ok", "detail": "5ms"},
        ]
        app._evaluate_auto_repair(results)

        assert app._repair_status == REPAIR_IDLE
        assert app._unhealthy_since is None
        assert app._auto_repair_deadline is None


# ---------------------------------------------------------------------------
# Tests — Repair execution
# ---------------------------------------------------------------------------

class TestRepairExecution:
    def test_execute_fan_repair_calls_script(self):
        app = _make_app()
        _init_only(app)

        # Mock health checks to return ok immediately
        app._run_health_checks_only = AsyncMock(
            return_value=[
                {"name": "Pink Room State", "status": "ok", "detail": "on"},
                {"name": "Pink Room Ping", "status": "ok", "detail": "3ms"},
                {"name": "Blue Room State", "status": "ok", "detail": "off"},
                {"name": "Blue Room Ping", "status": "ok", "detail": "5ms"},
            ]
        )

        _run(app._execute_fan_repair(SAMPLE_FANS[0]))

        # Verify script was called with correct entities
        app.call_service.assert_called_once_with(
            "script/zen32_hard_reset",
            power_switch_entity="switch.upstairs_pink_room_scene_controller",
            relay_control_select_entity="select.upstairs_pink_room_scene_controller_relay_control",
            scene_control_select_entity="select.upstairs_pink_room_scene_controller_scene_control_relay",
            unavailable_fan_entity="fan.pink_room_fan_fan",
        )
        assert app._fan_repair_states["Pink Room"]["status"] == REPAIR_SUCCESS

    def test_execute_fan_repair_timeout(self):
        app = _make_app({"repair_recovery_wait_s": 10})
        _init_only(app)

        app._run_health_checks_only = AsyncMock(
            return_value=[
                {"name": "Pink Room State", "status": "critical", "detail": "unavailable"},
                {"name": "Pink Room Ping", "status": "critical", "detail": "timeout"},
                {"name": "Blue Room State", "status": "ok", "detail": "off"},
                {"name": "Blue Room Ping", "status": "ok", "detail": "5ms"},
            ]
        )

        _run(app._execute_fan_repair(SAMPLE_FANS[0]))

        assert app._fan_repair_states["Pink Room"]["status"] == REPAIR_FAILED
        assert "Did not recover" in app._fan_repair_states["Pink Room"]["detail"]

    def test_execute_fan_repair_error(self):
        app = _make_app()
        _init_only(app)
        app.call_service = MagicMock(side_effect=Exception("Service unavailable"))

        _run(app._execute_fan_repair(SAMPLE_FANS[0]))

        assert app._fan_repair_states["Pink Room"]["status"] == REPAIR_FAILED
        assert "error" in app._fan_repair_states["Pink Room"]["detail"].lower()

    def test_manual_repair_resets_failed_states(self):
        app = _make_app()
        _init_only(app)
        app._fan_repair_states["Pink Room"]["status"] = REPAIR_FAILED
        app._fan_repair_states["Blue Room"]["status"] = REPAIR_FAILED
        app.create_task = MagicMock()

        app._start_manual_repair()

        assert app._fan_repair_states["Pink Room"]["status"] == REPAIR_IDLE
        assert app._fan_repair_states["Blue Room"]["status"] == REPAIR_IDLE


# ---------------------------------------------------------------------------
# Tests — Repair state reporting
# ---------------------------------------------------------------------------

class TestRepairStateReporting:
    def test_build_repair_state_includes_device_repairs(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(side_effect=["off", "5"])
        app._fan_repair_states["Pink Room"]["status"] = REPAIR_FAILED
        app._fan_repair_states["Pink Room"]["detail"] = "Did not recover"

        state = app._build_repair_state()

        assert "device_repairs" in state
        assert state["device_repairs"]["Pink Room"]["status"] == REPAIR_FAILED
        assert state["device_repairs"]["Blue Room"]["status"] == REPAIR_IDLE
        assert state["status"] == REPAIR_FAILED

    def test_build_repair_state_detail_shows_active_fan(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(side_effect=["on", "5"])
        app._fan_repair_states["Blue Room"]["status"] = REPAIR_IN_PROGRESS

        state = app._build_repair_state()

        assert "Blue Room" in state["detail"]


# ---------------------------------------------------------------------------
# Tests — Repair command handler
# ---------------------------------------------------------------------------

class TestRepairCommandHandler:
    def test_start_repair_command(self):
        app = _make_app()
        _init_only(app)
        app.create_task = MagicMock()

        app._on_repair_command(
            "health_check_repair_fans",
            {"action": "start_repair"},
            {},
        )

        app.create_task.assert_called_once()

    def test_update_config_command(self):
        app = _make_app()
        _init_only(app)

        app._on_repair_command(
            "health_check_repair_fans",
            {"action": "update_repair_config", "auto_repair_enabled": True},
            {},
        )

        calls = [c[0][0] for c in app.call_service.call_args_list]
        assert "input_boolean/turn_on" in calls
