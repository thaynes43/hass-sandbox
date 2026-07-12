"""Unit tests for UpsChecker."""

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

from health_checks.checker_apps.battery_checker.ups_checker import UpsChecker


DEFAULT_ARGS: Dict[str, Any] = {
    "checker_id": "ups",
    "checker_name": "UPS",
    "check_interval_s": 120,
    "ups_devices": [
        {
            "name": "UPS A",
            "battery_entity": "sensor.ups_a_battery",
            "load_entity": "sensor.ups_a_load",
            "battery_warning": 90,
            "battery_critical": 50,
            "load_warning": 90,
            "load_critical": 100,
        },
    ],
}


def _make_app(extra_args: dict | None = None) -> UpsChecker:
    ad = MagicMock()
    config = MagicMock()
    app = UpsChecker(ad, config)

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


def _startup(app: UpsChecker) -> None:
    app.initialize()
    _run(app._async_startup())


def _init_only(app: UpsChecker) -> None:
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
        app.get_state = MagicMock(return_value="100")
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
    def test_registers_two_checks_per_device(self):
        app = _make_app()
        _startup(app)
        register_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "register_checker"
        ]
        assert len(register_calls) >= 1
        payload = json.loads(register_calls[0][1]["payload"])
        assert payload["check_names"] == ["UPS A Battery", "UPS A Load"]

    def test_registers_multiple_devices(self):
        app = _make_app({
            "ups_devices": [
                {
                    "name": "UPS A",
                    "battery_entity": "sensor.ups_a_battery",
                    "load_entity": "sensor.ups_a_load",
                },
                {
                    "name": "UPS B",
                    "battery_entity": "sensor.ups_b_battery",
                    "load_entity": "sensor.ups_b_load",
                },
            ],
        })
        _startup(app)
        register_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "register_checker"
        ]
        payload = json.loads(register_calls[0][1]["payload"])
        assert payload["check_names"] == [
            "UPS A Battery", "UPS A Load",
            "UPS B Battery", "UPS B Load",
        ]

    def test_no_dependencies_in_registration(self):
        """UPS devices are infrastructure — no per-sensor dependencies."""
        app = _make_app()
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
        assert payload["checker_id"] == "ups"
        assert payload["checker_name"] == "UPS"


# ------------------------------------------------------------------
# Battery check (low = bad)
# ------------------------------------------------------------------


class TestBatteryCheck:
    def test_100_percent_ok(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="100")
        status, detail = app._check_battery(app._devices[0])
        assert status == "ok"
        assert "100%" in detail

    def test_89_percent_warning(self):
        """89 is below 90 warning threshold."""
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="89")
        status, detail = app._check_battery(app._devices[0])
        assert status == "warning"
        assert "89%" in detail
        assert "<90%" in detail

    def test_50_percent_warning_not_critical(self):
        """50 is not < 50 (critical uses strict less-than), so it's warning (< 90)."""
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="50")
        status, detail = app._check_battery(app._devices[0])
        assert status == "warning"

    def test_49_percent_critical(self):
        """49 is below 50 critical threshold."""
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="49")
        status, detail = app._check_battery(app._devices[0])
        assert status == "critical"
        assert "49%" in detail
        assert "<50%" in detail

    def test_unavailable_critical(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="unavailable")
        status, detail = app._check_battery(app._devices[0])
        assert status == "critical"
        assert "unavailable" in detail

    def test_unknown_critical(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="unknown")
        status, detail = app._check_battery(app._devices[0])
        assert status == "critical"

    def test_none_critical(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value=None)
        status, detail = app._check_battery(app._devices[0])
        assert status == "critical"
        assert "not found" in detail

    def test_90_percent_ok(self):
        """90 is not < 90, so it's ok."""
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="90")
        status, _ = app._check_battery(app._devices[0])
        assert status == "ok"


# ------------------------------------------------------------------
# Load check (high = bad)
# ------------------------------------------------------------------


class TestLoadCheck:
    def test_50_percent_ok(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="50")
        status, detail = app._check_load(app._devices[0])
        assert status == "ok"
        assert "50%" in detail

    def test_90_percent_warning(self):
        """90 meets >= 90 warning threshold."""
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="90")
        status, detail = app._check_load(app._devices[0])
        assert status == "warning"
        assert "90%" in detail

    def test_99_percent_warning(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="99")
        status, detail = app._check_load(app._devices[0])
        assert status == "warning"

    def test_100_percent_critical(self):
        """100 meets >= 100 critical threshold."""
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="100")
        status, detail = app._check_load(app._devices[0])
        assert status == "critical"
        assert "100%" in detail

    def test_110_percent_critical(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="110")
        status, detail = app._check_load(app._devices[0])
        assert status == "critical"

    def test_unavailable_critical(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="unavailable")
        status, detail = app._check_load(app._devices[0])
        assert status == "critical"
        assert "unavailable" in detail

    def test_89_percent_ok(self):
        """89 is below 90 warning threshold — ok."""
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="89")
        status, _ = app._check_load(app._devices[0])
        assert status == "ok"


# ------------------------------------------------------------------
# Run checks (full cycle)
# ------------------------------------------------------------------


