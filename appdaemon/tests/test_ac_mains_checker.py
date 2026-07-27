"""Tests for AcMainsChecker — AC mains loss detection on battery-backed devices."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

# Mock hassapi before importing the app under test
mock_hass = MagicMock()
mock_hass.Hass = type("_MockHass", (), {"__init__": lambda self, *a, **kw: None})
sys.modules["hassapi"] = mock_hass

_repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo_root / "apps"))
sys.path.insert(0, str(_repo_root))

from health_checks.checker_apps.ac_mains_checker.ac_mains_checker import (  # noqa: E402
    AcMainsChecker,
)


# ----------------------------------------------------------------------
# Fixtures / helpers
# ----------------------------------------------------------------------

MOCK_ENTITIES = {
    # Mains-powered range extenders (the devices we care about)
    "binary_sensor.shed_extender_ac_mains_disconnected": {
        "state": "off",
        "attributes": {"friendly_name": "shed_extender AC mains disconnected"},
    },
    "binary_sensor.garage_range_extender_ac_mains_disconnected": {
        "state": "on",
        "attributes": {"friendly_name": "garage_range_extender AC mains disconnected"},
    },
    # Battery-only device — reports "on" permanently because it has no mains.
    # Must be excluded or it alerts forever.
    "binary_sensor.basement_hallway_qsensor_ac_mains_disconnected": {
        "state": "on",
        "attributes": {
            "friendly_name": "basement_hallway_qsensor AC mains disconnected"
        },
    },
    # The paired "re-connected" sensor must never be picked up as its own check
    "binary_sensor.shed_extender_ac_mains_re_connected": {
        "state": "on",
        "attributes": {"friendly_name": "shed_extender AC mains re-connected"},
    },
    # Unrelated entity
    "sensor.shed_extender_battery_level": {
        "state": "33.0",
        "attributes": {"friendly_name": "shed_extender Battery level"},
    },
}

DEFAULT_ARGS = {
    "checker_id": "ac_mains",
    "checker_name": "AC Mains",
    "check_interval_s": 300,
    "health_dependencies": [{"checker_id": "zwave"}],
    "entity_patterns": [
        {"include": "binary_sensor\\..*_ac_mains_disconnected$"},
        {"exclude": ".*qsensor.*"},
    ],
}


def _mock_get_state(entity_id=None, **kwargs):
    """Return all entities when called with no args, or a single state."""
    if entity_id is None:
        return MOCK_ENTITIES
    entity = MOCK_ENTITIES.get(entity_id)
    return entity["state"] if entity else None


def _make_app(extra_args: dict | None = None) -> AcMainsChecker:
    ad = MagicMock()
    config = MagicMock()
    app = AcMainsChecker(ad, config)
    args = dict(DEFAULT_ARGS)
    if extra_args:
        args.update(extra_args)
    app.args = args
    app.get_state = AsyncMock(side_effect=_mock_get_state)
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


def _startup(app):
    app.initialize()
    _run(app._async_startup())


def _init_and_discover(app):
    app.initialize()
    _run(app._discover_entities())


def _report_payloads(app):
    """Extract all report_status payloads fired by the app."""
    return [
        json.loads(c[1]["payload"])
        for c in app.fire_event.call_args_list
        if c[1].get("command") == "report_status"
    ]


def _results_by_name(app):
    payloads = _report_payloads(app)
    assert payloads, "no report_status payload fired"
    return {r["name"]: r for r in payloads[-1]["results"]}


# ----------------------------------------------------------------------
# Discovery
# ----------------------------------------------------------------------


class TestDiscovery:
    def test_discovers_ac_mains_disconnected_entities(self):
        app = _make_app()
        _init_and_discover(app)
        assert "binary_sensor.shed_extender_ac_mains_disconnected" in app._entities
        assert (
            "binary_sensor.garage_range_extender_ac_mains_disconnected"
            in app._entities
        )

    def test_excludes_battery_only_device(self):
        """A battery-only Q Sensor reports 'on' forever — must not be monitored."""
        app = _make_app()
        _init_and_discover(app)
        assert (
            "binary_sensor.basement_hallway_qsensor_ac_mains_disconnected"
            not in app._entities
        )

    def test_does_not_match_re_connected_sensor(self):
        """The paired re-connected sensor must not become its own check."""
        app = _make_app()
        _init_and_discover(app)
        assert (
            "binary_sensor.shed_extender_ac_mains_re_connected" not in app._entities
        )

    def test_ignores_unrelated_entities(self):
        app = _make_app()
        _init_and_discover(app)
        assert "sensor.shed_extender_battery_level" not in app._entities
        assert len(app._entities) == 2

    def test_friendly_name_strips_ac_mains_suffix(self):
        app = _make_app()
        _init_and_discover(app)
        assert (
            app._entities["binary_sensor.shed_extender_ac_mains_disconnected"]
            == "shed_extender"
        )

    def test_falls_back_to_entity_id_without_friendly_name(self):
        app = _make_app()
        app.get_state = AsyncMock(
            return_value={
                "binary_sensor.nameless_ac_mains_disconnected": {
                    "state": "off",
                    "attributes": {},
                }
            }
        )
        app.initialize()
        _run(app._discover_entities())
        assert (
            app._entities["binary_sensor.nameless_ac_mains_disconnected"]
            == "binary_sensor.nameless_ac_mains_disconnected"
        )

    def test_warns_when_no_entities_matched(self):
        app = _make_app({"entity_patterns": [{"include": "binary_sensor\\.nomatch$"}]})
        _startup(app)
        warnings = [
            c
            for c in app.log.call_args_list
            if c[1].get("level") == "WARNING" and "No entities" in str(c[0])
        ]
        assert warnings


# ----------------------------------------------------------------------
# Registration
# ----------------------------------------------------------------------


class TestRegistration:
    def test_registers_sorted_check_names(self):
        app = _make_app()
        _startup(app)
        register_calls = [
            c
            for c in app.fire_event.call_args_list
            if c[1].get("command") == "register_checker"
        ]
        payload = json.loads(register_calls[0][1]["payload"])
        assert payload["check_names"] == ["garage_range_extender", "shed_extender"]
        assert payload["checker_id"] == "ac_mains"
        assert payload["checker_name"] == "AC Mains"

    def test_declares_zwave_dependency_for_all_checks(self):
        """An unreachable node is a Z-Wave fault, so zwave must mask these."""
        app = _make_app()
        _startup(app)
        register_calls = [
            c
            for c in app.fire_event.call_args_list
            if c[1].get("command") == "register_checker"
        ]
        payload = json.loads(register_calls[0][1]["payload"])
        assert payload["dependencies"] == [
            {
                "checker_id": "zwave",
                "affects_checks": ["garage_range_extender", "shed_extender"],
            }
        ]

    def test_omits_dependencies_when_none_configured(self):
        app = _make_app({"health_dependencies": []})
        _startup(app)
        register_calls = [
            c
            for c in app.fire_event.call_args_list
            if c[1].get("command") == "register_checker"
        ]
        payload = json.loads(register_calls[0][1]["payload"])
        assert "dependencies" not in payload

    def test_re_registers_on_controller_ready(self):
        app = _make_app()
        _startup(app)
        app.get_state = MagicMock(side_effect=_mock_get_state)
        before = len(
            [
                c
                for c in app.fire_event.call_args_list
                if c[1].get("command") == "register_checker"
            ]
        )
        app._on_controller_ready("health_check_controller_ready", {}, {})
        after = len(
            [
                c
                for c in app.fire_event.call_args_list
                if c[1].get("command") == "register_checker"
            ]
        )
        assert after == before + 1


# ----------------------------------------------------------------------
# Evaluation
# ----------------------------------------------------------------------


class TestEvaluation:
    def test_off_is_ok(self):
        app = _make_app()
        _init_and_discover(app)
        app.get_state = MagicMock(return_value="off")
        result = app._evaluate_entity("binary_sensor.x", "Device")
        assert result["status"] == "ok"
        assert result["_metric_value"] == 0

    def test_on_is_critical_by_default(self):
        app = _make_app()
        _init_and_discover(app)
        app.get_state = MagicMock(return_value="on")
        result = app._evaluate_entity("binary_sensor.x", "Device")
        assert result["status"] == "critical"
        assert "backup battery" in result["detail"]
        assert result["_metric_value"] == 1

    def test_disconnected_status_is_configurable(self):
        app = _make_app({"disconnected_status": "warning"})
        _init_and_discover(app)
        app.get_state = MagicMock(return_value="on")
        result = app._evaluate_entity("binary_sensor.x", "Device")
        assert result["status"] == "warning"

    def test_invalid_disconnected_status_falls_back_to_critical(self):
        app = _make_app({"disconnected_status": "banana"})
        app.initialize()
        assert app._disconnected_status == "critical"
        warnings = [
            c
            for c in app.log.call_args_list
            if c[1].get("level") == "WARNING" and "banana" in str(c[0])
        ]
        assert warnings

    def test_unavailable_is_unknown_not_critical(self):
        """No data is not a power loss — that's the zwave checker's job."""
        app = _make_app()
        _init_and_discover(app)
        for missing in ("unavailable", "unknown", None):
            app.get_state = MagicMock(return_value=missing)
            result = app._evaluate_entity("binary_sensor.x", "Device")
            assert result["status"] == "unknown", missing
            assert "_metric_value" not in result

    def test_unexpected_state_is_unknown(self):
        app = _make_app()
        _init_and_discover(app)
        app.get_state = MagicMock(return_value="sideways")
        result = app._evaluate_entity("binary_sensor.x", "Device")
        assert result["status"] == "unknown"
        assert "sideways" in result["detail"]

    def test_state_read_exception_is_unknown(self):
        app = _make_app()
        _init_and_discover(app)
        app.get_state = MagicMock(side_effect=RuntimeError("boom"))
        result = app._evaluate_entity("binary_sensor.x", "Device")
        assert result["status"] == "unknown"
        assert "boom" in result["detail"]

    def test_state_case_is_normalized(self):
        app = _make_app()
        _init_and_discover(app)
        app.get_state = MagicMock(return_value="ON")
        assert app._evaluate_entity("binary_sensor.x", "Device")["status"] == "critical"


# ----------------------------------------------------------------------
# Check cycle
# ----------------------------------------------------------------------


class TestRunChecks:
    def test_reports_mixed_results(self):
        app = _make_app()
        _startup(app)
        app.get_state = MagicMock(side_effect=_mock_get_state)
        app._run_checks()
        results = _results_by_name(app)
        assert results["shed_extender"]["status"] == "ok"
        assert results["garage_range_extender"]["status"] == "critical"

    def test_metric_value_stripped_from_results(self):
        app = _make_app()
        _startup(app)
        app.get_state = MagicMock(side_effect=_mock_get_state)
        app._run_checks()
        payload = _report_payloads(app)[-1]
        for result in payload["results"]:
            assert "_metric_value" not in result

    def test_emits_gauge_metrics(self):
        app = _make_app()
        _startup(app)
        app.get_state = MagicMock(side_effect=_mock_get_state)
        app._run_checks()
        payload = _report_payloads(app)[-1]
        metrics = {m["labels"]["device"]: m for m in payload["metrics"]}
        assert metrics["shed_extender"]["value"] == 0
        assert metrics["garage_range_extender"]["value"] == 1
        assert metrics["shed_extender"]["name"] == "ac_mains_disconnected"
        assert metrics["shed_extender"]["type"] == "gauge"

    def test_summary_logs_at_warning_when_disconnected(self):
        app = _make_app()
        _startup(app)
        app.get_state = MagicMock(side_effect=_mock_get_state)
        app.log.reset_mock()
        app._run_checks()
        summary = [c for c in app.log.call_args_list if "Check complete" in str(c[0])]
        assert summary
        assert summary[-1][1]["level"] == "WARNING"

    def test_summary_logs_at_info_when_all_ok(self):
        app = _make_app()
        _startup(app)
        app.get_state = MagicMock(return_value="off")
        app.log.reset_mock()
        app._run_checks()
        summary = [c for c in app.log.call_args_list if "Check complete" in str(c[0])]
        assert summary
        assert summary[-1][1]["level"] == "INFO"


# ----------------------------------------------------------------------
# Lifecycle
# ----------------------------------------------------------------------


class TestLifecycle:
    def test_initialize_schedules_startup(self):
        app = _make_app()
        app.initialize()
        app.run_in.assert_called_once()
        assert app.run_in.call_args[0][1] == 0

    def test_startup_registers_listeners(self):
        app = _make_app()
        _startup(app)
        events = [c[0][1] for c in app.listen_event.call_args_list]
        assert "health_check_controller_ready" in events
        assert "health_check_recheck" in events

    def test_first_check_starts_periodic_timer(self):
        app = _make_app()
        _startup(app)
        app.get_state = MagicMock(side_effect=_mock_get_state)
        app._first_check({})
        app.run_every.assert_called_once()
        assert app.run_every.call_args[0][2] == 300

    def test_recheck_triggers_run(self):
        app = _make_app()
        _startup(app)
        app.get_state = MagicMock(side_effect=_mock_get_state)
        before = len(_report_payloads(app))
        app._on_recheck("health_check_recheck", {}, {})
        assert len(_report_payloads(app)) == before + 1
