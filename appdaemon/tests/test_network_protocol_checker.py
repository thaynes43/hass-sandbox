"""Unit tests for NetworkProtocolChecker.

Mocks AppDaemon methods and shared check utilities — no real network access.
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
sys.path.insert(0, str(_repo_root / "apps" / "health_checks"))
sys.path.insert(0, str(_repo_root))

from health_checks.checker_apps.network_protocol_checker.network_protocol_checker import (
    NetworkProtocolChecker,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DEFAULT_ARGS: Dict[str, Any] = {
    "checker_id": "zigbee",
    "checker_name": "Zigbee",
    "entity_id": "binary_sensor.zigbee2mqtt_bridge_connection_state",
    "entity_healthy_state": "on",
    "entity_check_name": "Integration Status",
    "radio_host": "tubeszb-zigbee01.haynesnetwork",
    "radio_check_name": "Coordinator Ping",
    "web_ui_url": "https://zigbee.haynesops.com",
    "web_ui_check_name": "Web UI",
    "check_interval_s": 180,
}


def _make_app(extra_args: dict | None = None) -> NetworkProtocolChecker:
    """Create a NetworkProtocolChecker with mocked AppDaemon methods."""
    ad = MagicMock()
    config = MagicMock()
    app = NetworkProtocolChecker(ad, config)

    args = dict(DEFAULT_ARGS)
    if extra_args:
        args.update(extra_args)
    app.args = args

    app.get_state = AsyncMock(return_value="on")
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
    """Run a coroutine in a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _startup(app: NetworkProtocolChecker) -> None:
    """Initialize the app and run the async startup coroutine."""
    app.initialize()
    _run(app._async_startup())


# ---------------------------------------------------------------------------
# Tests — Lifecycle
# ---------------------------------------------------------------------------

class TestLifecycle:
    def test_initialize_calls_run_in(self):
        """initialize() should register a run_in callback."""
        app = _make_app()
        app.initialize()
        app.run_in.assert_called_once()

    def test_startup_registers_with_controller(self):
        """Startup should fire register_checker event."""
        app = _make_app()
        _startup(app)

        # Find the register_checker call
        register_calls = [
            c for c in app.fire_event.call_args_list
            if c[0][0] == "health_check_command"
            and c[1].get("command") == "register_checker"
        ]
        assert len(register_calls) == 1
        payload = json.loads(register_calls[0][1]["payload"])
        assert payload["checker_id"] == "zigbee"
        assert payload["checker_name"] == "Zigbee"
        assert len(payload["check_names"]) == 3

    def test_startup_listens_for_controller_ready(self):
        """Startup should listen for health_check_controller_ready."""
        app = _make_app()
        _startup(app)
        listen_calls = [
            c for c in app.listen_event.call_args_list
            if c[0][1] == "health_check_controller_ready"
        ]
        assert len(listen_calls) == 1

    def test_startup_listens_for_recheck(self):
        """Startup should listen for health_check_recheck."""
        app = _make_app()
        _startup(app)
        listen_calls = [
            c for c in app.listen_event.call_args_list
            if c[0][1] == "health_check_recheck"
        ]
        assert len(listen_calls) == 1

    def test_startup_schedules_timer(self):
        """Startup should schedule a run_in for the check timer."""
        app = _make_app()
        _startup(app)
        # run_in is called in initialize() and again in _async_startup for timer
        assert app.run_in.call_count >= 2


# ---------------------------------------------------------------------------
# Tests — Registration
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_controller_ready_triggers_re_register(self):
        """Controller ready event should trigger re-registration."""
        app = _make_app()
        _startup(app)
        app.fire_event.reset_mock()

        app._on_controller_ready("health_check_controller_ready", {}, {})

        register_calls = [
            c for c in app.fire_event.call_args_list
            if c[0][0] == "health_check_command"
            and c[1].get("command") == "register_checker"
        ]
        assert len(register_calls) == 1

    def test_check_names_reflect_config(self):
        """Check names should only include configured checks."""
        app = _make_app({"radio_host": "", "web_ui_url": ""})
        _startup(app)

        register_calls = [
            c for c in app.fire_event.call_args_list
            if c[0][0] == "health_check_command"
            and c[1].get("command") == "register_checker"
        ]
        payload = json.loads(register_calls[0][1]["payload"])
        assert payload["check_names"] == ["Integration Status"]


# ---------------------------------------------------------------------------
# Tests — Check execution
# ---------------------------------------------------------------------------

