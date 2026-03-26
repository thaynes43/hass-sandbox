"""Unit tests for HealthCheckController.

Mocks AppDaemon methods and HAProvisioner — no real HA access required.
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

from health_checks.controller.health_check_controller import (
    HealthCheckController,
    HEARTBEAT_ENTITY_ID,
    SENSOR_ENTITY_ID,
    _parse_retention,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DEFAULT_ARGS: Dict[str, Any] = {
    "ha_url": "http://ha:8123",
    "ha_token_env": "TOKEN",
    "heartbeat_interval_s": 60,
    "alert_history_max": 50,
    "alert_retention": "1:12:00:00",
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
             "from_status": "ok", "to_status": "critical", "detail": "down",
             "previous_state_entered": None, "previous_state_duration_s": None}
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

    def test_register_stores_dependencies(self):
        """Registering with dependencies should store them in checker dict."""
        app = _make_app()
        _startup(app)

        deps = [{"checker_id": "mqtt_broker", "affects_checks": ["MQTT Status"]}]
        app._on_command("health_check_command", {
            "command": "register_checker",
            "payload": json.dumps({
                "checker_id": "mqtt_lights",
                "checker_name": "MQTT Lights",
                "check_names": ["MQTT Status", "HA State"],
                "dependencies": deps,
            }),
        }, {})

        assert app._checkers["mqtt_lights"]["dependencies"] == deps

    def test_register_without_dependencies_defaults_empty(self):
        """Registering without dependencies should default to empty list."""
        app = _make_app()
        _startup(app)

        app._on_command("health_check_command", {
            "command": "register_checker",
            "payload": json.dumps({
                "checker_id": "zigbee",
                "checker_name": "Zigbee",
                "check_names": ["Bridge"],
            }),
        }, {})

        assert app._checkers["zigbee"]["dependencies"] == []


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

        # Pre-fill with 5 recent alerts
        now = datetime.datetime.now()
        app._checkers["test"]["alert_history"] = [
            {
                "timestamp": (now - datetime.timedelta(minutes=i)).isoformat(timespec="seconds"),
                "check": "Check1",
                "from_status": "ok",
                "to_status": "critical",
                "detail": f"d{i}",
                "previous_state_entered": None,
                "previous_state_duration_s": None,
            }
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


# ---------------------------------------------------------------------------
# Tests — Repair support
# ---------------------------------------------------------------------------

def _register_repair_checker(app: HealthCheckController, checker_id: str = "spa") -> None:
    """Register a checker that supports repair."""
    app._on_command("health_check_command", {
        "command": "register_checker",
        "payload": json.dumps({
            "checker_id": checker_id,
            "checker_name": "Spa",
            "check_names": ["Ping", "Connection"],
            "supports_repair": True,
        }),
    }, {})


def _register_no_repair_checker(app: HealthCheckController, checker_id: str = "zigbee") -> None:
    """Register a checker that does NOT support repair."""
    app._on_command("health_check_command", {
        "command": "register_checker",
        "payload": json.dumps({
            "checker_id": checker_id,
            "checker_name": "Zigbee",
            "check_names": ["Bridge"],
        }),
    }, {})


class TestRepairRegistration:
    def test_register_with_supports_repair(self):
        """Registering with supports_repair=True should store it."""
        app = _make_app()
        _startup(app)
        _register_repair_checker(app)

        assert app._checkers["spa"]["supports_repair"] is True
        assert app._checkers["spa"]["repair_state"] is None

    def test_register_without_supports_repair(self):
        """Registering without supports_repair should default to False."""
        app = _make_app()
        _startup(app)
        _register_no_repair_checker(app)

        assert app._checkers["zigbee"]["supports_repair"] is False

    def test_supports_repair_published_in_sensor(self):
        """Published sensor attributes should include supports_repair."""
        app = _make_app()
        _startup(app)
        _register_repair_checker(app)

        call_args = app.set_state.call_args
        attrs = call_args[1]["attributes"]
        assert attrs["checkers"]["spa"]["supports_repair"] is True
        assert attrs["checkers"]["spa"]["repair_state"] is None


class TestRepairStateStorage:
    def test_repair_state_stored_on_report(self):
        """repair_state in report_status should be stored and published."""
        app = _make_app()
        _startup(app)
        _register_repair_checker(app)
        app.set_state.reset_mock()

        repair_state = {"status": "pending", "detail": "Auto-repair in 5m"}
        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "spa",
                "results": [
                    {"name": "Ping", "status": "critical", "detail": "timeout"},
                    {"name": "Connection", "status": "ok", "detail": "on"},
                ],
                "repair_state": repair_state,
            }),
        }, {})

        assert app._checkers["spa"]["repair_state"] == repair_state
        call_args = app.set_state.call_args
        attrs = call_args[1]["attributes"]
        assert attrs["checkers"]["spa"]["repair_state"] == repair_state

    def test_repair_state_not_overwritten_without_key(self):
        """report_status without repair_state should not clear existing state."""
        app = _make_app()
        _startup(app)
        _register_repair_checker(app)

        # Set repair state via a report
        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "spa",
                "results": [
                    {"name": "Ping", "status": "ok", "detail": "2ms"},
                    {"name": "Connection", "status": "ok", "detail": "on"},
                ],
                "repair_state": {"status": "success", "detail": "Recovered"},
            }),
        }, {})

        # Report again without repair_state
        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "spa",
                "results": [
                    {"name": "Ping", "status": "ok", "detail": "2ms"},
                    {"name": "Connection", "status": "ok", "detail": "on"},
                ],
            }),
        }, {})

        # repair_state should still be the previous value
        assert app._checkers["spa"]["repair_state"]["status"] == "success"


class TestRepairLifecycleAlertHistory:
    """Tests for repair status transitions recorded in alert history."""

    def test_repair_transition_recorded_in_alert_history(self):
        """Repair transition to in_progress should create an alert history entry."""
        app = _make_app()
        _startup(app)
        _register_repair_checker(app)

        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "spa",
                "results": [
                    {"name": "Ping", "status": "critical", "detail": "timeout"},
                    {"name": "Connection", "status": "ok", "detail": "on"},
                ],
                "repair_state": {"status": "in_progress", "detail": "Power cycling..."},
            }),
        }, {})

        history = app._checkers["spa"]["alert_history"]
        repair_events = [a for a in history if a.get("is_repair_event")]
        assert len(repair_events) == 1
        entry = repair_events[0]
        assert entry["check"] == "Auto-Repair"
        assert entry["from_status"] == "idle"
        assert entry["to_status"] == "in_progress"
        assert entry["detail"] == "Power cycling..."
        assert entry["is_repair_event"] is True

    def test_repair_success_recorded(self):
        """Transition from in_progress to success should be recorded."""
        app = _make_app()
        _startup(app)
        _register_repair_checker(app)

        # First report: idle → in_progress
        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "spa",
                "results": [
                    {"name": "Ping", "status": "critical", "detail": "timeout"},
                    {"name": "Connection", "status": "ok", "detail": "on"},
                ],
                "repair_state": {"status": "in_progress", "detail": "Power cycling..."},
            }),
        }, {})

        # Second report: in_progress → success
        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "spa",
                "results": [
                    {"name": "Ping", "status": "ok", "detail": "2ms"},
                    {"name": "Connection", "status": "ok", "detail": "on"},
                ],
                "repair_state": {"status": "success", "detail": "Recovered after 45s"},
            }),
        }, {})

        history = app._checkers["spa"]["alert_history"]
        repair_events = [a for a in history if a.get("is_repair_event")]
        assert len(repair_events) == 2
        # Most recent first (inserted at index 0)
        assert repair_events[0]["from_status"] == "in_progress"
        assert repair_events[0]["to_status"] == "success"
        assert repair_events[0]["detail"] == "Recovered after 45s"
        assert repair_events[0]["is_repair_event"] is True

    def test_idle_to_idle_not_recorded(self):
        """idle→idle transition should not create a repair alert entry."""
        app = _make_app()
        _startup(app)
        _register_repair_checker(app)

        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "spa",
                "results": [
                    {"name": "Ping", "status": "ok", "detail": "2ms"},
                    {"name": "Connection", "status": "ok", "detail": "on"},
                ],
                "repair_state": {"status": "idle"},
            }),
        }, {})

        history = app._checkers["spa"]["alert_history"]
        repair_events = [a for a in history if a.get("is_repair_event")]
        assert len(repair_events) == 0

    def test_idle_to_pending_not_recorded(self):
        """idle→pending is noise and should not create a repair alert entry."""
        app = _make_app()
        _startup(app)
        _register_repair_checker(app)

        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "spa",
                "results": [
                    {"name": "Ping", "status": "critical", "detail": "timeout"},
                    {"name": "Connection", "status": "ok", "detail": "on"},
                ],
                "repair_state": {"status": "pending", "detail": "Auto-repair in 5m"},
            }),
        }, {})

        history = app._checkers["spa"]["alert_history"]
        repair_events = [a for a in history if a.get("is_repair_event")]
        assert len(repair_events) == 0

    def test_repair_failed_recorded(self):
        """in_progress → failed should be recorded."""
        app = _make_app()
        _startup(app)
        _register_repair_checker(app)

        # Set up in_progress state
        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "spa",
                "results": [
                    {"name": "Ping", "status": "critical", "detail": "timeout"},
                    {"name": "Connection", "status": "ok", "detail": "on"},
                ],
                "repair_state": {"status": "in_progress", "detail": "Power cycling..."},
            }),
        }, {})

        # Transition to failed
        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "spa",
                "results": [
                    {"name": "Ping", "status": "critical", "detail": "timeout"},
                    {"name": "Connection", "status": "ok", "detail": "on"},
                ],
                "repair_state": {"status": "failed", "detail": "Did not recover after 300s"},
            }),
        }, {})

        history = app._checkers["spa"]["alert_history"]
        repair_events = [a for a in history if a.get("is_repair_event")]
        # idle→in_progress and in_progress→failed
        assert len(repair_events) == 2
        assert repair_events[0]["from_status"] == "in_progress"
        assert repair_events[0]["to_status"] == "failed"
        assert repair_events[0]["detail"] == "Did not recover after 300s"


class TestRepairRouting:
    def test_start_repair_fires_targeted_event(self):
        """start_repair should fire health_check_repair_{checker_id}."""
        app = _make_app()
        _startup(app)
        _register_repair_checker(app)
        app.fire_event.reset_mock()

        app._on_command("health_check_command", {
            "command": "start_repair",
            "payload": json.dumps({"checker_id": "spa"}),
        }, {})

        app.fire_event.assert_called_once_with(
            "health_check_repair_spa",
            action="start_repair",
        )

    def test_start_repair_rejects_non_repair_checker(self):
        """start_repair should reject checkers without supports_repair."""
        app = _make_app()
        _startup(app)
        _register_no_repair_checker(app)
        app.fire_event.reset_mock()

        app._on_command("health_check_command", {
            "command": "start_repair",
            "payload": json.dumps({"checker_id": "zigbee"}),
        }, {})

        app.fire_event.assert_not_called()

    def test_start_repair_rejects_unknown_checker(self):
        """start_repair for an unknown checker should be rejected."""
        app = _make_app()
        _startup(app)
        app.fire_event.reset_mock()

        app._on_command("health_check_command", {
            "command": "start_repair",
            "payload": json.dumps({"checker_id": "nonexistent"}),
        }, {})

        app.fire_event.assert_not_called()

    def test_update_repair_config_fires_targeted_event(self):
        """update_repair_config should forward config to the checker."""
        app = _make_app()
        _startup(app)
        _register_repair_checker(app)
        app.fire_event.reset_mock()

        app._on_command("health_check_command", {
            "command": "update_repair_config",
            "payload": json.dumps({
                "checker_id": "spa",
                "auto_repair_enabled": True,
                "auto_repair_delay_min": 10,
            }),
        }, {})

        app.fire_event.assert_called_once_with(
            "health_check_repair_spa",
            action="update_repair_config",
            auto_repair_enabled=True,
            auto_repair_delay_min=10,
        )

    def test_update_repair_config_rejects_non_repair_checker(self):
        """update_repair_config should reject checkers without supports_repair."""
        app = _make_app()
        _startup(app)
        _register_no_repair_checker(app)
        app.fire_event.reset_mock()

        app._on_command("health_check_command", {
            "command": "update_repair_config",
            "payload": json.dumps({
                "checker_id": "zigbee",
                "auto_repair_enabled": True,
                "auto_repair_delay_min": 5,
            }),
        }, {})

        app.fire_event.assert_not_called()


# ---------------------------------------------------------------------------
# Tests — Alert history enhancement (Enhancement 1)
# ---------------------------------------------------------------------------

class TestAlertHistoryEnhancement:
    def test_first_transition_has_null_previous_state(self):
        """First status transition (no prior last_changed) should have null fields."""
        app = _make_app()
        _startup(app)

        app._on_command("health_check_command", {
            "command": "register_checker",
            "payload": json.dumps({
                "checker_id": "test",
                "checker_name": "Test",
                "check_names": ["Bridge"],
            }),
        }, {})

        # Initial checks have last_changed=None; transition from unknown → critical
        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "test",
                "results": [{"name": "Bridge", "status": "critical", "detail": "down"}],
            }),
        }, {})

        alerts = app._checkers["test"]["alert_history"]
        assert len(alerts) == 1
        assert alerts[0]["previous_state_entered"] is None
        assert alerts[0]["previous_state_duration_s"] is None

    def test_second_transition_has_duration(self):
        """Second status transition should have previous_state_entered and duration."""
        app = _make_app()
        _startup(app)

        app._on_command("health_check_command", {
            "command": "register_checker",
            "payload": json.dumps({
                "checker_id": "test",
                "checker_name": "Test",
                "check_names": ["Bridge"],
            }),
        }, {})

        # First report: unknown → ok (no alert, ok is the "good" direction)
        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "test",
                "results": [{"name": "Bridge", "status": "ok", "detail": "up"}],
            }),
        }, {})

        # Second report: ok → critical (alert should have previous_state_entered set)
        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "test",
                "results": [{"name": "Bridge", "status": "critical", "detail": "down"}],
            }),
        }, {})

        alerts = app._checkers["test"]["alert_history"]
        assert len(alerts) == 1
        assert alerts[0]["previous_state_entered"] is not None
        assert alerts[0]["previous_state_duration_s"] is not None
        assert alerts[0]["previous_state_duration_s"] >= 0.0

    def test_alert_entry_has_all_required_fields(self):
        """Alert history entries should include all enhanced fields."""
        app = _make_app()
        _startup(app)

        app._on_command("health_check_command", {
            "command": "register_checker",
            "payload": json.dumps({
                "checker_id": "test",
                "checker_name": "Test",
                "check_names": ["Bridge"],
            }),
        }, {})

        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "test",
                "results": [{"name": "Bridge", "status": "critical", "detail": "down"}],
            }),
        }, {})

        alerts = app._checkers["test"]["alert_history"]
        assert len(alerts) == 1
        entry = alerts[0]
        assert "timestamp" in entry
        assert "check" in entry
        assert "from_status" in entry
        assert "to_status" in entry
        assert "detail" in entry
        assert "previous_state_entered" in entry
        assert "previous_state_duration_s" in entry


# ---------------------------------------------------------------------------
# Tests — Alert retention TTL (Enhancement 2)
# ---------------------------------------------------------------------------

class TestAlertRetentionTTL:
    def test_parse_retention_dd_hh_mm_ss(self):
        """_parse_retention should parse DD:HH:MM:SS format."""
        td = _parse_retention("1:12:00:00")
        assert td.total_seconds() == 129600.0  # 1d 12h

    def test_parse_retention_hh_mm_ss(self):
        """_parse_retention should parse HH:MM:SS format."""
        td = _parse_retention("02:30:00")
        assert td.total_seconds() == 9000.0  # 2.5h

    def test_parse_retention_default_on_bad_input(self):
        """_parse_retention should fall back to default on invalid input."""
        from health_checks.controller.health_check_controller import _DEFAULT_RETENTION
        td = _parse_retention("not-valid")
        assert td == _DEFAULT_RETENTION

    def test_default_retention_applied(self):
        """Controller should use 1:12:00:00 if alert_retention not configured."""
        app = _make_app({"alert_retention": "1:12:00:00"})
        app.initialize()
        assert app._alert_retention.total_seconds() == 129600.0

    def test_old_alerts_pruned_on_report(self):
        """Alerts older than retention period should be pruned on report."""
        app = _make_app({"alert_retention": "00:01:00"})  # 1 minute
        _startup(app)

        app._on_command("health_check_command", {
            "command": "register_checker",
            "payload": json.dumps({
                "checker_id": "test",
                "checker_name": "Test",
                "check_names": ["Check1"],
            }),
        }, {})

        # Plant an old alert (2 minutes ago) and a recent one
        now = datetime.datetime.now()
        old_ts = (now - datetime.timedelta(minutes=2)).isoformat(timespec="seconds")
        recent_ts = (now - datetime.timedelta(seconds=10)).isoformat(timespec="seconds")

        app._checkers["test"]["alert_history"] = [
            {
                "timestamp": recent_ts,
                "check": "Check1", "from_status": "ok", "to_status": "critical",
                "detail": "recent", "previous_state_entered": None,
                "previous_state_duration_s": None,
            },
            {
                "timestamp": old_ts,
                "check": "Check1", "from_status": "ok", "to_status": "critical",
                "detail": "old", "previous_state_entered": None,
                "previous_state_duration_s": None,
            },
        ]

        # Trigger a report (status same → no new alert, but pruning should run)
        # Force a transition so the report code path runs
        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "test",
                "results": [{"name": "Check1", "status": "ok", "detail": "recovered"}],
            }),
        }, {})

        # The old alert should be gone; recent + possibly new transition alert remains
        history = app._checkers["test"]["alert_history"]
        timestamps = [a["timestamp"] for a in history]
        assert old_ts not in timestamps

    def test_max_cap_still_applied_after_ttl_prune(self):
        """alert_history_max cap should still apply after TTL pruning."""
        app = _make_app({"alert_history_max": 2, "alert_retention": "1:00:00:00"})
        _startup(app)

        app._on_command("health_check_command", {
            "command": "register_checker",
            "payload": json.dumps({
                "checker_id": "test",
                "checker_name": "Test",
                "check_names": ["Check1"],
            }),
        }, {})

        now = datetime.datetime.now()
        # 4 recent alerts (all within TTL)
        app._checkers["test"]["alert_history"] = [
            {
                "timestamp": (now - datetime.timedelta(minutes=i)).isoformat(timespec="seconds"),
                "check": "Check1", "from_status": "ok", "to_status": "critical",
                "detail": f"d{i}", "previous_state_entered": None,
                "previous_state_duration_s": None,
            }
            for i in range(4)
        ]

        # Trigger status change to run prune logic
        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "test",
                "results": [{"name": "Check1", "status": "ok", "detail": "ok"}],
            }),
        }, {})

        assert len(app._checkers["test"]["alert_history"]) <= 2


# ---------------------------------------------------------------------------
# Tests — Clear alert history (Enhancement 3)
# ---------------------------------------------------------------------------

class TestClearAlertHistory:
    def _setup_with_alerts(self) -> HealthCheckController:
        app = _make_app()
        _startup(app)

        for cid in ["zigbee", "zwave"]:
            app._on_command("health_check_command", {
                "command": "register_checker",
                "payload": json.dumps({
                    "checker_id": cid,
                    "checker_name": cid.capitalize(),
                    "check_names": ["Bridge"],
                }),
            }, {})
            app._checkers[cid]["alert_history"] = [
                {"timestamp": "2026-01-01T00:00:00", "check": "Bridge",
                 "from_status": "ok", "to_status": "critical", "detail": "d",
                 "previous_state_entered": None, "previous_state_duration_s": None}
            ]

        app.set_state.reset_mock()
        return app

    def test_clear_all_alert_history(self):
        """clear_alert_history without checker_id clears all checkers."""
        app = self._setup_with_alerts()

        app._on_command("health_check_command", {
            "command": "clear_alert_history",
            "payload": "{}",
        }, {})

        for cid in ["zigbee", "zwave"]:
            assert app._checkers[cid]["alert_history"] == []
        # Should publish updated status
        app.set_state.assert_called()

    def test_clear_specific_checker_alert_history(self):
        """clear_alert_history with checker_id clears only that checker."""
        app = self._setup_with_alerts()

        app._on_command("health_check_command", {
            "command": "clear_alert_history",
            "payload": json.dumps({"checker_id": "zigbee"}),
        }, {})

        assert app._checkers["zigbee"]["alert_history"] == []
        # zwave untouched
        assert len(app._checkers["zwave"]["alert_history"]) == 1

    def test_clear_unknown_checker_logs_warning(self):
        """clear_alert_history for unknown checker_id should log a warning."""
        app = _make_app()
        _startup(app)

        app._on_command("health_check_command", {
            "command": "clear_alert_history",
            "payload": json.dumps({"checker_id": "nonexistent"}),
        }, {})

        app.log.assert_any_call(
            "clear_alert_history for unknown checker: 'nonexistent'",
            level="WARNING",
        )

    def test_clear_when_no_alerts_does_not_crash(self):
        """clear_alert_history when history is empty should not crash."""
        app = _make_app()
        _startup(app)

        app._on_command("health_check_command", {
            "command": "register_checker",
            "payload": json.dumps({
                "checker_id": "zigbee",
                "checker_name": "Zigbee",
                "check_names": ["Bridge"],
            }),
        }, {})
        assert app._checkers["zigbee"]["alert_history"] == []

        app._on_command("health_check_command", {
            "command": "clear_alert_history",
            "payload": "{}",
        }, {})

        assert app._checkers["zigbee"]["alert_history"] == []


# ---------------------------------------------------------------------------
# Tests — Warning status level (Enhancement 4)
# ---------------------------------------------------------------------------

class TestWarningStatusLevel:
    def test_warning_status_stored_on_report(self):
        """A warning check should set checker status to warning."""
        app = _make_app()
        _startup(app)

        app._on_command("health_check_command", {
            "command": "register_checker",
            "payload": json.dumps({
                "checker_id": "test",
                "checker_name": "Test",
                "check_names": ["Check1", "Check2"],
            }),
        }, {})

        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "test",
                "results": [
                    {"name": "Check1", "status": "warning", "detail": "slightly off"},
                    {"name": "Check2", "status": "ok", "detail": "good"},
                ],
            }),
        }, {})

        assert app._checkers["test"]["status"] == "warning"

    def test_warning_published_in_sensor(self):
        """Warning overall status should appear in the sensor state."""
        app = _make_app()
        _startup(app)

        app._on_command("health_check_command", {
            "command": "register_checker",
            "payload": json.dumps({
                "checker_id": "test",
                "checker_name": "Test",
                "check_names": ["Check1"],
            }),
        }, {})
        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "test",
                "results": [{"name": "Check1", "status": "warning", "detail": "warn"}],
            }),
        }, {})

        call_args = app.set_state.call_args
        assert call_args[1]["state"] == "warning"

    def test_warning_does_not_override_degraded(self):
        """Warning should not override degraded status."""
        app = _make_app()
        _startup(app)

        app._on_command("health_check_command", {
            "command": "register_checker",
            "payload": json.dumps({
                "checker_id": "test",
                "checker_name": "Test",
                "check_names": ["Check1", "Check2"],
            }),
        }, {})

        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "test",
                "results": [
                    {"name": "Check1", "status": "degraded", "detail": "degraded"},
                    {"name": "Check2", "status": "warning", "detail": "warn"},
                ],
            }),
        }, {})

        assert app._checkers["test"]["status"] == "degraded"

    def test_warning_does_not_override_critical(self):
        """Warning should not override critical status."""
        app = _make_app()
        _startup(app)

        app._on_command("health_check_command", {
            "command": "register_checker",
            "payload": json.dumps({
                "checker_id": "test",
                "checker_name": "Test",
                "check_names": ["Check1", "Check2"],
            }),
        }, {})

        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "test",
                "results": [
                    {"name": "Check1", "status": "critical", "detail": "down"},
                    {"name": "Check2", "status": "warning", "detail": "warn"},
                ],
            }),
        }, {})

        assert app._checkers["test"]["status"] == "critical"

    def test_severity_ordering_ok_lt_warning_lt_degraded_lt_critical(self):
        """Severity ordering: ok < warning < degraded < critical."""
        app = _make_app()
        _startup(app)

        app._on_command("health_check_command", {
            "command": "register_checker",
            "payload": json.dumps({
                "checker_id": "test",
                "checker_name": "Test",
                "check_names": ["C1", "C2", "C3", "C4"],
            }),
        }, {})

        # All four statuses present — critical should win
        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "test",
                "results": [
                    {"name": "C1", "status": "ok", "detail": ""},
                    {"name": "C2", "status": "warning", "detail": ""},
                    {"name": "C3", "status": "degraded", "detail": ""},
                    {"name": "C4", "status": "critical", "detail": ""},
                ],
            }),
        }, {})
        assert app._checkers["test"]["status"] == "critical"

        # Remove critical — degraded should win
        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "test",
                "results": [
                    {"name": "C1", "status": "ok", "detail": ""},
                    {"name": "C2", "status": "warning", "detail": ""},
                    {"name": "C3", "status": "degraded", "detail": ""},
                    {"name": "C4", "status": "ok", "detail": ""},
                ],
            }),
        }, {})
        assert app._checkers["test"]["status"] == "degraded"

        # Remove degraded — warning should win
        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "test",
                "results": [
                    {"name": "C1", "status": "ok", "detail": ""},
                    {"name": "C2", "status": "warning", "detail": ""},
                    {"name": "C3", "status": "ok", "detail": ""},
                    {"name": "C4", "status": "ok", "detail": ""},
                ],
            }),
        }, {})
        assert app._checkers["test"]["status"] == "warning"

    def test_warning_overall_status_between_ok_and_degraded(self):
        """Overall sensor state with one warning checker and one ok checker is warning."""
        app = _make_app()
        _startup(app)

        for cid in ["a", "b"]:
            app._on_command("health_check_command", {
                "command": "register_checker",
                "payload": json.dumps({
                    "checker_id": cid, "checker_name": cid,
                    "check_names": ["C"],
                }),
            }, {})

        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "a",
                "results": [{"name": "C", "status": "ok", "detail": ""}],
            }),
        }, {})
        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "b",
                "results": [{"name": "C", "status": "warning", "detail": ""}],
            }),
        }, {})

        call_args = app.set_state.call_args
        assert call_args[1]["state"] == "warning"


# ---------------------------------------------------------------------------
# Tests — Dependency system (Enhancement 5)
# ---------------------------------------------------------------------------

class TestDependencySystem:
    def _register_broker(self, app: HealthCheckController) -> None:
        app._on_command("health_check_command", {
            "command": "register_checker",
            "payload": json.dumps({
                "checker_id": "mqtt_broker",
                "checker_name": "MQTT Broker",
                "check_names": ["Broker Status"],
            }),
        }, {})

    def _register_lights(
        self, app: HealthCheckController, affects: list | None = None
    ) -> None:
        deps = [{"checker_id": "mqtt_broker"}]
        if affects is not None:
            deps = [{"checker_id": "mqtt_broker", "affects_checks": affects}]
        app._on_command("health_check_command", {
            "command": "register_checker",
            "payload": json.dumps({
                "checker_id": "mqtt_lights",
                "checker_name": "MQTT Lights",
                "check_names": ["HA State", "MQTT Status"],
                "dependencies": deps,
            }),
        }, {})

    def test_registration_stores_dependencies(self):
        """Dependencies should be stored in the checker dict at registration."""
        app = _make_app()
        _startup(app)
        self._register_broker(app)
        self._register_lights(app, affects=["MQTT Status"])

        deps = app._checkers["mqtt_lights"]["dependencies"]
        assert len(deps) == 1
        assert deps[0]["checker_id"] == "mqtt_broker"
        assert deps[0]["affects_checks"] == ["MQTT Status"]

    def test_published_view_overrides_when_dependency_down(self):
        """When dependency is critical, affected checks published as unknown."""
        app = _make_app()
        _startup(app)
        self._register_broker(app)
        self._register_lights(app, affects=["MQTT Status"])

        # Broker goes critical
        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "mqtt_broker",
                "results": [{"name": "Broker Status", "status": "critical", "detail": "down"}],
            }),
        }, {})

        # Lights report both ok
        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "mqtt_lights",
                "results": [
                    {"name": "HA State", "status": "ok", "detail": "on"},
                    {"name": "MQTT Status", "status": "ok", "detail": "ok"},
                ],
            }),
        }, {})

        # Published: MQTT Status should be unknown, HA State unaffected
        call_args = app.set_state.call_args
        attrs = call_args[1]["attributes"]
        checks = {c["name"]: c for c in attrs["checkers"]["mqtt_lights"]["checks"]}
        assert checks["MQTT Status"]["status"] == "unknown"
        assert checks["MQTT Status"]["detail"] == "dependency unavailable: MQTT Broker"
        assert checks["HA State"]["status"] == "ok"

    def test_internal_state_not_modified_by_dependency(self):
        """Internal _checkers state must not be changed by dependency resolution."""
        app = _make_app()
        _startup(app)
        self._register_broker(app)
        self._register_lights(app, affects=["MQTT Status"])

        # Broker down
        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "mqtt_broker",
                "results": [{"name": "Broker Status", "status": "critical", "detail": "down"}],
            }),
        }, {})

        # Lights ok
        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "mqtt_lights",
                "results": [
                    {"name": "HA State", "status": "ok", "detail": "on"},
                    {"name": "MQTT Status", "status": "ok", "detail": "ok"},
                ],
            }),
        }, {})

        # Internal state should still show ok
        internal_checks = {
            c["name"]: c for c in app._checkers["mqtt_lights"]["checks"]
        }
        assert internal_checks["MQTT Status"]["status"] == "ok"
        assert app._checkers["mqtt_lights"]["status"] == "ok"

    def test_all_checks_affected_when_affects_checks_omitted(self):
        """When affects_checks is omitted, all checks should be overridden."""
        app = _make_app()
        _startup(app)
        self._register_broker(app)
        # No affects_checks — all checks affected
        self._register_lights(app, affects=None)

        # Broker down
        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "mqtt_broker",
                "results": [{"name": "Broker Status", "status": "critical", "detail": "down"}],
            }),
        }, {})

        # Lights report ok
        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "mqtt_lights",
                "results": [
                    {"name": "HA State", "status": "ok", "detail": "on"},
                    {"name": "MQTT Status", "status": "ok", "detail": "ok"},
                ],
            }),
        }, {})

        call_args = app.set_state.call_args
        attrs = call_args[1]["attributes"]
        checks = {c["name"]: c for c in attrs["checkers"]["mqtt_lights"]["checks"]}
        # Both checks affected
        assert checks["HA State"]["status"] == "unknown"
        assert checks["MQTT Status"]["status"] == "unknown"

    def test_partial_dependency_only_affects_named_checks(self):
        """Partial dependency (affects_checks set) should only affect named checks."""
        app = _make_app()
        _startup(app)
        self._register_broker(app)
        # Only MQTT Status affected
        self._register_lights(app, affects=["MQTT Status"])

        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "mqtt_broker",
                "results": [{"name": "Broker Status", "status": "critical", "detail": "down"}],
            }),
        }, {})
        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "mqtt_lights",
                "results": [
                    {"name": "HA State", "status": "ok", "detail": "on"},
                    {"name": "MQTT Status", "status": "ok", "detail": "ok"},
                ],
            }),
        }, {})

        call_args = app.set_state.call_args
        attrs = call_args[1]["attributes"]
        checks = {c["name"]: c for c in attrs["checkers"]["mqtt_lights"]["checks"]}
        assert checks["HA State"]["status"] == "ok"
        assert checks["MQTT Status"]["status"] == "unknown"

    def test_missing_dependency_checker_treated_as_unhealthy(self):
        """A dependency on a checker not yet registered should override affected checks."""
        app = _make_app()
        _startup(app)

        # Register lights with dep on a broker that hasn't registered yet
        app._on_command("health_check_command", {
            "command": "register_checker",
            "payload": json.dumps({
                "checker_id": "mqtt_lights",
                "checker_name": "MQTT Lights",
                "check_names": ["HA State", "MQTT Status"],
                "dependencies": [
                    {"checker_id": "mqtt_broker", "affects_checks": ["MQTT Status"]}
                ],
            }),
        }, {})

        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "mqtt_lights",
                "results": [
                    {"name": "HA State", "status": "ok", "detail": "on"},
                    {"name": "MQTT Status", "status": "ok", "detail": "ok"},
                ],
            }),
        }, {})

        call_args = app.set_state.call_args
        attrs = call_args[1]["attributes"]
        checks = {c["name"]: c for c in attrs["checkers"]["mqtt_lights"]["checks"]}
        # mqtt_broker not registered → treated as unhealthy
        assert checks["MQTT Status"]["status"] == "unknown"

    def test_dependency_recovery_restores_real_status(self):
        """When dependency recovers to ok, affected checks return to real status."""
        app = _make_app()
        _startup(app)
        self._register_broker(app)
        self._register_lights(app, affects=["MQTT Status"])

        # Broker down
        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "mqtt_broker",
                "results": [{"name": "Broker Status", "status": "critical", "detail": "down"}],
            }),
        }, {})
        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "mqtt_lights",
                "results": [
                    {"name": "HA State", "status": "ok", "detail": "on"},
                    {"name": "MQTT Status", "status": "ok", "detail": "ok"},
                ],
            }),
        }, {})

        # Verify override is active
        attrs_down = app.set_state.call_args[1]["attributes"]
        checks_down = {c["name"]: c for c in attrs_down["checkers"]["mqtt_lights"]["checks"]}
        assert checks_down["MQTT Status"]["status"] == "unknown"

        # Broker recovers
        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "mqtt_broker",
                "results": [{"name": "Broker Status", "status": "ok", "detail": "up"}],
            }),
        }, {})

        # Now the published state should show the real status
        attrs_up = app.set_state.call_args[1]["attributes"]
        checks_up = {c["name"]: c for c in attrs_up["checkers"]["mqtt_lights"]["checks"]}
        assert checks_up["MQTT Status"]["status"] == "ok"

    def test_warning_dependency_does_not_override(self):
        """A dependency with warning status should NOT override affected checks."""
        app = _make_app()
        _startup(app)
        self._register_broker(app)
        self._register_lights(app, affects=["MQTT Status"])

        # Broker is warning (acceptable — dependency satisfied)
        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "mqtt_broker",
                "results": [{"name": "Broker Status", "status": "warning", "detail": "slow"}],
            }),
        }, {})
        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "mqtt_lights",
                "results": [
                    {"name": "HA State", "status": "ok", "detail": "on"},
                    {"name": "MQTT Status", "status": "ok", "detail": "ok"},
                ],
            }),
        }, {})

        call_args = app.set_state.call_args
        attrs = call_args[1]["attributes"]
        checks = {c["name"]: c for c in attrs["checkers"]["mqtt_lights"]["checks"]}
        # warning dependency should not suppress real status
        assert checks["MQTT Status"]["status"] == "ok"

    def test_dependency_system_is_generic(self):
        """Adding a new dependency chain requires no controller code changes."""
        # This test validates the generic design by wiring up a completely new
        # dependency chain (internet_checker → cloud_integration_checker) that
        # was never anticipated in the controller code, and confirming it works.
        app = _make_app()
        _startup(app)

        # Register internet checker
        app._on_command("health_check_command", {
            "command": "register_checker",
            "payload": json.dumps({
                "checker_id": "internet_checker",
                "checker_name": "Internet",
                "check_names": ["External Ping"],
            }),
        }, {})

        # Register cloud integration checker with dependency on internet
        app._on_command("health_check_command", {
            "command": "register_checker",
            "payload": json.dumps({
                "checker_id": "cloud_integration",
                "checker_name": "Cloud Integration",
                "check_names": ["API Health", "Local Fallback"],
                "dependencies": [
                    {"checker_id": "internet_checker", "affects_checks": ["API Health"]}
                ],
            }),
        }, {})

        # Internet goes down
        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "internet_checker",
                "results": [{"name": "External Ping", "status": "critical", "detail": "no route"}],
            }),
        }, {})
        app._on_command("health_check_command", {
            "command": "report_status",
            "payload": json.dumps({
                "checker_id": "cloud_integration",
                "results": [
                    {"name": "API Health", "status": "ok", "detail": "ok"},
                    {"name": "Local Fallback", "status": "ok", "detail": "ok"},
                ],
            }),
        }, {})

        attrs = app.set_state.call_args[1]["attributes"]
        checks = {
            c["name"]: c
            for c in attrs["checkers"]["cloud_integration"]["checks"]
        }
        assert checks["API Health"]["status"] == "unknown"
        assert checks["Local Fallback"]["status"] == "ok"

    def test_is_dependency_flag_published_correctly(self):
        """checker_a is referenced by checker_b → is_dependency=True; checker_b is not."""
        app = _make_app()
        _startup(app)

        # Register checker_a (will be a dependency of checker_b)
        app._on_command("health_check_command", {
            "command": "register_checker",
            "payload": json.dumps({
                "checker_id": "checker_a",
                "checker_name": "Checker A",
                "check_names": ["Check A"],
            }),
        }, {})

        # Register checker_b with a dependency on checker_a
        app._on_command("health_check_command", {
            "command": "register_checker",
            "payload": json.dumps({
                "checker_id": "checker_b",
                "checker_name": "Checker B",
                "check_names": ["Check B"],
                "dependencies": [{"checker_id": "checker_a"}],
            }),
        }, {})

        call_args = app.set_state.call_args
        attrs = call_args[1]["attributes"]
        assert attrs["checkers"]["checker_a"]["is_dependency"] is True
        assert attrs["checkers"]["checker_b"]["is_dependency"] is False

    def test_is_dependency_false_when_no_checker_depends_on_it(self):
        """A checker not referenced by any other checker has is_dependency=False."""
        app = _make_app()
        _startup(app)

        app._on_command("health_check_command", {
            "command": "register_checker",
            "payload": json.dumps({
                "checker_id": "standalone",
                "checker_name": "Standalone",
                "check_names": ["Check S"],
            }),
        }, {})

        call_args = app.set_state.call_args
        attrs = call_args[1]["attributes"]
        assert attrs["checkers"]["standalone"]["is_dependency"] is False
