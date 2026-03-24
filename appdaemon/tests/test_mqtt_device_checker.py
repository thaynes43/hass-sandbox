"""Unit tests for MqttDeviceChecker.

Mocks AppDaemon methods and HA state -- no real network or MQTT access.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

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
    "mqtt_namespace": "mqtt",
    "mqtt_topic_prefix": "zigbee2mqtt",
    "mqtt_stale_s": 600,
    "broker_dependency_id": "mqtt_broker",
    "entity_patterns": [
        {"include": "light\\.basement.*"},
        {"exclude": ".*night_light.*"},
    ],
}

# Simulated HA state for get_state()
MOCK_ALL_STATES: Dict[str, Any] = {
    "light.basement_hue_01": {
        "state": "on",
        "attributes": {"friendly_name": "Basement Hue 01"},
    },
    "light.basement_hue_02": {
        "state": "off",
        "attributes": {"friendly_name": "Basement Hue 02"},
    },
    "light.basement_night_light": {
        "state": "on",
        "attributes": {"friendly_name": "Basement Night Light"},
    },
    "light.upstairs_hue_01": {
        "state": "on",
        "attributes": {"friendly_name": "Upstairs Hue 01"},
    },
    "switch.basement_inovelli_dimmer": {
        "state": "on",
        "attributes": {"friendly_name": "Basement Inovelli Dimmer"},
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

    states = all_states if all_states is not None else dict(MOCK_ALL_STATES)

    # _discover_entities is async and awaits get_state() with no args.
    # _check_ha_entity calls get_state(entity_id) synchronously.
    # Use an AsyncMock that returns the full states dict when awaited (no args),
    # and falls back to sync behavior for single-entity lookups.
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
    # mqtt_subscribe via call_service("mqtt/subscribe", ...) — already mocked by call_service
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


def _startup(app: MqttDeviceChecker) -> None:
    """Initialize the app and run the async startup coroutine."""
    app.initialize()
    _run(app._async_startup())


def _find_events(app, command: str) -> list:
    """Find fire_event calls matching the given command."""
    return [
        c for c in app.fire_event.call_args_list
        if c[0][0] == "health_check_command"
        and c[1].get("command") == command
    ]


# ---------------------------------------------------------------------------
# Tests -- Entity Discovery
# ---------------------------------------------------------------------------

class TestEntityDiscovery:
    def test_discovers_matching_entities(self):
        """Include patterns should match expected entities."""
        app = _make_app()
        _startup(app)

        assert "light.basement_hue_01" in app._entities
        assert "light.basement_hue_02" in app._entities

    def test_excludes_night_lights(self):
        """Exclude patterns should filter out night light entities."""
        app = _make_app()
        _startup(app)

        assert "light.basement_night_light" not in app._entities

    def test_excludes_non_matching(self):
        """Non-matching entities should not be included."""
        app = _make_app()
        _startup(app)

        assert "light.upstairs_hue_01" not in app._entities
        assert "switch.basement_inovelli_dimmer" not in app._entities

    def test_empty_patterns_discovers_nothing(self):
        """No patterns should discover no entities."""
        app = _make_app({"entity_patterns": []})
        _startup(app)

        assert len(app._entities) == 0

    def test_include_only_no_excludes(self):
        """Include-only patterns work without excludes."""
        app = _make_app({
            "entity_patterns": [
                {"include": "light\\.basement.*"},
            ],
        })
        _startup(app)

        # All basement lights including night light
        assert "light.basement_hue_01" in app._entities
        assert "light.basement_night_light" in app._entities

    def test_uses_friendly_name_from_attributes(self):
        """Discovered entities should store friendly_name from attributes."""
        app = _make_app()
        _startup(app)

        assert app._entities["light.basement_hue_01"] == "Basement Hue 01"

    def test_discovery_handles_missing_attributes(self):
        """Entities without attributes dict should use entity_id as name."""
        states = {
            "light.basement_bare": {"state": "on"},
        }
        app = _make_app(all_states=states)
        _startup(app)

        assert app._entities.get("light.basement_bare") == "light.basement_bare"

    def test_discovery_skips_non_dict_state(self):
        """Non-dict state objects should be skipped."""
        states = {
            "light.basement_hue_01": "on",  # Just a string, not a dict
        }
        app = _make_app(all_states=states)
        _startup(app)

        assert "light.basement_hue_01" not in app._entities

    def test_logs_discovered_entities(self):
        """All matched entities should be logged on startup."""
        app = _make_app()
        _startup(app)

        # Check that log was called with entity IDs
        log_msgs = [str(c) for c in app.log.call_args_list]
        combined = " ".join(log_msgs)
        assert "basement_hue_01" in combined
        assert "basement_hue_02" in combined


# ---------------------------------------------------------------------------
# Tests -- Registration
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_registers_with_controller(self):
        """Startup should fire register_checker event."""
        app = _make_app()
        _startup(app)

        calls = _find_events(app, "register_checker")
        assert len(calls) == 1
        payload = json.loads(calls[0][1]["payload"])
        assert payload["checker_id"] == "basement_lights"
        assert payload["checker_name"] == "Basement Lights"

    def test_check_names_include_ha_and_mqtt(self):
        """Each entity should have both HA State and MQTT checks."""
        app = _make_app()
        _startup(app)

        calls = _find_events(app, "register_checker")
        payload = json.loads(calls[0][1]["payload"])
        check_names = payload["check_names"]

        # 2 entities * 2 checks each = 4
        assert len(check_names) == 4
        ha_checks = [n for n in check_names if n.endswith("HA State")]
        mqtt_checks = [n for n in check_names if n.endswith("MQTT")]
        assert len(ha_checks) == 2
        assert len(mqtt_checks) == 2

    def test_dependencies_registered(self):
        """MQTT checks should declare dependency on broker checker."""
        app = _make_app()
        _startup(app)

        calls = _find_events(app, "register_checker")
        payload = json.loads(calls[0][1]["payload"])
        deps = payload["dependencies"]

        assert len(deps) == 1
        assert deps[0]["checker_id"] == "mqtt_broker"
        # Only MQTT checks are affected
        for check_name in deps[0]["affects_checks"]:
            assert check_name.endswith(" MQTT")

    def test_no_dependency_when_empty_broker_id(self):
        """Empty broker_dependency_id should skip dependency."""
        app = _make_app({"broker_dependency_id": ""})
        _startup(app)

        calls = _find_events(app, "register_checker")
        payload = json.loads(calls[0][1]["payload"])
        assert payload["dependencies"] == []

    def test_controller_ready_re_registers(self):
        """Controller ready event should trigger re-registration."""
        app = _make_app()
        _startup(app)
        app.fire_event.reset_mock()

        app._on_controller_ready("health_check_controller_ready", {}, {})

        calls = _find_events(app, "register_checker")
        assert len(calls) == 1


# ---------------------------------------------------------------------------
# Tests -- HA Entity Check
# ---------------------------------------------------------------------------

class TestHAEntityCheck:
    def test_ok_when_entity_has_valid_state(self):
        app = _make_app()
        _startup(app)

        status, detail = app._check_ha_entity("light.basement_hue_01")
        assert status == "ok"
        assert "on" in detail

    def test_critical_when_unavailable(self):
        app = _make_app()
        _startup(app)
        app.get_state = MagicMock(return_value="unavailable")

        status, detail = app._check_ha_entity("light.basement_hue_01")
        assert status == "critical"
        assert "unavailable" in detail

    def test_critical_when_unknown(self):
        app = _make_app()
        _startup(app)
        app.get_state = MagicMock(return_value="unknown")

        status, detail = app._check_ha_entity("light.basement_hue_01")
        assert status == "critical"

    def test_critical_when_none(self):
        app = _make_app()
        _startup(app)
        app.get_state = MagicMock(return_value=None)

        status, detail = app._check_ha_entity("light.basement_hue_01")
        assert status == "critical"
        assert "not found" in detail

    def test_critical_on_exception(self):
        app = _make_app()
        _startup(app)
        app.get_state = MagicMock(side_effect=RuntimeError("boom"))

        status, detail = app._check_ha_entity("light.basement_hue_01")
        assert status == "critical"
        assert "error" in detail.lower()


# ---------------------------------------------------------------------------
# Tests -- MQTT Linkquality Check
# ---------------------------------------------------------------------------

class TestMQTTLinkquality:
    def test_unknown_when_no_data(self):
        app = _make_app()
        _startup(app)

        status, detail = app._check_mqtt_linkquality("Some Device", time.time())
        assert status == "unknown"
        assert "no MQTT data" in detail

    def test_ok_when_fresh_data(self):
        app = _make_app()
        _startup(app)
        now = time.time()
        app._mqtt_linkquality["Some Device"] = {
            "linkquality": 150,
            "last_seen": now - 30,  # 30s ago
        }

        status, detail = app._check_mqtt_linkquality("Some Device", now)
        assert status == "ok"
        assert "150" in detail

    def test_critical_when_stale(self):
        app = _make_app()
        _startup(app)
        now = time.time()
        app._mqtt_linkquality["Some Device"] = {
            "linkquality": 100,
            "last_seen": now - 700,  # 700s ago, stale_s=600
        }

        status, detail = app._check_mqtt_linkquality("Some Device", now)
        assert status == "critical"
        assert "stale" in detail


# ---------------------------------------------------------------------------
# Tests -- MQTT Message Tracking
# ---------------------------------------------------------------------------

class TestMQTTMessageTracking:
    def test_tracks_linkquality_message(self):
        app = _make_app()
        _startup(app)

        app._on_mqtt_message(
            "MQTT_MESSAGE",
            {
                "topic": "zigbee2mqtt/Basement Hue 01",
                "payload": json.dumps({"linkquality": 200, "state": "ON"}),
            },
            {},
        )

        assert "Basement Hue 01" in app._mqtt_linkquality
        assert app._mqtt_linkquality["Basement Hue 01"]["linkquality"] == 200

    def test_ignores_messages_without_linkquality(self):
        app = _make_app()
        _startup(app)

        app._on_mqtt_message(
            "MQTT_MESSAGE",
            {
                "topic": "zigbee2mqtt/Basement Hue 01",
                "payload": json.dumps({"state": "ON"}),
            },
            {},
        )

        assert "Basement Hue 01" not in app._mqtt_linkquality

    def test_ignores_bridge_topic(self):
        app = _make_app()
        _startup(app)

        app._on_mqtt_message(
            "MQTT_MESSAGE",
            {
                "topic": "zigbee2mqtt/bridge",
                "payload": json.dumps({"linkquality": 100}),
            },
            {},
        )

        assert "bridge" not in app._mqtt_linkquality

    def test_ignores_group_topic(self):
        app = _make_app()
        _startup(app)

        app._on_mqtt_message(
            "MQTT_MESSAGE",
            {
                "topic": "zigbee2mqtt/group",
                "payload": json.dumps({"linkquality": 100}),
            },
            {},
        )

        assert "group" not in app._mqtt_linkquality

    def test_ignores_wrong_prefix(self):
        app = _make_app()
        _startup(app)

        app._on_mqtt_message(
            "MQTT_MESSAGE",
            {
                "topic": "homeassistant/some/topic",
                "payload": json.dumps({"linkquality": 100}),
            },
            {},
        )

        assert len(app._mqtt_linkquality) == 0

    def test_handles_malformed_payload(self):
        app = _make_app()
        _startup(app)

        # Should not raise
        app._on_mqtt_message(
            "MQTT_MESSAGE",
            {"topic": "zigbee2mqtt/device1", "payload": "not-json"},
            {},
        )
        app._on_mqtt_message(
            "MQTT_MESSAGE",
            {"topic": "zigbee2mqtt/device1", "payload": None},
            {},
        )

    def test_extracts_device_name_from_subtopic(self):
        """Device name is extracted from first path segment after prefix."""
        app = _make_app()
        _startup(app)

        app._on_mqtt_message(
            "MQTT_MESSAGE",
            {
                "topic": "zigbee2mqtt/My Device/availability",
                "payload": json.dumps({"linkquality": 50}),
            },
            {},
        )

        assert "My Device" in app._mqtt_linkquality

    def test_dict_payload_handled(self):
        """Payload already parsed as dict should work."""
        app = _make_app()
        _startup(app)

        app._on_mqtt_message(
            "MQTT_MESSAGE",
            {
                "topic": "zigbee2mqtt/Device1",
                "payload": {"linkquality": 99},
            },
            {},
        )

        assert app._mqtt_linkquality["Device1"]["linkquality"] == 99


# ---------------------------------------------------------------------------
# Tests -- Cross-Check Warning Logic
# ---------------------------------------------------------------------------

class TestCrossCheckLogic:
    def test_ha_critical_mqtt_ok_becomes_warning(self):
        """HA critical + MQTT ok should downgrade HA to warning."""
        app = _make_app()
        _startup(app)
        app.fire_event.reset_mock()

        # Set MQTT data as fresh for discovered entities
        now = time.time()
        for friendly_name in app._entities.values():
            app._mqtt_linkquality[friendly_name] = {
                "linkquality": 100,
                "last_seen": now - 10,
            }

        # Make HA return unavailable
        app.get_state = MagicMock(return_value="unavailable")

        app._run_checks()

        calls = _find_events(app, "report_status")
        payload = json.loads(calls[0][1]["payload"])

        ha_results = [r for r in payload["results"] if "HA State" in r["name"]]
        for r in ha_results:
            assert r["status"] == "warning"
            assert "MQTT ok" in r["detail"]

    def test_both_critical_stays_critical(self):
        """HA critical + MQTT critical should keep HA as critical."""
        app = _make_app()
        _startup(app)
        app.fire_event.reset_mock()

        # No MQTT data -> MQTT will be unknown, not ok
        app.get_state = MagicMock(return_value="unavailable")

        app._run_checks()

        calls = _find_events(app, "report_status")
        payload = json.loads(calls[0][1]["payload"])

        ha_results = [r for r in payload["results"] if "HA State" in r["name"]]
        for r in ha_results:
            # MQTT is unknown (no data), not ok, so HA stays critical
            assert r["status"] == "critical"

    def test_ha_ok_mqtt_critical_no_downgrade(self):
        """HA ok + MQTT critical should keep both as-is."""
        app = _make_app()
        _startup(app)
        app.fire_event.reset_mock()

        # Fresh MQTT but stale
        now = time.time()
        for friendly_name in app._entities.values():
            app._mqtt_linkquality[friendly_name] = {
                "linkquality": 100,
                "last_seen": now - 1000,  # Stale
            }

        app._run_checks()

        calls = _find_events(app, "report_status")
        payload = json.loads(calls[0][1]["payload"])

        ha_results = [r for r in payload["results"] if "HA State" in r["name"]]
        mqtt_results = [r for r in payload["results"] if "MQTT" in r["name"]]
        for r in ha_results:
            assert r["status"] == "ok"
        for r in mqtt_results:
            assert r["status"] == "critical"


# ---------------------------------------------------------------------------
# Tests -- Full Check Cycle
# ---------------------------------------------------------------------------

class TestCheckCycle:
    def test_reports_all_entities(self):
        """Check cycle should report results for all discovered entities."""
        app = _make_app()
        _startup(app)
        app.fire_event.reset_mock()

        app._run_checks()

        calls = _find_events(app, "report_status")
        assert len(calls) == 1
        payload = json.loads(calls[0][1]["payload"])
        assert payload["checker_id"] == "basement_lights"
        # 2 entities * 2 checks = 4
        assert len(payload["results"]) == 4

    def test_force_recheck_runs_checks(self):
        """Recheck event should trigger check execution."""
        app = _make_app()
        _startup(app)
        app.fire_event.reset_mock()

        app._on_recheck("health_check_recheck", {}, {})

        calls = _find_events(app, "report_status")
        assert len(calls) == 1

    def test_no_entities_reports_empty(self):
        """Checker with no matched entities should report empty results."""
        app = _make_app({"entity_patterns": []})
        _startup(app)
        app.fire_event.reset_mock()

        app._run_checks()

        calls = _find_events(app, "report_status")
        assert len(calls) == 1
        payload = json.loads(calls[0][1]["payload"])
        assert payload["results"] == []


# ---------------------------------------------------------------------------
# Tests -- Short Name Generation
# ---------------------------------------------------------------------------

class TestShortName:
    def test_strips_domain_and_title_cases(self):
        app = _make_app()
        _startup(app)

        assert app._short_name("light.basement_hue_01") == "Basement Hue 01"

    def test_handles_no_domain(self):
        app = _make_app()
        _startup(app)

        assert app._short_name("no_domain_entity") == "No Domain Entity"
