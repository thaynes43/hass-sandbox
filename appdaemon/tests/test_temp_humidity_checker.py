"""Unit tests for TempHumidityChecker."""

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

from health_checks.checker_apps.temp_humidity_checker.temp_humidity_checker import (
    TempHumidityChecker,
)


DEFAULT_ARGS: Dict[str, Any] = {
    "checker_id": "cigar_humidity",
    "checker_name": "Cigar Room Humidity",
    "check_interval_s": 120,
    "humidity_low_warning": 62,
    "humidity_high_warning": 68,
    "humidity_low_critical": 60,
    "humidity_high_critical": 70,
    "sensors": [
        {
            "entity_id": "sensor.cigar_humidity_sensor_01_humidity",
            "name": "Cigar Sensor 01",
            "type": "humidity",
            "dependency": "zwave",
        },
        {
            "entity_id": "sensor.cigar_humidity_sensor_02_humidity",
            "name": "Cigar Sensor 02",
            "type": "humidity",
            "dependency": "zwave",
        },
        {
            "entity_id": "sensor.basement_aqara_w100_01_humidity",
            "name": "Aqara W100 01",
            "type": "humidity",
            "dependency": "zigbee",
        },
    ],
}


def _make_app(extra_args: dict | None = None) -> TempHumidityChecker:
    ad = MagicMock()
    config = MagicMock()
    app = TempHumidityChecker(ad, config)

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


def _startup(app: TempHumidityChecker) -> None:
    app.initialize()
    _run(app._async_startup())


