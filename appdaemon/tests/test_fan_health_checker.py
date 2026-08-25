"""Unit tests for FanHealthChecker.

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

from health_checks.checker_apps.fan_health_checker import fan_health_checker as fhc_module
from health_checks.checker_apps.fan_health_checker.fan_health_checker import (
    FanHealthChecker,
    REPAIR_FAILED,
    REPAIR_IDLE,
    REPAIR_IN_PROGRESS,
    REPAIR_PENDING,
    REPAIR_SUCCESS,
)


def _full_state(state: str, percentage=None, direction=None) -> dict:
    """Build a full HA state dict as returned by get_state(attribute='all')."""
    attrs: Dict[str, Any] = {}
    if percentage is not None:
        attrs["percentage"] = percentage
    if direction is not None:
        attrs["direction"] = direction
    return {"entity_id": "fan.x", "state": state, "attributes": attrs}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_FANS = [
    {
        "name": "Pink Room",
        "entity_id": "fan.pink_room_fan_fan",
        "ip": "192.168.50.112",
        "power_switch": "switch.upstairs_pink_room_scene_controller",
        "relay_control": "select.upstairs_pink_room_scene_controller_relay_control",
        "scene_control": "select.upstairs_pink_room_scene_controller_scene_control_relay",
    },
    {
        "name": "Blue Room",
        "entity_id": "fan.blue_room_fan_fan",
        "ip": "192.168.50.134",
        "power_switch": "switch.upstairs_blue_room_scene_controller",
        "relay_control": "select.upstairs_blue_room_scene_controller_relay_control",
        "scene_control": "select.upstairs_blue_room_scene_controller_scene_control_relay",
    },
]

THIRD_FAN = {
    "name": "White Room",
    "entity_id": "fan.white_room_fan_fan",
    "ip": "192.168.50.187",
    "power_switch": "switch.upstairs_white_room_scene_controller",
    "relay_control": "select.upstairs_white_room_scene_controller_relay_control",
    "scene_control": "select.upstairs_white_room_scene_controller_scene_control_relay",
}

DEFAULT_ARGS: Dict[str, Any] = {
    "ha_url": "http://ha:8123",
    "ha_token_env": "TOKEN",
    "checker_id": "fans",
    "checker_name": "Fans",
    "check_interval_s": 180,
    "repair_recovery_wait_s": 10,
    "auto_repair_enabled_default": False,
    "auto_repair_delay_min_default": 5,
    "repair_script": "script.zen32_hard_reset",
    "fans": SAMPLE_FANS,
}


def _make_app(extra_args: dict | None = None) -> FanHealthChecker:
    ad = MagicMock()
    config = MagicMock()
    app = FanHealthChecker(ad, config)

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
    return prov


def _startup(app: FanHealthChecker, mock_prov: MagicMock | None = None) -> None:
    if mock_prov is None:
        mock_prov = _make_mock_provisioner()
    app.initialize()
    with patch(
        "health_checks.checker_apps.fan_health_checker.fan_health_checker.HAProvisioner",
        return_value=mock_prov,
    ):
        _run(app._async_startup())


def _init_only(app: FanHealthChecker) -> None:
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
        assert mock_prov.ensure_helper.call_count == 2

    def test_startup_registers_with_supports_repair(self):
        app = _make_app()
        _startup(app)
        register_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "register_checker"
        ]
        assert len(register_calls) == 1
        payload = json.loads(register_calls[0][1]["payload"])
        assert payload["supports_repair"] is True
        assert payload["checker_id"] == "fans"

    def test_registers_correct_check_names(self):
        app = _make_app()
        _startup(app)
        register_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "register_checker"
        ]
        payload = json.loads(register_calls[0][1]["payload"])
        names = payload["check_names"]
        assert len(names) == 4  # 2 fans x 2 checks
        assert "Pink Room State" in names
        assert "Pink Room Ping" in names
        assert "Blue Room State" in names
        assert "Blue Room Ping" in names

    def test_startup_registers_event_listeners(self):
        app = _make_app()
        _startup(app)
        event_names = [c[0][1] for c in app.listen_event.call_args_list]
        assert "health_check_controller_ready" in event_names
        assert "health_check_recheck" in event_names
        assert "health_check_repair_fans" in event_names


# ---------------------------------------------------------------------------
# Tests — Check execution
# ---------------------------------------------------------------------------

class TestChecks:
    def test_fan_entity_ok(self):
        app = _make_app()
        _init_only(app)
        app.get_state = AsyncMock(return_value="on")
        result = _run(app._check_fan_entity(SAMPLE_FANS[0]))
        assert result["status"] == "ok"
        assert result["name"] == "Pink Room State"

    def test_fan_entity_unavailable(self):
        app = _make_app()
        _init_only(app)
        app.get_state = AsyncMock(return_value="unavailable")
        result = _run(app._check_fan_entity(SAMPLE_FANS[0]))
        assert result["status"] == "critical"

    def test_fan_entity_unknown(self):
        app = _make_app()
        _init_only(app)
        app.get_state = AsyncMock(return_value="unknown")
        result = _run(app._check_fan_entity(SAMPLE_FANS[0]))
        assert result["status"] == "critical"

    def test_fan_entity_none(self):
        app = _make_app()
        _init_only(app)
        app.get_state = AsyncMock(return_value=None)
        result = _run(app._check_fan_entity(SAMPLE_FANS[0]))
        assert result["status"] == "critical"

    def test_fan_entity_off_is_ok(self):
        """A fan that is 'off' is still healthy — just not running."""
        app = _make_app()
        _init_only(app)
        app.get_state = AsyncMock(return_value="off")
        result = _run(app._check_fan_entity(SAMPLE_FANS[0]))
        assert result["status"] == "ok"

    def test_fan_ping_ok(self):
        app = _make_app()
        _init_only(app)
        with patch(
            "health_checks.checker_apps.fan_health_checker.fan_health_checker.ping_check",
            new_callable=AsyncMock,
            return_value={"status": "ok", "detail": "3ms"},
        ) as mock_ping:
            result = _run(app._check_fan_ping(SAMPLE_FANS[0]))
        assert result["status"] == "ok"
        assert result["name"] == "Pink Room Ping"
        # Pin the retry wiring — a silent revert to single-attempt pings
        # reintroduces the false-warning noise from ESP power-save misses
        mock_ping.assert_awaited_once_with(
            SAMPLE_FANS[0]["ip"], attempts=fhc_module.PING_ATTEMPTS
        )
        assert fhc_module.PING_ATTEMPTS >= 2

    def test_fan_ping_critical(self):
        app = _make_app()
        _init_only(app)
        with patch(
            "health_checks.checker_apps.fan_health_checker.fan_health_checker.ping_check",
            new_callable=AsyncMock,
            return_value={"status": "critical", "detail": "timeout"},
        ):
            result = _run(app._check_fan_ping(SAMPLE_FANS[0]))
        assert result["status"] == "critical"


# ---------------------------------------------------------------------------
# Tests — Per-fan repair state
# ---------------------------------------------------------------------------

class TestPerFanRepairState:
    def test_initial_state_all_idle(self):
        app = _make_app()
        _init_only(app)
        for fr in app._fan_repair_states.values():
            assert fr["status"] == REPAIR_IDLE

    def test_recovered_fan_resets_failed_state(self):
        app = _make_app()
        _init_only(app)
        app._fan_repair_states["Pink Room"]["status"] = REPAIR_FAILED
        app._fan_repair_states["Pink Room"]["detail"] = "Did not recover"

        results = [
            {"name": "Pink Room State", "status": "ok", "detail": "on"},
            {"name": "Pink Room Ping", "status": "ok", "detail": "3ms"},
            {"name": "Blue Room State", "status": "ok", "detail": "off"},
            {"name": "Blue Room Ping", "status": "ok", "detail": "5ms"},
        ]
        app._reset_recovered_fans(results)

        assert app._fan_repair_states["Pink Room"]["status"] == REPAIR_IDLE

    def test_still_failing_fan_keeps_failed_state(self):
        app = _make_app()
        _init_only(app)
        app._fan_repair_states["Pink Room"]["status"] = REPAIR_FAILED

        results = [
            {"name": "Pink Room State", "status": "critical", "detail": "unavailable"},
            {"name": "Pink Room Ping", "status": "critical", "detail": "timeout"},
            {"name": "Blue Room State", "status": "ok", "detail": "off"},
            {"name": "Blue Room Ping", "status": "ok", "detail": "5ms"},
        ]
        app._reset_recovered_fans(results)

        assert app._fan_repair_states["Pink Room"]["status"] == REPAIR_FAILED

    def test_aggregate_status_in_progress(self):
        app = _make_app()
        _init_only(app)
        app._fan_repair_states["Pink Room"]["status"] = REPAIR_IN_PROGRESS
        app._fan_repair_states["Blue Room"]["status"] = REPAIR_IDLE
        assert app._aggregate_repair_status() == REPAIR_IN_PROGRESS

    def test_aggregate_status_failed(self):
        app = _make_app()
        _init_only(app)
        app._fan_repair_states["Pink Room"]["status"] = REPAIR_FAILED
        app._fan_repair_states["Blue Room"]["status"] = REPAIR_IDLE
        assert app._aggregate_repair_status() == REPAIR_FAILED

    def test_aggregate_status_all_idle(self):
        app = _make_app()
        _init_only(app)
        assert app._aggregate_repair_status() == REPAIR_IDLE


# ---------------------------------------------------------------------------
# Tests — Auto-repair
# ---------------------------------------------------------------------------

class TestAutoRepair:
    def test_auto_repair_triggers_for_first_failing_fan(self):
        app = _make_app()
        _init_only(app)
        app._cached_auto_repair_enabled = True
        app._cached_auto_repair_delay_min = 5
        app._fan_unhealthy_since["Pink Room"] = (
            datetime.datetime.now() - datetime.timedelta(minutes=10)
        )
        app.create_task = MagicMock()

        results = [
            {"name": "Pink Room State", "status": "critical", "detail": "unavailable"},
            {"name": "Pink Room Ping", "status": "critical", "detail": "timeout"},
            {"name": "Blue Room State", "status": "ok", "detail": "off"},
            {"name": "Blue Room Ping", "status": "ok", "detail": "5ms"},
        ]
        app._evaluate_auto_repair(results)

        # Should have started repair (create_task called)
        assert app._fan_repair_states["Pink Room"]["status"] == REPAIR_IN_PROGRESS

    def test_auto_repair_skips_already_failed_fan(self):
        app = _make_app()
        _init_only(app)
        app._cached_auto_repair_enabled = True
        app._cached_auto_repair_delay_min = 5
        old = datetime.datetime.now() - datetime.timedelta(minutes=10)
        app._fan_unhealthy_since["Pink Room"] = old
        app._fan_unhealthy_since["Blue Room"] = old
        app._fan_repair_states["Pink Room"]["status"] = REPAIR_FAILED
        app.create_task = MagicMock()

        results = [
            {"name": "Pink Room State", "status": "critical", "detail": "unavailable"},
            {"name": "Pink Room Ping", "status": "critical", "detail": "timeout"},
            {"name": "Blue Room State", "status": "critical", "detail": "unavailable"},
            {"name": "Blue Room Ping", "status": "critical", "detail": "timeout"},
        ]
        app._evaluate_auto_repair(results)

        # Pink Room already failed, should repair Blue Room
        assert app._fan_repair_states["Pink Room"]["status"] == REPAIR_FAILED
        assert app._fan_repair_states["Blue Room"]["status"] == REPAIR_IN_PROGRESS

    def test_auto_repair_disabled_stays_idle(self):
        app = _make_app()
        _init_only(app)
        app._cached_auto_repair_enabled = False
        app._cached_auto_repair_delay_min = 5

        results = [
            {"name": "Pink Room State", "status": "critical", "detail": "unavailable"},
            {"name": "Pink Room Ping", "status": "critical", "detail": "timeout"},
            {"name": "Blue Room State", "status": "ok", "detail": "off"},
            {"name": "Blue Room Ping", "status": "ok", "detail": "5ms"},
        ]
        app._update_fan_unhealthy_timers(results)
        app._evaluate_auto_repair(results)

        # Timer still accrues while disabled so enabling later has history
        assert app._fan_repair_states["Pink Room"]["status"] == REPAIR_IDLE
        assert app._fan_unhealthy_since["Pink Room"] is not None
        assert app._fan_unhealthy_since["Blue Room"] is None

    def test_all_ok_cancels_pending(self):
        app = _make_app()
        _init_only(app)
        app._repair_status = REPAIR_PENDING
        app._fan_unhealthy_since["Pink Room"] = datetime.datetime.now()
        app._auto_repair_deadline = datetime.datetime.now()

        results = [
            {"name": "Pink Room State", "status": "ok", "detail": "on"},
            {"name": "Pink Room Ping", "status": "ok", "detail": "3ms"},
            {"name": "Blue Room State", "status": "ok", "detail": "off"},
            {"name": "Blue Room Ping", "status": "ok", "detail": "5ms"},
        ]
        app._update_fan_unhealthy_timers(results)
        app._evaluate_auto_repair(results)

        assert app._repair_status == REPAIR_IDLE
        assert app._fan_unhealthy_since["Pink Room"] is None
        assert app._auto_repair_deadline is None

    def test_ping_only_failure_never_triggers_repair(self):
        """A missed ping with the entity still reachable must not power-cycle.

        Regression: single-packet ping misses (ESP Wi-Fi power-save) were
        treated as failures and triggered hard resets of running fans.
        """
        app = _make_app()
        _init_only(app)
        app._cached_auto_repair_enabled = True
        app._cached_auto_repair_delay_min = 5
        app.create_task = MagicMock()

        results = [
            {"name": "Pink Room State", "status": "ok", "detail": "on"},
            {"name": "Pink Room Ping", "status": "critical", "detail": "timeout"},
            {"name": "Blue Room State", "status": "ok", "detail": "off"},
            {"name": "Blue Room Ping", "status": "ok", "detail": "5ms"},
        ]
        # Evaluate repeatedly — even sustained ping-only failure never repairs
        for _ in range(3):
            app._update_fan_unhealthy_timers(results)
            app._evaluate_auto_repair(results)

        assert app._fan_repair_states["Pink Room"]["status"] == REPAIR_IDLE
        assert app._fan_unhealthy_since["Pink Room"] is None
        app.create_task.assert_not_called()

    def test_second_fan_gets_own_grace_period(self):
        """A newly-failing fan must serve its own delay even when another fan
        has been down long past the deadline.

        Regression: a single global unhealthy timer let one long-down fan
        fast-track immediate hard resets of any other fan that blipped.
        """
        # Third healthy fan so two-down doesn't look like a systemic outage
        app = _make_app({"fans": SAMPLE_FANS + [THIRD_FAN]})
        _init_only(app)
        app._cached_auto_repair_enabled = True
        app._cached_auto_repair_delay_min = 5
        # Pink Room down for 30 min, repair already attempted and failed
        app._fan_unhealthy_since["Pink Room"] = (
            datetime.datetime.now() - datetime.timedelta(minutes=30)
        )
        app._fan_repair_states["Pink Room"]["status"] = REPAIR_FAILED
        app.create_task = MagicMock()

        results = [
            {"name": "Pink Room State", "status": "critical", "detail": "unavailable"},
            {"name": "Pink Room Ping", "status": "critical", "detail": "timeout"},
            {"name": "Blue Room State", "status": "critical", "detail": "unavailable"},
            {"name": "Blue Room Ping", "status": "ok", "detail": "5ms"},
            {"name": "White Room State", "status": "ok", "detail": "on"},
            {"name": "White Room Ping", "status": "ok", "detail": "3ms"},
        ]
        app._update_fan_unhealthy_timers(results)
        app._evaluate_auto_repair(results)

        # Blue Room just failed — pending its own 5-min grace, NOT repaired
        assert app._fan_repair_states["Blue Room"]["status"] == REPAIR_IDLE
        assert app._repair_status == REPAIR_PENDING
        assert app._fan_unhealthy_since["Blue Room"] is not None
        expected_deadline = app._fan_unhealthy_since["Blue Room"] + datetime.timedelta(
            minutes=5
        )
        assert app._auto_repair_deadline == expected_deadline
        app.create_task.assert_not_called()

    def test_fan_recovery_resets_grace_timer(self):
        """A fan that recovers clears its timer; a later failure restarts it."""
        app = _make_app()
        _init_only(app)
        app._cached_auto_repair_enabled = True
        app._cached_auto_repair_delay_min = 5
        app._fan_unhealthy_since["Pink Room"] = (
            datetime.datetime.now() - datetime.timedelta(minutes=10)
        )
        app.create_task = MagicMock()

        ok_results = [
            {"name": "Pink Room State", "status": "ok", "detail": "on"},
            {"name": "Pink Room Ping", "status": "ok", "detail": "3ms"},
            {"name": "Blue Room State", "status": "ok", "detail": "off"},
            {"name": "Blue Room Ping", "status": "ok", "detail": "5ms"},
        ]
        app._update_fan_unhealthy_timers(ok_results)
        app._evaluate_auto_repair(ok_results)
        assert app._fan_unhealthy_since["Pink Room"] is None

        bad_results = [
            {"name": "Pink Room State", "status": "critical", "detail": "unavailable"},
            {"name": "Pink Room Ping", "status": "critical", "detail": "timeout"},
            {"name": "Blue Room State", "status": "ok", "detail": "off"},
            {"name": "Blue Room Ping", "status": "ok", "detail": "5ms"},
        ]
        app._update_fan_unhealthy_timers(bad_results)
        app._evaluate_auto_repair(bad_results)
        # Fresh timer → pending, not repaired
        assert app._fan_repair_states["Pink Room"]["status"] == REPAIR_IDLE
        assert app._repair_status == REPAIR_PENDING
        app.create_task.assert_not_called()

    def test_auto_repair_picks_oldest_unhealthy_candidate_first(self):
        """Ordering must follow unhealthy-since, not config order."""
        app = _make_app({"fans": SAMPLE_FANS + [THIRD_FAN]})
        _init_only(app)
        app._cached_auto_repair_enabled = True
        app._cached_auto_repair_delay_min = 5
        now = datetime.datetime.now()
        # Blue (config-second) down 30 min — past its deadline; Pink
        # (config-first) blipped 1 min ago — grace not elapsed.
        app._fan_unhealthy_since["Blue Room"] = now - datetime.timedelta(minutes=30)
        app._fan_unhealthy_since["Pink Room"] = now - datetime.timedelta(minutes=1)
        app.create_task = MagicMock()

        results = [
            {"name": "Pink Room State", "status": "critical", "detail": "unavailable"},
            {"name": "Pink Room Ping", "status": "ok", "detail": "3ms"},
            {"name": "Blue Room State", "status": "critical", "detail": "unavailable"},
            {"name": "Blue Room Ping", "status": "critical", "detail": "timeout"},
            {"name": "White Room State", "status": "ok", "detail": "on"},
            {"name": "White Room Ping", "status": "ok", "detail": "3ms"},
        ]
        app._evaluate_auto_repair(results)

        assert app._fan_repair_states["Blue Room"]["status"] == REPAIR_IN_PROGRESS
        assert app._fan_repair_states["Pink Room"]["status"] == REPAIR_IDLE

    def test_disable_while_pending_clears_countdown(self):
        """Turning auto-repair off mid-countdown must clear the pending state
        so reports stop advertising a repair that can never fire."""
        app = _make_app()
        _init_only(app)
        app._cached_auto_repair_enabled = True
        app._cached_auto_repair_delay_min = 5
        app.create_task = MagicMock()

        results = [
            {"name": "Pink Room State", "status": "critical", "detail": "unavailable"},
            {"name": "Pink Room Ping", "status": "critical", "detail": "timeout"},
            {"name": "Blue Room State", "status": "ok", "detail": "off"},
            {"name": "Blue Room Ping", "status": "ok", "detail": "5ms"},
        ]
        app._update_fan_unhealthy_timers(results)
        app._evaluate_auto_repair(results)
        assert app._repair_status == REPAIR_PENDING
        assert app._auto_repair_deadline is not None

        app._cached_auto_repair_enabled = False
        app._update_fan_unhealthy_timers(results)
        app._evaluate_auto_repair(results)

        assert app._repair_status == REPAIR_IDLE
        assert app._auto_repair_deadline is None
        # Timer keeps accruing so re-enabling computes from actual downtime
        assert app._fan_unhealthy_since["Pink Room"] is not None
        app.create_task.assert_not_called()

    def test_all_fans_down_suspends_auto_repair(self):
        """All fans entity-down at once is a systemic outage signature —
        no fan should be power-cycled and timers stay cleared."""
        app = _make_app()
        _init_only(app)
        app._cached_auto_repair_enabled = True
        app._cached_auto_repair_delay_min = 5
        app.create_task = MagicMock()

        all_down = [
            {"name": "Pink Room State", "status": "critical", "detail": "unavailable"},
            {"name": "Pink Room Ping", "status": "critical", "detail": "timeout"},
            {"name": "Blue Room State", "status": "critical", "detail": "unavailable"},
            {"name": "Blue Room Ping", "status": "critical", "detail": "timeout"},
        ]
        for _ in range(3):
            app._update_fan_unhealthy_timers(all_down)
            app._evaluate_auto_repair(all_down)

        assert app._systemic_outage is True
        assert all(t is None for t in app._fan_unhealthy_since.values())
        assert app._fan_repair_states["Pink Room"]["status"] == REPAIR_IDLE
        assert app._fan_repair_states["Blue Room"]["status"] == REPAIR_IDLE
        app.create_task.assert_not_called()

        # Partial recovery: Blue back, Pink still down → fresh grace for Pink
        partial = [
            {"name": "Pink Room State", "status": "critical", "detail": "unavailable"},
            {"name": "Pink Room Ping", "status": "critical", "detail": "timeout"},
            {"name": "Blue Room State", "status": "ok", "detail": "off"},
            {"name": "Blue Room Ping", "status": "ok", "detail": "5ms"},
        ]
        app._update_fan_unhealthy_timers(partial)
        app._evaluate_auto_repair(partial)

        assert app._systemic_outage is False
        assert app._fan_unhealthy_since["Pink Room"] is not None
        # Fresh timer → pending, not an instant repair
        assert app._fan_repair_states["Pink Room"]["status"] == REPAIR_IDLE
        assert app._repair_status == REPAIR_PENDING
        app.create_task.assert_not_called()

    def test_unhealthy_timers_update_while_repair_active(self):
        """Timers must track every cycle even while a repair is in progress —
        a fan that recovers mid-repair must not keep a stale timer that
        fast-tracks a later grace-less power-cycle."""
        app = _make_app()
        _init_only(app)
        app._fan_repair_states["Pink Room"]["status"] = REPAIR_IN_PROGRESS
        app._fan_unhealthy_since["Blue Room"] = (
            datetime.datetime.now() - datetime.timedelta(minutes=30)
        )
        app.get_state = AsyncMock(return_value="on")

        with patch(
            "health_checks.checker_apps.fan_health_checker.fan_health_checker.ping_check",
            new_callable=AsyncMock,
            return_value={"status": "ok", "detail": "3ms"},
        ):
            _run(app._run_checks())

        # Blue recovered during Pink's repair — its stale timer is cleared
        assert app._fan_unhealthy_since["Blue Room"] is None

    def test_aggregate_status_reports_global_pending(self):
        """The pending countdown is global (per-fan states never hold it);
        the aggregate must surface it or the card countdown and the
        controller's pending paging-hold never engage."""
        app = _make_app()
        _init_only(app)
        app._repair_status = REPAIR_PENDING

        assert app._aggregate_repair_status() == REPAIR_PENDING
        assert app._build_repair_state()["status"] == REPAIR_PENDING

    def test_run_checks_evaluates_repair_on_raw_statuses(self):
        """Auto-repair must see RAW statuses: an entity-down/ping-ok fan's
        State check is critical when evaluated, even though the per-device
        cross-check downgrades it to warning for reporting. Pins the
        evaluate-before-cross-check ordering in _run_checks."""
        app = _make_app()
        _init_only(app)
        app._cached_auto_repair_delay_min = 5
        app._fan_unhealthy_since["Pink Room"] = (
            datetime.datetime.now() - datetime.timedelta(minutes=10)
        )
        app.create_task = MagicMock()

        async def fake_get_state(entity_id=None, *a, **kw):
            if entity_id == PINK_ENTITY:
                return "unavailable"
            return "on"  # helpers: auto-repair toggle reads as enabled

        app.get_state = AsyncMock(side_effect=fake_get_state)
        with patch(
            "health_checks.checker_apps.fan_health_checker.fan_health_checker.ping_check",
            new_callable=AsyncMock,
            return_value={"status": "ok", "detail": "3ms"},
        ):
            _run(app._run_checks())

        # Repair started — evaluate saw the raw critical State check (the
        # cross-check downgrade to warning applies to reporting only)
        assert app._fan_repair_states["Pink Room"]["status"] == REPAIR_IN_PROGRESS


