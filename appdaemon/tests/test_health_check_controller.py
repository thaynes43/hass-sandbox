"""Unit tests for HealthCheckController.

Mocks AppDaemon methods and HAProvisioner — no real HA access required.
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

from health_checks.controller.health_check_controller import (
    HealthCheckController,
    HEARTBEAT_ENTITY_ID,
    SENSOR_ENTITY_ID,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DEFAULT_ARGS: Dict[str, Any] = {
    "ha_url": "http://ha:8123",
    "ha_token_env": "TOKEN",
    "heartbeat_interval_s": 60,
    "alert_history_max": 20,
}


def _make_app(extra_args: dict | None = None) -> HealthCheckController:
    """Create a HealthCheckController with mocked AppDaemon methods."""
    ad = MagicMock()
    config = MagicMock()
    app = HealthCheckController(ad, config)

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
    """Run a coroutine in a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_mock_provisioner() -> MagicMock:
    prov = MagicMock()
    prov.ensure_helper = AsyncMock(return_value=False)
    prov.ensure_script = AsyncMock(return_value=False)
    return prov


def _startup(app: HealthCheckController, mock_prov: MagicMock | None = None) -> None:
    """Initialize the app and run the async startup coroutine."""
    if mock_prov is None:
        mock_prov = _make_mock_provisioner()
    app.initialize()
    with patch(
        "health_checks.controller.health_check_controller.HAProvisioner",
        return_value=mock_prov,
    ):
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

    def test_startup_provisions_entities(self):
        """Async startup should provision heartbeat helper and relay script."""
        app = _make_app()
        mock_prov = _make_mock_provisioner()
        _startup(app, mock_prov)
        mock_prov.ensure_helper.assert_called_once()
        mock_prov.ensure_script.assert_called_once()

    def test_startup_registers_event_listener(self):
        """Async startup should register the health_check_command listener."""
        app = _make_app()
        _startup(app)
        app.listen_event.assert_called_once_with(
            app._on_command, "health_check_command"
        )

    def test_startup_starts_heartbeat(self):
        """Async startup should start the heartbeat timer."""
        app = _make_app()
        _startup(app)
        app.run_every.assert_called_once()
        args = app.run_every.call_args[0]
        assert args[0] == app._heartbeat_tick
        assert args[2] == 60  # interval

    def test_startup_fires_ready_event(self):
        """Async startup should fire health_check_controller_ready."""
        app = _make_app()
        _startup(app)
        app.fire_event.assert_called_once_with(
            "health_check_controller_ready"
        )

    def test_startup_publishes_initial_status(self):
        """Async startup should publish initial sensor state."""
        app = _make_app()
        _startup(app)
        app.set_state.assert_called_once()
        call_args = app.set_state.call_args
        assert call_args[0][0] == SENSOR_ENTITY_ID
        assert call_args[1]["state"] == "unknown"

    def test_startup_skips_provisioning_without_config(self):
        """Startup should skip provisioning if ha_url is not configured."""
        app = _make_app({"ha_url": None})
        _startup(app)
        # Should still fire ready event and publish sensor
        app.fire_event.assert_called_once()
        app.set_state.assert_called_once()


# ---------------------------------------------------------------------------
# Tests — Heartbeat
# ---------------------------------------------------------------------------

class TestHeartbeat:
    def test_heartbeat_tick_calls_service(self):
        """Heartbeat tick should update the input_datetime helper."""
        app = _make_app()
        _startup(app)
        app.call_service.reset_mock()
        app._heartbeat_tick({})
        app.call_service.assert_called_once()
        call_args = app.call_service.call_args
        assert call_args[0][0] == "input_datetime/set_datetime"
        assert call_args[1]["entity_id"] == HEARTBEAT_ENTITY_ID
        assert "datetime" in call_args[1]


# ---------------------------------------------------------------------------
# Tests — Register checker
# ---------------------------------------------------------------------------