def _init_only(app: TempHumidityChecker) -> None:
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
        app.get_state = MagicMock(return_value="65.0")
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
        assert payload["check_names"] == [
            "Cigar Sensor 01",
            "Cigar Sensor 02",
            "Aqara W100 01",
        ]

    def test_registers_dependencies_grouped(self):
        app = _make_app()
        _startup(app)
        register_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "register_checker"
        ]
        payload = json.loads(register_calls[0][1]["payload"])
        deps = payload["dependencies"]
        # Should have two dependency groups: zwave and zigbee
        dep_ids = {d["checker_id"] for d in deps}
        assert dep_ids == {"zwave", "zigbee"}
        # zwave should affect both cigar sensors
        zwave_dep = next(d for d in deps if d["checker_id"] == "zwave")
        assert set(zwave_dep["affects_checks"]) == {"Cigar Sensor 01", "Cigar Sensor 02"}
        # zigbee should affect aqara sensor
        zigbee_dep = next(d for d in deps if d["checker_id"] == "zigbee")
        assert zigbee_dep["affects_checks"] == ["Aqara W100 01"]

    def test_no_dependencies_omits_key(self):
        app = _make_app({
            "sensors": [
                {
                    "entity_id": "sensor.test_humidity",
                    "name": "Test Sensor",
                    "type": "humidity",
                },
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
        assert payload["checker_id"] == "cigar_humidity"
        assert payload["checker_name"] == "Cigar Room Humidity"


# ------------------------------------------------------------------
# Threshold evaluation
# ------------------------------------------------------------------


class TestThresholdEvaluation:
    """Test _evaluate_sensor with default humidity thresholds: 60/62/68/70."""

    def test_ok_in_range(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="65.0")
        status, detail = app._evaluate_sensor(app._sensors[0])
        assert status == "ok"
        assert "65.0" in detail

    def test_ok_at_low_warning_boundary(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="62.0")
        status, _ = app._evaluate_sensor(app._sensors[0])
        assert status == "ok"

    def test_ok_at_high_warning_boundary(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="68.0")
        status, _ = app._evaluate_sensor(app._sensors[0])
        assert status == "ok"

    def test_warning_below_low_warning(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="61.0")
        status, detail = app._evaluate_sensor(app._sensors[0])
        assert status == "warning"
        assert "below warning" in detail

    def test_warning_above_high_warning(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="69.0")
        status, detail = app._evaluate_sensor(app._sensors[0])
        assert status == "warning"
        assert "above warning" in detail

    def test_critical_below_low_critical(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="59.0")
        status, detail = app._evaluate_sensor(app._sensors[0])
        assert status == "critical"
        assert "below critical" in detail

    def test_critical_above_high_critical(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="71.0")
        status, detail = app._evaluate_sensor(app._sensors[0])
        assert status == "critical"
        assert "above critical" in detail

    def test_critical_at_critical_boundary(self):
        """Value exactly at critical boundary is within critical, so warning."""
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="60.0")
        status, _ = app._evaluate_sensor(app._sensors[0])
        # 60.0 == low_crit, so not < low_crit, not > high_crit,
        # but < low_warn (62), so warning
        assert status == "warning"

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


# ------------------------------------------------------------------
# Temperature type
# ------------------------------------------------------------------


class TestTemperatureType:
    def test_temperature_ok(self):
        app = _make_app({
            "temp_low_warning": 60,
            "temp_high_warning": 70,
            "temp_low_critical": 58,
            "temp_high_critical": 72,
            "sensors": [
                {
                    "entity_id": "sensor.temp_01",
                    "name": "Temp 01",
                    "type": "temperature",
                },
            ],
        })
        _init_only(app)
        app.get_state = MagicMock(return_value="65.0")
        status, detail = app._evaluate_sensor(app._sensors[0])
        assert status == "ok"
        assert "temperature" in detail

    def test_temperature_warning(self):
        app = _make_app({
            "temp_low_warning": 60,
            "temp_high_warning": 70,
            "temp_low_critical": 58,
            "temp_high_critical": 72,
            "sensors": [
                {
                    "entity_id": "sensor.temp_01",
                    "name": "Temp 01",
                    "type": "temperature",
                },
            ],
        })
        _init_only(app)
        app.get_state = MagicMock(return_value="71.0")
        status, detail = app._evaluate_sensor(app._sensors[0])
        assert status == "warning"
        assert "temperature" in detail


# ------------------------------------------------------------------
# Both type
# ------------------------------------------------------------------


class TestBothType:
    def test_both_ok(self):
        app = _make_app({
            "temp_low_warning": 60,
            "temp_high_warning": 80,
            "temp_low_critical": 50,
            "temp_high_critical": 90,
            "humidity_low_warning": 60,
            "humidity_high_warning": 80,
            "humidity_low_critical": 50,
            "humidity_high_critical": 90,
            "sensors": [
                {
                    "entity_id": "sensor.both_01",
                    "name": "Both 01",
                    "type": "both",
                },
            ],
        })
        _init_only(app)
        app.get_state = MagicMock(return_value="70.0")
        status, _ = app._evaluate_sensor(app._sensors[0])
        assert status == "ok"

    def test_both_returns_worst_status(self):
        """When temp is ok but humidity is critical, return critical."""
        app = _make_app({
            "temp_low_warning": 10,
            "temp_high_warning": 90,
            "temp_low_critical": 5,
            "temp_high_critical": 95,
            "humidity_low_warning": 62,
            "humidity_high_warning": 68,
            "humidity_low_critical": 60,
            "humidity_high_critical": 70,
            "sensors": [
                {
                    "entity_id": "sensor.both_01",
                    "name": "Both 01",
                    "type": "both",
                },
            ],
        })
        _init_only(app)
        # 50.0: temp ok (10-90), humidity critical (<60)
        app.get_state = MagicMock(return_value="50.0")
        status, detail = app._evaluate_sensor(app._sensors[0])
        assert status == "critical"
        assert "humidity" in detail


# ------------------------------------------------------------------
# Per-sensor overrides
# ------------------------------------------------------------------


class TestPerSensorOverrides:
    def test_sensor_override_applied(self):
        app = _make_app({
            "humidity_low_warning": 62,
            "humidity_high_warning": 68,
            "humidity_low_critical": 60,
            "humidity_high_critical": 70,
            "sensors": [
                {
                    "entity_id": "sensor.override_01",
                    "name": "Override Sensor",
                    "type": "humidity",
                    "humidity_low_warning": 64,
                    "humidity_high_warning": 66,
                },
            ],
        })
        _init_only(app)
        # 63 is ok with defaults (62-68) but warning with override (64-66)
        app.get_state = MagicMock(return_value="63.0")
        status, detail = app._evaluate_sensor(app._sensors[0])
        assert status == "warning"
        assert "below warning" in detail

    def test_sensor_inherits_defaults_for_unset_keys(self):
        app = _make_app({
            "humidity_low_warning": 62,
            "humidity_high_warning": 68,
            "humidity_low_critical": 60,
            "humidity_high_critical": 70,
            "sensors": [
                {
                    "entity_id": "sensor.partial_01",
                    "name": "Partial Override",
                    "type": "humidity",
                    "humidity_low_warning": 64,
                    # Other thresholds should use defaults
                },
            ],
        })
        _init_only(app)
        sensor = app._sensors[0]
        assert sensor["humidity_low_warning"] == 64.0
        assert sensor["humidity_high_warning"] == 68.0  # default
        assert sensor["humidity_low_critical"] == 60.0  # default
        assert sensor["humidity_high_critical"] == 70.0  # default


# ------------------------------------------------------------------
# Run checks (full cycle)
# ------------------------------------------------------------------


class TestRunChecks:
    def test_reports_all_results(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="65.0")
        app._run_checks()

        report_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
        ]
        assert len(report_calls) == 1
        payload = json.loads(report_calls[0][1]["payload"])
        assert payload["checker_id"] == "cigar_humidity"
        assert len(payload["results"]) == 3

    def test_mixed_statuses(self):
        app = _make_app()
        _init_only(app)
        # Sensor 1: ok, Sensor 2: critical (unavailable), Sensor 3: warning
        app.get_state = MagicMock(side_effect=["65.0", "unavailable", "61.0"])
        app._run_checks()

        report_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
        ]
        payload = json.loads(report_calls[0][1]["payload"])
        results = payload["results"]
        assert results[0]["status"] == "ok"
        assert results[1]["status"] == "critical"
        assert results[2]["status"] == "warning"

    def test_logs_warning_when_issues_found(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="unavailable")
        app._run_checks()
        # Should log at WARNING level
        warning_calls = [
            c for c in app.log.call_args_list
            if c[1].get("level") == "WARNING"
        ]
        assert len(warning_calls) >= 1