class TestRunChecks:
    def test_reports_all_results_single_device(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="95")
        app._run_checks()

        report_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
        ]
        assert len(report_calls) == 1
        payload = json.loads(report_calls[0][1]["payload"])
        assert payload["checker_id"] == "ups"
        assert len(payload["results"]) == 2
        assert payload["results"][0]["name"] == "UPS A Battery"
        assert payload["results"][1]["name"] == "UPS A Load"

    def test_reports_all_results_two_devices(self):
        app = _make_app({
            "ups_devices": [
                {
                    "name": "UPS A",
                    "battery_entity": "sensor.ups_a_battery",
                    "load_entity": "sensor.ups_a_load",
                    "battery_warning": 90,
                    "battery_critical": 50,
                    "load_warning": 90,
                    "load_critical": 100,
                },
                {
                    "name": "UPS B",
                    "battery_entity": "sensor.ups_b_battery",
                    "load_entity": "sensor.ups_b_load",
                    "battery_warning": 90,
                    "battery_critical": 50,
                    "load_warning": 90,
                    "load_critical": 100,
                },
            ],
        })
        _init_only(app)
        # UPS A: battery=95 (ok), load=50 (ok), UPS B: battery=40 (critical), load=95 (warning)
        app.get_state = MagicMock(side_effect=["95", "50", "40", "95"])
        app._run_checks()

        report_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
        ]
        assert len(report_calls) == 1
        payload = json.loads(report_calls[0][1]["payload"])
        results = payload["results"]
        assert len(results) == 4
        assert results[0]["name"] == "UPS A Battery"
        assert results[0]["status"] == "ok"
        assert results[1]["name"] == "UPS A Load"
        assert results[1]["status"] == "ok"
        assert results[2]["name"] == "UPS B Battery"
        assert results[2]["status"] == "critical"
        assert results[3]["name"] == "UPS B Load"
        assert results[3]["status"] == "warning"

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


# ------------------------------------------------------------------
# Event handlers
# ------------------------------------------------------------------


class TestForceRecheck:
    def test_recheck_runs_checks(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="95")
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
        app.get_state = MagicMock(return_value="95")
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
    def test_empty_devices_list(self):
        app = _make_app({"ups_devices": []})
        _init_only(app)
        app._run_checks()
        report_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
        ]
        payload = json.loads(report_calls[0][1]["payload"])
        assert payload["results"] == []

    def test_default_checker_id(self):
        app = _make_app()
        del app.args["checker_id"]
        del app.args["checker_name"]
        _init_only(app)
        assert app._checker_id == "ups"
        assert app._checker_name == "UPS"

    def test_non_numeric_state(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="error")
        status, detail = app._check_battery(app._devices[0])
        assert status == "critical"
        assert "non-numeric" in detail

    def test_exception_reading_state(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(side_effect=RuntimeError("connection lost"))
        status, detail = app._check_battery(app._devices[0])
        assert status == "critical"
        assert "error reading state" in detail

    def test_default_thresholds_applied(self):
        """Device without explicit thresholds gets defaults."""
        app = _make_app({
            "ups_devices": [
                {
                    "name": "Minimal UPS",
                    "battery_entity": "sensor.min_battery",
                    "load_entity": "sensor.min_load",
                },
            ],
        })
        _init_only(app)
        dev = app._devices[0]
        assert dev["battery_warning"] == 90.0
        assert dev["battery_critical"] == 50.0
        assert dev["load_warning"] == 90.0
        assert dev["load_critical"] == 100.0


# ------------------------------------------------------------------
# Metrics (battery_percent + load_percent gauges per device)
# ------------------------------------------------------------------


class TestMetrics:
    def test_metrics_emitted_for_battery_and_load(self):
        app = _make_app()
        _init_only(app)
        # battery=95, load=50
        app.get_state = MagicMock(side_effect=["95", "50"])
        app._run_checks()

        report_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
        ]
        payload = json.loads(report_calls[0][1]["payload"])
        metrics = payload["metrics"]
        assert len(metrics) == 2
        by_name = {m["name"]: m for m in metrics}
        assert by_name["battery_percent"]["value"] == 95.0
        assert by_name["battery_percent"]["type"] == "gauge"
        assert by_name["battery_percent"]["labels"] == {"device": "UPS A"}
        assert by_name["load_percent"]["value"] == 50.0
        assert by_name["load_percent"]["labels"] == {"device": "UPS A"}

    def test_metrics_emitted_for_multiple_devices(self):
        app = _make_app({
            "ups_devices": [
                {
                    "name": "UPS A",
                    "battery_entity": "sensor.ups_a_battery",
                    "load_entity": "sensor.ups_a_load",
                },
                {
                    "name": "UPS B",
                    "battery_entity": "sensor.ups_b_battery",
                    "load_entity": "sensor.ups_b_load",
                },
            ],
        })
        _init_only(app)
        app.get_state = MagicMock(side_effect=["95", "50", "40", "99"])
        app._run_checks()

        report_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
        ]
        payload = json.loads(report_calls[0][1]["payload"])
        metrics = payload["metrics"]
        assert len(metrics) == 4
        devices = {m["labels"]["device"] for m in metrics}
        assert devices == {"UPS A", "UPS B"}

    def test_metrics_skip_unavailable_reading(self):
        app = _make_app()
        _init_only(app)
        # battery unavailable (skipped), load=50 (included)
        app.get_state = MagicMock(side_effect=["unavailable", "50"])
        app._run_checks()

        report_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
        ]
        payload = json.loads(report_calls[0][1]["payload"])
        metrics = payload["metrics"]
        assert len(metrics) == 1
        assert metrics[0]["name"] == "load_percent"

    def test_metrics_key_omitted_when_all_unavailable(self):
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

    def test_stale_value_cleared_when_reading_goes_unavailable(self):
        """A prior good reading must not leak into metrics once the sensor
        goes unavailable on a later cycle."""
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="95")
        app._check_battery(app._devices[0])
        assert app._last_metric_value["sensor.ups_a_battery"] == 95.0

        app.get_state = MagicMock(return_value="unavailable")
        app._check_battery(app._devices[0])
        assert "sensor.ups_a_battery" not in app._last_metric_value
