"""Unit tests for MqttBrokerChecker.

Mocks AppDaemon methods and MQTT plugin -- no real broker access.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

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

from health_checks.checker_apps.mqtt_broker_checker.mqtt_broker_checker import (
    MqttBrokerChecker,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DEFAULT_ARGS: Dict[str, Any] = {
    "checker_id": "mqtt_broker",
    "checker_name": "MQTT Broker",
    "check_interval_s": 120,
    "ping_timeout_s": 10,
    "mqtt_namespace": "mqtt",
    "ping_topic": "appdaemon/health_check/ping",
}


def _make_app(extra_args: dict | None = None) -> MqttBrokerChecker:
    """Create a MqttBrokerChecker with mocked AppDaemon methods."""
    ad = MagicMock()
    config = MagicMock()
    app = MqttBrokerChecker(ad, config)

    args = dict(DEFAULT_ARGS)
    if extra_args:
        args.update(extra_args)
    app.args = args

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
    # mqtt_publish is no longer used; broker checker calls call_service("mqtt/publish", ...)
    app.mqtt_subscribe = MagicMock()

    return app


def _run(coro):
    """Run a coroutine in a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _startup(app: MqttBrokerChecker) -> None:
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
# Tests -- Lifecycle
# ---------------------------------------------------------------------------

class TestLifecycle:
    def test_initialize_calls_run_in(self):
        app = _make_app()
        app.initialize()
        app.run_in.assert_called_once()

    def test_startup_registers_with_controller(self):
        app = _make_app()
        _startup(app)

        calls = _find_events(app, "register_checker")
        assert len(calls) == 1
        payload = json.loads(calls[0][1]["payload"])
        assert payload["checker_id"] == "mqtt_broker"
        assert payload["checker_name"] == "MQTT Broker"
        assert payload["check_names"] == ["Broker Connectivity"]

    def test_startup_listens_for_controller_ready(self):
        app = _make_app()
        _startup(app)
        listen_calls = [
            c for c in app.listen_event.call_args_list
            if c[0][1] == "health_check_controller_ready"
        ]
        assert len(listen_calls) == 1

    def test_startup_listens_for_recheck(self):
        app = _make_app()
        _startup(app)
        listen_calls = [
            c for c in app.listen_event.call_args_list
            if c[0][1] == "health_check_recheck"
        ]
        assert len(listen_calls) == 1

    def test_startup_subscribes_to_mqtt(self):
        app = _make_app()
        _startup(app)
        mqtt_calls = [
            c for c in app.listen_event.call_args_list
            if len(c[0]) > 1 and c[0][1] == "MQTT_MESSAGE"
        ]
        assert len(mqtt_calls) == 1
        assert mqtt_calls[0][1].get("namespace") == "mqtt"

    def test_startup_schedules_first_check(self):
        app = _make_app()
        _startup(app)
        # run_in called in initialize() and _async_startup for first_check
        assert app.run_in.call_count >= 2


# ---------------------------------------------------------------------------
# Tests -- Registration
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_controller_ready_re_registers(self):
        app = _make_app()
        _startup(app)
        app.fire_event.reset_mock()

        app._on_controller_ready("health_check_controller_ready", {}, {})

        calls = _find_events(app, "register_checker")
        assert len(calls) == 1

    def test_controller_ready_runs_ping(self):
        app = _make_app()
        _startup(app)
        app.call_service.reset_mock()

        app._on_controller_ready("health_check_controller_ready", {}, {})

        # Should have published a ping via call_service("mqtt/publish", ...)
        mqtt_calls = [c for c in app.call_service.call_args_list if c[0][0] == "mqtt/publish"]
        assert len(mqtt_calls) == 1


# ---------------------------------------------------------------------------
# Tests -- Ping/Pong
# ---------------------------------------------------------------------------

