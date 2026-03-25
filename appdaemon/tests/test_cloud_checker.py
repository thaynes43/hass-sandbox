"""Unit tests for CloudChecker.

Mocks AppDaemon methods and http_check — no real network access required.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Mock hassapi before importing the app
# ---------------------------------------------------------------------------
mock_hass = MagicMock()
mock_hass.Hass = type("_MockHass", (), {"__init__": lambda self, *a, **kw: None})
sys.modules["hassapi"] = mock_hass

_repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo_root / "apps"))
sys.path.insert(0, str(_repo_root))

from health_checks.checker_apps.cloud_checker.cloud_checker import CloudChecker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DEFAULT_ARGS: Dict[str, Any] = {
    "checker_id": "cloud",
    "checker_name": "Cloud",
    "check_interval_s": 120,
    "check_timeout_s": 5,
    "force_fail": False,
    "checks": [
        {"url": "https://www.google.com", "name": "Google"},
    ],
}


def _make_app(extra_args: dict | None = None) -> CloudChecker:
    ad = MagicMock()
    config = MagicMock()
    app = CloudChecker(ad, config)

    args = dict(DEFAULT_ARGS)
    if extra_args:
        args.update(extra_args)
    app.args = args

    app.get_state = MagicMock(return_value=None)
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
    """Run a coroutine in a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _startup(app: CloudChecker) -> None:
    """Initialize the app and run the async startup coroutine."""
    app.initialize()
    _run(app._async_startup())


# ---------------------------------------------------------------------------
# Tests — Lifecycle
# ---------------------------------------------------------------------------

class TestLifecycle:
    def test_initialize_calls_run_in(self):
        """initialize() should schedule startup via run_in."""
        app = _make_app()
        app.initialize()
        app.run_in.assert_called_once()

    def test_initialize_parses_checks(self):
        """initialize() should parse the checks list from args."""
        app = _make_app()
        app.initialize()
        assert len(app._checks) == 1
        assert app._checks[0]["url"] == "https://www.google.com"
        assert app._checks[0]["name"] == "Google"

    def test_initialize_defaults(self):
        """initialize() should apply default values for optional args."""
        app = _make_app({"check_interval_s": None, "check_timeout_s": None})
        # Override with explicit removal to test defaults
        app2 = _make_app({})
        app2.args.pop("check_interval_s", None)
        app2.args.pop("check_timeout_s", None)
        app2.initialize()
        assert app2._check_interval_s == 120
        assert app2._check_timeout_s == 5

    def test_initialize_force_fail_false_by_default(self):
        """force_fail should default to False when not configured."""
        app = _make_app()
        app.args.pop("force_fail", None)
        app.initialize()
        assert app._force_fail is False

    def test_startup_registers_with_controller(self):
        """Async startup should fire a register_checker event."""
        app = _make_app()
        _startup(app)
        app.fire_event.assert_called()
        # Find the register_checker call
        register_call = next(
            c for c in app.fire_event.call_args_list
            if c[0][0] == "health_check_command"
            and c[1].get("command") == "register_checker"
        )
        payload = json.loads(register_call[1]["payload"])
        assert payload["checker_id"] == "cloud"
        assert payload["checker_name"] == "Cloud"
        assert "Google" in payload["check_names"]

    def test_startup_listens_for_controller_ready(self):
        """Async startup should listen for health_check_controller_ready."""
        app = _make_app()
        _startup(app)
        listen_calls = [c[0] for c in app.listen_event.call_args_list]
        event_names = [c[1] for c in listen_calls]
        assert "health_check_controller_ready" in event_names

    def test_startup_listens_for_recheck(self):
        """Async startup should listen for health_check_recheck."""
        app = _make_app()
        _startup(app)
        listen_calls = [c[0] for c in app.listen_event.call_args_list]
        event_names = [c[1] for c in listen_calls]
        assert "health_check_recheck" in event_names

    def test_startup_schedules_first_check(self):
        """Async startup should schedule the first check via run_in."""
        app = _make_app()
        _startup(app)
        app.run_in.assert_called()


# ---------------------------------------------------------------------------
# Tests — Registration
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_register_includes_check_names(self):
        """Registration payload should include all configured check names."""
        app = _make_app({
            "checks": [
                {"url": "https://www.google.com", "name": "Google"},
                {"url": "https://www.cloudflare.com", "name": "Cloudflare"},
            ]
        })
        app.initialize()
        app._register()

        call = app.fire_event.call_args
        assert call[0][0] == "health_check_command"
        payload = json.loads(call[1]["payload"])
        assert payload["check_names"] == ["Google", "Cloudflare"]

    def test_register_with_no_checks(self):
        """Registration with empty checks list should produce empty check_names."""
        app = _make_app({"checks": []})
        app.initialize()
        app._register()

        call = app.fire_event.call_args
        payload = json.loads(call[1]["payload"])
        assert payload["check_names"] == []

    def test_re_register_on_controller_ready(self):
        """_on_controller_ready should re-register and trigger a check."""
        app = _make_app()
        _startup(app)
        app.fire_event.reset_mock()
        app.create_task.reset_mock()

        app._on_controller_ready("health_check_controller_ready", {}, {})

        # Should have fired register_checker
        register_calls = [
            c for c in app.fire_event.call_args_list
            if c[0][0] == "health_check_command"
        ]
        assert len(register_calls) >= 1
        # Should have scheduled a check
        app.create_task.assert_called()


# ---------------------------------------------------------------------------
# Tests — force_fail mode
# ---------------------------------------------------------------------------