class TestCheckExecution:
    def test_all_checks_ok(self):
        """All checks passing should report all ok."""
        app = _make_app()
        _startup(app)
        app.fire_event.reset_mock()
        app.get_state.return_value = "on"

        with patch(
            "health_checks.checker_apps.network_protocol_checker.network_protocol_checker.ping_check",
            new_callable=AsyncMock,
            return_value={"status": "ok", "detail": "2ms"},
        ), patch(
            "health_checks.checker_apps.network_protocol_checker.network_protocol_checker.http_check",
            new_callable=AsyncMock,
            return_value={"status": "ok", "detail": "200 OK"},
        ):
            _run(app._run_checks())

        report_calls = [
            c for c in app.fire_event.call_args_list
            if c[0][0] == "health_check_command"
            and c[1].get("command") == "report_status"
        ]
        assert len(report_calls) == 1
        payload = json.loads(report_calls[0][1]["payload"])
        assert payload["checker_id"] == "zigbee"
        assert all(r["status"] == "ok" for r in payload["results"])
        assert len(payload["results"]) == 3

    def test_entity_warning_when_wrong_state_partial_failure(self):
        """Entity in wrong state with other checks ok should report warning (partial failure)."""
        app = _make_app()
        _startup(app)
        app.fire_event.reset_mock()
        app.get_state.return_value = "off"

        with patch(
            "health_checks.checker_apps.network_protocol_checker.network_protocol_checker.ping_check",
            new_callable=AsyncMock,
            return_value={"status": "ok", "detail": "2ms"},
        ), patch(
            "health_checks.checker_apps.network_protocol_checker.network_protocol_checker.http_check",
            new_callable=AsyncMock,
            return_value={"status": "ok", "detail": "200 OK"},
        ):
            _run(app._run_checks())

        report_calls = [
            c for c in app.fire_event.call_args_list
            if c[0][0] == "health_check_command"
            and c[1].get("command") == "report_status"
        ]
        payload = json.loads(report_calls[0][1]["payload"])
        entity_result = next(
            r for r in payload["results"] if r["name"] == "Integration Status"
        )
        assert entity_result["status"] == "warning"
        assert "off" in entity_result["detail"]

    def test_entity_warning_when_not_found_partial_failure(self):
        """Entity returning None with other checks ok should report warning (partial failure)."""
        app = _make_app()
        _startup(app)
        app.fire_event.reset_mock()
        app.get_state.return_value = None

        with patch(
            "health_checks.checker_apps.network_protocol_checker.network_protocol_checker.ping_check",
            new_callable=AsyncMock,
            return_value={"status": "ok", "detail": "2ms"},
        ), patch(
            "health_checks.checker_apps.network_protocol_checker.network_protocol_checker.http_check",
            new_callable=AsyncMock,
            return_value={"status": "ok", "detail": "200 OK"},
        ):
            _run(app._run_checks())

        payload = json.loads([
            c for c in app.fire_event.call_args_list
            if c[0][0] == "health_check_command"
            and c[1].get("command") == "report_status"
        ][0][1]["payload"])
        entity_result = next(
            r for r in payload["results"] if r["name"] == "Integration Status"
        )
        assert entity_result["status"] == "warning"
        assert "not found" in entity_result["detail"].lower()

    def test_ping_warning_when_partial_failure(self):
        """Failed ping with other checks ok should report warning (partial failure)."""
        app = _make_app()
        _startup(app)
        app.fire_event.reset_mock()
        app.get_state.return_value = "on"

        with patch(
            "health_checks.checker_apps.network_protocol_checker.network_protocol_checker.ping_check",
            new_callable=AsyncMock,
            return_value={"status": "critical", "detail": "timeout"},
        ), patch(
            "health_checks.checker_apps.network_protocol_checker.network_protocol_checker.http_check",
            new_callable=AsyncMock,
            return_value={"status": "ok", "detail": "200 OK"},
        ):
            _run(app._run_checks())

        payload = json.loads([
            c for c in app.fire_event.call_args_list
            if c[0][0] == "health_check_command"
            and c[1].get("command") == "report_status"
        ][0][1]["payload"])
        ping_result = next(
            r for r in payload["results"] if r["name"] == "Coordinator Ping"
        )
        assert ping_result["status"] == "warning"

    def test_http_warning_when_partial_failure(self):
        """Failed HTTP check with other checks ok should report warning (partial failure)."""
        app = _make_app()
        _startup(app)
        app.fire_event.reset_mock()
        app.get_state.return_value = "on"

        with patch(
            "health_checks.checker_apps.network_protocol_checker.network_protocol_checker.ping_check",
            new_callable=AsyncMock,
            return_value={"status": "ok", "detail": "2ms"},
        ), patch(
            "health_checks.checker_apps.network_protocol_checker.network_protocol_checker.http_check",
            new_callable=AsyncMock,
            return_value={"status": "critical", "detail": "HTTP 503"},
        ):
            _run(app._run_checks())

        payload = json.loads([
            c for c in app.fire_event.call_args_list
            if c[0][0] == "health_check_command"
            and c[1].get("command") == "report_status"
        ][0][1]["payload"])
        web_result = next(
            r for r in payload["results"] if r["name"] == "Web UI"
        )
        assert web_result["status"] == "warning"
        assert "503" in web_result["detail"]

    def test_all_checks_critical_stays_critical(self):
        """All checks failing should stay critical (not downgraded)."""
        app = _make_app()
        _startup(app)
        app.fire_event.reset_mock()
        app.get_state.return_value = "off"

        with patch(
            "health_checks.checker_apps.network_protocol_checker.network_protocol_checker.ping_check",
            new_callable=AsyncMock,
            return_value={"status": "critical", "detail": "timeout"},
        ), patch(
            "health_checks.checker_apps.network_protocol_checker.network_protocol_checker.http_check",
            new_callable=AsyncMock,
            return_value={"status": "critical", "detail": "HTTP 503"},
        ):
            _run(app._run_checks())

        payload = json.loads([
            c for c in app.fire_event.call_args_list
            if c[0][0] == "health_check_command"
            and c[1].get("command") == "report_status"
        ][0][1]["payload"])
        for r in payload["results"]:
            assert r["status"] == "critical"

    def test_skipped_checks_not_reported(self):
        """Unconfigured checks should not appear in results."""
        app = _make_app({"radio_host": "", "web_ui_url": ""})
        _startup(app)
        app.fire_event.reset_mock()
        app.get_state.return_value = "on"

        _run(app._run_checks())

        payload = json.loads([
            c for c in app.fire_event.call_args_list
            if c[0][0] == "health_check_command"
            and c[1].get("command") == "report_status"
        ][0][1]["payload"])
        assert len(payload["results"]) == 1
        assert payload["results"][0]["name"] == "Integration Status"

    def test_force_recheck_runs_checks(self):
        """Recheck event should trigger check execution."""
        app = _make_app()
        _startup(app)

        app._on_recheck("health_check_recheck", {}, {})
        # Should have called create_task for _run_checks
        app.create_task.assert_called()