# ---------------------------------------------------------------------------
# Tests — Repair execution
# ---------------------------------------------------------------------------

class TestRepairExecution:
    def test_execute_fan_repair_calls_script(self):
        app = _make_app()
        _init_only(app)

        # Mock health checks to return ok immediately
        app._run_health_checks_only = AsyncMock(
            return_value=[
                {"name": "Pink Room State", "status": "ok", "detail": "on"},
                {"name": "Pink Room Ping", "status": "ok", "detail": "3ms"},
                {"name": "Blue Room State", "status": "ok", "detail": "off"},
                {"name": "Blue Room Ping", "status": "ok", "detail": "5ms"},
            ]
        )

        _run(app._execute_fan_repair(SAMPLE_FANS[0]))

        # Verify script was invoked fire-and-forget with correct variables
        app.call_service.assert_called_once_with(
            "script/turn_on",
            entity_id="script.zen32_hard_reset",
            variables={
                "power_switch_entity": "switch.upstairs_pink_room_scene_controller",
                "relay_control_select_entity": "select.upstairs_pink_room_scene_controller_relay_control",
                "scene_control_select_entity": "select.upstairs_pink_room_scene_controller_scene_control_relay",
                "unavailable_fan_entity": "fan.pink_room_fan_fan",
            },
        )
        assert app._fan_repair_states["Pink Room"]["status"] == REPAIR_SUCCESS

    def test_execute_fan_repair_timeout(self):
        app = _make_app({"repair_recovery_wait_s": 10})
        _init_only(app)

        app._run_health_checks_only = AsyncMock(
            return_value=[
                {"name": "Pink Room State", "status": "critical", "detail": "unavailable"},
                {"name": "Pink Room Ping", "status": "critical", "detail": "timeout"},
                {"name": "Blue Room State", "status": "ok", "detail": "off"},
                {"name": "Blue Room Ping", "status": "ok", "detail": "5ms"},
            ]
        )

        _run(app._execute_fan_repair(SAMPLE_FANS[0]))

        assert app._fan_repair_states["Pink Room"]["status"] == REPAIR_FAILED
        assert "Did not recover" in app._fan_repair_states["Pink Room"]["detail"]

    def test_execute_fan_repair_error(self):
        app = _make_app()
        _init_only(app)
        app.call_service = MagicMock(side_effect=Exception("Service unavailable"))

        _run(app._execute_fan_repair(SAMPLE_FANS[0]))

        assert app._fan_repair_states["Pink Room"]["status"] == REPAIR_FAILED
        assert "error" in app._fan_repair_states["Pink Room"]["detail"].lower()

    def test_manual_repair_skips_ping_only_failing_fan(self):
        """The manual Repair button power-cycles only entity-down fans — a
        ping-only blip never earns a power cycle, even manually."""
        app = _make_app()
        _init_only(app)
        app._run_health_checks_only = AsyncMock(
            return_value=[
                {"name": "Pink Room State", "status": "critical", "detail": "unavailable"},
                {"name": "Pink Room Ping", "status": "critical", "detail": "timeout"},
                {"name": "Blue Room State", "status": "ok", "detail": "on"},
                {"name": "Blue Room Ping", "status": "critical", "detail": "timeout"},
            ]
        )
        app._execute_fan_repair = AsyncMock()

        _run(app._repair_all_failing())

        app._execute_fan_repair.assert_awaited_once_with(SAMPLE_FANS[0])

    def test_repair_aborts_when_script_busy(self):
        """The repair script is mode:single — turn_on while it runs is
        silently dropped. The attempt must fail honestly, not pretend a
        power-cycle happened and burn the fan's one attempt."""
        app = _make_app()
        _init_only(app)
        app.get_state = AsyncMock(return_value="on")  # script busy

        with patch.object(fhc_module, "SCRIPT_BUSY_WAIT_S", 0):
            _run(app._execute_fan_repair(SAMPLE_FANS[0]))

        assert app._fan_repair_states["Pink Room"]["status"] == REPAIR_FAILED
        assert "busy" in app._fan_repair_states["Pink Room"]["detail"].lower()
        services = [c[0][0] for c in app.call_service.call_args_list]
        assert "script/turn_on" not in services

    def test_repair_waits_then_proceeds_when_script_frees(self):
        """A busy script that frees within the wait window → repair proceeds."""
        app = _make_app()
        _init_only(app)
        app.get_state = AsyncMock(side_effect=["on", "off"])
        app._run_health_checks_only = AsyncMock(
            return_value=[
                {"name": "Pink Room State", "status": "ok", "detail": "on"},
                {"name": "Pink Room Ping", "status": "ok", "detail": "3ms"},
                {"name": "Blue Room State", "status": "ok", "detail": "off"},
                {"name": "Blue Room Ping", "status": "ok", "detail": "5ms"},
            ]
        )
        with patch.object(fhc_module, "REPAIR_POLL_INTERVAL_S", 0):
            _run(app._execute_fan_repair(SAMPLE_FANS[0]))

        services = [c[0][0] for c in app.call_service.call_args_list]
        assert "script/turn_on" in services
        assert app._fan_repair_states["Pink Room"]["status"] == REPAIR_SUCCESS

    def test_manual_repair_resets_failed_states(self):
        app = _make_app()
        _init_only(app)
        app._fan_repair_states["Pink Room"]["status"] = REPAIR_FAILED
        app._fan_repair_states["Blue Room"]["status"] = REPAIR_FAILED
        app.create_task = MagicMock()

        app._start_manual_repair()

        assert app._fan_repair_states["Pink Room"]["status"] == REPAIR_IDLE
        assert app._fan_repair_states["Blue Room"]["status"] == REPAIR_IDLE