class TestRegisterChecker:
    def test_register_new_checker(self):
        """Registering a new checker should add it to internal state."""
        app = _make_app()
        _startup(app)
        app.set_state.reset_mock()

        app._on_command("health_check_command", {
            "command": "register_checker",
            "payload": json.dumps({
                "checker_id": "zigbee",
                "checker_name": "Zigbee",
                "check_names": ["Bridge", "Ping", "Web UI"],
            }),
        }, {})

        assert "zigbee" in app._checkers
        assert app._checkers["zigbee"]["name"] == "Zigbee"
        assert len(app._checkers["zigbee"]["checks"]) == 3
        assert app._checkers["zigbee"]["status"] == "unknown"
        # Should publish updated status
        app.set_state.assert_called()

    def test_re_register_preserves_alert_history(self):
        """Re-registering a checker should preserve existing alert history."""
        app = _make_app()
        _startup(app)

        # First register
        app._on_command("health_check_command", {
            "command": "register_checker",
            "payload": json.dumps({
                "checker_id": "zigbee",
                "checker_name": "Zigbee",
                "check_names": ["Bridge"],
            }),
        }, {})

        # Add an alert to history
        app._checkers["zigbee"]["alert_history"] = [
            {"timestamp": "2026-01-01T00:00:00", "check": "Bridge",
             "from_status": "ok", "to_status": "critical", "detail": "down"}
        ]

        # Re-register
        app._on_command("health_check_command", {
            "command": "register_checker",
            "payload": json.dumps({
                "checker_id": "zigbee",
                "checker_name": "Zigbee",
                "check_names": ["Bridge", "Ping"],
            }),
        }, {})

        assert len(app._checkers["zigbee"]["alert_history"]) == 1
        assert len(app._checkers["zigbee"]["checks"]) == 2

    def test_register_missing_checker_id(self):
        """register_checker with no checker_id should be rejected."""
        app = _make_app()
        _startup(app)
        app.set_state.reset_mock()

        app._on_command("health_check_command", {
            "command": "register_checker",
            "payload": json.dumps({"checker_name": "Bad"}),
        }, {})

        assert len(app._checkers) == 0


# ---------------------------------------------------------------------------
# Tests — Report status
# ---------------------------------------------------------------------------

class TestReportStatus:
    def _setup_with_checker(self) -> HealthCheckController:
        app = _make_app()
        _startup(app)
        app._on_command("health_check_command", {
            "command": "register_checker",
            "payload": json.dumps({
                "checker_id": "zigbee",
                "checker_name": "Zigbee",
                "check_names": ["Bridge", "Ping", "Web UI"],
            }),
        }, {})
        app.set_state.reset_mock()
        return app

    def test_all_ok_report(self):
        """All-ok report should set checker status to ok."""
        app = self._setup_with_checker()
        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "zigbee",
                "results": [
                    {"name": "Bridge", "status": "ok", "detail": "Connected"},
                    {"name": "Ping", "status": "ok", "detail": "2ms"},
                    {"name": "Web UI", "status": "ok", "detail": "200 OK"},
                ],
            }),
        }, {})

        assert app._checkers["zigbee"]["status"] == "ok"
        assert app._checkers["zigbee"]["last_check"] is not None
        # Sensor should be published
        app.set_state.assert_called()
        call_args = app.set_state.call_args
        assert call_args[1]["state"] == "ok"

    def test_critical_report(self):
        """A critical check should set overall checker status to critical."""
        app = self._setup_with_checker()
        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "zigbee",
                "results": [
                    {"name": "Bridge", "status": "critical", "detail": "Disconnected"},
                    {"name": "Ping", "status": "ok", "detail": "2ms"},
                    {"name": "Web UI", "status": "ok", "detail": "200 OK"},
                ],
            }),
        }, {})

        assert app._checkers["zigbee"]["status"] == "critical"
        call_args = app.set_state.call_args
        assert call_args[1]["state"] == "critical"

    def test_status_change_creates_alert(self):
        """Transitioning from ok to critical should create an alert entry."""
        app = self._setup_with_checker()

        # First report: all ok
        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "zigbee",
                "results": [
                    {"name": "Bridge", "status": "ok", "detail": "Connected"},
                    {"name": "Ping", "status": "ok", "detail": "2ms"},
                    {"name": "Web UI", "status": "ok", "detail": "200 OK"},
                ],
            }),
        }, {})

        # Second report: Bridge goes critical
        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "zigbee",
                "results": [
                    {"name": "Bridge", "status": "critical", "detail": "Disconnected"},
                    {"name": "Ping", "status": "ok", "detail": "2ms"},
                    {"name": "Web UI", "status": "ok", "detail": "200 OK"},
                ],
            }),
        }, {})

        alerts = app._checkers["zigbee"]["alert_history"]
        assert len(alerts) == 1
        assert alerts[0]["check"] == "Bridge"
        assert alerts[0]["from_status"] == "ok"
        assert alerts[0]["to_status"] == "critical"

    def test_alert_history_trimmed(self):
        """Alert history should be trimmed to alert_history_max."""
        app = _make_app({"alert_history_max": 3})
        _startup(app)

        app._on_command("health_check_command", {
            "command": "register_checker",
            "payload": json.dumps({
                "checker_id": "test",
                "checker_name": "Test",
                "check_names": ["Check1"],
            }),
        }, {})

        # Pre-fill with 5 alerts
        app._checkers["test"]["alert_history"] = [
            {"timestamp": f"t{i}", "check": "Check1",
             "from_status": "ok", "to_status": "critical", "detail": f"d{i}"}
            for i in range(5)
        ]

        # Report a status change to trigger trim
        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "test",
                "results": [{"name": "Check1", "status": "ok", "detail": "recovered"}],
            }),
        }, {})

        # Should have 1 new alert + trimmed to max 3
        assert len(app._checkers["test"]["alert_history"]) <= 3

    def test_unknown_checker_rejected(self):
        """report_status for an unregistered checker should be rejected."""
        app = _make_app()
        _startup(app)
        app.set_state.reset_mock()

        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "nonexistent",
                "results": [{"name": "X", "status": "ok", "detail": ""}],
            }),
        }, {})

        # Should log warning but not crash; no sensor update for unknown checker
        app.log.assert_any_call(
            "report_status for unknown checker: 'nonexistent'",
            level="WARNING",
        )


