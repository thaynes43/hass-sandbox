"""Unit tests for RepairableDeviceChecker."""

from __future__ import annotations

import asyncio
import datetime
import json
import socket
import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import closing_create_task

mock_hass = MagicMock()
mock_hass.Hass = type("_MockHass", (), {"__init__": lambda self, *a, **kw: None})
sys.modules["hassapi"] = mock_hass

_repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo_root / "apps"))
sys.path.insert(0, str(_repo_root))

from health_checks.checker_apps.device_checker.repairable_device_checker import (
    RepairableDeviceChecker,
    REPAIR_FAILED,
    REPAIR_IDLE,
    REPAIR_IN_PROGRESS,
    REPAIR_PENDING,
    REPAIR_SUCCESS,
)

# Reached the way the checker reaches it (its import put the health_checks
# root on sys.path), so patches land on the module object it actually uses.
import shared.switch_power_cycle as switch_power_cycle  # noqa: E402


DEFAULT_ARGS: Dict[str, Any] = {
    "ha_url": "http://ha:8123",
    "ha_token_env": "TOKEN",
    "checker_id": "printer",
    "checker_name": "Printer",
    "ping_host": "192.168.0.211",
    "ping_check_name": "Ping",
    "check_interval_s": 180,
    "repair_switch": "switch.downstairs_study_printer_switch",
    "repair_recovery_wait_s": 10,
    "repair_off_duration_s": 2,
    "auto_repair_enabled_default": False,
    "auto_repair_delay_min_default": 5,
    "entities": [
        {"entity_id": "sensor.brother_mfc_l3780cdw_series", "name": "Status"},
    ],
}


def _make_app(extra_args: dict | None = None) -> RepairableDeviceChecker:
    ad = MagicMock()
    config = MagicMock()
    app = RepairableDeviceChecker(ad, config)

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
    app.create_task = closing_create_task()

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


def _startup(app: RepairableDeviceChecker, mock_prov: MagicMock | None = None) -> None:
    if mock_prov is None:
        mock_prov = _make_mock_provisioner()
    app.initialize()
    with patch(
        "health_checks.checker_apps.device_checker.repairable_device_checker.HAProvisioner",
        return_value=mock_prov,
    ):
        _run(app._async_startup())


def _init_only(app: RepairableDeviceChecker) -> None:
    app.initialize()


SWITCH = DEFAULT_ARGS["repair_switch"]


@pytest.fixture(autouse=True)
def _no_real_sleeps():
    """Every sleep in the repair path is a wait, not a condition — skip them.

    The off duration, the turn-on confirmation polls and the recovery polls
    would otherwise really sleep (5 s a poll), and the confirmation alone is up
    to 2 x 60 s.  Elapsed times are counted from the constants, not the clock,
    so assertions on them are unaffected.
    """
    with patch("asyncio.sleep", new=AsyncMock(return_value=None)) as sleep:
        yield sleep


def _ts(offset_s: float) -> str:
    """An HA-style ``last_changed``, *offset_s* from now (aware, UTC)."""
    return (
        datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(seconds=offset_s)
    ).isoformat()


def _fresh(state: str) -> Dict[str, Any]:
    """The switch in *state*, changed after the power cycle started."""
    return {"entity_id": SWITCH, "state": state, "last_changed": _ts(3600)}


def _stale(state: str) -> Dict[str, Any]:
    """The switch in *state*, unchanged since well before the power cycle."""
    return {"entity_id": SWITCH, "state": state, "last_changed": _ts(-86400)}


def _turn_ons(app) -> int:
    return sum(
        1 for c in app.call_service.call_args_list
        if c.args and c.args[0] == "switch/turn_on"
    )


def _switch_reports(app, on_after_turn_ons: int | None = 1) -> None:
    """Install an awaited ``get_state`` for the repair switch.

    The switch reads a fresh ``off`` until *on_after_turn_ons* ``turn_on``
    calls have been made, then a fresh ``on``.  ``None`` = it never comes back.
    """

    def _state(entity_id=None, attribute=None, **kwargs):
        assert entity_id == SWITCH
        if on_after_turn_ons is not None and _turn_ons(app) >= on_after_turn_ons:
            return _fresh("on")
        return _fresh("off")

    app.get_state = AsyncMock(side_effect=_state)


class TestLifecycle:
    def test_registers_with_supports_repair(self):
        app = _make_app()
        _startup(app)
        register_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "register_checker"
        ]
        payload = json.loads(register_calls[0][1]["payload"])
        assert payload["supports_repair"] is True

    def test_provisions_repair_helpers(self):
        app = _make_app()
        mock_prov = _make_mock_provisioner()
        _startup(app, mock_prov)
        assert mock_prov.ensure_helper.call_count == 2


    def test_listens_for_repair_events(self):
        app = _make_app()
        _startup(app)
        event_names = [c[0][1] for c in app.listen_event.call_args_list]
        assert "health_check_repair_printer" in event_names

    def test_inherits_check_names(self):
        app = _make_app()
        _startup(app)
        register_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "register_checker"
        ]
        payload = json.loads(register_calls[0][1]["payload"])
        assert "Ping" in payload["check_names"]
        assert "Status" in payload["check_names"]