# ---------------------------------------------------------------------------
# Tests — Repair state reporting
# ---------------------------------------------------------------------------

class TestRepairStateReporting:
    def test_build_repair_state_includes_device_repairs(self):
        app = _make_app()
        _init_only(app)
        app._cached_auto_repair_enabled = False
        app._cached_auto_repair_delay_min = 5
        app._fan_repair_states["Pink Room"]["status"] = REPAIR_FAILED
        app._fan_repair_states["Pink Room"]["detail"] = "Did not recover"

        state = app._build_repair_state()

        assert "device_repairs" in state
        assert state["device_repairs"]["Pink Room"]["status"] == REPAIR_FAILED
        assert state["device_repairs"]["Blue Room"]["status"] == REPAIR_IDLE
        assert state["status"] == REPAIR_FAILED

    def test_build_repair_state_detail_shows_active_fan(self):
        app = _make_app()
        _init_only(app)
        app._cached_auto_repair_enabled = True
        app._cached_auto_repair_delay_min = 5
        app._fan_repair_states["Blue Room"]["status"] = REPAIR_IN_PROGRESS

        state = app._build_repair_state()

        assert "Blue Room" in state["detail"]


# ---------------------------------------------------------------------------
# Tests — Repair command handler
# ---------------------------------------------------------------------------

