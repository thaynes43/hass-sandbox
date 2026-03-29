"""Unit tests for SpaHealthChecker.

Mocks AppDaemon methods, HAProvisioner, and check_utils — no real HA access required.
"""

from __future__ import annotations

import asyncio
import datetime
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

from health_checks.checker_apps.spa_health_checker.spa_health_checker import (
    SpaHealthChecker,
    REPAIR_FAILED,
    REPAIR_IDLE,
    REPAIR_IN_PROGRESS,
    REPAIR_PENDING,
    REPAIR_SUCCESS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DEFAULT_ARGS: Dict[str, Any] = {
    "ha_url": "http://ha:8123",
    "ha_token_env": "TOKEN",
    "checker_id": "spa",
    "checker_name": "Westford Spa",
    "gateway_host": "192.168.0.163",
    "connection_entities": [
        "binary_sensor.westford_spa_overall_connection",
        "binary_sensor.westford_spa_transport_connection",
    ],
    "staleness_entity": "climate.westford_spa_thermostat_1",
    "staleness_threshold_s": 300,
    "repair_switch": "switch.power_distribution_hi_density_hot_tub",
    "repair_recovery_wait_s": 300,
    "check_interval_s": 120,
    "auto_repair_enabled_default": False,
    "auto_repair_delay_min_default": 15,
}


def _make_app(extra_args: dict | None = None) -> SpaHealthChecker:
    ad = MagicMock()
    config = MagicMock()
    app = SpaHealthChecker(ad, config)

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
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_mock_provisioner() -> MagicMock:
    prov = MagicMock()
    prov.ensure_helper = AsyncMock(return_value=False)
    prov.ensure_script = AsyncMock(return_value=False)
    return prov


def _startup(app: SpaHealthChecker, mock_prov: MagicMock | None = None) -> None:
    if mock_prov is None:
        mock_prov = _make_mock_provisioner()
    app.initialize()
    with patch(
        "health_checks.checker_apps.spa_health_checker.spa_health_checker.HAProvisioner",
        return_value=mock_prov,
    ):
        _run(app._async_startup())


def _init_only(app: SpaHealthChecker) -> None:
    """Call initialize without running async startup."""
    app.initialize()


# ---------------------------------------------------------------------------
# Tests — Lifecycle
# ---------------------------------------------------------------------------

class TestLifecycle:
    def test_initialize_calls_run_in(self):
        app = _make_app()
        app.initialize()
        app.run_in.assert_called_once()

    def test_startup_provisions_helpers(self):
        app = _make_app()
        mock_prov = _make_mock_provisioner()
        _startup(app, mock_prov)
        # Should provision input_boolean and input_number
        assert mock_prov.ensure_helper.call_count == 2

    def test_startup_registers_with_supports_repair(self):
        app = _make_app()
        _startup(app)
        # Find the register call
        register_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "register_checker"
        ]
        assert len(register_calls) == 1
        payload = json.loads(register_calls[0][1]["payload"])
        assert payload["supports_repair"] is True
        assert payload["checker_id"] == "spa"
        assert "Gateway Ping" in payload["check_names"]
        assert "Thermostat Staleness" in payload["check_names"]

    def test_startup_registers_event_listeners(self):
        app = _make_app()
        _startup(app)
        event_names = [c[0][1] for c in app.listen_event.call_args_list]
        assert "health_check_controller_ready" in event_names
        assert "health_check_recheck" in event_names
        assert "health_check_repair_spa" in event_names

    def test_check_names_derived_from_entities(self):
        app = _make_app()
        _startup(app)
        register_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "register_checker"
        ]
        payload = json.loads(register_calls[0][1]["payload"])
        names = payload["check_names"]
        assert "Overall Connection" in names
        assert "Transport Connection" in names


# ---------------------------------------------------------------------------
# Tests — Check execution
# ---------------------------------------------------------------------------