class TestChecksInherited:
    def test_entity_no_healthy_state_ok(self):
        """Entity with no healthy_state is ok if not unavailable/unknown."""
        app = _make_app()
        _init_only(app)
        app.get_state = AsyncMock(return_value="idle")
        result = _run(app._check_entity_state(app._entities[0]))
        assert result["status"] == "ok"

    def test_entity_unavailable_critical(self):
        app = _make_app()
        _init_only(app)
        app.get_state = AsyncMock(return_value="unavailable")
        result = _run(app._check_entity_state(app._entities[0]))
        assert result["status"] == "critical"


class TestRepairStateMachine:
    def test_initial_state_idle(self):
        app = _make_app()
        _init_only(app)
        assert app._repair_status == REPAIR_IDLE

    def test_unhealthy_with_auto_repair_goes_pending(self):
        app = _make_app()
        _init_only(app)
        app._cached_auto_repair_enabled = True
        app._cached_auto_repair_delay_min = 5

        results = [
            {"name": "Ping", "status": "critical", "detail": "timeout"},
            {"name": "Status", "status": "critical", "detail": "unavailable"},
        ]
        app._evaluate_auto_repair(results)
        assert app._repair_status == REPAIR_PENDING

    def test_all_ok_cancels_pending(self):
        app = _make_app()
        _init_only(app)
        app._repair_status = REPAIR_PENDING
        app._unhealthy_since = datetime.datetime.now()

        results = [
            {"name": "Ping", "status": "ok", "detail": "3ms"},
            {"name": "Status", "status": "ok", "detail": "idle"},
        ]
        app._evaluate_auto_repair(results)
        assert app._repair_status == REPAIR_IDLE

    def test_failed_no_auto_retry(self):
        app = _make_app()
        _init_only(app)
        app._repair_status = REPAIR_FAILED

        results = [
            {"name": "Ping", "status": "critical", "detail": "timeout"},
        ]
        app._evaluate_auto_repair(results)
        assert app._repair_status == REPAIR_FAILED

    def test_failed_clears_when_checks_recover(self):
        app = _make_app()
        _init_only(app)
        app._repair_status = REPAIR_FAILED
        app._repair_detail = "Did not recover after 300s"
        app._unhealthy_since = datetime.datetime.now()

        results = [
            {"name": "Ping", "status": "ok", "detail": "3ms"},
            {"name": "Status", "status": "ok", "detail": "idle"},
        ]
        app._evaluate_auto_repair(results)
        assert app._repair_status == REPAIR_IDLE
        assert app._repair_detail == ""
        assert app._unhealthy_since is None

    def test_manual_repair_from_failed(self):
        app = _make_app()
        _init_only(app)
        app._repair_status = REPAIR_FAILED
        app.create_task = closing_create_task()

        app._start_repair()
        assert app._repair_status == REPAIR_IN_PROGRESS

    def test_no_repair_switch_fails(self):
        app = _make_app({"repair_switch": ""})
        _init_only(app)

        app._start_repair()
        assert app._repair_status == REPAIR_FAILED
        assert "No repair switch" in app._repair_detail


class TestWarningNeverArmsARepair:
    """The DNS-fallback `warning` (device up, name broken) is UI-only: even
    long past the dwell it neither schedules nor starts a power cycle."""

    def test_idle_long_warning_does_nothing(self):
        app = _make_app()
        _init_only(app)
        app._cached_auto_repair_enabled = True
        app._cached_auto_repair_delay_min = 5
        app.create_task = closing_create_task()
        warning = [{
            "name": "Ping",
            "status": "warning",
            "detail": "4ms via 192.168.0.70 — cannot resolve movieroomsonos.haynesnetwork",
        }]

        for _ in range(4):
            app._evaluate_auto_repair(warning)
        app._unhealthy_since = datetime.datetime.now() - datetime.timedelta(hours=2)
        app._evaluate_auto_repair(warning)

        assert app._repair_status == REPAIR_IDLE
        assert app._auto_repair_deadline is None
        app.create_task.assert_not_called()

    def test_warning_stands_down_a_pending_repair(self):
        """Port down → pending; DNS breaks and the Port comes back (fallback
        warning). The countdown must not survive, or the next single bad
        cycle would power-cycle with no dwell at all."""
        app = _make_app()
        _init_only(app)
        app._cached_auto_repair_enabled = True
        app._cached_auto_repair_delay_min = 10
        app.create_task = closing_create_task()
        critical = [{"name": "Ping", "status": "critical", "detail": "timeout (3 attempts)"}]
        warning = [{
            "name": "Ping",
            "status": "warning",
            "detail": "4ms via 192.168.0.70 — cannot resolve movieroomsonos.haynesnetwork",
        }]

        app._evaluate_auto_repair(critical)
        assert app._repair_status == REPAIR_PENDING
        # The deadline passes while the device is up behind broken DNS.
        app._unhealthy_since = datetime.datetime.now() - datetime.timedelta(minutes=30)
        app._evaluate_auto_repair(warning)

        assert app._repair_status == REPAIR_IDLE
        assert app._auto_repair_deadline is None
        assert app._unhealthy_since is None

        # One bad cycle afterwards starts a fresh dwell instead of firing.
        app._evaluate_auto_repair(critical)
        assert app._repair_status == REPAIR_PENDING
        app.create_task.assert_not_called()