class TestPingPong:
    def test_run_ping_publishes_to_topic(self):
        app = _make_app()
        _startup(app)
        app.call_service.reset_mock()
        app.run_in.reset_mock()

        app._run_ping()

        mqtt_calls = [c for c in app.call_service.call_args_list if c[0][0] == "mqtt/publish"]
        assert len(mqtt_calls) == 1
        call_kwargs = mqtt_calls[0][1]
        assert call_kwargs["topic"] == "appdaemon/health_check/ping"
        msg = json.loads(call_kwargs["payload"])
        assert "nonce" in msg
        assert "ts" in msg
        assert call_kwargs["namespace"] == "mqtt"

    def test_run_ping_schedules_timeout(self):
        app = _make_app()
        _startup(app)
        app.run_in.reset_mock()

        app._run_ping()

        # Should schedule _evaluate_ping
        timeout_calls = [
            c for c in app.run_in.call_args_list
            if c[0][0] == app._evaluate_ping
        ]
        assert len(timeout_calls) == 1
        assert timeout_calls[0][0][1] == 10  # ping_timeout_s

    def test_pong_received_reports_ok(self):
        app = _make_app()
        _startup(app)
        app.fire_event.reset_mock()

        # Simulate a ping
        app._run_ping()
        nonce = app._current_nonce
        ts = time.time()

        # Simulate receiving the pong
        app._on_mqtt_message(
            "MQTT_MESSAGE",
            {"payload": json.dumps({"nonce": nonce, "ts": ts})},
            {},
        )

        calls = _find_events(app, "report_status")
        assert len(calls) == 1
        payload = json.loads(calls[0][1]["payload"])
        assert payload["results"][0]["status"] == "ok"
        assert "round-trip" in payload["results"][0]["detail"]

    def test_pong_wrong_nonce_ignored(self):
        app = _make_app()
        _startup(app)
        app.fire_event.reset_mock()

        app._run_ping()

        # Pong with wrong nonce
        app._on_mqtt_message(
            "MQTT_MESSAGE",
            {"payload": json.dumps({"nonce": "wrong", "ts": time.time()})},
            {},
        )

        # Should not report status (wrong nonce doesn't match current)
        calls = _find_events(app, "report_status")
        assert len(calls) == 0

    def test_timeout_reports_critical(self):
        """After startup retries are exhausted, timeout reports critical."""
        app = _make_app()
        _startup(app)
        app.fire_event.reset_mock()

        # Start a ping (sets the initial nonce)
        app._run_ping()

        # Exhaust startup retries — each retry calls _run_ping which changes nonce
        for _ in range(app._startup_retries + 1):
            nonce = app._current_nonce
            app._evaluate_ping({"nonce": nonce})

        calls = _find_events(app, "report_status")
        assert len(calls) == 1
        payload = json.loads(calls[0][1]["payload"])
        assert payload["results"][0]["status"] == "critical"
        assert "timeout" in payload["results"][0]["detail"]

    def test_startup_retry_on_timeout(self):
        """During startup, timeout retries instead of reporting critical."""
        app = _make_app()
        _startup(app)
        app.fire_event.reset_mock()

        app._run_ping()
        nonce = app._current_nonce
        retries_before = app._startup_retries

        # First timeout should retry, not report
        app._evaluate_ping({"nonce": nonce})

        assert app._startup_retries == retries_before - 1
        calls = _find_events(app, "report_status")
        assert len(calls) == 0  # No critical reported yet

    def test_no_startup_retry_after_success(self):
        """After a successful pong, timeout goes straight to critical."""
        app = _make_app()
        _startup(app)
        app._ever_succeeded = True
        app.fire_event.reset_mock()

        app._run_ping()
        nonce = app._current_nonce
        app._evaluate_ping({"nonce": nonce})

        calls = _find_events(app, "report_status")
        assert len(calls) == 1
        payload = json.loads(calls[0][1]["payload"])
        assert payload["results"][0]["status"] == "critical"

    def test_stale_timeout_ignored(self):
        app = _make_app()
        _startup(app)
        app.fire_event.reset_mock()

        app._run_ping()
        first_nonce = app._current_nonce

        # Run a second ping (changes nonce)
        app._run_ping()

        # Old timeout fires
        app._evaluate_ping({"nonce": first_nonce})

        # Should not report for old nonce
        calls = _find_events(app, "report_status")
        assert len(calls) == 0

    def test_timeout_skipped_if_pong_received(self):
        app = _make_app()
        _startup(app)
        app.fire_event.reset_mock()

        app._run_ping()
        nonce = app._current_nonce

        # Pong arrives
        app._on_mqtt_message(
            "MQTT_MESSAGE",
            {"payload": json.dumps({"nonce": nonce, "ts": time.time()})},
            {},
        )

        # Timeout fires late -- should be ignored
        app.fire_event.reset_mock()
        app._evaluate_ping({"nonce": nonce})

        calls = _find_events(app, "report_status")
        assert len(calls) == 0

    def test_publish_failure_reports_critical(self):
        app = _make_app()
        _startup(app)
        app.fire_event.reset_mock()
        app.call_service.side_effect = RuntimeError("connection lost")

        app._run_ping()

        calls = _find_events(app, "report_status")
        assert len(calls) == 1
        payload = json.loads(calls[0][1]["payload"])
        assert payload["results"][0]["status"] == "critical"
        assert "publish failed" in payload["results"][0]["detail"]

    def test_malformed_pong_payload_handled(self):
        app = _make_app()
        _startup(app)

        # Should not raise
        app._on_mqtt_message(
            "MQTT_MESSAGE",
            {"payload": "not-json"},
            {},
        )
        app._on_mqtt_message(
            "MQTT_MESSAGE",
            {"payload": "{}"},
            {},
        )
        app._on_mqtt_message(
            "MQTT_MESSAGE",
            {"payload": None},
            {},
        )


# ---------------------------------------------------------------------------
# Tests -- Force recheck
# ---------------------------------------------------------------------------

class TestForceRecheck:
    def test_recheck_runs_ping(self):
        app = _make_app()
        _startup(app)
        app.call_service.reset_mock()

        app._on_recheck("health_check_recheck", {}, {})

        mqtt_calls = [c for c in app.call_service.call_args_list if c[0][0] == "mqtt/publish"]
        assert len(mqtt_calls) == 1


# ---------------------------------------------------------------------------
# Tests -- Custom config
# ---------------------------------------------------------------------------

class TestCustomConfig:
    def test_custom_checker_id(self):
        app = _make_app({"checker_id": "custom_broker", "checker_name": "Custom"})
        _startup(app)

        calls = _find_events(app, "register_checker")
        payload = json.loads(calls[0][1]["payload"])
        assert payload["checker_id"] == "custom_broker"
        assert payload["checker_name"] == "Custom"

    def test_custom_ping_topic(self):
        app = _make_app({"ping_topic": "test/health/ping"})
        _startup(app)
        app.call_service.reset_mock()

        app._run_ping()
        mqtt_calls = [c for c in app.call_service.call_args_list if c[0][0] == "mqtt/publish"]
        assert mqtt_calls[0][1]["topic"] == "test/health/ping"