# ---------------------------------------------------------------------------
# Tests — Z-Wave configuration
# ---------------------------------------------------------------------------

class TestZWaveConfig:
    """Verify the app works with Z-Wave-style configuration."""

    ZWAVE_ARGS = {
        "checker_id": "zwave",
        "checker_name": "Z-Wave",
        "entity_id": "sensor.800_series_long_range_gpio_module_status",
        "entity_healthy_state": "ready",
        "entity_check_name": "Integration Status",
        "radio_host": "tubeszb-zwave01.haynesnetwork",
        "radio_check_name": "Radio Ping",
        "web_ui_url": "https://zwave.haynesops.com",
        "web_ui_check_name": "Web UI",
        "check_interval_s": 180,
    }

    def test_zwave_registers_correctly(self):
        """Z-Wave config should register with correct names."""
        app = _make_app(self.ZWAVE_ARGS)
        _startup(app)

        register_calls = [
            c for c in app.fire_event.call_args_list
            if c[0][0] == "health_check_command"
            and c[1].get("command") == "register_checker"
        ]
        payload = json.loads(register_calls[0][1]["payload"])
        assert payload["checker_id"] == "zwave"
        assert payload["checker_name"] == "Z-Wave"
        assert "Integration Status" in payload["check_names"]
        assert "Radio Ping" in payload["check_names"]

    def test_zwave_entity_check(self):
        """Z-Wave entity check should match 'ready' state."""
        app = _make_app(self.ZWAVE_ARGS)
        _startup(app)
        app.get_state.return_value = "ready"

        result = _run(app._check_entity_state())
        assert result["status"] == "ok"
        assert result["detail"] == "ready"

    def test_zwave_entity_not_ready(self):
        """Z-Wave entity not in 'ready' state should report critical."""
        app = _make_app(self.ZWAVE_ARGS)
        _startup(app)
        app.get_state.return_value = "dead"

        result = _run(app._check_entity_state())
        assert result["status"] == "critical"
        assert "dead" in result["detail"]


# ---------------------------------------------------------------------------
# Tests — YAML boolean coercion
# ---------------------------------------------------------------------------

class TestYamlBooleanCoercion:
    """PyYAML coerces 'on'/'off' to True/False — the app must handle this."""

    def test_bool_true_becomes_on(self):
        """YAML 'on' parsed as bool True should match HA state 'on'."""
        app = _make_app({"entity_healthy_state": True})
        _startup(app)
        app.get_state.return_value = "on"

        result = _run(app._check_entity_state())
        assert result["status"] == "ok"
        assert result["detail"] == "on"

    def test_bool_false_becomes_off(self):
        """YAML 'off' parsed as bool False should match HA state 'off'."""
        app = _make_app({"entity_healthy_state": False})
        _startup(app)
        app.get_state.return_value = "off"

        result = _run(app._check_entity_state())
        assert result["status"] == "ok"
        assert result["detail"] == "off"

    def test_string_on_still_works(self):
        """Quoted 'on' staying as string should still work."""
        app = _make_app({"entity_healthy_state": "on"})
        _startup(app)
        app.get_state.return_value = "on"

        result = _run(app._check_entity_state())
        assert result["status"] == "ok"