class TestChecks:
    def test_gateway_ping_ok(self):
        app = _make_app()
        _init_only(app)
        with patch(
            "health_checks.checker_apps.spa_health_checker.spa_health_checker.ping_check",
            new_callable=AsyncMock,
            return_value={"status": "ok", "detail": "5ms"},
        ):
            result = _run(app._check_gateway_ping())
        assert result["status"] == "ok"
        assert result["name"] == "Gateway Ping"

    def test_gateway_ping_critical(self):
        app = _make_app()
        _init_only(app)
        with patch(
            "health_checks.checker_apps.spa_health_checker.spa_health_checker.ping_check",
            new_callable=AsyncMock,
            return_value={"status": "critical", "detail": "timeout"},
        ):
            result = _run(app._check_gateway_ping())
        assert result["status"] == "critical"

    def test_connection_entity_ok(self):
        app = _make_app()
        _init_only(app)
        app.get_state = AsyncMock(return_value="on")
        result = _run(
            app._check_connection_entity(
                "binary_sensor.westford_spa_overall_connection"
            )
        )
        assert result["status"] == "ok"
        assert result["name"] == "Overall Connection"

    def test_connection_entity_off(self):
        app = _make_app()
        _init_only(app)
        app.get_state = AsyncMock(return_value="off")
        result = _run(
            app._check_connection_entity(
                "binary_sensor.westford_spa_overall_connection"
            )
        )
        assert result["status"] == "critical"

    def test_connection_entity_not_found(self):
        app = _make_app()
        _init_only(app)
        app.get_state = AsyncMock(return_value=None)
        result = _run(
            app._check_connection_entity(
                "binary_sensor.westford_spa_overall_connection"
            )
        )
        assert result["status"] == "critical"
        assert "not found" in result["detail"]

    def test_staleness_ok(self):
        app = _make_app()
        _init_only(app)
        recent = datetime.datetime.utcnow().isoformat()
        app.get_state = AsyncMock(return_value={"last_updated": recent})
        result = _run(app._check_staleness())
        assert result["status"] == "ok"
        assert result["name"] == "Thermostat Staleness"

    def test_staleness_critical(self):
        app = _make_app()
        _init_only(app)
        old = (
            datetime.datetime.utcnow() - datetime.timedelta(seconds=600)
        ).isoformat()
        app.get_state = AsyncMock(return_value={"last_updated": old})
        result = _run(app._check_staleness())
        assert result["status"] == "critical"
        assert "Stale" in result["detail"]

    def test_staleness_entity_not_found(self):
        app = _make_app()
        _init_only(app)
        app.get_state = AsyncMock(return_value=None)
        result = _run(app._check_staleness())
        assert result["status"] == "critical"

    def test_run_checks_reports_with_repair_state(self):
        """_run_checks should include repair_state in the status report."""
        app = _make_app()
        _init_only(app)
        # Mock all checks to return ok
        app.get_state = MagicMock(side_effect=[
            "off",  # auto_repair_enabled helper
            "15",   # auto_repair_delay helper
        ])

        async def mock_checks():
            # Patch individual check methods
            app._check_gateway_ping = AsyncMock(
                return_value={"name": "Gateway Ping", "status": "ok", "detail": "5ms"}
            )
            app._check_connection_entity = AsyncMock(
                return_value={"name": "Overall Connection", "status": "ok", "detail": "connected"}
            )
            app._check_staleness = AsyncMock(
                return_value={"name": "Thermostat Staleness", "status": "ok", "detail": "Updated 10s ago"}
            )
            await app._run_checks()

        _run(mock_checks())

        # Find the report_status call
        report_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
        ]
        assert len(report_calls) >= 1
        payload = json.loads(report_calls[-1][1]["payload"])
        assert "repair_state" in payload
        assert payload["repair_state"]["status"] == REPAIR_IDLE


# ---------------------------------------------------------------------------
# Tests — Repair state machine
# ---------------------------------------------------------------------------