# ---------------------------------------------------------------------------
# Tests — Force recheck
# ---------------------------------------------------------------------------

class TestForceRecheck:
    def test_force_recheck_fires_event(self):
        """force_recheck should broadcast health_check_recheck event."""
        app = _make_app()
        _startup(app)
        app.fire_event.reset_mock()

        app._on_command("health_check_command", {
            "command": "force_recheck",
            "payload": "{}",
        }, {})

        app.fire_event.assert_called_once_with("health_check_recheck")


# ---------------------------------------------------------------------------
# Tests — Overall status computation
# ---------------------------------------------------------------------------

class TestOverallStatus:
    def test_no_checkers_is_unknown(self):
        """With no checkers registered, overall should be unknown."""
        app = _make_app()
        _startup(app)
        call_args = app.set_state.call_args
        assert call_args[1]["state"] == "unknown"

    def test_all_ok(self):
        """If all checkers are ok, overall should be ok."""
        app = _make_app()
        _startup(app)

        # Register and report ok for two checkers
        for cid, cname in [("zigbee", "Zigbee"), ("zwave", "Z-Wave")]:
            app._on_command("health_check_command", {
                "command": "register_checker",
                "payload": json.dumps({
                    "checker_id": cid,
                    "checker_name": cname,
                    "check_names": ["Check1"],
                }),
            }, {})
            app._on_command("health_check_command", {
                "command": "report_status",
                "payload": json.dumps({
                    "checker_id": cid,
                    "results": [{"name": "Check1", "status": "ok", "detail": "good"}],
                }),
            }, {})

        call_args = app.set_state.call_args
        assert call_args[1]["state"] == "ok"

    def test_one_critical_overall_critical(self):
        """If any checker is critical, overall should be critical."""
        app = _make_app()
        _startup(app)

        # Register two checkers
        for cid in ["zigbee", "zwave"]:
            app._on_command("health_check_command", {
                "command": "register_checker",
                "payload": json.dumps({
                    "checker_id": cid, "checker_name": cid,
                    "check_names": ["Check1"],
                }),
            }, {})

        # zigbee ok, zwave critical
        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "zigbee",
                "results": [{"name": "Check1", "status": "ok", "detail": "good"}],
            }),
        }, {})
        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "zwave",
                "results": [{"name": "Check1", "status": "critical", "detail": "down"}],
            }),
        }, {})

        call_args = app.set_state.call_args
        assert call_args[1]["state"] == "critical"

    def test_mixed_unknown_ok(self):
        """If some checkers are unknown and some ok, overall should be unknown."""
        app = _make_app()
        _startup(app)

        # Register two but only report for one
        for cid in ["zigbee", "zwave"]:
            app._on_command("health_check_command", {
                "command": "register_checker",
                "payload": json.dumps({
                    "checker_id": cid, "checker_name": cid,
                    "check_names": ["Check1"],
                }),
            }, {})

        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "zigbee",
                "results": [{"name": "Check1", "status": "ok", "detail": "good"}],
            }),
        }, {})

        call_args = app.set_state.call_args
        assert call_args[1]["state"] == "unknown"


# ---------------------------------------------------------------------------
# Tests — Payload parsing
# ---------------------------------------------------------------------------

class TestPayloadParsing:
    def test_string_payload(self):
        """Commands with JSON string payload should be parsed."""
        app = _make_app()
        _startup(app)

        app._on_command("health_check_command", {
            "command": "register_checker",
            "payload": json.dumps({
                "checker_id": "test",
                "checker_name": "Test",
                "check_names": ["A"],
            }),
        }, {})

        assert "test" in app._checkers

    def test_dict_payload(self):
        """Commands with dict payload should work directly."""
        app = _make_app()
        _startup(app)

        app._on_command("health_check_command", {
            "command": "register_checker",
            "payload": {
                "checker_id": "test",
                "checker_name": "Test",
                "check_names": ["A"],
            },
        }, {})

        assert "test" in app._checkers

    def test_invalid_json_payload(self):
        """Invalid JSON string payload should be handled gracefully."""
        app = _make_app()
        _startup(app)

        app._on_command("health_check_command", {
            "command": "register_checker",
            "payload": "not-json{{{",
        }, {})

        # Should not crash — checker_id will be empty, rejected
        assert len(app._checkers) == 0
