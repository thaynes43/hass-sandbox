"""Unit tests for BatteryChecker (auto-discovery pattern)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

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
    "entity_patterns": [
        {"include": "sensor\\.test_.*_battery$"},
    ],
}

MOCK_ENTITIES = {
    "sensor.test_device_a_battery": {
        "state": "85",
        "attributes": {
            "device_class": "battery",
            "unit_of_measurement": "%",
            "friendly_name": "Test Device A Battery",
        },
    },
    "sensor.test_device_b_battery": {
        "state": "15",
        "attributes": {
            "device_class": "battery",
            "unit_of_measurement": "%",
            "friendly_name": "Test Device B Battery",
        },
    },
    # Non-matching entity (wrong device_class)
    "sensor.temperature_reading": {
        "state": "72",
        "attributes": {
            "device_class": "temperature",
            "unit_of_measurement": "\u00b0F",
            "friendly_name": "Room Temperature",
        },
    },
}


def _mock_get_state(entity_id=None, **kwargs):
    """Return all entities when called with no args, or a single state."""
    if entity_id is None:
        return MOCK_ENTITIES
    entity = MOCK_ENTITIES.get(entity_id)
    return entity["state"] if entity else None


def _make_app(extra_args: dict | None = None) -> BatteryChecker:
    ad = MagicMock()
    config = MagicMock()
    app = BatteryChecker(ad, config)

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


def _startup(app: BatteryChecker) -> None:
    app.initialize()
    _run(app._async_startup())


def _init_and_discover(app: BatteryChecker) -> None:
    """Initialize and run discovery (but not full startup)."""
    app.initialize()
    _run(app._discover_entities())


def _init_only(app: BatteryChecker) -> None:
    app.initialize()


# ------------------------------------------------------------------
# Discovery
# ------------------------------------------------------------------


class TestDiscovery:
    def test_discovers_battery_entities(self):
        app = _make_app()
        _init_and_discover(app)
        assert len(app._entities) == 2
        assert "sensor.test_device_a_battery" in app._entities
        assert "sensor.test_device_b_battery" in app._entities

    def test_excludes_non_battery_device_class(self):
        app = _make_app()
        _init_and_discover(app)
        assert "sensor.temperature_reading" not in app._entities

    def test_excludes_non_percentage_unit(self):
        entities = dict(MOCK_ENTITIES)
        entities["sensor.test_voltage_battery"] = {
            "state": "3.2",
            "attributes": {
                "device_class": "battery",
                "unit_of_measurement": "V",
                "friendly_name": "Test Voltage Battery",
            },
        }
        app = _make_app()
        app.get_state = AsyncMock(side_effect=lambda entity_id=None, **kw: entities if entity_id is None else (entities.get(entity_id, {}).get("state") if entity_id else entities))
        _init_and_discover(app)
        assert "sensor.test_voltage_battery" not in app._entities

    def test_include_pattern_filters(self):
        """Only entities matching include pattern are discovered."""
        entities = dict(MOCK_ENTITIES)
        entities["sensor.other_device_battery"] = {
            "state": "90",
            "attributes": {
                "device_class": "battery",
                "unit_of_measurement": "%",
                "friendly_name": "Other Device Battery",
            },
        }
        app = _make_app()
        app.get_state = AsyncMock(side_effect=lambda entity_id=None, **kw: entities if entity_id is None else (entities.get(entity_id, {}).get("state") if entity_id else entities))
        _init_and_discover(app)
        # "sensor.other_device_battery" does not match "sensor\.test_.*_battery$"
        assert "sensor.other_device_battery" not in app._entities
        assert len(app._entities) == 2

    def test_exclude_pattern_removes_match(self):
        app = _make_app({
            "entity_patterns": [
                {"include": "sensor\\.test_.*_battery$"},
                {"exclude": "sensor\\.test_device_b"},
            ],
        })
        _init_and_discover(app)
        assert "sensor.test_device_a_battery" in app._entities
        assert "sensor.test_device_b_battery" not in app._entities

    def test_friendly_name_strips_battery_suffix(self):
        app = _make_app()
        _init_and_discover(app)
        assert app._entities["sensor.test_device_a_battery"] == "Test Device A"
        assert app._entities["sensor.test_device_b_battery"] == "Test Device B"

    def test_friendly_name_strips_battery_level_suffix(self):
        entities = {
            "sensor.test_gadget_battery": {
                "state": "50",
                "attributes": {
                    "device_class": "battery",
                    "unit_of_measurement": "%",
                    "friendly_name": "Test Gadget Battery level",
                },
            },
        }
        app = _make_app()
        app.get_state = AsyncMock(side_effect=lambda entity_id=None, **kw: entities if entity_id is None else (entities.get(entity_id, {}).get("state") if entity_id else entities))
        _init_and_discover(app)
        assert app._entities["sensor.test_gadget_battery"] == "Test Gadget"

    def test_friendly_name_strips_battery_level_case_insensitive(self):
        entities = {
            "sensor.test_thing_battery": {
                "state": "50",
                "attributes": {
                    "device_class": "battery",
                    "unit_of_measurement": "%",
                    "friendly_name": "Test Thing Battery Level",
                },
            },
        }
        app = _make_app()
        app.get_state = AsyncMock(side_effect=lambda entity_id=None, **kw: entities if entity_id is None else (entities.get(entity_id, {}).get("state") if entity_id else entities))
        _init_and_discover(app)
        assert app._entities["sensor.test_thing_battery"] == "Test Thing"

    def test_no_entities_matched_logs_warning(self):
        app = _make_app({
            "entity_patterns": [
                {"include": "sensor\\.nonexistent_.*"},
            ],
        })
        _startup(app)
        warning_calls = [
            c for c in app.log.call_args_list
            if c[1].get("level") == "WARNING" and "No entities" in str(c[0])
        ]
        assert len(warning_calls) == 1

    def test_missing_friendly_name_uses_entity_id(self):
        entities = {
            "sensor.test_bare_battery": {
                "state": "50",
                "attributes": {
                    "device_class": "battery",
                    "unit_of_measurement": "%",
                },
            },
        }
        app = _make_app()
        app.get_state = AsyncMock(side_effect=lambda entity_id=None, **kw: entities if entity_id is None else (entities.get(entity_id, {}).get("state") if entity_id else entities))
        _init_and_discover(app)
        # No friendly_name -> uses entity_id, no suffix stripping matches
        assert app._entities["sensor.test_bare_battery"] == "sensor.test_bare_battery"

    def test_non_dict_state_obj_skipped(self):
        entities = dict(MOCK_ENTITIES)
        entities["sensor.test_weird_battery"] = "just_a_string"
        app = _make_app()
        app.get_state = AsyncMock(side_effect=lambda entity_id=None, **kw: entities if entity_id is None else None)
        _init_and_discover(app)
        assert "sensor.test_weird_battery" not in app._entities

    def test_empty_state_returns_no_entities(self):
        app = _make_app()
        app.get_state = AsyncMock(side_effect=lambda entity_id=None, **kw: {} if entity_id is None else None)
        _init_and_discover(app)
        assert len(app._entities) == 0


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
        # Names are sorted display names (battery suffix stripped)
        assert payload["check_names"] == ["Test Device A", "Test Device B"]

    def test_health_dependencies_included(self):
        app = _make_app({
            "health_dependencies": [{"checker_id": "mqtt_broker"}],
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
        assert set(deps[0]["affects_checks"]) == {"Test Device A", "Test Device B"}

    def test_no_dependencies_omits_key(self):
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
        assert payload["checker_id"] == "test_batteries"
        assert payload["checker_name"] == "Test Batteries"

    def test_multiple_health_dependencies(self):
        app = _make_app({
            "health_dependencies": [
                {"checker_id": "mqtt_broker"},
                {"checker_id": "zigbee"},
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
        assert dep_ids == {"mqtt_broker", "zigbee"}


# ------------------------------------------------------------------
# Threshold evaluation
# ------------------------------------------------------------------


class TestThresholdEvaluation:
    """Test _evaluate_entity with default thresholds: warning=20, critical=10."""

    def _eval(self, app, entity_id, display_name, state_value):
        app.get_state = MagicMock(return_value=state_value)
        return app._evaluate_entity(entity_id, display_name)

    def test_ok_above_warning(self):
        app = _make_app()
        _init_only(app)
        result = self._eval(app, "sensor.test_a", "Device A", "85")
        assert result["status"] == "ok"
        assert "85%" in result["detail"]

    def test_warning_between_warn_and_crit(self):
        app = _make_app()
        _init_only(app)
        result = self._eval(app, "sensor.test_a", "Device A", "15")
        assert result["status"] == "warning"
        assert "15%" in result["detail"]
        assert "warning" in result["detail"]

    def test_critical_below_crit(self):
        app = _make_app()
        _init_only(app)
        result = self._eval(app, "sensor.test_a", "Device A", "5")
        assert result["status"] == "critical"
        assert "5%" in result["detail"]
        assert "critical" in result["detail"]

    def test_exactly_at_warning_threshold_is_warning(self):
        app = _make_app()
        _init_only(app)
        result = self._eval(app, "sensor.test_a", "Device A", "20")
        assert result["status"] == "warning"

    def test_exactly_at_critical_threshold_is_critical(self):
        app = _make_app()
        _init_only(app)
        result = self._eval(app, "sensor.test_a", "Device A", "10")
        assert result["status"] == "critical"

    def test_just_above_warning_is_ok(self):
        app = _make_app()
        _init_only(app)
        result = self._eval(app, "sensor.test_a", "Device A", "21")
        assert result["status"] == "ok"

    def test_unavailable_state_is_unknown_not_critical(self):
        # A missing reading is 'no data', not a low battery — must never page.
        app = _make_app()
        _init_only(app)
        result = self._eval(app, "sensor.test_a", "Device A", "unavailable")
        assert result["status"] == "unknown"
        assert "unavailable" in result["detail"]

    def test_unknown_state_is_unknown_not_critical(self):
        app = _make_app()
        _init_only(app)
        result = self._eval(app, "sensor.test_a", "Device A", "unknown")
        assert result["status"] == "unknown"
        assert "unknown" in result["detail"]

    def test_none_state_is_unknown_not_critical(self):
        app = _make_app()
        _init_only(app)
        result = self._eval(app, "sensor.test_a", "Device A", None)
        assert result["status"] == "unknown"
        assert "not found" in result["detail"]

    def test_non_numeric_state_is_unknown_not_critical(self):
        app = _make_app()
        _init_only(app)
        result = self._eval(app, "sensor.test_a", "Device A", "error")
        assert result["status"] == "unknown"
        assert "non-numeric" in result["detail"]

    def test_exception_reading_state_is_unknown_not_critical(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(side_effect=RuntimeError("connection lost"))
        result = app._evaluate_entity("sensor.test_a", "Device A")
        assert result["status"] == "unknown"
        assert "error reading state" in result["detail"]

    def test_genuine_low_battery_still_critical(self):
        # The numeric path is unchanged: a real low reading still pages.
        app = _make_app()
        _init_only(app)
        result = self._eval(app, "sensor.test_a", "Device A", "5")
        assert result["status"] == "critical"

    def test_whole_group_unavailable_never_pages(self):
        # The 2026-07-08 incident: an integration blip drops every entity in
        # the group to 'unavailable' at once. None may be 'critical', or the
        # for-duration gate would eventually page for a non-battery outage.
        app = _make_app()
        _startup(app)
        app.fire_event.reset_mock()
        app.get_state = MagicMock(return_value="unavailable")
        app._run_checks()
        report = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
        ][0]
        results = json.loads(report[1]["payload"])["results"]
        assert results, "expected at least one battery result"
        assert all(r["status"] == "unknown" for r in results)
        assert not any(r["status"] == "critical" for r in results)

    def test_float_state_parsed(self):
        app = _make_app()
        _init_only(app)
        result = self._eval(app, "sensor.test_a", "Device A", "99.5")
        assert result["status"] == "ok"
        assert "100%" in result["detail"]  # {99.5:.0f} rounds to 100

    def test_result_includes_display_name(self):
        app = _make_app()
        _init_only(app)
        result = self._eval(app, "sensor.test_a", "My Device", "50")
        assert result["name"] == "My Device"


# ------------------------------------------------------------------
# Run checks (full cycle)
# ------------------------------------------------------------------


class TestRunChecks:
    def test_reports_all_results(self):
        app = _make_app()
        _startup(app)
        app.fire_event.reset_mock()
        # Mock get_state for individual entity reads
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
        _startup(app)
        app.fire_event.reset_mock()
        # Two entities sorted by entity_id: device_a first, device_b second
        app.get_state = MagicMock(side_effect=["85", "unavailable"])
        app._run_checks()

        report_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
        ]
        payload = json.loads(report_calls[0][1]["payload"])
        results = payload["results"]
        assert results[0]["status"] == "ok"
        assert results[1]["status"] == "unknown"

    def test_logs_warning_when_issues_found(self):
        app = _make_app()
        _startup(app)
        app.log.reset_mock()
        app.get_state = MagicMock(return_value="unavailable")
        app._run_checks()
        warning_calls = [
            c for c in app.log.call_args_list
            if c[1].get("level") == "WARNING"
        ]
        assert len(warning_calls) >= 1

    def test_logs_info_when_all_ok(self):
        app = _make_app()
        _startup(app)
        app.log.reset_mock()
        app.get_state = MagicMock(return_value="85")
        app._run_checks()
        info_calls = [
            c for c in app.log.call_args_list
            if c[1].get("level") == "INFO" and "Check complete" in str(c[0])
        ]
        assert len(info_calls) == 1

    def test_empty_entities_reports_empty_results(self):
        app = _make_app({
            "entity_patterns": [{"include": "sensor\\.nonexistent_.*"}],
        })
        _startup(app)
        app.fire_event.reset_mock()
        app._run_checks()
        report_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
        ]
        payload = json.loads(report_calls[0][1]["payload"])
        assert payload["results"] == []


# ------------------------------------------------------------------
# Metrics (battery_percent gauge per device)
# ------------------------------------------------------------------


class TestMetrics:
    def test_metrics_emitted_for_numeric_readings(self):
        app = _make_app()
        _startup(app)
        app.fire_event.reset_mock()
        # device_a=85 (ok), device_b=15 (warning) — both numeric
        app.get_state = MagicMock(side_effect=["85", "15"])
        app._run_checks()

        report_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
        ]
        payload = json.loads(report_calls[0][1]["payload"])
        metrics = payload["metrics"]
        assert len(metrics) == 2
        by_device = {m["labels"]["device"]: m for m in metrics}
        assert by_device["Test Device A"]["name"] == "battery_percent"
        assert by_device["Test Device A"]["value"] == 85.0
        assert by_device["Test Device A"]["type"] == "gauge"
        assert by_device["Test Device B"]["value"] == 15.0

    def test_metrics_skip_unavailable_reading(self):
        app = _make_app()
        _startup(app)
        app.fire_event.reset_mock()
        # device_a=85 (ok), device_b=unavailable (skipped from metrics)
        app.get_state = MagicMock(side_effect=["85", "unavailable"])
        app._run_checks()

        report_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
        ]
        payload = json.loads(report_calls[0][1]["payload"])
        metrics = payload["metrics"]
        assert len(metrics) == 1
        assert metrics[0]["labels"]["device"] == "Test Device A"

    def test_metrics_key_omitted_when_all_unavailable(self):
        app = _make_app()
        _startup(app)
        app.fire_event.reset_mock()
        app.get_state = MagicMock(return_value="unavailable")
        app._run_checks()

        report_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
        ]
        payload = json.loads(report_calls[0][1]["payload"])
        assert "metrics" not in payload

    def test_metrics_internal_key_not_leaked_into_results(self):
        app = _make_app()
        _startup(app)
        app.fire_event.reset_mock()
        app.get_state = MagicMock(side_effect=["85", "15"])
        app._run_checks()

        report_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
        ]
        payload = json.loads(report_calls[0][1]["payload"])
        for result in payload["results"]:
            assert "_metric_value" not in result

    def test_evaluate_entity_stashes_metric_value(self):
        app = _make_app()
        _init_only(app)
        app.get_state = MagicMock(return_value="42")
        result = app._evaluate_entity("sensor.test_a", "Device A")
        assert result["_metric_value"] == 42.0


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
        _startup(app)
        app.fire_event.reset_mock()
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

    def test_default_checker_id_and_name(self):
        app = _make_app({"checker_id": None, "checker_name": None})
        del app.args["checker_id"]
        del app.args["checker_name"]
        _init_only(app)
        assert app._checker_id == "batteries"
        assert app._checker_name == "Batteries"

    def test_compile_patterns(self):
        app = _make_app({
            "entity_patterns": [
                {"include": "sensor\\.foo_.*"},
                {"include": "sensor\\.bar_.*"},
                {"exclude": "sensor\\.foo_excluded"},
            ],
        })
        _init_only(app)
        assert len(app._include_patterns) == 2
        assert len(app._exclude_patterns) == 1


# ------------------------------------------------------------------
# Event handlers
# ------------------------------------------------------------------


class TestEventHandlers:
    def test_controller_ready_re_registers_and_runs_checks(self):
        app = _make_app()
        _startup(app)
        app.fire_event.reset_mock()
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

    def test_recheck_runs_checks(self):
        app = _make_app()
        _startup(app)
        app.fire_event.reset_mock()
        app.get_state = MagicMock(return_value="85")
        app._on_recheck("health_check_recheck", {}, {})

        report_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
        ]
        assert len(report_calls) == 1


# ------------------------------------------------------------------
# Disconnect-aware guard (Part B — gateway disconnect vs real low battery)
# ------------------------------------------------------------------


class TestDisconnectAware:
    """disconnect_aware=true downgrades an implausible drop to warning;
    a genuine gradual decline still pages critical; disconnect_aware=false
    (the default) leaves existing battery groups' behavior unchanged."""

    def _eval(self, app, entity_id, display_name, state_value):
        app.get_state = MagicMock(return_value=state_value)
        return app._evaluate_entity(entity_id, display_name)

    def test_implausible_drop_downgraded_to_warning(self):
        """100% -> 0% with disconnect_aware enabled is a suspected disconnect, not critical."""
        app = _make_app({
            "disconnect_aware": True,
            "disconnect_healthy_floor": 40,
            "critical_threshold": 5,
            "warning_threshold": 25,
        })
        _init_only(app)
        app._last_good_value["sensor.test_a"] = 100.0

        result = self._eval(app, "sensor.test_a", "Device A", "0")

        assert result["status"] == "warning"
        assert "suspected gateway disconnect" in result["detail"]
        assert "100" in result["detail"]
        assert "0" in result["detail"]

    def test_genuine_low_battery_still_critical(self):
        """A gradual decline (no healthy baseline recorded) still pages critical
        even with disconnect_aware enabled."""
        app = _make_app({
            "disconnect_aware": True,
            "disconnect_healthy_floor": 40,
            "critical_threshold": 5,
            "warning_threshold": 25,
        })
        _init_only(app)
        # No last_good_value recorded (cold start, or baseline was already low)
        result = self._eval(app, "sensor.test_a", "Device A", "0")

        assert result["status"] == "critical"
        assert "suspected gateway disconnect" not in result["detail"]

    def test_low_baseline_decline_still_critical(self):
        """8% -> 0% (baseline already below healthy_floor) is a real dying
        battery, not a disconnect — stays critical even with the guard on."""
        app = _make_app({
            "disconnect_aware": True,
            "disconnect_healthy_floor": 40,
            "critical_threshold": 5,
            "warning_threshold": 25,
        })
        _init_only(app)
        app._last_good_value["sensor.test_a"] = 8.0

        result = self._eval(app, "sensor.test_a", "Device A", "0")

        assert result["status"] == "critical"

    def test_disconnect_aware_false_behavior_unchanged(self):
        """With disconnect_aware disabled (the default), an implausible drop
        still pages critical exactly like before this feature existed."""
        app = _make_app({
            "critical_threshold": 5,
            "warning_threshold": 25,
        })
        _init_only(app)
        # Even if last_good_value were somehow populated, the flag gates it off.
        app._last_good_value["sensor.test_a"] = 100.0

        result = self._eval(app, "sensor.test_a", "Device A", "0")

        assert result["status"] == "critical"
        assert "suspected gateway disconnect" not in result["detail"]

    def test_last_good_value_updated_above_critical_threshold(self):
        """A healthy reading should update the baseline for future comparisons."""
        app = _make_app({"disconnect_aware": True, "critical_threshold": 5})
        _init_only(app)

        self._eval(app, "sensor.test_a", "Device A", "90")

        assert app._last_good_value["sensor.test_a"] == 90.0

    def test_last_good_value_not_updated_when_disconnect_aware_false(self):
        """Baseline tracking is skipped entirely when the flag is off."""
        app = _make_app({"critical_threshold": 5})
        _init_only(app)

        self._eval(app, "sensor.test_a", "Device A", "90")

        assert "sensor.test_a" not in app._last_good_value

    def test_discovery_seeds_last_good_value_from_healthy_state(self):
        """At discovery, a currently-healthy entity seeds its baseline."""
        app = _make_app({
            "disconnect_aware": True,
            "critical_threshold": 10,
        })
        _init_and_discover(app)

        assert app._last_good_value["sensor.test_device_a_battery"] == 85.0
        # Device B is at 15%, above critical_threshold(10) so it also seeds
        assert app._last_good_value["sensor.test_device_b_battery"] == 15.0

    def test_discovery_does_not_seed_when_disconnect_aware_false(self):
        app = _make_app()
        _init_and_discover(app)
        assert app._last_good_value == {}