class TestRelapseAfterSuccess:
    """A ``success`` that does not stick must not stand for the rest of the
    outage: it is one of the bridge's repair-hold states, so it would keep
    withholding the page for a device that is down again. It becomes
    ``failed`` — which releases the page — and the one-repair-per-outage cap
    holds: no second auto-repair until a fully healthy cycle."""

    _DOWN = [{"name": "Ping", "status": "critical", "detail": "timeout (3 attempts)"}]
    _UP = [{"name": "Ping", "status": "ok", "detail": "3ms"}]

    def _app(self):
        app = _make_app()
        _init_only(app)
        app._cached_auto_repair_enabled = True
        app._cached_auto_repair_delay_min = 5
        app.create_task = closing_create_task()
        app._repair_status = REPAIR_SUCCESS
        app._repair_detail = "Recovered after 45s"
        app._unhealthy_since = None  # cleared by the successful repair
        return app

    def test_relapse_marks_the_repair_failed(self):
        app = self._app()

        app._evaluate_auto_repair(self._DOWN)

        assert app._repair_status == REPAIR_FAILED
        assert app._repair_detail == (
            "Relapsed after a successful repair — recovery did not stick"
        )
        assert _logged(app, "WARNING", "relapsed after a successful repair")
        app.create_task.assert_not_called()
        app.call_service.assert_not_called()

    def test_no_second_repair_this_outage(self):
        app = self._app()
        app._evaluate_auto_repair(self._DOWN)

        # Long past any dwell: still no new power cycle.
        app._unhealthy_since = datetime.datetime.now() - datetime.timedelta(hours=3)
        for _ in range(3):
            app._evaluate_auto_repair(self._DOWN)

        assert app._repair_status == REPAIR_FAILED
        app.create_task.assert_not_called()

    def test_the_reported_state_releases_the_bridge_hold(self):
        from shared.alertmanager_bridge import _REPAIR_HOLD_STATES

        app = self._app()
        app._refresh_auto_repair_config = AsyncMock()
        app._run_checks_only = AsyncMock(return_value=self._DOWN)

        _run(app._run_checks())

        state = _report_payloads(app)[-1]["repair_state"]
        assert state["status"] == REPAIR_FAILED
        assert state["status"] not in _REPAIR_HOLD_STATES

    def test_success_plus_unknown_is_left_alone(self):
        """An `unknown` result says nothing about the device, so a good repair
        stays `success`, no relapse is logged, and nothing is armed. (DNS no
        longer produces one — an unresolvable ping is critical — but other
        paths can.)"""
        app = self._app()
        unknown = [{"name": "Ping", "status": "unknown", "detail": "no data"}]

        app._evaluate_auto_repair(unknown)

        assert app._repair_status == REPAIR_SUCCESS
        assert app._repair_detail == "Recovered after 45s"
        assert not _logged(app, "WARNING", "relapsed")
        assert app._unhealthy_since is None
        assert app._auto_repair_deadline is None
        app.create_task.assert_not_called()

    def test_success_plus_fallback_warning_counts_as_healthy(self):
        """The fallback warning means the device answered: a healthy cycle,
        so `success` clears to `idle` — never a relapse, never a repair."""
        app = self._app()

        app._evaluate_auto_repair([{
            "name": "Ping",
            "status": "warning",
            "detail": "4ms via 192.168.0.70 — cannot resolve movieroomsonos.haynesnetwork",
        }])

        assert app._repair_status == REPAIR_IDLE
        assert "Relapsed" not in app._repair_detail
        app.create_task.assert_not_called()

    def test_success_then_healthy_still_goes_idle(self):
        app = self._app()

        app._evaluate_auto_repair(self._UP)

        assert app._repair_status == REPAIR_IDLE
        assert app._repair_detail == ""

    def test_relapse_then_healthy_goes_idle(self):
        app = self._app()
        app._evaluate_auto_repair(self._DOWN)

        app._evaluate_auto_repair(self._UP)

        assert app._repair_status == REPAIR_IDLE
        assert app._repair_detail == ""


