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

# Modern Forms fans are Wi-Fi devices: each one associates with a UniFi AP
# whose state sensor can be declared per fan (the ZEN32 is only the Z-Wave
# relay that power-cycles them).
AP_ENTITY = "sensor.kitchen_pantry_u7_pro_state"
AP_NAME = "Kitchen Pantry U7 Pro"

AP_FANS = [
    {**SAMPLE_FANS[0], "ap_status_entity": AP_ENTITY, "ap_name": AP_NAME},
    SAMPLE_FANS[1],
]


def _ok_results() -> list[dict]:
    """Every check green for both sample fans."""
    return [
        {"name": "Pink Room State", "status": "ok", "detail": "on"},
        {"name": "Pink Room Ping", "status": "ok", "detail": "3ms"},
        {"name": "Blue Room State", "status": "ok", "detail": "off"},
        {"name": "Blue Room Ping", "status": "ok", "detail": "5ms"},
    ]


def _pink_down_results() -> list[dict]:
    """Pink Room entity-down (repair-worthy); Blue Room healthy."""
    return [
        {"name": "Pink Room State", "status": "critical", "detail": "unavailable"},
        {"name": "Pink Room Ping", "status": "critical", "detail": "timeout"},
        {"name": "Blue Room State", "status": "ok", "detail": "off"},
        {"name": "Blue Room Ping", "status": "ok", "detail": "5ms"},
    ]


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
        """Auto-repair toggle, delay — and the backoff-ladder helper, without
        which an AppDaemon app reload mid-incident resets every fan to
        attempt 1 and instant power-cycles resume."""
        app = _make_app()
        mock_prov = _make_mock_provisioner()
        _startup(app, mock_prov)

        assert mock_prov.ensure_helper.call_count == 3
        domains = [c.args[0] for c in mock_prov.ensure_helper.call_args_list]
        assert domains == ["input_boolean", "input_number", "input_text"]

        ladder_call = mock_prov.ensure_helper.call_args_list[-1]
        assert ladder_call.args[1] == "fans Health Repair Ladder"
        # input_text hard-caps at 255 chars — the ladder writer truncates to it
        assert ladder_call.kwargs["max"] == 255

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
        """Crashloop semantics: one clean cycle no longer clears FAILED — it
        only starts the recovery streak. Only ``repair_backoff_reset_min=0``
        (the old instant-reset behaviour) still resets on the first cycle."""
        held = _make_app()  # default repair_backoff_reset_min = 30
        _init_only(held)
        held._fan_repair_states["Pink Room"].update(
            {"status": REPAIR_FAILED, "detail": "Did not recover"}
        )

        held._reset_recovered_fans(_ok_results())

        fr = held._fan_repair_states["Pink Room"]
        assert fr["status"] == REPAIR_FAILED
        assert fr["recovered_at"] is not None  # streak started, not finished

        instant = _make_app({"repair_backoff_reset_min": 0})
        _init_only(instant)
        instant._fan_repair_states["Pink Room"].update(
            {"status": REPAIR_FAILED, "detail": "Did not recover"}
        )

        instant._reset_recovered_fans(_ok_results())

        assert instant._fan_repair_states["Pink Room"]["status"] == REPAIR_IDLE
        assert instant._fan_repair_states["Pink Room"]["recovered_at"] is None

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

    def test_failed_fan_waits_for_backoff(self):
        """A failed fan must not retry before its scheduled backoff — and a
        backoff wait is NOT advertised as a cancellable pending countdown
        (the per-fan detail carries the retry time instead)."""
        app = _make_app()
        _init_only(app)
        app._cached_auto_repair_enabled = True
        app._cached_auto_repair_delay_min = 5
        retry_at = datetime.datetime.now() + datetime.timedelta(minutes=10)
        app._fan_unhealthy_since["Pink Room"] = (
            datetime.datetime.now() - datetime.timedelta(minutes=30)
        )
        app._fan_repair_states["Pink Room"].update(
            {"status": REPAIR_FAILED, "attempts": 1, "next_retry_at": retry_at}
        )
        app.create_task = MagicMock()

        results = [
            {"name": "Pink Room State", "status": "critical", "detail": "unavailable"},
            {"name": "Pink Room Ping", "status": "critical", "detail": "timeout"},
            {"name": "Blue Room State", "status": "ok", "detail": "off"},
            {"name": "Blue Room Ping", "status": "ok", "detail": "5ms"},
        ]
        app._evaluate_auto_repair(results)

        assert app._fan_repair_states["Pink Room"]["status"] == REPAIR_FAILED
        assert app._repair_status != REPAIR_PENDING
        assert app._auto_repair_deadline is None
        app.create_task.assert_not_called()

    def test_stale_backoff_cannot_fire_instantly_on_reblip(self):
        """Regression: a FAILED fan with a long-past retry recovers its
        entity (ping still bad, so no full reset), then blips down again —
        the stale schedule must NOT fire instantly; the ladder is kept."""
        app = _make_app()
        _init_only(app)
        app._cached_auto_repair_enabled = True
        app._cached_auto_repair_delay_min = 5
        app._fan_unhealthy_since["Pink Room"] = (
            datetime.datetime.now() - datetime.timedelta(hours=3)
        )
        app._fan_repair_states["Pink Room"].update(
            {
                "status": REPAIR_FAILED,
                "attempts": 2,
                "next_retry_at": datetime.datetime.now()
                - datetime.timedelta(hours=2),
            }
        )
        app.create_task = MagicMock()

        # Partial recovery: entity back, ping still failing
        partial = [
            {"name": "Pink Room State", "status": "ok", "detail": "off"},
            {"name": "Pink Room Ping", "status": "critical", "detail": "timeout"},
            {"name": "Blue Room State", "status": "ok", "detail": "off"},
            {"name": "Blue Room Ping", "status": "ok", "detail": "5ms"},
        ]
        app._update_fan_unhealthy_timers(partial)
        app._evaluate_auto_repair(partial)
        app.create_task.assert_not_called()

        # Entity blips down again — stale retry must not fire immediately
        reblip = [
            {"name": "Pink Room State", "status": "critical", "detail": "unavailable"},
            {"name": "Pink Room Ping", "status": "critical", "detail": "timeout"},
            {"name": "Blue Room State", "status": "ok", "detail": "off"},
            {"name": "Blue Room Ping", "status": "ok", "detail": "5ms"},
        ]
        app._update_fan_unhealthy_timers(reblip)
        app._evaluate_auto_repair(reblip)

        fr = app._fan_repair_states["Pink Room"]
        assert fr["status"] == REPAIR_FAILED
        assert fr["attempts"] == 2  # ladder preserved
        app.create_task.assert_not_called()

    def test_success_relapse_resumes_backoff_ladder(self):
        """A repair "success" that did not stick RESUMES the ladder.

        Regression (2026-08-31 page storm): the relapse used to demote the
        fan to a fresh IDLE episode, so the next power-cycle came after one
        grace delay at attempt 1 — ~11 power-cycles in 5h. Now the false
        success counts as a failed attempt: the ladder climbs, the retry is
        pushed out by the doubled backoff, and the fan's unhealthy clock is
        restarted so no stale deadline can fire instantly.
        """
        app = _make_app()
        _init_only(app)
        app._cached_auto_repair_enabled = True
        app._cached_auto_repair_delay_min = 5
        app._fan_repair_states["Pink Room"].update(
            {
                "status": REPAIR_SUCCESS,
                "attempts": 1,
                "detail": "Recovered after 45s",
            }
        )
        app.create_task = MagicMock()

        before = datetime.datetime.now()
        app._update_fan_unhealthy_timers(_pink_down_results())
        app._evaluate_auto_repair(_pink_down_results())

        fr = app._fan_repair_states["Pink Room"]
        assert fr["status"] == REPAIR_FAILED
        assert fr["attempts"] == 2  # ladder resumed, not restarted
        assert "Relapsed" in fr["detail"]
        delta_min = (fr["next_retry_at"] - before).total_seconds() / 60
        assert abs(delta_min - 10) < 0.1  # 5 × 2^(2-1)
        # Fresh unhealthy clock — no stale grace deadline to fast-track a
        # power-cycle on the very next cycle.
        assert app._fan_unhealthy_since["Pink Room"] >= before
        assert app._repair_status != REPAIR_PENDING
        app.create_task.assert_not_called()

    def test_repeated_relapses_keep_doubling_up_to_the_cap(self):
        """Each relapse climbs one rung: 5 → 10 → 20 minutes, then the cap."""
        app = _make_app({"repair_backoff_max_min": 20})
        _init_only(app)
        app._cached_auto_repair_enabled = True
        app._cached_auto_repair_delay_min = 5
        app.create_task = MagicMock()

        for expected in (5, 10, 20, 20):
            # Each round starts from a repair that reported success (the fan
            # came back) and then dropped out again before it was sustained.
            app._fan_repair_states["Pink Room"]["status"] = REPAIR_SUCCESS
            before = datetime.datetime.now()
            app._update_fan_unhealthy_timers(_pink_down_results())

            fr = app._fan_repair_states["Pink Room"]
            delta_min = (fr["next_retry_at"] - before).total_seconds() / 60
            assert abs(delta_min - expected) < 0.1, (
                f"attempt {fr['attempts']}: expected ~{expected}m, "
                f"got {delta_min:.2f}m"
            )
            assert fr["status"] == REPAIR_FAILED

        assert app._fan_repair_states["Pink Room"]["attempts"] == 4
        app.create_task.assert_not_called()

    def test_disabled_blocks_due_fan_retry(self):
        """Disabling auto-repair mid-backoff must block a due retry."""
        app = _make_app()
        _init_only(app)
        app._cached_auto_repair_enabled = False
        app._cached_auto_repair_delay_min = 5
        app._fan_unhealthy_since["Pink Room"] = (
            datetime.datetime.now() - datetime.timedelta(minutes=30)
        )
        app._fan_repair_states["Pink Room"].update(
            {
                "status": REPAIR_FAILED,
                "attempts": 1,
                "next_retry_at": datetime.datetime.now()
                - datetime.timedelta(seconds=1),
            }
        )
        app.create_task = MagicMock()

        results = [
            {"name": "Pink Room State", "status": "critical", "detail": "unavailable"},
            {"name": "Pink Room Ping", "status": "critical", "detail": "timeout"},
            {"name": "Blue Room State", "status": "ok", "detail": "off"},
            {"name": "Blue Room Ping", "status": "ok", "detail": "5ms"},
        ]
        app._evaluate_auto_repair(results)

        assert app._fan_repair_states["Pink Room"]["status"] == REPAIR_FAILED
        app.create_task.assert_not_called()

    def test_failed_fan_retries_after_backoff_expires(self):
        """CrashLoopBackOff: an entity-down failed fan retries once its
        backoff expires — a failed repair never ends the episode."""
        app = _make_app()
        _init_only(app)
        app._cached_auto_repair_enabled = True
        app._cached_auto_repair_delay_min = 5
        app._fan_unhealthy_since["Pink Room"] = (
            datetime.datetime.now() - datetime.timedelta(minutes=30)
        )
        app._fan_repair_states["Pink Room"].update(
            {
                "status": REPAIR_FAILED,
                "attempts": 1,
                "next_retry_at": datetime.datetime.now()
                - datetime.timedelta(seconds=1),
            }
        )
        app.create_task = MagicMock()

        results = [
            {"name": "Pink Room State", "status": "critical", "detail": "unavailable"},
            {"name": "Pink Room Ping", "status": "critical", "detail": "timeout"},
            {"name": "Blue Room State", "status": "ok", "detail": "off"},
            {"name": "Blue Room Ping", "status": "ok", "detail": "5ms"},
        ]
        app._evaluate_auto_repair(results)

        assert app._fan_repair_states["Pink Room"]["status"] == REPAIR_IN_PROGRESS
        app.create_task.assert_called_once()

    def test_fan_backoff_doubles_and_caps(self):
        """Per-fan backoff: delay × 2^(n-1) minutes, capped at
        repair_backoff_max_min."""
        app = _make_app({"repair_backoff_max_min": 20})
        _init_only(app)
        app._cached_auto_repair_delay_min = 5

        expected_minutes = [5, 10, 20, 20]  # doubles then caps at 20
        for n, expected in enumerate(expected_minutes, start=1):
            before = datetime.datetime.now()
            app._register_fan_repair_failure("Pink Room", "Did not recover")
            fr = app._fan_repair_states["Pink Room"]
            assert fr["attempts"] == n
            assert fr["status"] == REPAIR_FAILED
            delta_min = (fr["next_retry_at"] - before).total_seconds() / 60
            assert abs(delta_min - expected) < 0.1, (
                f"attempt {n}: expected ~{expected}m, got {delta_min:.2f}m"
            )
            assert f"attempt {n}" in fr["detail"]
        # Other fans untouched
        assert app._fan_repair_states["Blue Room"]["attempts"] == 0

    def test_sustained_recovery_resets_backoff_counter(self):
        """Only a SUSTAINED recovery ends the episode.

        A single clean cycle starts the streak and keeps the ladder; the
        counter and schedule clear once the streak reaches
        ``repair_backoff_reset_min`` (and the ladder helper is emptied).
        """
        app = _make_app()  # default repair_backoff_reset_min = 30
        _init_only(app)
        app._fan_repair_states["Pink Room"].update(
            {
                "status": REPAIR_FAILED,
                "attempts": 3,
                "next_retry_at": datetime.datetime.now(),
            }
        )

        app._reset_recovered_fans(_ok_results())

        fr = app._fan_repair_states["Pink Room"]
        assert fr["status"] == REPAIR_FAILED
        assert fr["attempts"] == 3
        assert fr["next_retry_at"] is not None
        assert fr["recovered_at"] is not None

        # Backdate the streak past the reset window — the ladder clears.
        fr["recovered_at"] -= datetime.timedelta(minutes=31)
        app.call_service.reset_mock()
        app._reset_recovered_fans(_ok_results())

        assert fr["status"] == REPAIR_IDLE
        assert fr["attempts"] == 0
        assert fr["next_retry_at"] is None
        assert fr["recovered_at"] is None
        # The cleared ladder is persisted too, so a reload can't resurrect it.
        persisted = app.call_service.call_args_list[-1]
        assert persisted.args[0] == "input_text/set_value"
        assert persisted.kwargs["value"] == "{}"

    def test_recovery_streak_restarts_after_a_relapse(self):
        """A partial streak does not accumulate across a dropout: going
        entity-down again clears ``recovered_at``, so the fan starts the
        30-minute clean run from scratch."""
        app = _make_app()
        _init_only(app)
        app._cached_auto_repair_delay_min = 5
        app._fan_repair_states["Pink Room"].update(
            {
                "status": REPAIR_FAILED,
                "attempts": 2,
                "next_retry_at": datetime.datetime.now()
                + datetime.timedelta(minutes=10),
            }
        )

        app._reset_recovered_fans(_ok_results())
        fr = app._fan_repair_states["Pink Room"]
        streak_start = fr["recovered_at"]
        assert streak_start is not None

        # Entity drops again 29 minutes in (still FAILED, so no relapse
        # bookkeeping) — the streak must be discarded, not resumed.
        fr["recovered_at"] = streak_start - datetime.timedelta(minutes=29)
        app._update_fan_unhealthy_timers(_pink_down_results())
        assert fr["recovered_at"] is None
        assert fr["attempts"] == 2

        # Back to healthy: a brand-new streak, so nothing resets yet.
        app._reset_recovered_fans(_ok_results())
        assert fr["status"] == REPAIR_FAILED
        assert fr["attempts"] == 2
        assert fr["recovered_at"] > streak_start

    def test_earliest_due_wins_between_first_attempt_and_retry(self):
        """A backoff retry due now must not be starved by a newer IDLE fan
        whose grace deadline is later (and vice versa — due times compete)."""
        app = _make_app({"fans": SAMPLE_FANS + [THIRD_FAN]})
        _init_only(app)
        app._cached_auto_repair_enabled = True
        app._cached_auto_repair_delay_min = 5
        now = datetime.datetime.now()
        # Pink: failed, retry became due 1 min ago
        app._fan_unhealthy_since["Pink Room"] = now - datetime.timedelta(minutes=60)
        app._fan_repair_states["Pink Room"].update(
            {
                "status": REPAIR_FAILED,
                "attempts": 1,
                "next_retry_at": now - datetime.timedelta(minutes=1),
            }
        )
        # Blue: idle, entity down 2 min — grace deadline still 3 min away
        app._fan_unhealthy_since["Blue Room"] = now - datetime.timedelta(minutes=2)
        app.create_task = MagicMock()

        results = [
            {"name": "Pink Room State", "status": "critical", "detail": "unavailable"},
            {"name": "Pink Room Ping", "status": "critical", "detail": "timeout"},
            {"name": "Blue Room State", "status": "critical", "detail": "unavailable"},
            {"name": "Blue Room Ping", "status": "ok", "detail": "5ms"},
            {"name": "White Room State", "status": "ok", "detail": "on"},
            {"name": "White Room Ping", "status": "ok", "detail": "3ms"},
        ]
        app._evaluate_auto_repair(results)

        assert app._fan_repair_states["Pink Room"]["status"] == REPAIR_IN_PROGRESS
        assert app._fan_repair_states["Blue Room"]["status"] == REPAIR_IDLE

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
        # One rung already climbed: a "success" must NOT zero the ladder —
        # only a sustained recovery does (crashloop semantics).
        app._fan_repair_states["Pink Room"].update(
            {
                "status": REPAIR_FAILED,
                "attempts": 2,
                "next_retry_at": datetime.datetime.now(),
            }
        )

        # Mock health checks to return ok immediately
        app._run_health_checks_only = AsyncMock(return_value=_ok_results())

        _run(app._execute_fan_repair(SAMPLE_FANS[0]))

        # Verify script was invoked fire-and-forget with correct variables
        script_calls = [
            c for c in app.call_service.call_args_list
            if c.args[0] == "script/turn_on"
        ]
        assert len(script_calls) == 1
        assert script_calls[0].kwargs == {
            "entity_id": "script.zen32_hard_reset",
            "variables": {
                "power_switch_entity": "switch.upstairs_pink_room_scene_controller",
                "relay_control_select_entity": "select.upstairs_pink_room_scene_controller_relay_control",
                "scene_control_select_entity": "select.upstairs_pink_room_scene_controller_scene_control_relay",
                "unavailable_fan_entity": "fan.pink_room_fan_fan",
            },
        }

        fr = app._fan_repair_states["Pink Room"]
        assert fr["status"] == REPAIR_SUCCESS
        assert fr["attempts"] == 2  # ladder survives the success
        assert fr["next_retry_at"] is None
        # ...and is written through, so an app reload keeps the rung.
        ladder_calls = [
            c for c in app.call_service.call_args_list
            if c.args[0] == "input_text/set_value"
        ]
        assert json.loads(ladder_calls[-1].kwargs["value"]) == {"Pink Room": [2, None]}

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

        fr = app._fan_repair_states["Pink Room"]
        assert fr["status"] == REPAIR_FAILED
        assert "Did not recover" in fr["detail"]
        # Failure must schedule a backoff retry (terminal-FAILED mutant fails)
        assert fr["attempts"] == 1
        delta_min = (
            fr["next_retry_at"] - datetime.datetime.now()
        ).total_seconds() / 60
        assert abs(delta_min - app._cached_auto_repair_delay_min) < 0.5

    def test_execute_fan_repair_error(self):
        app = _make_app()
        _init_only(app)
        app.call_service = MagicMock(side_effect=Exception("Service unavailable"))

        _run(app._execute_fan_repair(SAMPLE_FANS[0]))

        fr = app._fan_repair_states["Pink Room"]
        assert fr["status"] == REPAIR_FAILED
        assert "error" in fr["detail"].lower()
        assert fr["attempts"] == 1
        assert fr["next_retry_at"] is not None

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

        fr = app._fan_repair_states["Pink Room"]
        assert fr["status"] == REPAIR_FAILED
        assert "busy" in fr["detail"].lower()
        assert fr["attempts"] == 1
        assert fr["next_retry_at"] is not None
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