# ------------------------------------------------------------------
# Event handlers
# ------------------------------------------------------------------


class TestForceRecheck:
    def test_recheck_runs_checks(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="65.0")
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
        app.get_state = MagicMock(return_value="65.0")
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
        # Remove keys to test defaults
        del app.args["checker_id"]
        del app.args["checker_name"]
        _init_only(app)
        assert app._checker_id == "temp_humidity"
        assert app._checker_name == "Temp/Humidity"

    def test_integer_state_parsed(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="65")
        status, _ = app._evaluate_sensor(app._sensors[0])
        assert status == "ok"


# ------------------------------------------------------------------
# Metrics emission
# ------------------------------------------------------------------


class TestMetricsEmission:
    def test_humidity_sensor_emits_humidity_percent_metric(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="65.0")
        app._run_checks()

        report_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
        ]
        payload = json.loads(report_calls[0][1]["payload"])
        assert "metrics" in payload
        # 3 humidity sensors, one metric each
        assert len(payload["metrics"]) == 3
        m = payload["metrics"][0]
        assert m["name"] == "humidity_percent"
        assert m["value"] == 65.0
        assert m["type"] == "gauge"
        assert m["labels"] == {"sensor": "Cigar Sensor 01"}

    def test_temperature_sensor_emits_temperature_fahrenheit_metric(self):
        app = _make_app({
            "sensors": [
                {
                    "entity_id": "sensor.temp_01",
                    "name": "Temp 01",
                    "type": "temperature",
                },
            ],
        })
        _init_only(app)
        app.get_state = MagicMock(return_value="65.0")
        app._run_checks()

        report_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
        ]
        payload = json.loads(report_calls[0][1]["payload"])
        assert payload["metrics"] == [
            {
                "name": "temperature_fahrenheit",
                "value": 65.0,
                "type": "gauge",
                "labels": {"sensor": "Temp 01"},
            }
        ]

    def test_both_type_sensor_emits_both_metrics(self):
        app = _make_app({
            "sensors": [
                {
                    "entity_id": "sensor.both_01",
                    "name": "Both 01",
                    "type": "both",
                },
            ],
        })
        _init_only(app)
        app.get_state = MagicMock(return_value="65.0")
        app._run_checks()

        report_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
        ]
        payload = json.loads(report_calls[0][1]["payload"])
        names = {m["name"] for m in payload["metrics"]}
        assert names == {"temperature_fahrenheit", "humidity_percent"}
        for m in payload["metrics"]:
            assert m["value"] == 65.0
            assert m["type"] == "gauge"
            assert m["labels"] == {"sensor": "Both 01"}

    def test_unavailable_sensor_emits_no_metric(self):
        """Unavailable/unknown/non-numeric readings must not produce a metric entry."""
        app = _make_app()
        _init_only(app)
        # Sensor 1: ok, Sensor 2: unavailable, Sensor 3: warning
        app.get_state = MagicMock(side_effect=["65.0", "unavailable", "61.0"])
        app._run_checks()

        report_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
        ]
        payload = json.loads(report_calls[0][1]["payload"])
        # Only 2 of the 3 sensors produced a valid numeric reading
        assert len(payload["metrics"]) == 2
        sensor_names = {m["labels"]["sensor"] for m in payload["metrics"]}
        assert sensor_names == {"Cigar Sensor 01", "Aqara W100 01"}

    def test_all_sensors_unavailable_omits_metrics_key(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="unavailable")
        app._run_checks()

        report_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
        ]
        payload = json.loads(report_calls[0][1]["payload"])
        assert "metrics" not in payload

    def test_metrics_omitted_when_no_sensors(self):
        app = _make_app({"sensors": []})
        _init_only(app)
        app._run_checks()

        report_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
        ]
        payload = json.loads(report_calls[0][1]["payload"])
        assert "metrics" not in payload

    def test_evaluate_sensor_with_value_returns_none_on_error(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="unavailable")
        status, detail, value = app._evaluate_sensor_with_value(app._sensors[0])
        assert status == "critical"
        assert value is None

    def test_evaluate_sensor_with_value_returns_numeric_value(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="65.0")
        status, detail, value = app._evaluate_sensor_with_value(app._sensors[0])
        assert status == "ok"
        assert value == 65.0

    def test_evaluate_sensor_backward_compatible_two_tuple(self):
        """_evaluate_sensor must keep returning a plain (status, detail) tuple."""
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="65.0")
        result = app._evaluate_sensor(app._sensors[0])
        assert len(result) == 2
