"""Unit tests for BatteryChecker."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

mock_hass = MagicMock()
mock_hass.Hass = type("_MockHass", (), {"__init__": lambda self, *a, **kw: None})
sys.modules["hassapi"] = mock_hass

_repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo_root / "apps"))
sys.path.insert(0, str(_repo_root))

from health_checks.checker_apps.battery_checker.battery_checker import (
    BatteryChecker,
)


DEFAULT_ARGS: Dict[str, Any] = {
    "checker_id": "test_batteries",
    "checker_name": "Test Batteries",
    "check_interval_s": 300,
    "warning_threshold": 20,
    "critical_threshold": 10,
    "sensors": [
        {"entity_id": "sensor.device_a_battery", "name": "Device A"},
        {"entity_id": "sensor.device_b_battery", "name": "Device B"},
    ],
}


def _make_app(extra_args: dict | None = None) -> BatteryChecker:
    ad = MagicMock()
    config = MagicMock()
    app = BatteryChecker(ad, config)

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


def _startup(app: BatteryChecker) -> None:
    app.initialize()
    _run(app._async_startup())


def _init_only(app: BatteryChecker) -> None:
    app.initialize()


# ------------------------------------------------------------------
# Lifecycle
# ------------------------------------------------------------------


class TestLifecycle:
    def test_initialize_calls_run_in(self):
        app = _make_app()
        app.initialize()
        app.run_in.assert_called_once()

    def test_startup_registers_event_listeners(self):
        app = _make_app()
        _startup(app)
        event_names = [c[0][1] for c in app.listen_event.call_args_list]
        assert "health_check_controller_ready" in event_names
        assert "health_check_recheck" in event_names

    def test_startup_schedules_first_check(self):
        app = _make_app()
        _startup(app)
        # run_in called twice: once in initialize (for _on_startup), once in _async_startup (for _first_check)
        assert app.run_in.call_count == 2

    def test_first_check_runs_checks_and_starts_timer(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="85")
        app._first_check({})
        # Should have fired report_status
        report_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
        ]
        assert len(report_calls) == 1
        # Should have started run_every
        app.run_every.assert_called_once()


# ------------------------------------------------------------------
# Registration
# ------------------------------------------------------------------


class TestRegistration:
    def test_registers_correct_check_names(self):
        app = _make_app()
        _startup(app)
        register_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "register_checker"
        ]
        assert len(register_calls) >= 1
        payload = json.loads(register_calls[0][1]["payload"])
        assert payload["check_names"] == ["Device A", "Device B"]

    def test_registers_per_sensor_dependencies_grouped(self):
        app = _make_app({
            "sensors": [
                {"entity_id": "sensor.a_battery", "name": "Device A", "dependency": "zwave"},
                {"entity_id": "sensor.b_battery", "name": "Device B", "dependency": "zwave"},
                {"entity_id": "sensor.c_battery", "name": "Device C", "dependency": "zigbee"},
            ],
        })
        _startup(app)
        register_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "register_checker"
        ]
        payload = json.loads(register_calls[0][1]["payload"])
        deps = payload["dependencies"]
        dep_ids = {d["checker_id"] for d in deps}
        assert dep_ids == {"zwave", "zigbee"}
        zwave_dep = next(d for d in deps if d["checker_id"] == "zwave")
        assert set(zwave_dep["affects_checks"]) == {"Device A", "Device B"}
        zigbee_dep = next(d for d in deps if d["checker_id"] == "zigbee")
        assert zigbee_dep["affects_checks"] == ["Device C"]

    def test_health_dependencies_included(self):
        app = _make_app({
            "health_dependencies": ["mqtt_broker"],
            "sensors": [
                {"entity_id": "sensor.a_battery", "name": "Device A"},
                {"entity_id": "sensor.b_battery", "name": "Device B"},
            ],
        })
        _startup(app)
        register_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "register_checker"
        ]
        payload = json.loads(register_calls[0][1]["payload"])
        deps = payload["dependencies"]
        assert len(deps) == 1
        assert deps[0]["checker_id"] == "mqtt_broker"
        assert set(deps[0]["affects_checks"]) == {"Device A", "Device B"}

    def test_health_dependencies_combined_with_per_sensor(self):
        app = _make_app({
            "health_dependencies": ["mqtt_broker"],
            "sensors": [
                {"entity_id": "sensor.a_battery", "name": "Device A", "dependency": "zwave"},
                {"entity_id": "sensor.b_battery", "name": "Device B"},
            ],
        })
        _startup(app)
        register_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "register_checker"
        ]
        payload = json.loads(register_calls[0][1]["payload"])
        deps = payload["dependencies"]
        dep_ids = {d["checker_id"] for d in deps}
        assert dep_ids == {"mqtt_broker", "zwave"}
        mqtt_dep = next(d for d in deps if d["checker_id"] == "mqtt_broker")
        assert set(mqtt_dep["affects_checks"]) == {"Device A", "Device B"}
        zwave_dep = next(d for d in deps if d["checker_id"] == "zwave")
        assert zwave_dep["affects_checks"] == ["Device A"]

    def test_no_dependencies_omits_key(self):
        app = _make_app({
            "sensors": [
                {"entity_id": "sensor.test_battery", "name": "Test Sensor"},
            ],
        })
        _startup(app)
        register_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "register_checker"
        ]
        payload = json.loads(register_calls[0][1]["payload"])
        assert "dependencies" not in payload

    def test_checker_id_and_name(self):
        app = _make_app()
        _startup(app)
        register_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "register_checker"
        ]
        payload = json.loads(register_calls[0][1]["payload"])
        assert payload["checker_id"] == "test_batteries"
        assert payload["checker_name"] == "Test Batteries"


# ------------------------------------------------------------------
# Threshold evaluation
# ------------------------------------------------------------------


class TestThresholdEvaluation:
    """Test _evaluate_sensor with default thresholds: warning=20, critical=10."""

    def test_ok_above_warning(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="85")
        status, detail = app._evaluate_sensor(app._sensors[0])
        assert status == "ok"
        assert "85%" in detail

    def test_warning_between_warn_and_crit(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="15")
        status, detail = app._evaluate_sensor(app._sensors[0])
        assert status == "warning"
        assert "15%" in detail
        assert "warning" in detail

    def test_critical_below_crit(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="5")
        status, detail = app._evaluate_sensor(app._sensors[0])
        assert status == "critical"
        assert "5%" in detail
        assert "critical" in detail

    def test_exactly_at_warning_threshold_is_warning(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="20")
        status, _ = app._evaluate_sensor(app._sensors[0])
        assert status == "warning"

    def test_exactly_at_critical_threshold_is_critical(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="10")
        status, _ = app._evaluate_sensor(app._sensors[0])
        assert status == "critical"

    def test_just_above_warning_is_ok(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="21")
        status, _ = app._evaluate_sensor(app._sensors[0])
        assert status == "ok"

    def test_unavailable_state(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="unavailable")
        status, detail = app._evaluate_sensor(app._sensors[0])
        assert status == "critical"
        assert "unavailable" in detail

    def test_unknown_state(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="unknown")
        status, detail = app._evaluate_sensor(app._sensors[0])
        assert status == "critical"
        assert "unknown" in detail

    def test_none_state(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value=None)
        status, detail = app._evaluate_sensor(app._sensors[0])
        assert status == "critical"
        assert "not found" in detail

    def test_non_numeric_state(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="error")
        status, detail = app._evaluate_sensor(app._sensors[0])
        assert status == "critical"
        assert "non-numeric" in detail

    def test_exception_reading_state(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(side_effect=RuntimeError("connection lost"))
        status, detail = app._evaluate_sensor(app._sensors[0])
        assert status == "critical"
        assert "error reading state" in detail

    def test_per_sensor_threshold_override(self):
        app = _make_app({
            "sensors": [
                {
                    "entity_id": "sensor.override_battery",
                    "name": "Override Device",
                    "warning_threshold": 30,
                    "critical_threshold": 15,
                },
            ],
        })
        _init_only(app)
        # 25 is ok with defaults (warning=20) but warning with override (warning=30)
        app.get_state = MagicMock(return_value="25")
        status, detail = app._evaluate_sensor(app._sensors[0])
        assert status == "warning"

    def test_per_sensor_inherits_defaults_for_unset_keys(self):
        app = _make_app({
            "sensors": [
                {
                    "entity_id": "sensor.partial_battery",
                    "name": "Partial Override",
                    "warning_threshold": 30,
                    # critical_threshold should use default (10)
                },
            ],
        })
        _init_only(app)
        sensor = app._sensors[0]
        assert sensor["warning_threshold"] == 30.0
        assert sensor["critical_threshold"] == 10.0  # default


# ------------------------------------------------------------------
# Run checks (full cycle)
# ------------------------------------------------------------------


class TestRunChecks:
    def test_reports_all_results(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="85")
        app._run_checks()

        report_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
        ]
        assert len(report_calls) == 1
        payload = json.loads(report_calls[0][1]["payload"])
        assert payload["checker_id"] == "test_batteries"
        assert len(payload["results"]) == 2

    def test_mixed_statuses(self):
        app = _make_app()
        _init_only(app)
        # Sensor 1: ok (85%), Sensor 2: critical (unavailable)
        app.get_state = MagicMock(side_effect=["85", "unavailable"])
        app._run_checks()

        report_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
        ]
        payload = json.loads(report_calls[0][1]["payload"])
        results = payload["results"]
        assert results[0]["status"] == "ok"
        assert results[1]["status"] == "critical"

    def test_logs_warning_when_issues_found(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="unavailable")
        app._run_checks()
        warning_calls = [
            c for c in app.log.call_args_list
            if c[1].get("level") == "WARNING"
        ]
        assert len(warning_calls) >= 1

    def test_logs_info_when_all_ok(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="85")
        app._run_checks()
        # The "Check complete" log should be at INFO level
        info_calls = [
            c for c in app.log.call_args_list
            if c[1].get("level") == "INFO" and "Check complete" in str(c[0])
        ]
        assert len(info_calls) == 1


# ------------------------------------------------------------------
# Event handlers
# ------------------------------------------------------------------


class TestForceRecheck:
    def test_recheck_runs_checks(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="85")
        app._on_recheck("health_check_recheck", {}, {})

        report_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
        ]
        assert len(report_calls) == 1


class TestControllerReady:
    def test_re_registers_and_runs_checks(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="85")
        app._on_controller_ready("health_check_controller_ready", {}, {})

        register_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "register_checker"
        ]
        report_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
        ]
        assert len(register_calls) == 1
        assert len(report_calls) == 1


# ------------------------------------------------------------------
# Edge cases
# ------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_sensors_list(self):
        app = _make_app({"sensors": []})
        _init_only(app)
        app._run_checks()
        report_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
        ]
        payload = json.loads(report_calls[0][1]["payload"])
        assert payload["results"] == []

    def test_default_checker_id(self):
        app = _make_app({"checker_id": None, "checker_name": None})
        del app.args["checker_id"]
        del app.args["checker_name"]
        _init_only(app)
        assert app._checker_id == "batteries"
        assert app._checker_name == "Batteries"

    def test_integer_state_parsed(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="50")
        status, _ = app._evaluate_sensor(app._sensors[0])
        assert status == "ok"

    def test_float_state_parsed(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="99.5")
        status, detail = app._evaluate_sensor(app._sensors[0])
        assert status == "ok"
        assert "100%" in detail  # {99.5:.0f} rounds to 100