class TestRepairExecution:
    def test_successful_repair(self):
        app = _make_app({"repair_recovery_wait_s": 10, "repair_off_duration_s": 0})
        _init_only(app)
        app._repair_status = REPAIR_IN_PROGRESS
        _switch_reports(app)

        app._run_checks_only = AsyncMock(return_value=[
            {"name": "Ping", "status": "ok", "detail": "3ms"},
            {"name": "Status", "status": "ok", "detail": "idle"},
        ])

        _run(app._execute_repair())

        assert app._repair_status == REPAIR_SUCCESS
        calls = [c[0][0] for c in app.call_service.call_args_list]
        assert "switch/turn_off" in calls
        assert "switch/turn_on" in calls

    def test_repair_timeout(self):
        app = _make_app({"repair_recovery_wait_s": 10, "repair_off_duration_s": 0})
        _init_only(app)
        app._repair_status = REPAIR_IN_PROGRESS
        _switch_reports(app)

        app._run_checks_only = AsyncMock(return_value=[
            {"name": "Ping", "status": "critical", "detail": "timeout"},
        ])

        _run(app._execute_repair())
        assert app._repair_status == REPAIR_FAILED

    def test_repair_error(self):
        app = _make_app()
        _init_only(app)
        app._repair_status = REPAIR_IN_PROGRESS
        app.call_service = MagicMock(side_effect=Exception("fail"))

        _run(app._execute_repair())
        assert app._repair_status == REPAIR_FAILED


_ALL_OK = [
    {"name": "Ping", "status": "ok", "detail": "3ms"},
    {"name": "Status", "status": "ok", "detail": "idle"},
]

_CONFIRM_POLLS = (
    switch_power_cycle.SWITCH_CONFIRM_TIMEOUT_S
    // switch_power_cycle.SWITCH_CONFIRM_POLL_S
)


def _report_payloads(app) -> list[dict]:
    return [
        json.loads(c[1]["payload"])
        for c in app.fire_event.call_args_list
        if c[1].get("command") == "report_status"
    ]


def _logged(app, level: str, fragment: str) -> bool:
    return any(
        c[1].get("level") == level and fragment in str(c[0][0])
        for c in app.log.call_args_list
    )