# ---------------------------------------------------------------------------
# Tests — Backoff-ladder persistence
# ---------------------------------------------------------------------------


def _many_fans(count: int) -> list[dict]:
    """``count`` fans with long-ish names, for the helper length limit."""
    return [
        {
            "name": f"Upstairs Ceiling Fan {i:02d}",
            "entity_id": f"fan.ceiling_{i:02d}",
            "ip": f"192.168.50.{100 + i}",
            "power_switch": f"switch.ceiling_{i:02d}",
            "relay_control": f"select.ceiling_{i:02d}_relay",
            "scene_control": f"select.ceiling_{i:02d}_scene",
        }
        for i in range(count)
    ]


class TestLadderPersistence:
    """The ladder lives in ``input_text.<checker_id>_health_repair_ladder``.

    An HA restart or plugin reconnect re-initialises every AppDaemon app
    (observed 2026-08-31 07:24); without persistence that reset a climbing
    ladder back to instant attempt-1 power-cycles.
    """

    def test_persist_writes_compact_json_for_climbing_fans_only(self):
        app = _make_app()
        _init_only(app)
        retry = datetime.datetime.now() + datetime.timedelta(minutes=10)
        app._fan_repair_states["Pink Room"].update(
            {"status": REPAIR_FAILED, "attempts": 2, "next_retry_at": retry}
        )
        app.call_service.reset_mock()

        app._persist_ladder()

        # Blue Room (attempts 0) is absent — only live ladders are stored.
        app.call_service.assert_called_once_with(
            "input_text/set_value",
            entity_id="input_text.fans_health_repair_ladder",
            value=json.dumps(
                {"Pink Room": [2, retry.isoformat(timespec="seconds")]},
                separators=(",", ":"),
            ),
        )

    def test_persist_writes_empty_object_when_no_ladders(self):
        app = _make_app()
        _init_only(app)
        app.call_service.reset_mock()

        app._persist_ladder()

        assert app.call_service.call_args.kwargs["value"] == "{}"

    def test_persist_truncates_lowest_ladders_first(self):
        """input_text caps at 255 chars; the fans with the least backoff to
        lose are the ones dropped."""
        fans = _many_fans(5)
        app = _make_app({"fans": fans})
        _init_only(app)
        retry = datetime.datetime.now() + datetime.timedelta(minutes=10)
        for rung, fan in enumerate(fans, start=1):  # attempts 1..5
            app._fan_repair_states[fan["name"]].update(
                {"status": REPAIR_FAILED, "attempts": rung, "next_retry_at": retry}
            )
        app.call_service.reset_mock()

        app._persist_ladder()

        value = app.call_service.call_args.kwargs["value"]
        assert len(value) <= 255
        stored = json.loads(value)
        assert 0 < len(stored) < len(fans)  # something had to go
        # Exactly the highest ladders survived (fans are ordered by rung).
        kept = {f["name"] for f in fans[-len(stored):]}
        assert set(stored) == kept

    def test_persist_failure_never_breaks_repair_logic(self):
        app = _make_app()
        _init_only(app)
        app._fan_repair_states["Pink Room"]["attempts"] = 1
        app.call_service = MagicMock(side_effect=Exception("HA unreachable"))

        app._persist_ladder()  # must not raise

        warnings = [
            c for c in app.log.call_args_list
            if c[1].get("level") == "WARNING"
        ]
        assert any("repair ladder" in str(c) for c in warnings)

    def test_seed_restores_attempts_and_floors_a_stale_retry(self):
        app = _make_app()
        _init_only(app)
        app._cached_auto_repair_delay_min = 5
        stale = datetime.datetime.now() - datetime.timedelta(hours=2)
        future = datetime.datetime.now() + datetime.timedelta(hours=1)
        app.get_state = AsyncMock(
            return_value=json.dumps(
                {
                    "Pink Room": [3, stale.isoformat(timespec="seconds")],
                    "Blue Room": [1, future.isoformat(timespec="seconds")],
                }
            )
        )

        before = datetime.datetime.now()
        _run(app._seed_repair_ladder())

        app.get_state.assert_awaited_once_with(
            "input_text.fans_health_repair_ladder"
        )
        pink = app._fan_repair_states["Pink Room"]
        assert pink["attempts"] == 3
        assert pink["status"] == REPAIR_FAILED
        assert "restored" in pink["detail"].lower()
        # A retry time already in the past must not fire the instant the app
        # comes back — it is floored to now + one grace delay.
        assert pink["next_retry_at"] >= before + datetime.timedelta(minutes=5)
        # A future retry is kept as-is.
        blue = app._fan_repair_states["Blue Room"]
        assert blue["attempts"] == 1
        assert blue["next_retry_at"] == future.replace(microsecond=0)

    def test_seed_floors_a_missing_retry_time(self):
        app = _make_app()
        _init_only(app)
        app._cached_auto_repair_delay_min = 5
        app.get_state = AsyncMock(return_value=json.dumps({"Pink Room": [4, None]}))

        before = datetime.datetime.now()
        _run(app._seed_repair_ladder())

        pink = app._fan_repair_states["Pink Room"]
        assert pink["attempts"] == 4
        assert pink["status"] == REPAIR_FAILED
        assert pink["next_retry_at"] >= before + datetime.timedelta(minutes=5)

    def test_seed_skips_unknown_fans_and_garbage_entries(self):
        app = _make_app()
        _init_only(app)
        app.get_state = AsyncMock(
            return_value=json.dumps(
                {
                    "Ghost Room": [4, None],       # not a configured fan
                    "Pink Room": ["nope", None],   # unparseable attempt count
                    "Blue Room": [0, None],        # nothing to restore
                }
            )
        )

        _run(app._seed_repair_ladder())

        for fr in app._fan_repair_states.values():
            assert fr["attempts"] == 0
            assert fr["status"] == REPAIR_IDLE
            assert fr["next_retry_at"] is None

    def test_seed_tolerates_unusable_helper_values(self):
        """A fresh/garbled helper must leave every fan idle, never crash
        startup — the helper is unknown until the first write."""
        for raw in ("", "unknown", "unavailable", "not json", '"a string"', "[1,2]"):
            app = _make_app()
            _init_only(app)
            app.get_state = AsyncMock(return_value=raw)

            _run(app._seed_repair_ladder())

            fr = app._fan_repair_states["Pink Room"]
            assert fr["attempts"] == 0, f"raw={raw!r}"
            assert fr["status"] == REPAIR_IDLE, f"raw={raw!r}"

    def test_seed_survives_a_helper_read_error(self):
        app = _make_app()
        _init_only(app)
        app.get_state = AsyncMock(side_effect=Exception("websocket down"))

        _run(app._seed_repair_ladder())  # must not raise

        assert app._fan_repair_states["Pink Room"]["attempts"] == 0


