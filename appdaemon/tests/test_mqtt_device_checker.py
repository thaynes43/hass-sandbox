"""Unit tests for MqttDeviceChecker (MQTT last-seen based).

Mocks AppDaemon methods and HA state -- no real network or MQTT access.
"""

from __future__ import annotations

import asyncio
import copy
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Mock hassapi before importing the app
# ---------------------------------------------------------------------------

import types

_mock_hassapi = types.ModuleType("hassapi")


class _MockHass:
    """Minimal stand-in for hass.Hass so subclass methods are preserved."""
    def __init__(self, *args, **kwargs):
        pass


_mock_hassapi.Hass = _MockHass
sys.modules["hassapi"] = _mock_hassapi

_repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo_root / "apps"))
sys.path.insert(0, str(_repo_root))

from health_checks.checker_apps.mqtt_device_checker.mqtt_device_checker import (
    MqttDeviceChecker,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DEFAULT_ARGS: Dict[str, Any] = {
    "checker_id": "basement_lights",
    "checker_name": "Basement Lights",
    "check_interval_s": 300,
    "mqtt_stale_s": 21600,
    "mqtt_namespace": "mqtt",
    "mqtt_topic_prefix": "zigbee2mqtt",
    "protocol_dependency_id": "zigbee",
    "entity_patterns": [
        {"include": "light\\.basement.*"},
        {"exclude": ".*night_light.*"},
    ],
}

MOCK_ALL_STATES: Dict[str, Any] = {
    "light.basement_hue_01": {
        "state": "on",
        "attributes": {"friendly_name": "basement_hue_01"},
    },
    "light.basement_hue_02": {
        "state": "off",
        "attributes": {"friendly_name": "basement_hue_02"},
    },
    "light.basement_night_light": {
        "state": "on",
        "attributes": {"friendly_name": "Basement Night Light"},
    },
    "light.upstairs_hue_01": {
        "state": "on",
        "attributes": {"friendly_name": "Upstairs Hue 01"},
    },
}


def _make_app(
    extra_args: dict | None = None,
    all_states: dict | None = None,
) -> MqttDeviceChecker:
    """Create a MqttDeviceChecker with mocked AppDaemon methods."""
    ad = MagicMock()
    config = MagicMock()
    app = MqttDeviceChecker(ad, config)

    args = dict(DEFAULT_ARGS)
    if extra_args:
        args.update(extra_args)
    app.args = args

    states = all_states if all_states is not None else copy.deepcopy(MOCK_ALL_STATES)

    async def _async_all_states():
        return states

    def mock_get_state(entity_id=None, **kwargs):
        if entity_id is None:
            return _async_all_states()
        obj = states.get(entity_id)
        if obj is None:
            return None
        if isinstance(obj, dict) and "state" in obj:
            return obj["state"]
        return obj

    app.get_state = MagicMock(side_effect=mock_get_state)
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


def _startup(app: MqttDeviceChecker) -> None:
    app.initialize()
    try:
        _run(app._async_startup())
    except TypeError:
        _run(app._discover_entities())
        app._register()
        app.listen_event(app._on_controller_ready, "health_check_controller_ready")
        app.listen_event(app._on_recheck, "health_check_recheck")
        app.run_in(app._first_check, 10)


def _find_events(app, command):
    return [
        c
        for c in app.fire_event.call_args_list
        if c[0][0] == "health_check_command"
        and c[1].get("command") == command
    ]


# ---------------------------------------------------------------------------
# Tests -- Entity Discovery
# ---------------------------------------------------------------------------

class TestEntityDiscovery:
    def test_discovers_matching_entities(self):
        app = _make_app()
        _startup(app)
        assert "light.basement_hue_01" in app._entities
        assert "light.basement_hue_02" in app._entities

    def test_excludes_night_lights(self):
        app = _make_app()
        _startup(app)
        assert "light.basement_night_light" not in app._entities

    def test_excludes_non_matching(self):
        app = _make_app()
        _startup(app)
        assert "light.upstairs_hue_01" not in app._entities

    def test_logs_discovered_entities(self):
        app = _make_app()
        _startup(app)
        log_calls = [str(c) for c in app.log.call_args_list]
        assert any("basement_hue_01" in c for c in log_calls)


# ---------------------------------------------------------------------------
# Tests -- Registration
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_registers_with_controller(self):
        app = _make_app()
        _startup(app)
        calls = _find_events(app, "register_checker")
        assert len(calls) >= 1

    def test_check_names_include_state_and_mqtt(self):
        app = _make_app()
        _startup(app)
        calls = _find_events(app, "register_checker")
        payload = json.loads(calls[0][1]["payload"])
        names = payload["check_names"]
        assert any("State" in n for n in names)
        assert any("MQTT" in n for n in names)

    def test_dependencies_registered(self):
        app = _make_app()
        _startup(app)
        calls = _find_events(app, "register_checker")
        payload = json.loads(calls[0][1]["payload"])
        deps = payload.get("dependencies", [])
        assert len(deps) == 1
        assert deps[0]["checker_id"] == "zigbee"
        assert all(n.endswith(" MQTT") for n in deps[0]["affects_checks"])

    def test_no_dependency_when_empty(self):
        app = _make_app({"protocol_dependency_id": ""})
        _startup(app)
        calls = _find_events(app, "register_checker")
        payload = json.loads(calls[0][1]["payload"])
        assert payload.get("dependencies", []) == []


# ---------------------------------------------------------------------------
# Tests -- HA Entity Check
# ---------------------------------------------------------------------------

class TestHAEntityCheck:
    def test_ok_when_entity_has_valid_state(self):
        app = _make_app()
        _startup(app)
        status, detail = app._check_ha_entity("light.basement_hue_01")
        assert status == "ok"

    def test_critical_when_unavailable(self):
        app = _make_app()
        _startup(app)
        app.get_state = MagicMock(return_value="unavailable")
        status, detail = app._check_ha_entity("light.basement_hue_01")
        assert status == "critical"

    def test_critical_when_none(self):
        app = _make_app()
        _startup(app)
        app.get_state = MagicMock(return_value=None)
        status, detail = app._check_ha_entity("light.basement_hue_01")
        assert status == "critical"


# ---------------------------------------------------------------------------
# Tests -- MQTT Recency Check
# ---------------------------------------------------------------------------

class TestMqttRecencyCheck:
    def test_unknown_when_no_data(self):
        app = _make_app()
        _startup(app)
        status, detail = app._check_mqtt_recency("basement_hue_01", time.time())
        assert status == "unknown"
        assert "no MQTT data" in detail

    def test_ok_when_recent(self):
        app = _make_app()
        _startup(app)
        now = time.time()
        app._mqtt_last_seen["basement_hue_01"] = now - 60  # 1 min ago
        status, detail = app._check_mqtt_recency("basement_hue_01", now)
        assert status == "ok"

    def test_critical_when_stale(self):
        app = _make_app()
        _startup(app)
        now = time.time()
        app._mqtt_last_seen["basement_hue_01"] = now - 30000  # 8+ hours ago
        status, detail = app._check_mqtt_recency("basement_hue_01", now)
        assert status == "critical"
        assert "stale" in detail


# ---------------------------------------------------------------------------
# Tests -- MQTT Message Tracking
# ---------------------------------------------------------------------------

class TestMqttMessageTracking:
    def test_tracks_device_message(self):
        app = _make_app()
        _startup(app)
        app._on_mqtt_message("MQTT_MESSAGE", {
            "topic": "zigbee2mqtt/basement_hue_01",
            "payload": '{"state":"ON"}',
        }, {})
        assert "basement_hue_01" in app._mqtt_last_seen

    def test_ignores_bridge_topic(self):
        app = _make_app()
        _startup(app)
        app._on_mqtt_message("MQTT_MESSAGE", {
            "topic": "zigbee2mqtt/bridge/state",
            "payload": "online",
        }, {})
        assert "bridge" not in app._mqtt_last_seen

    def test_ignores_none_topic(self):
        app = _make_app()
        _startup(app)
        # Should not raise
        app._on_mqtt_message("MQTT_MESSAGE", {
            "topic": None,
            "state": "Connected",
        }, {})

    def test_ignores_wrong_prefix(self):
        app = _make_app()
        _startup(app)
        app._on_mqtt_message("MQTT_MESSAGE", {
            "topic": "homeassistant/light/foo",
            "payload": "{}",
        }, {})
        assert len(app._mqtt_last_seen) == 0


# ---------------------------------------------------------------------------
# Tests -- Cross-check Logic
# ---------------------------------------------------------------------------

class TestCrossCheckLogic:
    def test_ha_critical_mqtt_ok_becomes_warning(self):
        """When HA entity is unavailable but MQTT is recent, HA -> warning."""
        states = copy.deepcopy(MOCK_ALL_STATES)
        states["light.basement_hue_01"]["state"] = "unavailable"
        app = _make_app(all_states=states)
        _startup(app)
        app._mqtt_last_seen["basement_hue_01"] = time.time() - 60
        app.fire_event.reset_mock()

        app._run_checks()

        calls = _find_events(app, "report_status")
        payload = json.loads(calls[0][1]["payload"])
        ha_check = next(
            r for r in payload["results"]
            if r["name"] == "Basement Hue 01 State"
        )
        assert ha_check["status"] == "warning"
        assert "MQTT ok" in ha_check["detail"]

    def test_mqtt_stale_ha_ok_becomes_warning(self):
        """When MQTT is stale but HA entity is ok, MQTT -> warning."""
        app = _make_app()
        _startup(app)
        app._mqtt_last_seen["basement_hue_01"] = time.time() - 30000
        app.fire_event.reset_mock()

        app._run_checks()

        calls = _find_events(app, "report_status")
        payload = json.loads(calls[0][1]["payload"])
        mqtt_check = next(
            r for r in payload["results"]
            if r["name"] == "Basement Hue 01 MQTT"
        )
        assert mqtt_check["status"] == "warning"
        assert "HA state ok" in mqtt_check["detail"]

    def test_both_critical_stays_critical(self):
        """When HA is unavailable and MQTT is stale, both stay critical."""
        states = copy.deepcopy(MOCK_ALL_STATES)
        states["light.basement_hue_01"]["state"] = "unavailable"
        app = _make_app(all_states=states)
        _startup(app)
        app._mqtt_last_seen["basement_hue_01"] = time.time() - 30000
        app.fire_event.reset_mock()

        app._run_checks()

        calls = _find_events(app, "report_status")
        payload = json.loads(calls[0][1]["payload"])
        ha_check = next(
            r for r in payload["results"]
            if r["name"] == "Basement Hue 01 State"
        )
        mqtt_check = next(
            r for r in payload["results"]
            if r["name"] == "Basement Hue 01 MQTT"
        )
        assert ha_check["status"] == "critical"
        assert mqtt_check["status"] == "critical"


# ---------------------------------------------------------------------------
# Tests -- Check Cycle
# ---------------------------------------------------------------------------

class TestCheckCycle:
    def test_reports_all_entities(self):
        app = _make_app()
        _startup(app)
        app.fire_event.reset_mock()

        app._run_checks()

        calls = _find_events(app, "report_status")
        assert len(calls) == 1
        payload = json.loads(calls[0][1]["payload"])
        # 2 entities x 2 checks each = 4
        assert len(payload["results"]) == 4

    def test_force_recheck_runs_checks(self):
        app = _make_app()
        _startup(app)
        app.fire_event.reset_mock()

        app._on_recheck("health_check_recheck", {}, {})

        calls = _find_events(app, "report_status")
        assert len(calls) == 1


# ---------------------------------------------------------------------------
# Tests -- Short Name & Format Age
# ---------------------------------------------------------------------------

class TestShortName:
    def test_strips_domain_and_title_cases(self):
        app = _make_app()
        assert app._short_name("light.basement_hue_01") == "Basement Hue 01"

    def test_handles_no_domain(self):
        app = _make_app()
        assert app._short_name("basement_hue_01") == "Basement Hue 01"


class TestFormatAge:
    def test_seconds(self):
        app = _make_app()
        assert app._format_age(45) == "45s"

    def test_minutes(self):
        app = _make_app()
        assert app._format_age(300) == "5m"

    def test_hours(self):
        app = _make_app()
        assert app._format_age(7200) == "2h 0m"

    def test_days(self):
        app = _make_app()
        assert app._format_age(90000) == "1d 1h"