class TestTurnOnConfirmation:
    """A repair must never leave the device unpowered: after ``turn_on`` the
    switch has to report ``on`` before the recovery wait starts, with one
    retry, and a switch that never comes back fails the repair at once.

    ``get_state`` is an ``AsyncMock`` throughout — the ``_make_app`` default
    is a sync ``MagicMock``, which the awaited read would turn into an
    exception, and every "not on" assertion would then pass for that reason.
    """

    def _app(self):
        app = _make_app({"repair_recovery_wait_s": 10, "repair_off_duration_s": 0})
        _init_only(app)
        app._repair_status = REPAIR_IN_PROGRESS
        return app

    def test_confirmed_on_first_try(self):
        app = self._app()
        _switch_reports(app)
        app._run_checks_only = AsyncMock(return_value=_ALL_OK)

        _run(app._execute_repair())

        assert app._repair_status == REPAIR_SUCCESS
        assert _turn_ons(app) == 1
        # Read with the full state, so last_changed is available.
        assert app.get_state.await_args.kwargs.get("attribute") == "all"
        assert _logged(app, "INFO", f"{SWITCH} confirmed on")

    def test_confirmed_only_after_the_retry(self):
        app = self._app()
        _switch_reports(app, on_after_turn_ons=2)
        turn_ons_when_recovery_polled: list[int] = []

        async def _checks():
            turn_ons_when_recovery_polled.append(_turn_ons(app))
            return _ALL_OK

        app._run_checks_only = AsyncMock(side_effect=_checks)

        _run(app._execute_repair())

        assert _turn_ons(app) == 2
        assert _logged(app, "WARNING", "turning it on again")
        assert app._repair_status == REPAIR_SUCCESS
        # The recovery clock starts only once the switch is confirmed on: no
        # recovery poll before the retry, and the duration counts from there.
        assert turn_ons_when_recovery_polled == [2]
        assert _report_payloads(app)[-1]["repair_events"] == [
            {"result": "success", "duration_s": 5},
        ]

    def test_never_on_fails_without_the_recovery_wait(self):
        app = self._app()
        _switch_reports(app, on_after_turn_ons=None)
        app._run_checks_only = AsyncMock(return_value=_ALL_OK)

        _run(app._execute_repair())

        assert app._repair_status == REPAIR_FAILED
        assert app._repair_detail == (
            f"{SWITCH} did not turn back on — check the outlet"
        )
        assert _turn_ons(app) == 2
        app._run_checks_only.assert_not_awaited()
        assert _logged(app, "ERROR", "still not on after a second turn_on")
        # Two full confirmation windows, and not one read more.
        assert app.get_state.await_count == 2 * _CONFIRM_POLLS
        final = _report_payloads(app)[-1]
        assert final["repair_state"]["status"] == REPAIR_FAILED
        assert final["repair_events"] == [{"result": "failed"}]
        assert app._pending_repair_events == []

    def test_missing_entity_fails(self):
        """The unifi integration can drop the PDU's outlet entities entirely."""
        app = self._app()
        app.get_state = AsyncMock(return_value=None)
        app._run_checks_only = AsyncMock(return_value=_ALL_OK)

        _run(app._execute_repair())

        assert app._repair_status == REPAIR_FAILED
        # The card and the Alertmanager description point at the real fix:
        # reloading the integration, not the outlet.
        assert app._repair_detail == (
            f"{SWITCH} did not turn back on — the entity is missing from Home "
            f"Assistant — reload the integration that owns it"
        )
        assert _turn_ons(app) == 2
        app._run_checks_only.assert_not_awaited()
        assert _logged(app, "ERROR", "the entity is missing")

    def test_an_unavailable_switch_points_at_the_integration(self):
        """An integration that lost the device keeps the entity but marks it
        unavailable: that is not the outlet's fault either."""
        app = self._app()
        app.get_state = AsyncMock(return_value={
            "entity_id": SWITCH, "state": "unavailable", "last_changed": None,
        })
        app._run_checks_only = AsyncMock(return_value=_ALL_OK)

        _run(app._execute_repair())

        assert app._repair_status == REPAIR_FAILED
        assert app._repair_detail == (
            f"{SWITCH} did not turn back on — "
            f"{switch_power_cycle.UNAVAILABLE_ENTITY_NOTE}"
        )
        app._run_checks_only.assert_not_awaited()

    def test_a_read_that_raises_is_not_a_missing_entity(self):
        """get_state raising (e.g. the HASS plugin disconnected) must not be
        reported as "reload the integration": the detail says the switch
        could not be read, and the exception is logged at WARNING."""
        app = self._app()
        app.get_state = AsyncMock(side_effect=RuntimeError("plugin disconnected"))
        app._run_checks_only = AsyncMock(return_value=_ALL_OK)

        _run(app._execute_repair())

        assert app._repair_status == REPAIR_FAILED
        assert app._repair_detail == (
            f"{SWITCH} did not turn back on — "
            f"{switch_power_cycle.READ_ERROR_NOTE}"
        )
        assert "reload the integration" not in app._repair_detail
        assert _logged(app, "WARNING", "plugin disconnected")
        app._run_checks_only.assert_not_awaited()

    def test_a_stale_on_is_not_a_confirmation(self):
        """HA reports late: the first reads after turn_on can still be the
        ``on`` from before the cycle. Taking it would confirm a lost turn_on."""
        app = self._app()
        reads = {"n": 0}

        def _state(entity_id=None, attribute=None, **kwargs):
            reads["n"] += 1
            if _turn_ons(app) >= 2:
                return _fresh("on")
            if reads["n"] <= 3:
                return _stale("on")  # HA has not reported the off yet
            return _fresh("off")  # ...and the first turn_on was lost

        app.get_state = AsyncMock(side_effect=_state)
        app._run_checks_only = AsyncMock(return_value=_ALL_OK)

        _run(app._execute_repair())

        assert _turn_ons(app) == 2
        assert app._repair_status == REPAIR_SUCCESS

    def test_an_on_that_never_changed_is_accepted_at_the_window_end(self):
        """The switch never went off (turn_off lost or coalesced): it is
        powered, so the repair goes on to its recovery wait — with a warning,
        and without a needless second turn_on."""
        app = self._app()
        app.get_state = AsyncMock(return_value=_stale("on"))
        app._run_checks_only = AsyncMock(return_value=_ALL_OK)

        _run(app._execute_repair())

        assert _turn_ons(app) == 1
        assert app.get_state.await_count == _CONFIRM_POLLS
        # WARNING: on a successful repair this line is the only record that
        # HA never saw the outlet go off.
        assert _logged(app, "WARNING", "never saw it go off")
        assert app._repair_status == REPAIR_SUCCESS
        # On success the note is logged only — the card keeps a plain detail.
        assert app._repair_detail == "Recovered after 5s"
        assert _logged(app, "INFO", switch_power_cycle.NEVER_OFF_NOTE)

    def test_never_off_note_reaches_the_failure_detail(self):
        """The outlet may still have cycled, so it is not a failure — but if
        the device then does not recover, the card and the page say so."""
        app = self._app()
        app.get_state = AsyncMock(return_value=_stale("on"))
        app._run_checks_only = AsyncMock(return_value=[
            {"name": "Ping", "status": "critical", "detail": "timeout"},
        ])

        _run(app._execute_repair())

        assert app._repair_status == REPAIR_FAILED
        assert app._repair_detail == (
            "Did not recover after 10s (the outlet never reported off — "
            "it may not have been power cycled)"
        )
        state = _report_payloads(app)[-1]["repair_state"]
        assert state["detail"] == app._repair_detail

    def test_no_note_leaves_the_failure_detail_plain(self):
        app = self._app()
        _switch_reports(app)
        app._run_checks_only = AsyncMock(return_value=[
            {"name": "Ping", "status": "critical", "detail": "timeout"},
        ])

        _run(app._execute_repair())

        assert app._repair_detail == "Did not recover after 10s"

    def test_confirm_window_constants_are_overridable(self):
        app = self._app()
        _switch_reports(app, on_after_turn_ons=None)
        app._run_checks_only = AsyncMock(return_value=_ALL_OK)

        with patch.object(switch_power_cycle, "SWITCH_CONFIRM_TIMEOUT_S", 10), \
                patch.object(switch_power_cycle, "SWITCH_CONFIRM_POLL_S", 5):
            _run(app._execute_repair())

        # 10 s / 5 s = two reads a window, two windows.
        assert app.get_state.await_count == 4
        assert app._repair_status == REPAIR_FAILED

    def test_last_changed_as_datetime_or_naive_string_still_confirms_early(self):
        """AppDaemon can return last_changed as a datetime, and HA's naive
        timestamps are UTC: both must confirm on the first read, not burn the
        window and claim HA never saw the outlet go off."""
        after = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
        for changed in (after, after.replace(tzinfo=None).isoformat()):
            app = self._app()
            app.get_state = AsyncMock(return_value={
                "entity_id": SWITCH, "state": "on", "last_changed": changed,
            })
            result = _run(switch_power_cycle.power_cycle_switch(app, SWITCH, 0))
            assert result == switch_power_cycle.PowerCycleResult(switch_on=True)
            assert app.get_state.await_count == 1, changed
            assert not _logged(app, "WARNING", "never saw it go off")

    def test_an_off_read_then_on_confirms_even_without_a_timestamp(self):
        """If the reads saw the outlet off, HA did register the cycle: the
        next on confirms, with no timestamp needed and no never-off note."""
        app = self._app()
        reads = iter([
            {"entity_id": SWITCH, "state": "off", "last_changed": None},
            {"entity_id": SWITCH, "state": "on", "last_changed": "garbage"},
        ])
        app.get_state = AsyncMock(side_effect=lambda *a, **k: next(reads))
        result = _run(switch_power_cycle.power_cycle_switch(app, SWITCH, 0))
        assert result == switch_power_cycle.PowerCycleResult(switch_on=True)
        assert app.get_state.await_count == 2
        assert not _logged(app, "WARNING", "never saw it go off")

    def test_helper_result_carries_switch_on_and_note(self):
        app = self._app()
        _switch_reports(app)
        ok = _run(switch_power_cycle.power_cycle_switch(app, SWITCH, 0))
        assert ok == switch_power_cycle.PowerCycleResult(switch_on=True, note="")

        app = self._app()
        app.get_state = AsyncMock(return_value=_stale("on"))
        never_off = _run(switch_power_cycle.power_cycle_switch(app, SWITCH, 0))
        assert never_off == switch_power_cycle.PowerCycleResult(
            switch_on=True, note=switch_power_cycle.NEVER_OFF_NOTE
        )

        app = self._app()
        _switch_reports(app, on_after_turn_ons=None)
        dead = _run(switch_power_cycle.power_cycle_switch(app, SWITCH, 0))
        assert dead == switch_power_cycle.PowerCycleResult(switch_on=False)

    def test_recovery_accepts_the_dns_fallback_warning(self):
        """Ping answered via ping_fallback_host (warning) = the device is up:
        a DNS outage must not turn a power cycle that worked into a failure."""
        app = self._app()
        _switch_reports(app)
        app._run_checks_only = AsyncMock(return_value=[{
            "name": "Ping",
            "status": "warning",
            "detail": "4ms via 192.168.0.70 — cannot resolve movieroomsonos.haynesnetwork",
        }])

        _run(app._execute_repair())

        assert app._repair_status == REPAIR_SUCCESS

    def test_recovery_wait_ends_by_wall_clock(self):
        """Each check takes time too (a timed-out ping is seconds): the wait
        must end after repair_recovery_wait_s of *wall-clock* time, not after
        that many seconds of sleeps. Here every check takes 60 s, so a 300 s
        wait is 6 checks (starting at 0, 60, …, 300 s), not 60."""
        app = _make_app({"repair_recovery_wait_s": 300, "repair_off_duration_s": 0})
        _init_only(app)
        app._repair_status = REPAIR_IN_PROGRESS
        _switch_reports(app)
        clock = {"now": 1000.0}

        async def _slow_checks():
            clock["now"] += 60
            return [{"name": "Ping", "status": "critical", "detail": "timeout"}]

        app._run_checks_only = AsyncMock(side_effect=_slow_checks)

        with patch("time.monotonic", new=lambda: clock["now"]):
            _run(app._execute_repair())

        assert app._run_checks_only.await_count == 6  # starts at 0, 60, …, 300 s
        assert app._repair_status == REPAIR_FAILED
        assert app._repair_detail == "Did not recover after 300s"

    def test_recovery_duration_is_wall_clock_on_success(self):
        app = _make_app({"repair_recovery_wait_s": 300, "repair_off_duration_s": 0})
        _init_only(app)
        app._repair_status = REPAIR_IN_PROGRESS
        _switch_reports(app)
        clock = {"now": 1000.0}
        calls = {"n": 0}

        async def _checks():
            calls["n"] += 1
            clock["now"] += 40
            status = "ok" if calls["n"] == 2 else "critical"
            return [{"name": "Ping", "status": status, "detail": ""}]

        app._run_checks_only = AsyncMock(side_effect=_checks)

        with patch("time.monotonic", new=lambda: clock["now"]):
            _run(app._execute_repair())

        assert app._repair_status == REPAIR_SUCCESS
        # Measured when the recovering check started: 0 s, then 40 s.
        assert app._repair_detail == "Recovered after 40s"

    def test_recovery_does_not_accept_unknown(self):
        app = self._app()
        _switch_reports(app)
        app._run_checks_only = AsyncMock(return_value=[
            {"name": "Ping", "status": "unknown", "detail": "no data"},
        ])

        _run(app._execute_repair())

        assert app._repair_status == REPAIR_FAILED

    def test_unreadable_switch_counts_as_not_on(self):
        app = self._app()
        app.get_state = AsyncMock(side_effect=RuntimeError("plugin down"))
        app._run_checks_only = AsyncMock(return_value=_ALL_OK)

        _run(app._execute_repair())

        assert app._repair_status == REPAIR_FAILED
        app._run_checks_only.assert_not_awaited()