# ---------------------------------------------------------------------------
# Tests — Access-point awareness (Wi-Fi fans, not Z-Wave)
# ---------------------------------------------------------------------------

class TestAccessPointGate:
    """Modern Forms fans are Wi-Fi devices. When the AP they associate with
    is down, the fan being unreachable is a network fault — power-cycling it
    via the ZEN32 relay cannot help, so the repair is held and the alert
    text names the AP."""

    def _run_cycle(self, app, ap_state, fan_state="unavailable"):
        """Run one full check cycle with a fake HA behind it."""
        async def fake_get_state(entity_id=None, *a, **kw):
            if entity_id == AP_ENTITY:
                return ap_state
            if entity_id == PINK_ENTITY:
                return fan_state
            if entity_id.startswith("input_boolean"):
                return "on"
            if entity_id.startswith("input_number"):
                return "5"
            return "on"

        app.get_state = AsyncMock(side_effect=fake_get_state)
        with patch(
            "health_checks.checker_apps.fan_health_checker.fan_health_checker.ping_check",
            new_callable=AsyncMock,
            return_value={"status": "ok", "detail": "3ms"},
        ):
            _run(app._run_checks())

    def _detail_for(self, fan, ap_state):
        app = _make_app({"fans": AP_FANS})
        _init_only(app)

        async def fake_get_state(entity_id=None, *a, **kw):
            return ap_state if entity_id == AP_ENTITY else "unavailable"

        app.get_state = AsyncMock(side_effect=fake_get_state)
        return _run(app._check_fan_entity(fan))["detail"]

    def test_ap_down_holds_the_power_cycle(self):
        """An entity-down fan whose AP is disconnected is not repair-worthy:
        no power-cycle, and its grace timer never starts (an old one is
        cleared) so the AP outage cannot bank time toward a repair."""
        app = _make_app({"fans": AP_FANS})
        _init_only(app)
        app.create_task = MagicMock()
        # Pretend this fan had already been counting down for 30 minutes.
        app._fan_unhealthy_since["Pink Room"] = (
            datetime.datetime.now() - datetime.timedelta(minutes=30)
        )

        self._run_cycle(app, ap_state="disconnected")

        assert app._ap_state_by_fan["Pink Room"] == "disconnected"
        assert app._is_fan_repair_worthy(AP_FANS[0], _pink_down_results()) is False
        assert app._fan_unhealthy_since["Pink Room"] is None
        assert app._fan_repair_states["Pink Room"]["status"] == REPAIR_IDLE
        assert app._repair_status == REPAIR_IDLE
        app.create_task.assert_not_called()

    def test_ap_up_leaves_the_fan_repair_worthy(self):
        """AP connected → the fan itself is at fault, so a due repair fires."""
        app = _make_app({"fans": AP_FANS})
        _init_only(app)
        app.create_task = MagicMock()
        app._fan_unhealthy_since["Pink Room"] = (
            datetime.datetime.now() - datetime.timedelta(minutes=30)
        )

        self._run_cycle(app, ap_state="connected")

        assert app._is_fan_repair_worthy(AP_FANS[0], _pink_down_results()) is True
        assert app._fan_repair_states["Pink Room"]["status"] == REPAIR_IN_PROGRESS
        app.create_task.assert_called_once()

    def test_unknown_ap_state_does_not_gate(self):
        """A broken UniFi integration (unavailable/unknown/None) must not
        silently disable fan repair — only a positive down state gates."""
        for ap_state in ("unavailable", "unknown", None):
            app = _make_app({"fans": AP_FANS})
            _init_only(app)
            app.create_task = MagicMock()
            app._fan_unhealthy_since["Pink Room"] = (
                datetime.datetime.now() - datetime.timedelta(minutes=30)
            )

            self._run_cycle(app, ap_state=ap_state)

            assert app._is_fan_repair_worthy(AP_FANS[0], _pink_down_results()) is True, (
                f"ap_state={ap_state!r}"
            )
            assert (
                app._fan_repair_states["Pink Room"]["status"] == REPAIR_IN_PROGRESS
            ), f"ap_state={ap_state!r}"

    def test_every_down_state_gates(self):
        """disconnected / not_home / off all mean "AP is down" (the UniFi
        integration uses different sensor flavours per device class)."""
        for ap_state in ("disconnected", "not_home", "off"):
            app = _make_app({"fans": AP_FANS})
            _init_only(app)
            app._ap_state_by_fan["Pink Room"] = ap_state
            assert (
                app._is_fan_repair_worthy(AP_FANS[0], _pink_down_results()) is False
            ), f"ap_state={ap_state!r}"

    def test_detail_names_the_ap_when_it_is_down(self):
        detail = self._detail_for(AP_FANS[0], "disconnected")
        assert detail == (
            "State: unavailable (Wi-Fi fan; AP Kitchen Pantry U7 Pro is "
            "disconnected — fan offline expected, power-cycle held until "
            "the AP recovers)"
        )

    def test_detail_blames_the_fan_when_the_ap_is_up(self):
        detail = self._detail_for(AP_FANS[0], "Connected")
        assert detail == (
            "State: unavailable (Wi-Fi fan; AP Kitchen Pantry U7 Pro: "
            "connected — fan itself unreachable)"
        )

    def test_detail_says_unknown_when_the_ap_sensor_is_dark(self):
        detail = self._detail_for(AP_FANS[0], None)
        assert detail == (
            "State: unavailable (Wi-Fi fan; AP Kitchen Pantry U7 Pro: "
            "state unknown)"
        )

    def test_detail_still_flags_wifi_without_an_ap_configured(self):
        """No ap_status_entity → no AP read at all, but the alert still says
        Wi-Fi (a triage agent misread these as Z-Wave fans on 2026-08-31)."""
        app = _make_app()
        _init_only(app)
        app.get_state = AsyncMock(return_value="unavailable")

        result = _run(app._check_fan_entity(SAMPLE_FANS[0]))

        assert result["detail"] == "State: unavailable (Wi-Fi fan)"
        app.get_state.assert_awaited_once_with(SAMPLE_FANS[0]["entity_id"])

    def test_ap_read_error_is_treated_as_unknown(self):
        app = _make_app({"fans": AP_FANS})
        _init_only(app)

        async def fake_get_state(entity_id=None, *a, **kw):
            if entity_id == AP_ENTITY:
                raise Exception("websocket down")
            return "unavailable"

        app.get_state = AsyncMock(side_effect=fake_get_state)
        result = _run(app._check_fan_entity(AP_FANS[0]))

        assert app._ap_state_by_fan["Pink Room"] is None
        assert "state unknown" in result["detail"]
        assert app._is_fan_repair_worthy(AP_FANS[0], _pink_down_results()) is True
