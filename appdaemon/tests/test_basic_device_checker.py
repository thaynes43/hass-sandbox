"""Unit tests for BasicDeviceChecker."""

from __future__ import annotations

import asyncio
import json
import socket
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
    """With a ``ping_fallback_host``, ``ping_host`` is resolved first.

    Resolves → the ping by name is authoritative: its result stands and the
    fallback is never pinged, so a device that merely drops ICMP keeps the
    configured miss tolerance and is not blamed on DNS. Does not resolve
    (``gaierror``, or a resolver that hangs past ``PING_RESOLVE_TIMEOUT_S``) →
    the fallback IP answers: up → warning, down → critical. Without a
    fallback nothing changes: no pre-resolve, the plain ping by name.
    """

    HOST = "movieroomsonos.haynesnetwork"
    FALLBACK = "192.168.0.70"
    MOD = "health_checks.checker_apps.device_checker.device_checker"
    PING = f"{MOD}.ping_check"
    UP = {"status": "ok", "detail": "4ms"}
    DOWN = {"status": "critical", "detail": "timeout (3 attempts)"}

    def _app(self, fallback: str | None = FALLBACK):
        extra = {"ping_host": self.HOST, "ping_attempts": 3, "entities": []}
        if fallback is not None:
            extra["ping_fallback_host"] = fallback
        app = _make_app(extra)
        _init_only(app)
        return app

    def _ping(self, by_name: dict | None = None, fallback: dict | None = None) -> AsyncMock:
        def _answer(host, attempts=1):
            return by_name if host == self.HOST else fallback

        return AsyncMock(side_effect=_answer)

    @staticmethod
    def _resolver(outcome):
        """Patch the event loop's getaddrinfo: "ok", "nxdomain" or "hang"."""

        async def _getaddrinfo(host, port, *args, **kwargs):
            if outcome == "nxdomain":
                raise socket.gaierror(-2, "Name does not resolve")
            if outcome == "hang":
                await asyncio.Event().wait()
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.0.70", 0))]

        return patch.object(
            asyncio.BaseEventLoop, "getaddrinfo",
            new=AsyncMock(side_effect=_getaddrinfo),
        )

    def _check(self, app, ping, resolver="ok"):
        with self._resolver(resolver) as getaddrinfo, patch(self.PING, new=ping):
            result = _run(app._check_ping())
        return result, getaddrinfo

    def test_resolves_and_ping_ok(self):
        app = self._app()
        ping = self._ping(self.UP)

        result, getaddrinfo = self._check(app, ping)

        assert result == {"name": "Ping", **self.UP}
        getaddrinfo.assert_awaited_once_with(self.HOST, None, family=socket.AF_INET)
        ping.assert_awaited_once_with(self.HOST, attempts=3)

    def test_resolves_and_ping_times_out_is_critical_without_the_fallback(self):
        """The name resolved, so the ping by name is authoritative: the device
        is down (or dropping ICMP). One ping_check of 3 attempts, no fallback —
        the miss tolerance stays 3, and DNS is not blamed."""
        app = self._app()
        ping = self._ping(self.DOWN, self.UP)

        result, _ = self._check(app, ping)

        assert result == {"name": "Ping", **self.DOWN}
        ping.assert_awaited_once_with(self.HOST, attempts=3)

    def test_nxdomain_and_fallback_up_is_a_warning(self):
        app = self._app()
        ping = self._ping(fallback=self.UP)

        result, _ = self._check(app, ping, "nxdomain")

        assert result == {
            "name": "Ping",
            "status": "warning",
            "detail": f"4ms via {self.FALLBACK} — cannot resolve {self.HOST}",
        }
        # The name is never pinged; the fallback gets the same attempts.
        ping.assert_awaited_once_with(self.FALLBACK, attempts=3)

    def test_resolver_timeout_and_fallback_up_is_a_warning(self):
        """A hung resolver (no answer at all) is a broken name path too."""
        app = self._app()
        ping = self._ping(fallback=self.UP)

        with patch(f"{self.MOD}.PING_RESOLVE_TIMEOUT_S", 0.05):
            result, _ = self._check(app, ping, "hang")

        assert result == {
            "name": "Ping",
            "status": "warning",
            "detail": f"4ms via {self.FALLBACK} — cannot resolve {self.HOST}",
        }
        ping.assert_awaited_once_with(self.FALLBACK, attempts=3)

    def test_nxdomain_and_fallback_dead_is_critical(self):
        """A dead device still pages and repairs during a DNS outage."""
        app = self._app()
        ping = self._ping(fallback=self.DOWN)

        result, _ = self._check(app, ping, "nxdomain")

        assert result == {
            "name": "Ping",
            "status": "critical",
            "detail": (
                f"timeout (3 attempts) via {self.FALLBACK} — "
                f"cannot resolve {self.HOST}"
            ),
        }

    def test_ping_that_cannot_resolve_after_a_good_lookup_uses_the_fallback(self):
        """A race: the name resolved here, then not in ``ping``."""
        app = self._app()
        ping = self._ping(
            {"status": "critical", "detail": f"cannot resolve {self.HOST} (3 attempts)"},
            self.UP,
        )

        result, _ = self._check(app, ping)

        assert result["status"] == "warning"
        assert result["detail"] == f"4ms via {self.FALLBACK} — cannot resolve {self.HOST}"

    def test_no_fallback_means_no_resolve_call(self):
        app = self._app(fallback=None)
        unresolved = {
            "status": "critical",
            "detail": f"cannot resolve {self.HOST} (3 attempts)",
        }
        ping = self._ping(unresolved)

        result, getaddrinfo = self._check(app, ping, "nxdomain")

        getaddrinfo.assert_not_called()
        ping.assert_awaited_once_with(self.HOST, attempts=3)
        assert result == {"name": "Ping", **unresolved}

    def test_fallback_use_is_logged_on_the_transitions_only(self):
        app = self._app()
        ping = self._ping(self.UP, self.UP)

        for outcome in ("nxdomain", "nxdomain", "ok", "ok"):
            self._check(app, ping, outcome)

        lines = [
            (c[1].get("level"), c[0][0]) for c in app.log.call_args_list
            if self.HOST in str(c[0][0]) and "initialising" not in str(c[0][0])
        ]
        assert [level for level, _ in lines] == ["WARNING", "INFO"]
        # startswith, not `in`: the resolver's own error text says
        # "Name does not resolve", which would satisfy a bare substring check.
        assert lines[0][1].startswith(f"{self.HOST} does not resolve (")
        assert lines[1][1].startswith(f"{self.HOST} resolves again")


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