class TestRepairCommandHandler:
    def test_start_repair_command(self):
        app = _make_app()
        _init_only(app)
        app.create_task = MagicMock()

        app._on_repair_command(
            "health_check_repair_fans",
            {"action": "start_repair"},
            {},
        )

        app.create_task.assert_called_once()

    def test_update_config_command(self):
        app = _make_app()
        _init_only(app)

        app._on_repair_command(
            "health_check_repair_fans",
            {"action": "update_repair_config", "auto_repair_enabled": True},
            {},
        )

        calls = [c[0][0] for c in app.call_service.call_args_list]
        assert "input_boolean/turn_on" in calls


# ---------------------------------------------------------------------------
# Tests — State cache + restore
# ---------------------------------------------------------------------------

PINK_ENTITY = "fan.pink_room_fan_fan"


class TestStateCache:
    def test_listener_caches_good_on_state(self):
        app = _make_app()
        _init_only(app)
        app._on_fan_state_change(
            PINK_ENTITY, "all", None, _full_state("on", 33, "forward"), {}
        )
        assert app._fan_state_cache[PINK_ENTITY] == {
            "state": "on",
            "percentage": 33,
            "direction": "forward",
        }

    def test_listener_caches_off_state(self):
        app = _make_app()
        _init_only(app)
        app._on_fan_state_change(
            PINK_ENTITY, "all", None, _full_state("off", 0, "forward"), {}
        )
        assert app._fan_state_cache[PINK_ENTITY]["state"] == "off"

    def test_listener_ignores_unavailable(self):
        app = _make_app()
        _init_only(app)
        # Seed a good value first
        app._fan_state_cache[PINK_ENTITY] = {
            "state": "on", "percentage": 66, "direction": "reverse",
        }
        app._on_fan_state_change(
            PINK_ENTITY, "all", None, _full_state("unavailable"), {}
        )
        # Good value must be preserved, not overwritten
        assert app._fan_state_cache[PINK_ENTITY]["percentage"] == 66

    def test_listener_frozen_during_repair(self):
        app = _make_app()
        _init_only(app)
        app._fan_state_cache[PINK_ENTITY] = {
            "state": "on", "percentage": 33, "direction": "forward",
        }
        app._fan_repair_states["Pink Room"]["status"] = REPAIR_IN_PROGRESS
        # A transient post-reboot default arrives mid-repair
        app._on_fan_state_change(
            PINK_ENTITY, "all", None, _full_state("on", 100, "forward"), {}
        )
        # Pre-repair value must be preserved
        assert app._fan_state_cache[PINK_ENTITY]["percentage"] == 33

    def test_listener_ignores_unknown_entity(self):
        app = _make_app()
        _init_only(app)
        app._on_fan_state_change(
            "fan.not_a_tracked_fan", "all", None, _full_state("on", 50), {}
        )
        assert "fan.not_a_tracked_fan" not in app._fan_state_cache

    def test_seed_state_cache_from_ha(self):
        app = _make_app()
        _init_only(app)
        app.get_state = AsyncMock(return_value=_full_state("on", 66, "reverse"))
        _run(app._seed_state_cache())
        assert app._fan_state_cache[PINK_ENTITY] == {
            "state": "on",
            "percentage": 66,
            "direction": "reverse",
        }

    def test_seed_state_cache_skips_unavailable(self):
        app = _make_app()
        _init_only(app)
        app.get_state = AsyncMock(return_value=_full_state("unavailable"))
        _run(app._seed_state_cache())
        assert PINK_ENTITY not in app._fan_state_cache

    def test_startup_registers_state_listeners(self):
        app = _make_app()
        _startup(app)
        ls_calls = [
            c for c in app.listen_state.call_args_list
            if c[1].get("attribute") == "all"
        ]
        # One listener per fan (2 sample fans)
        assert len(ls_calls) == 2
        entities = {c[0][1] for c in ls_calls}
        assert PINK_ENTITY in entities