class TestRepairEvents:
    """repair_events must be drained exactly once into the report_status
    payload that carries the repair's conclusion, and never duplicated on
    later reports."""

    @staticmethod
    def _report_payloads(app) -> list[dict]:
        return [
            json.loads(c[1]["payload"])
            for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
        ]

    def test_successful_repair_emits_repair_event(self):
        app = _make_app({"repair_recovery_wait_s": 10, "repair_off_duration_s": 0})
        _init_only(app)
        app._repair_status = REPAIR_IN_PROGRESS
        _switch_reports(app)

        app._run_checks_only = AsyncMock(return_value=[
            {"name": "Ping", "status": "ok", "detail": "3ms"},
            {"name": "Status", "status": "ok", "detail": "idle"},
        ])

        _run(app._execute_repair())

        payloads = self._report_payloads(app)
        # The final report (repair conclusion) must carry exactly one event.
        # Recovery is detected on the first poll (REPAIR_POLL_INTERVAL_S=5s).
        final = payloads[-1]
        assert final["repair_events"] == [
            {"result": "success", "duration_s": 5},
        ]
        # Buffer is drained — nothing left pending.
        assert app._pending_repair_events == []

    def test_repair_timeout_emits_failed_event_with_wait_budget(self):
        app = _make_app({"repair_recovery_wait_s": 10, "repair_off_duration_s": 0})
        _init_only(app)
        app._repair_status = REPAIR_IN_PROGRESS
        _switch_reports(app)

        app._run_checks_only = AsyncMock(return_value=[
            {"name": "Ping", "status": "critical", "detail": "timeout"},
        ])

        _run(app._execute_repair())

        payloads = self._report_payloads(app)
        final = payloads[-1]
        assert final["repair_events"] == [
            {"result": "failed", "duration_s": 10},
        ]
        assert app._pending_repair_events == []

    def test_repair_error_emits_failed_event_without_duration(self):
        app = _make_app()
        _init_only(app)
        app._repair_status = REPAIR_IN_PROGRESS
        app.call_service = MagicMock(side_effect=Exception("fail"))

        _run(app._execute_repair())

        payloads = self._report_payloads(app)
        final = payloads[-1]
        assert final["repair_events"] == [{"result": "failed"}]
        assert "duration_s" not in final["repair_events"][0]
        assert app._pending_repair_events == []

    def test_repair_event_not_repeated_on_next_report(self):
        """The event is a one-shot edge event — later reports must not
        carry it again."""
        app = _make_app({"repair_recovery_wait_s": 10, "repair_off_duration_s": 0})
        _init_only(app)
        app._repair_status = REPAIR_IN_PROGRESS
        _switch_reports(app)

        app._run_checks_only = AsyncMock(return_value=[
            {"name": "Ping", "status": "ok", "detail": "3ms"},
            {"name": "Status", "status": "ok", "detail": "idle"},
        ])
        _run(app._execute_repair())

        # Subsequent regular report_status payload must not repeat the event.
        payload = app._build_report_payload([
            {"name": "Ping", "status": "ok", "detail": "3ms"},
        ])
        assert "repair_events" not in payload

    def test_regular_report_has_no_repair_events_when_none_pending(self):
        app = _make_app()
        _init_only(app)

        payload = app._build_report_payload([
            {"name": "Ping", "status": "ok", "detail": "3ms"},
        ])
        assert "repair_events" not in payload
        assert payload["repair_state"]["status"] == REPAIR_IDLE


