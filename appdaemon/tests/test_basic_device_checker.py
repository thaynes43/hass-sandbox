"""Unit tests for BasicDeviceChecker."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

mock_hass = MagicMock()
mock_hass.Hass = type("_MockHass", (), {"__init__": lambda self, *a, **kw: None})
sys.modules["hassapi"] = mock_hass

_repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo_root / "apps"))
sys.path.insert(0, str(_repo_root))

from health_checks.checker_apps.device_checker.device_checker import (
    BasicDeviceChecker,
)


DEFAULT_ARGS: Dict[str, Any] = {
    "checker_id": "vestaboard",
    "checker_name": "Vestaboard",
    "ping_host": "192.168.50.159",
    "ping_check_name": "Ping",
    "check_interval_s": 180,
    "entities": [
        {
            "entity_id": "sensor.vestaboard_controller_status",
            "healthy_state": "active",
            "name": "Controller Status",
        },
        {
            "entity_id": "sensor.vestaboard_configuration_status",
            "healthy_state": "ok",
            "name": "Configuration Status",
        },
    ],
}


def _make_app(extra_args: dict | None = None) -> BasicDeviceChecker:
    ad = MagicMock()
    config = MagicMock()
    app = BasicDeviceChecker(ad, config)

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


def _startup(app: BasicDeviceChecker) -> None:
    app.initialize()
    _run(app._async_startup())


def _init_only(app: BasicDeviceChecker) -> None:
    app.initialize()


class TestLifecycle:
    def test_initialize_calls_run_in(self):
        app = _make_app()
        app.initialize()
        app.run_in.assert_called_once()

    def test_registers_correct_check_names(self):
        app = _make_app()
        _startup(app)
        register_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "register_checker"
        ]
        payload = json.loads(register_calls[0][1]["payload"])
        names = payload["check_names"]
        assert names == ["Ping", "Controller Status", "Configuration Status"]
        assert "supports_repair" not in payload

    def test_registers_without_ping(self):
        app = _make_app({"ping_host": ""})
        _startup(app)
        register_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "register_checker"
        ]
        payload = json.loads(register_calls[0][1]["payload"])
        assert "Ping" not in payload["check_names"]
        assert len(payload["check_names"]) == 2

    def test_registers_event_listeners(self):
        app = _make_app()
        _startup(app)
        event_names = [c[0][1] for c in app.listen_event.call_args_list]
        assert "health_check_controller_ready" in event_names
        assert "health_check_recheck" in event_names


class TestEntityChecks:
    def test_entity_ok(self):
        app = _make_app()
        _init_only(app)
        app.get_state = AsyncMock(return_value="active")
        result = _run(app._check_entity_state(app._entities[0]))
        assert result["status"] == "ok"
        assert result["name"] == "Controller Status"

    def test_entity_wrong_state(self):
        app = _make_app()
        _init_only(app)
        app.get_state = AsyncMock(return_value="error")
        result = _run(app._check_entity_state(app._entities[0]))
        assert result["status"] == "critical"
        assert "Expected 'active'" in result["detail"]

    def test_entity_not_found(self):
        app = _make_app()
        _init_only(app)
        app.get_state = AsyncMock(return_value=None)
        result = _run(app._check_entity_state(app._entities[0]))
        assert result["status"] == "critical"
        assert "None" in result["detail"]

    def test_yaml_bool_coercion(self):
        """YAML coerces 'on' to True — should be reversed."""
        app = _make_app({
            "entities": [
                {"entity_id": "binary_sensor.test", "healthy_state": True, "name": "Test"},
            ],
        })
        _init_only(app)
        assert app._entities[0]["healthy_state"] == "on"


class TestPingCheck:
    def test_ping_ok(self):
        app = _make_app()
        _init_only(app)
        with patch(
            "health_checks.checker_apps.device_checker.device_checker.ping_check",
            new_callable=AsyncMock,
            return_value={"status": "ok", "detail": "3ms"},
        ):
            result = _run(app._check_ping())
        assert result["status"] == "ok"
        assert result["name"] == "Ping"

    def test_ping_critical(self):
        app = _make_app()
        _init_only(app)
        with patch(
            "health_checks.checker_apps.device_checker.device_checker.ping_check",
            new_callable=AsyncMock,
            return_value={"status": "critical", "detail": "timeout"},
        ):
            result = _run(app._check_ping())
        assert result["status"] == "critical"

    def test_ping_attempts_defaults_to_one(self):
        app = _make_app()
        _init_only(app)
        with patch(
            "health_checks.checker_apps.device_checker.device_checker.ping_check",
            new_callable=AsyncMock,
            return_value={"status": "ok", "detail": "3ms"},
        ) as mock_ping:
            _run(app._check_ping())
        mock_ping.assert_awaited_once_with("192.168.50.159", attempts=1)

    def test_ping_attempts_passed_through(self):
        app = _make_app({"ping_attempts": 3})
        _init_only(app)
        with patch(
            "health_checks.checker_apps.device_checker.device_checker.ping_check",
            new_callable=AsyncMock,
            return_value={"status": "ok", "detail": "3ms"},
        ) as mock_ping:
            _run(app._check_ping())
        mock_ping.assert_awaited_once_with("192.168.50.159", attempts=3)


class TestPingFallbackHost:
    """``ping_fallback_host`` is pinged whenever the ping by name fails —
    ``cannot resolve <host>`` (resolver says NXDOMAIN) or a plain ``timeout``
    (resolver stopped answering, or the device is down) — so the device
    answers for itself: up → warning, down → critical. Without a fallback a
    failed ping stays critical."""

    HOST = "movieroomsonos.haynesnetwork"
    FALLBACK = "192.168.0.70"
    PING = "health_checks.checker_apps.device_checker.device_checker.ping_check"
    UNRESOLVED = {"status": "critical", "detail": f"cannot resolve {HOST} (3 attempts)"}

    def _app(self, fallback: str | None = FALLBACK):
        extra = {"ping_host": self.HOST, "ping_attempts": 3, "entities": []}
        if fallback is not None:
            extra["ping_fallback_host"] = fallback
        app = _make_app(extra)
        _init_only(app)
        return app

    def _ping(self, primary: dict, fallback: dict | None = None) -> AsyncMock:
        def _answer(host, attempts=1):
            return primary if host == self.HOST else fallback

        return AsyncMock(side_effect=_answer)

    def test_resolved_ok_never_touches_the_fallback(self):
        app = self._app()
        ping = self._ping({"status": "ok", "detail": "4ms"})

        with patch(self.PING, new=ping):
            result = _run(app._check_ping())

        assert result == {"name": "Ping", "status": "ok", "detail": "4ms"}
        ping.assert_awaited_once_with(self.HOST, attempts=3)

    def test_timeout_by_name_with_fallback_up_is_a_warning(self):
        """A resolver that stops answering makes the ping by name a plain
        timeout, not "cannot resolve": the fallback must engage for that too,
        or a DNS hang power-cycles a healthy device."""
        app = self._app()
        ping = self._ping(
            {"status": "critical", "detail": "timeout (3 attempts)"},
            {"status": "ok", "detail": "4ms"},
        )

        with patch(self.PING, new=ping):
            result = _run(app._check_ping())

        assert result == {
            "name": "Ping",
            "status": "warning",
            "detail": f"4ms via {self.FALLBACK} — {self.HOST}: timeout (3 attempts)",
        }

    def test_timeout_by_name_and_fallback_dead_is_critical(self):
        """The device really down: both fail, it stays critical (repairs, pages)."""
        app = self._app()
        ping = self._ping(
            {"status": "critical", "detail": "timeout (3 attempts)"},
            {"status": "critical", "detail": "timeout (3 attempts)"},
        )

        with patch(self.PING, new=ping):
            result = _run(app._check_ping())

        assert result["status"] == "critical"
        assert result["detail"] == (
            f"timeout (3 attempts) via {self.FALLBACK} — {self.HOST}: timeout (3 attempts)"
        )
        assert ping.await_count == 2

    def test_unresolved_and_fallback_ok_is_a_warning(self):
        app = self._app()
        ping = self._ping(self.UNRESOLVED, {"status": "ok", "detail": "4ms"})

        with patch(self.PING, new=ping):
            result = _run(app._check_ping())

        assert result == {
            "name": "Ping",
            "status": "warning",
            "detail": f"4ms via {self.FALLBACK} — cannot resolve {self.HOST} (3 attempts)",
        }
        assert ping.await_args_list[1].args == (self.FALLBACK,)
        assert ping.await_args_list[1].kwargs == {"attempts": 3}

    def test_unresolved_and_fallback_dead_is_critical(self):
        """A dead device still pages and repairs during a DNS outage."""
        app = self._app()
        ping = self._ping(
            self.UNRESOLVED, {"status": "critical", "detail": "timeout (3 attempts)"}
        )

        with patch(self.PING, new=ping):
            result = _run(app._check_ping())

        assert result == {
            "name": "Ping",
            "status": "critical",
            "detail": (
                f"timeout (3 attempts) via {self.FALLBACK} — "
                f"cannot resolve {self.HOST} (3 attempts)"
            ),
        }

    def test_unresolved_without_a_fallback_stays_critical(self):
        app = self._app(fallback=None)
        ping = self._ping(self.UNRESOLVED)

        with patch(self.PING, new=ping):
            result = _run(app._check_ping())

        assert result == {"name": "Ping", **self.UNRESOLVED}
        ping.assert_awaited_once()

    def test_fallback_use_is_logged_on_the_transitions_only(self):
        app = self._app()
        unresolved = self._ping(self.UNRESOLVED, {"status": "ok", "detail": "4ms"})
        resolved = self._ping({"status": "ok", "detail": "4ms"})

        with patch(self.PING, new=unresolved):
            _run(app._check_ping())
            _run(app._check_ping())
        with patch(self.PING, new=resolved):
            _run(app._check_ping())
            _run(app._check_ping())

        levels = [
            c[1].get("level") for c in app.log.call_args_list
            if self.HOST in str(c[0][0]) and "initialising" not in str(c[0][0])
        ]
        assert levels == ["WARNING", "INFO"]


class TestRunChecks:
    def test_reports_all_results(self):
        app = _make_app()
        _init_only(app)
        app.get_state = AsyncMock(side_effect=["active", "ok"])

        with patch(
            "health_checks.checker_apps.device_checker.device_checker.ping_check",
            new_callable=AsyncMock,
            return_value={"status": "ok", "detail": "3ms"},
        ):
            _run(app._run_checks())

        report_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
        ]
        assert len(report_calls) == 1
        payload = json.loads(report_calls[0][1]["payload"])
        assert len(payload["results"]) == 3  # ping + 2 entities
        assert "repair_state" not in payload

    def test_no_ping_skips_ping_check(self):
        app = _make_app({"ping_host": ""})
        _init_only(app)
        app.get_state = AsyncMock(side_effect=["active", "ok"])

        _run(app._run_checks())

        report_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
        ]
        payload = json.loads(report_calls[0][1]["payload"])
        assert len(payload["results"]) == 2


class TestPendingRepairEventsDrain:
    """BasicDeviceChecker itself never populates _pending_repair_events (it
    has no repair support), but it owns the drain logic that
    RepairableDeviceChecker relies on — verify it directly here."""

    def test_no_repair_events_key_when_buffer_empty(self):
        app = _make_app()
        _init_only(app)
        payload = app._build_report_payload([
            {"name": "Ping", "status": "ok", "detail": "3ms"},
        ])
        assert "repair_events" not in payload

    def test_drains_and_clears_pending_repair_events(self):
        app = _make_app()
        _init_only(app)
        app._pending_repair_events.append(
            {"result": "success", "duration_s": 12}
        )

        payload = app._build_report_payload([])
        assert payload["repair_events"] == [
            {"result": "success", "duration_s": 12}
        ]
        assert app._pending_repair_events == []

        # Next payload build must not repeat the drained event.
        payload2 = app._build_report_payload([])
        assert "repair_events" not in payload2