class TestRepairEvents:
    def test_success_emits_repair_event_with_elapsed_duration(self):
        app = _make_app()
        _init_only(app)

        app._run_health_checks_only = AsyncMock(
            return_value=[
                {"name": "Pink Room State", "status": "ok", "detail": "on"},
                {"name": "Pink Room Ping", "status": "ok", "detail": "3ms"},
                {"name": "Blue Room State", "status": "ok", "detail": "off"},
                {"name": "Blue Room Ping", "status": "ok", "detail": "5ms"},
            ]
        )
        with patch.object(fhc_module, "REPAIR_POLL_INTERVAL_S", 5):
            _run(app._execute_fan_repair(SAMPLE_FANS[0]))

        # Find the report_status call that carried the repair_events payload
        carrying_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
            and "repair_events" in json.loads(c[1]["payload"])
        ]
        assert len(carrying_calls) == 1
        payload = json.loads(carrying_calls[0][1]["payload"])
        assert payload["repair_events"] == [
            {"result": "success", "duration_s": 5, "device": "Pink Room"}
        ]

        # Buffer must be drained after delivery
        assert app._pending_repair_events == []

    def test_timeout_emits_failed_repair_event_with_wait_budget(self):
        app = _make_app({"repair_recovery_wait_s": 10})
        _init_only(app)

        app._run_health_checks_only = AsyncMock(
            return_value=[
                {"name": "Pink Room State", "status": "critical", "detail": "unavailable"},
                {"name": "Pink Room Ping", "status": "critical", "detail": "timeout"},
                {"name": "Blue Room State", "status": "ok", "detail": "off"},
                {"name": "Blue Room Ping", "status": "ok", "detail": "5ms"},
            ]
        )

        _run(app._execute_fan_repair(SAMPLE_FANS[0]))

        carrying_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
            and "repair_events" in json.loads(c[1]["payload"])
        ]
        assert len(carrying_calls) == 1
        payload = json.loads(carrying_calls[0][1]["payload"])
        assert payload["repair_events"] == [
            {"result": "failed", "duration_s": 10, "device": "Pink Room"}
        ]
        assert app._pending_repair_events == []

    def test_repair_event_delivered_promptly_on_concluding_report(self):
        """The report fired immediately when the repair concludes must carry
        the event — not some later, unrelated report."""
        app = _make_app()
        _init_only(app)

        app._run_health_checks_only = AsyncMock(
            return_value=[
                {"name": "Pink Room State", "status": "ok", "detail": "on"},
                {"name": "Pink Room Ping", "status": "ok", "detail": "3ms"},
                {"name": "Blue Room State", "status": "ok", "detail": "off"},
                {"name": "Blue Room Ping", "status": "ok", "detail": "5ms"},
            ]
        )
        with patch.object(fhc_module, "REPAIR_POLL_INTERVAL_S", 5):
            _run(app._execute_fan_repair(SAMPLE_FANS[0]))

        # The very last report_status call is the one that reports the
        # concluding (SUCCESS) repair state, and it must carry the event.
        report_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
        ]
        last_payload = json.loads(report_calls[-1][1]["payload"])
        assert last_payload["repair_events"] == [
            {"result": "success", "duration_s": 5, "device": "Pink Room"}
        ]

    def test_repair_event_not_repeated_on_next_regular_report(self):
        """Once drained, a repair_events entry must never appear again on a
        subsequent, unrelated report_status payload."""
        app = _make_app()
        _init_only(app)

        app._run_health_checks_only = AsyncMock(
            return_value=[
                {"name": "Pink Room State", "status": "ok", "detail": "on"},
                {"name": "Pink Room Ping", "status": "ok", "detail": "3ms"},
                {"name": "Blue Room State", "status": "ok", "detail": "off"},
                {"name": "Blue Room Ping", "status": "ok", "detail": "5ms"},
            ]
        )
        with patch.object(fhc_module, "REPAIR_POLL_INTERVAL_S", 5):
            _run(app._execute_fan_repair(SAMPLE_FANS[0]))

        app.fire_event.reset_mock()

        app.get_state = AsyncMock(return_value="on")
        with patch(
            "health_checks.checker_apps.fan_health_checker.fan_health_checker.ping_check",
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
        assert "repair_events" not in payload

    def test_no_repair_events_key_when_buffer_empty(self):
        app = _make_app()
        _init_only(app)

        app._report_repair_status_only()

        call = app.fire_event.call_args_list[-1]
        payload = json.loads(call[1]["payload"])
        assert "repair_events" not in payload


class TestStateRestore:
    def test_restore_on_reapplies_speed_and_direction(self):
        app = _make_app()
        _init_only(app)
        app._fan_state_cache[PINK_ENTITY] = {
            "state": "on", "percentage": 66, "direction": "reverse",
        }
        with patch.object(fhc_module, "RESTORE_STEP_DELAY_S", 0):
            _run(app._restore_fan_state(SAMPLE_FANS[0]))

        calls = app.call_service.call_args_list
        services = [c[0][0] for c in calls]
        assert "fan/turn_on" in services
        pct_call = next(c for c in calls if c[0][0] == "fan/set_percentage")
        assert pct_call[1]["percentage"] == 66
        dir_call = next(c for c in calls if c[0][0] == "fan/set_direction")
        assert dir_call[1]["direction"] == "reverse"

    def test_restore_off_turns_off(self):
        app = _make_app()
        _init_only(app)
        app._fan_state_cache[PINK_ENTITY] = {
            "state": "off", "percentage": 0, "direction": "forward",
        }
        with patch.object(fhc_module, "RESTORE_STEP_DELAY_S", 0):
            _run(app._restore_fan_state(SAMPLE_FANS[0]))

        services = [c[0][0] for c in app.call_service.call_args_list]
        assert services == ["fan/turn_off"]

    def test_restore_skips_when_no_cache(self):
        app = _make_app()
        _init_only(app)
        _run(app._restore_fan_state(SAMPLE_FANS[0]))
        app.call_service.assert_not_called()

    def test_restore_noop_when_disabled(self):
        app = _make_app({"restore_state_enabled": False})
        _init_only(app)
        app._fan_state_cache[PINK_ENTITY] = {
            "state": "on", "percentage": 66, "direction": "reverse",
        }
        _run(app._restore_fan_state(SAMPLE_FANS[0]))
        app.call_service.assert_not_called()

    def test_restore_on_without_percentage(self):
        """Fan cached as on with no percentage (e.g. preset) → turn_on only."""
        app = _make_app()
        _init_only(app)
        app._fan_state_cache[PINK_ENTITY] = {
            "state": "on", "percentage": None, "direction": None,
        }
        with patch.object(fhc_module, "RESTORE_STEP_DELAY_S", 0):
            _run(app._restore_fan_state(SAMPLE_FANS[0]))
        services = [c[0][0] for c in app.call_service.call_args_list]
        assert services == ["fan/turn_on"]

    def test_execute_repair_restores_state_on_recovery(self):
        app = _make_app()
        _init_only(app)
        app._fan_state_cache[PINK_ENTITY] = {
            "state": "on", "percentage": 33, "direction": "forward",
        }
        app._run_health_checks_only = AsyncMock(
            return_value=[
                {"name": "Pink Room State", "status": "ok", "detail": "on"},
                {"name": "Pink Room Ping", "status": "ok", "detail": "3ms"},
                {"name": "Blue Room State", "status": "ok", "detail": "off"},
                {"name": "Blue Room Ping", "status": "ok", "detail": "5ms"},
            ]
        )
        with patch.object(fhc_module, "RESTORE_STEP_DELAY_S", 0), \
             patch.object(fhc_module, "REPAIR_POLL_INTERVAL_S", 1):
            _run(app._execute_fan_repair(SAMPLE_FANS[0]))

        services = [c[0][0] for c in app.call_service.call_args_list]
        # Repair script called (via script/turn_on), then state restored
        assert "script/turn_on" in services
        assert "fan/turn_on" in services
        assert "fan/set_percentage" in services
        assert app._fan_repair_states["Pink Room"]["status"] == REPAIR_SUCCESS