class TestRepairStateMachine:
    def test_initial_state_is_idle(self):
        app = _make_app()
        _init_only(app)
        assert app._repair_status == REPAIR_IDLE

    def test_unhealthy_with_auto_repair_enabled_goes_pending(self):
        """When checks fail and auto-repair is enabled, state goes to pending."""
        app = _make_app()
        _init_only(app)
        # Mock auto-repair as enabled
        app._cached_auto_repair_enabled = True
        app._cached_auto_repair_delay_min = 5

        results = [
            {"name": "Gateway Ping", "status": "critical", "detail": "timeout"},
            {"name": "Overall Connection", "status": "ok", "detail": "connected"},
        ]
        app._evaluate_auto_repair(results)

        assert app._repair_status == REPAIR_PENDING
        assert app._auto_repair_deadline is not None
        assert app._unhealthy_since is not None

    def test_unhealthy_without_auto_repair_stays_idle(self):
        """When auto-repair is disabled, state stays idle even when unhealthy."""
        app = _make_app()
        _init_only(app)
        app._cached_auto_repair_enabled = False
        app._cached_auto_repair_delay_min = 15

        results = [
            {"name": "Gateway Ping", "status": "critical", "detail": "timeout"},
        ]
        app._evaluate_auto_repair(results)

        assert app._repair_status == REPAIR_IDLE
        # But should still track unhealthy_since
        assert app._unhealthy_since is not None

    def test_pending_cancelled_when_all_ok(self):
        """Pending repair should be cancelled when all checks pass."""
        app = _make_app()
        _init_only(app)
        app._repair_status = REPAIR_PENDING
        app._auto_repair_deadline = datetime.datetime.now() + datetime.timedelta(minutes=5)
        app._unhealthy_since = datetime.datetime.now()
        app._cached_auto_repair_enabled = True
        app._cached_auto_repair_delay_min = 5

        results = [
            {"name": "Gateway Ping", "status": "ok", "detail": "5ms"},
            {"name": "Overall Connection", "status": "ok", "detail": "connected"},
        ]
        app._evaluate_auto_repair(results)

        assert app._repair_status == REPAIR_IDLE
        assert app._auto_repair_deadline is None
        assert app._unhealthy_since is None

    def test_pending_triggers_repair_at_deadline(self):
        """When deadline is reached, pending should transition to in_progress."""
        app = _make_app()
        _init_only(app)
        app._repair_status = REPAIR_PENDING
        # Deadline in the past
        app._auto_repair_deadline = datetime.datetime.now() - datetime.timedelta(seconds=1)
        app._unhealthy_since = datetime.datetime.now() - datetime.timedelta(minutes=10)
        app._cached_auto_repair_enabled = True
        app._cached_auto_repair_delay_min = 5
        # Mock create_task to prevent actual repair execution
        app.create_task = MagicMock()

        results = [
            {"name": "Gateway Ping", "status": "critical", "detail": "timeout"},
        ]
        app._evaluate_auto_repair(results)

        assert app._repair_status == REPAIR_IN_PROGRESS

    def test_failed_stays_failed_no_auto_retry(self):
        """Failed repair should NOT auto-retry."""
        app = _make_app()
        _init_only(app)
        app._repair_status = REPAIR_FAILED
        app._repair_detail = "Did not recover"
        app._cached_auto_repair_enabled = True
        app._cached_auto_repair_delay_min = 5

        results = [
            {"name": "Gateway Ping", "status": "critical", "detail": "timeout"},
        ]
        app._evaluate_auto_repair(results)

        assert app._repair_status == REPAIR_FAILED

    def test_failed_clears_when_checks_recover(self):
        """REPAIR_FAILED should reset to IDLE when all checks pass."""
        app = _make_app()
        _init_only(app)
        app._repair_status = REPAIR_FAILED
        app._repair_detail = "Did not recover after 300s"
        app._unhealthy_since = datetime.datetime.now()

        results = [
            {"name": "Gateway Ping", "status": "ok", "detail": ""},
            {"name": "Overall Connection", "status": "ok", "detail": ""},
            {"name": "Thermostat Staleness", "status": "ok", "detail": ""},
        ]
        app._evaluate_auto_repair(results)

        assert app._repair_status == REPAIR_IDLE
        assert app._repair_detail == ""
        assert app._unhealthy_since is None

    def test_manual_repair_resets_from_failed(self):
        """Manual start_repair should reset from failed state."""
        app = _make_app()
        _init_only(app)
        app._repair_status = REPAIR_FAILED
        app.create_task = MagicMock()

        app._start_repair()

        assert app._repair_status == REPAIR_IN_PROGRESS

    def test_unknown_status_does_not_trigger_auto_repair(self):
        """Unknown status (AppDaemon may be partially up) should not trigger repair."""
        app = _make_app()
        _init_only(app)
        app._cached_auto_repair_enabled = True
        app._cached_auto_repair_delay_min = 5

        results = [
            {"name": "Gateway Ping", "status": "unknown", "detail": ""},
        ]
        app._evaluate_auto_repair(results)

        assert app._repair_status == REPAIR_IDLE
        assert app._unhealthy_since is None

    def test_success_clears_on_all_ok(self):
        """Success state should return to idle when all checks pass."""
        app = _make_app()
        _init_only(app)
        app._repair_status = REPAIR_SUCCESS
        app._repair_detail = "Recovered"
        app._cached_auto_repair_enabled = True
        app._cached_auto_repair_delay_min = 5

        results = [
            {"name": "Gateway Ping", "status": "ok", "detail": "5ms"},
        ]
        app._evaluate_auto_repair(results)

        assert app._repair_status == REPAIR_IDLE

    def test_no_repair_switch_fails_immediately(self):
        """start_repair without a repair_switch should immediately fail."""
        app = _make_app({"repair_switch": ""})
        _init_only(app)

        app._start_repair()

        assert app._repair_status == REPAIR_FAILED
        assert "No repair switch" in app._repair_detail