class TestForceFail:
    def test_force_fail_reports_critical(self):
        """In force_fail mode, all checks should report critical."""
        app = _make_app({"force_fail": True})
        app.initialize()

        _run(app._run_checks())

        call = app.fire_event.call_args
        assert call[0][0] == "health_check_command"
        payload = json.loads(call[1]["payload"])
        assert payload["checker_id"] == "cloud"
        assert len(payload["results"]) == 1
        assert payload["results"][0]["name"] == "Google"
        assert payload["results"][0]["status"] == "critical"
        assert "testing" in payload["results"][0]["detail"]

    def test_force_fail_does_not_call_http_check(self):
        """In force_fail mode, http_check should never be called."""
        app = _make_app({"force_fail": True})
        app.initialize()

        with patch(
            "health_checks.checker_apps.cloud_checker.cloud_checker.http_check"
        ) as mock_http:
            _run(app._run_checks())
            mock_http.assert_not_called()

    def test_force_fail_multiple_checks(self):
        """force_fail mode should mark all configured URLs as critical."""
        app = _make_app({
            "force_fail": True,
            "checks": [
                {"url": "https://www.google.com", "name": "Google"},
                {"url": "https://1.1.1.1", "name": "Cloudflare DNS"},
            ],
        })
        app.initialize()

        _run(app._run_checks())

        call = app.fire_event.call_args
        payload = json.loads(call[1]["payload"])
        assert len(payload["results"]) == 2
        for result in payload["results"]:
            assert result["status"] == "critical"


# ---------------------------------------------------------------------------
# Tests — Normal (non-force-fail) mode
# ---------------------------------------------------------------------------

class TestNormalMode:
    def test_ok_result_when_http_check_passes(self):
        """When http_check returns ok, the check result should be ok."""
        app = _make_app({"force_fail": False})
        app.initialize()

        with patch(
            "health_checks.checker_apps.cloud_checker.cloud_checker.http_check",
            new=AsyncMock(return_value={"status": "ok", "detail": "200 OK"}),
        ):
            _run(app._run_checks())

        call = app.fire_event.call_args
        payload = json.loads(call[1]["payload"])
        assert payload["results"][0]["status"] == "ok"
        assert payload["results"][0]["detail"] == "200 OK"

    def test_critical_result_when_http_check_fails(self):
        """When http_check returns critical, the check result should be critical."""
        app = _make_app({"force_fail": False})
        app.initialize()

        with patch(
            "health_checks.checker_apps.cloud_checker.cloud_checker.http_check",
            new=AsyncMock(return_value={"status": "critical", "detail": "timeout"}),
        ):
            _run(app._run_checks())

        call = app.fire_event.call_args
        payload = json.loads(call[1]["payload"])
        assert payload["results"][0]["status"] == "critical"

    def test_http_check_exception_reports_critical(self):
        """If http_check raises an exception, the check should be critical."""
        app = _make_app({"force_fail": False})
        app.initialize()

        with patch(
            "health_checks.checker_apps.cloud_checker.cloud_checker.http_check",
            new=AsyncMock(side_effect=Exception("connection refused")),
        ):
            _run(app._run_checks())

        call = app.fire_event.call_args
        payload = json.loads(call[1]["payload"])
        assert payload["results"][0]["status"] == "critical"
        assert "Error" in payload["results"][0]["detail"]

    def test_multiple_urls_checked_independently(self):
        """Each URL in checks is checked independently and reported separately."""
        app = _make_app({
            "force_fail": False,
            "checks": [
                {"url": "https://www.google.com", "name": "Google"},
                {"url": "https://1.1.1.1", "name": "Cloudflare DNS"},
            ],
        })
        app.initialize()

        results_iter = iter([
            {"status": "ok", "detail": "200 OK"},
            {"status": "critical", "detail": "timeout"},
        ])

        async def _fake_http(url, timeout_s=5):
            return next(results_iter)

        with patch(
            "health_checks.checker_apps.cloud_checker.cloud_checker.http_check",
            new=_fake_http,
        ):
            _run(app._run_checks())

        call = app.fire_event.call_args
        payload = json.loads(call[1]["payload"])
        assert len(payload["results"]) == 2
        names = {r["name"]: r["status"] for r in payload["results"]}
        assert names["Google"] == "ok"
        assert names["Cloudflare DNS"] == "critical"

    def test_report_includes_checker_id(self):
        """Report payload should include the checker_id."""
        app = _make_app({"force_fail": False})
        app.initialize()

        with patch(
            "health_checks.checker_apps.cloud_checker.cloud_checker.http_check",
            new=AsyncMock(return_value={"status": "ok", "detail": "200 OK"}),
        ):
            _run(app._run_checks())

        call = app.fire_event.call_args
        payload = json.loads(call[1]["payload"])
        assert payload["checker_id"] == "cloud"


# ---------------------------------------------------------------------------
# Tests — Event handlers
# ---------------------------------------------------------------------------

class TestEventHandlers:
    def test_on_recheck_triggers_check(self):
        """_on_recheck should schedule a check immediately."""
        app = _make_app()
        _startup(app)
        app.create_task.reset_mock()

        app._on_recheck("health_check_recheck", {}, {})

        app.create_task.assert_called_once()

    def test_first_check_starts_periodic_timer(self):
        """_first_check should start the periodic run_every timer."""
        app = _make_app()
        _startup(app)
        app.run_every.reset_mock()
        app.create_task.reset_mock()

        app._first_check({})

        app.run_every.assert_called_once()
        app.create_task.assert_called_once()