class TestReportPayload:
    def test_includes_repair_state(self):
        app = _make_app()
        _init_only(app)
        app._cached_auto_repair_enabled = False
        app._cached_auto_repair_delay_min = 5

        payload = app._build_report_payload([
            {"name": "Ping", "status": "ok", "detail": "3ms"},
        ])
        assert "repair_state" in payload
        assert payload["repair_state"]["status"] == REPAIR_IDLE


class TestRepairCommand:
    def test_start_repair_command(self):
        app = _make_app()
        _init_only(app)
        app.create_task = closing_create_task()

        app._on_repair_command(
            "health_check_repair_printer",
            {"action": "start_repair"},
            {},
        )
        assert app._repair_status == REPAIR_IN_PROGRESS


def _prod_entry(key: str) -> Dict[str, Any]:
    """One app entry from apps-prod.yaml, with ``!secret`` values stubbed."""
    import yaml

    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_constructor("!secret", lambda loader, node: f"secret:{node.value}")
    path = _repo_root / "apps" / "apps-prod.yaml"
    with open(path, encoding="utf-8") as fh:
        return yaml.load(fh, Loader=_Loader)[key]


class TestMovieRoomSonosProdConfig:
    """The real ``movie_room_sonos_health_checker`` entry: ping is its only
    signal (no ``entities:``, so a Music Assistant restart can never
    power-cycle the Port), and a sustained ping failure alone arms the PDU
    outlet power cycle."""

    MOD_BASE = "health_checks.checker_apps.device_checker.device_checker"

    def _app(self):
        app = _make_app()
        app.args = _prod_entry("movie_room_sonos_health_checker")
        _init_only(app)
        return app

    @staticmethod
    def _resolver(resolves: bool):
        """Patch the loop's getaddrinfo — unit tests never touch real DNS."""

        async def _getaddrinfo(host, port, *args, **kwargs):
            if not resolves:
                raise socket.gaierror(-2, "Name does not resolve")
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.0.70", 0))]

        return patch.object(
            asyncio.BaseEventLoop, "getaddrinfo",
            new=AsyncMock(side_effect=_getaddrinfo),
        )

    def test_ping_is_the_only_check(self):
        app = self._app()

        assert app._entities == []
        assert app._build_check_names() == ["Ping"]
        assert app._ping_fallback_host == "192.168.0.70"
        assert app._repair_switch == "switch.power_distribution_hi_density_outlet_21"
        assert app._cached_auto_repair_enabled is True
        assert app._cached_auto_repair_delay_min == 10

    def test_pings_the_hostname_with_retries(self):
        app = self._app()
        ping = AsyncMock(return_value={"status": "ok", "detail": "4ms"})

        with self._resolver(True), patch(f"{self.MOD_BASE}.ping_check", new=ping):
            results = _run(app._run_checks_only())

        ping.assert_awaited_once_with("movieroomsonos.haynesnetwork", attempts=3)
        assert results == [{"name": "Ping", "status": "ok", "detail": "4ms"}]
        # Nothing but the ping is read — no entity state at all.
        app.get_state.assert_not_called()

    def test_an_unresolved_name_falls_back_to_the_reserved_ip(self):
        app = self._app()
        ping = AsyncMock(
            return_value={"status": "critical", "detail": "timeout (3 attempts)"}
        )

        with self._resolver(False), patch(f"{self.MOD_BASE}.ping_check", new=ping):
            results = _run(app._run_checks_only())

        ping.assert_awaited_once_with("192.168.0.70", attempts=3)
        # A dead Port during a DNS outage is still critical: it repairs and pages.
        assert results[0]["status"] == "critical"

    def test_a_sustained_ping_failure_arms_then_fires_the_power_cycle(self):
        app = self._app()
        down = [{"name": "Ping", "status": "critical", "detail": "timeout (3 attempts)"}]

        app._evaluate_auto_repair(down)
        assert app._repair_status == REPAIR_PENDING

        app._unhealthy_since = datetime.datetime.now() - datetime.timedelta(minutes=11)
        app._auto_repair_deadline = app._unhealthy_since + datetime.timedelta(minutes=10)
        app._evaluate_auto_repair(down)
        assert app._repair_status == REPAIR_IN_PROGRESS