# ---------------------------------------------------------------------------
# Tests — Repair execution
# ---------------------------------------------------------------------------

class TestRepairExecution:
    def test_execute_repair_success(self):
        """Successful repair should set status to success."""
        app = _make_app({"repair_recovery_wait_s": 10})
        _init_only(app)
        app._repair_status = REPAIR_IN_PROGRESS

        # Mock health checks to return ok after power cycle
        app._run_health_checks_only = AsyncMock(
            return_value=[
                {"name": "Gateway Ping", "status": "ok", "detail": "5ms"},
                {"name": "Overall Connection", "status": "ok", "detail": "connected"},
            ]
        )

        _run(app._execute_repair())

        assert app._repair_status == REPAIR_SUCCESS
        assert "Recovered" in app._repair_detail

        # Verify switch was toggled
        service_calls = [c[0][0] for c in app.call_service.call_args_list]
        assert "switch/turn_off" in service_calls
        assert "switch/turn_on" in service_calls

    def test_execute_repair_timeout(self):
        """Failed repair should set status to failed after timeout."""
        app = _make_app({"repair_recovery_wait_s": 10})
        _init_only(app)
        app._repair_status = REPAIR_IN_PROGRESS

        # Mock health checks to always return critical
        app._run_health_checks_only = AsyncMock(
            return_value=[
                {"name": "Gateway Ping", "status": "critical", "detail": "timeout"},
            ]
        )

        _run(app._execute_repair())

        assert app._repair_status == REPAIR_FAILED
        assert "Did not recover" in app._repair_detail

    def test_execute_repair_error_handling(self):
        """Errors during repair should set status to failed."""
        app = _make_app()
        _init_only(app)
        app._repair_status = REPAIR_IN_PROGRESS
        app.call_service = MagicMock(side_effect=Exception("Service unavailable"))

        _run(app._execute_repair())

        assert app._repair_status == REPAIR_FAILED
        assert "error" in app._repair_detail.lower()


# ---------------------------------------------------------------------------
# Tests — Repair config updates
# ---------------------------------------------------------------------------

class TestRepairConfig:
    def test_update_auto_repair_enabled(self):
        app = _make_app()
        _init_only(app)

        app._update_repair_config({"auto_repair_enabled": True})

        app.call_service.assert_called_with(
            "input_boolean/turn_on",
            entity_id="input_boolean.spa_health_auto_repair",
        )

    def test_update_auto_repair_disabled(self):
        app = _make_app()
        _init_only(app)

        app._update_repair_config({"auto_repair_enabled": False})

        app.call_service.assert_called_with(
            "input_boolean/turn_off",
            entity_id="input_boolean.spa_health_auto_repair",
        )

    def test_update_auto_repair_delay(self):
        app = _make_app()
        _init_only(app)

        app._update_repair_config({"auto_repair_delay_min": 10})

        app.call_service.assert_called_with(
            "input_number/set_value",
            entity_id="input_number.spa_health_auto_repair_delay",
            value=10,
        )

    def test_build_repair_state(self):
        """_build_repair_state should return a complete dict."""
        app = _make_app()
        _init_only(app)
        app._cached_auto_repair_enabled = False
        app._cached_auto_repair_delay_min = 15

        state = app._build_repair_state()

        assert state["status"] == REPAIR_IDLE
        assert state["auto_repair_enabled"] is False
        assert state["auto_repair_delay_min"] == 15
        assert state["auto_repair_deadline"] is None


# ---------------------------------------------------------------------------
# Tests — Repair command handler
# ---------------------------------------------------------------------------

class TestRepairCommandHandler:
    def test_start_repair_command(self):
        app = _make_app()
        _init_only(app)
        app.create_task = MagicMock()

        app._on_repair_command(
            "health_check_repair_spa",
            {"action": "start_repair"},
            {},
        )

        assert app._repair_status == REPAIR_IN_PROGRESS

    def test_update_config_command(self):
        app = _make_app()
        _init_only(app)

        app._on_repair_command(
            "health_check_repair_spa",
            {"action": "update_repair_config", "auto_repair_enabled": True, "auto_repair_delay_min": 8},
            {},
        )

        # Should have called service to enable auto-repair
        calls = [c[0][0] for c in app.call_service.call_args_list]
        assert "input_boolean/turn_on" in calls
