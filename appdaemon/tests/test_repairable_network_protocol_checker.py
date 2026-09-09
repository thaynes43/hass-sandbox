"""Unit tests for RepairableNetworkProtocolChecker.

Every test in here is a hardware-safety guarantee for the TubesZB ESP32
restart ladder (see the module docstring): the board is only ever restarted
on the stale-client signature, never more often than the caps allow, and the
caps survive an AppDaemon restart.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
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

from health_checks.checker_apps.network_protocol_checker.repairable_network_protocol_checker import (
    RepairableNetworkProtocolChecker,
    ATTEMPT_WINDOW_S,
    REPAIR_FAILED,
    REPAIR_IDLE,
    REPAIR_IN_PROGRESS,
    REPAIR_PENDING,
    REPAIR_POLL_INTERVAL_S,
    REPAIR_SUCCESS,
)

MODULE = (
    "health_checks.checker_apps.network_protocol_checker"
    ".repairable_network_protocol_checker"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

#: Mirrors the production ``zwave_health_checker`` entry in apps-prod.yaml,
#: with only the recovery wait shortened (asyncio.sleep is patched anyway).
DEFAULT_ARGS: Dict[str, Any] = {
    "ha_url": "http://ha:8123",
    "ha_token_env": "TOKEN",
    "checker_id": "zwave",
    "checker_name": "Z-Wave",
    "entity_id": "sensor.800_series_long_range_gpio_module_status",
    "entity_healthy_state": "ready",
    "entity_check_name": "Integration Status",
    "radio_host": "tubeszb-zwave01.haynesnetwork",
    "radio_check_name": "Radio Ping",
    "web_ui_url": "https://zwave.haynesops.com",
    "web_ui_check_name": "Web UI",
    "check_interval_s": 180,
    "repair_button": "button.restart_the_esp32_device_2",
    "repair_requires_radio_ping": True,
    "repair_serial_connected_entity": "binary_sensor.tubeszb_zw_serial_connected_2",
    "repair_min_interval_s": 900,
    "repair_max_per_24h": 3,
    "repair_quiet_period_s": 180,
    "repair_recovery_wait_s": 30,
    "auto_repair_enabled_default": True,
    "auto_repair_delay_min_default": 5,
}

ENTITY_CHECK = DEFAULT_ARGS["entity_check_name"]
PING_CHECK = DEFAULT_ARGS["radio_check_name"]
WEB_CHECK = DEFAULT_ARGS["web_ui_check_name"]
SERIAL_ENTITY = DEFAULT_ARGS["repair_serial_connected_entity"]
REPAIR_BUTTON = DEFAULT_ARGS["repair_button"]

#: Default HA states: integration wedged, board still claims a serial client,
#: auto-repair on with a 5 minute dwell — i.e. the stale-client signature.
DEFAULT_STATES: Dict[str, Any] = {
    DEFAULT_ARGS["entity_id"]: "driver_failed",
    SERIAL_ENTITY: "on",
    "input_boolean.zwave_health_auto_repair": "on",
    "input_number.zwave_health_auto_repair_delay": "5",
}


def _make_app(
    extra_args: dict | None = None, states: dict | None = None
) -> RepairableNetworkProtocolChecker:
    ad = MagicMock()
    config = MagicMock()
    app = RepairableNetworkProtocolChecker(ad, config)

    args = dict(DEFAULT_ARGS)
    if extra_args:
        args.update(extra_args)
    app.args = args

    app.entity_states = dict(DEFAULT_STATES)
    if states:
        app.entity_states.update(states)
    #: What get_state(sensor.health_check_status, attribute="all") returns.
    app.controller_state = None

    def _get_state(entity_id=None, **kwargs):
        if kwargs.get("attribute") == "all":
            return app.controller_state
        return app.entity_states.get(entity_id)

    app.get_state = AsyncMock(side_effect=_get_state)
    app.set_state = MagicMock()
    app.call_service = MagicMock()
    app.listen_event = MagicMock()
    app.fire_event = MagicMock()
    app.run_in = MagicMock()
    app.run_every = MagicMock()
    app.log = MagicMock()

    # create_task captures the repair coroutine rather than scheduling it, so
    # a test decides whether (and when) the repair actually runs — _drive().
    app.captured_tasks = []
    app.create_task = MagicMock(side_effect=app.captured_tasks.append)

    return app


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _drive(app, coro):
    """Await *coro*, then any repair coroutine it handed to create_task.

    asyncio.sleep is patched out so the repair's recovery poll loop runs
    instantly instead of taking repair_recovery_wait_s seconds.
    """

    async def _inner():
        result = await coro
        while app.captured_tasks:
            await app.captured_tasks.pop(0)
        return result

    with patch(f"{MODULE}.asyncio.sleep", new=AsyncMock(return_value=None)):
        return _run(_inner())


def _drain(app):
    """Run any repair coroutine started outside an awaited call."""

    async def _noop():
        return None

    return _drive(app, _noop())


def _evaluate(app, results):
    return _drive(app, app._evaluate_auto_repair(results))


def _make_mock_provisioner() -> MagicMock:
    prov = MagicMock()
    prov.ensure_helper = AsyncMock(return_value=False)
    return prov


def _startup(app, mock_prov: MagicMock | None = None) -> None:
    if mock_prov is None:
        mock_prov = _make_mock_provisioner()
    app.initialize()
    with patch(f"{MODULE}.HAProvisioner", return_value=mock_prov):
        _run(app._async_startup())


def _init_only(app) -> None:
    app.initialize()
    # Populate the auto-repair cache the way a real check cycle would.
    _run(app._refresh_auto_repair_config())


def _results(entity: str = "critical", ping: str = "ok", web: str = "ok"):
    return [
        {
            "name": ENTITY_CHECK,
            "status": entity,
            "detail": "ready" if entity == "ok"
            else "Expected 'ready', got 'driver_failed'",
        },
        {
            "name": PING_CHECK,
            "status": ping,
            "detail": "2ms" if ping == "ok" else "timeout",
        },
        {
            "name": WEB_CHECK,
            "status": web,
            "detail": "200 OK" if web == "ok" else "HTTP 503",
        },
    ]


def _presses(app) -> List[Any]:
    return [
        c for c in app.call_service.call_args_list
        if c[0] and c[0][0] == "button/press"
    ]


def _report_payloads(app) -> List[dict]:
    return [
        json.loads(c[1]["payload"])
        for c in app.fire_event.call_args_list
        if c[1].get("command") == "report_status"
    ]


def _recovers(app) -> None:
    app._check_entity_state = AsyncMock(
        return_value={"name": ENTITY_CHECK, "status": "ok", "detail": "ready"}
    )


def _never_recovers(app) -> None:
    app._check_entity_state = AsyncMock(
        return_value={
            "name": ENTITY_CHECK,
            "status": "critical",
            "detail": "Expected 'ready', got 'driver_failed'",
        }
    )


def _ago(**kwargs) -> datetime.datetime:
    return datetime.datetime.now() - datetime.timedelta(**kwargs)


def _controller_state(repair_attempts: Optional[list], checker_id: str = "zwave"):
    """Shape of sensor.health_check_status as published by the controller."""
    repair_state: Dict[str, Any] = {"status": REPAIR_IDLE}
    if repair_attempts is not None:
        repair_state["repair_attempts"] = repair_attempts
    return {
        "state": "ok",
        "attributes": {"checkers": {checker_id: {"repair_state": repair_state}}},
    }


# ---------------------------------------------------------------------------
# 1. Dwell
# ---------------------------------------------------------------------------


class TestDwell:
    def test_dwell_not_elapsed_goes_pending_without_pressing(self):
        """Everything else allows a restart, but the integration has only
        just gone unhealthy — nothing may be pressed yet."""
        app = _make_app()
        _init_only(app)
        _recovers(app)

        _evaluate(app, _results())

        assert app._repair_status == REPAIR_PENDING
        assert app._auto_repair_deadline is not None
        assert app._auto_repair_deadline > datetime.datetime.now()
        assert _presses(app) == []
        assert app.captured_tasks == []
        assert app._repair_attempts == []

    def test_dwell_elapsed_presses_the_button_once(self):
        app = _make_app()
        _init_only(app)
        _recovers(app)
        app._unhealthy_since = _ago(minutes=10)  # dwell is 5 min

        _evaluate(app, _results())

        presses = _presses(app)
        assert len(presses) == 1
        assert presses[0][1]["entity_id"] == REPAIR_BUTTON
        assert len(app._repair_attempts) == 1


# ---------------------------------------------------------------------------
# 2. Ping-down no-op
# ---------------------------------------------------------------------------


class TestRadioPingGuard:
    def test_ping_critical_never_presses(self):
        """The board is genuinely offline — a software restart cannot help,
        so we do nothing and let normal alerting page."""
        app = _make_app()
        _init_only(app)
        app._unhealthy_since = _ago(minutes=60)

        _evaluate(app, _results(entity="critical", ping="critical"))

        assert _presses(app) == []
        assert app.captured_tasks == []
        assert app._repair_status != REPAIR_PENDING
        assert app._auto_repair_deadline is None
        assert "software restart cannot help" in app._repair_detail
        assert app._repair_attempts == []


# ---------------------------------------------------------------------------
# 3. Serial-connected guard
# ---------------------------------------------------------------------------


class TestSerialGuard:
    def test_serial_disconnected_never_presses(self):
        """Without the stale-client fingerprint (ESPHome still believes it
        has a serial client) the restart is not the right remedy."""
        app = _make_app(states={SERIAL_ENTITY: "off"})
        _init_only(app)
        app._unhealthy_since = _ago(minutes=60)

        _evaluate(app, _results())

        assert _presses(app) == []
        assert app.captured_tasks == []
        assert app._repair_status == REPAIR_IDLE
        assert "stale-client signature" in app._repair_detail
        assert SERIAL_ENTITY in app._repair_detail
        assert app._repair_attempts == []


# ---------------------------------------------------------------------------
# 4. Minimum interval
# ---------------------------------------------------------------------------


class TestMinimumInterval:
    def test_recent_attempt_blocks_restart(self):
        """The dwell is long satisfied, so the only thing that can hold this
        restart back is the 15 minute minimum interval — and the deadline it
        reports must be the interval's, not the dwell's."""
        app = _make_app()
        _init_only(app)
        _recovers(app)
        app._unhealthy_since = _ago(minutes=60)
        attempt = _ago(minutes=5)
        app._repair_attempts = [attempt]  # interval is 15 min

        _evaluate(app, _results())

        assert _presses(app) == []
        assert app.captured_tasks == []
        assert app._cap_reached is False
        assert app._repair_status == REPAIR_PENDING
        assert app._auto_repair_deadline == attempt + datetime.timedelta(
            seconds=DEFAULT_ARGS["repair_min_interval_s"]
        )
        assert app._auto_repair_deadline > datetime.datetime.now()
        assert len(app._repair_attempts) == 1

    def test_restart_allowed_once_interval_elapsed(self):
        app = _make_app()
        _init_only(app)
        _recovers(app)
        app._unhealthy_since = _ago(minutes=60)
        app._repair_attempts = [_ago(minutes=20)]

        _evaluate(app, _results())

        assert len(_presses(app)) == 1
        assert len(app._repair_attempts) == 2


# ---------------------------------------------------------------------------
# 5. Rolling 24 h cap
# ---------------------------------------------------------------------------


class TestTwentyFourHourCap:
    def test_cap_reached_blocks_and_marks_failed(self):
        app = _make_app()
        _init_only(app)
        _recovers(app)
        app._unhealthy_since = _ago(minutes=60)
        app._repair_attempts = [_ago(hours=3), _ago(hours=2), _ago(hours=1)]

        _evaluate(app, _results())

        assert _presses(app) == []
        assert app.captured_tasks == []
        assert app._cap_reached is True
        assert app._repair_status == REPAIR_FAILED
        assert "Cap reached" in app._repair_detail
        assert "3/3" in app._repair_detail
        assert len(app._repair_attempts) == 3

    def test_attempt_older_than_the_window_is_pruned(self):
        """2 recent attempts + 1 from 25 h ago is not a spent cap."""
        app = _make_app()
        _init_only(app)
        _recovers(app)
        app._unhealthy_since = _ago(minutes=60)
        stale = _ago(seconds=ATTEMPT_WINDOW_S + 3600)
        app._repair_attempts = [stale, _ago(minutes=60), _ago(minutes=30)]

        _evaluate(app, _results())

        assert len(_presses(app)) == 1
        assert app._cap_reached is False
        # 2 survivors + the new attempt; the 25 h-old entry is gone.
        assert len(app._repair_attempts) == 3
        assert stale not in app._repair_attempts


# ---------------------------------------------------------------------------
# 6. Cap escalation through a full check cycle
# ---------------------------------------------------------------------------


class TestCapEscalation:
    def test_spent_cap_forces_integration_status_back_to_critical(self):
        """apply_cross_check would mask the failure as a warning (ping and
        web UI still pass) — which is exactly why the outage never paged."""
        app = _make_app()
        _init_only(app)
        app._repair_attempts = [_ago(hours=3), _ago(hours=2), _ago(hours=1)]
        results = _results(entity="critical", ping="ok", web="ok")
        app._run_checks_only = AsyncMock(return_value=results)

        _drive(app, app._run_checks())

        entity = [r for r in results if r["name"] == ENTITY_CHECK][0]
        assert entity["status"] == "critical"
        assert "cap reached" in entity["detail"]
        reported = [
            r for r in _report_payloads(app)[-1]["results"]
            if r["name"] == ENTITY_CHECK
        ][0]
        assert reported["status"] == "critical"
        assert _presses(app) == []

    def test_without_a_spent_cap_the_same_failure_is_a_warning(self):
        app = _make_app()
        _init_only(app)
        results = _results(entity="critical", ping="ok", web="ok")
        app._run_checks_only = AsyncMock(return_value=results)

        _drive(app, app._run_checks())

        entity = [r for r in results if r["name"] == ENTITY_CHECK][0]
        assert entity["status"] == "warning"
        assert "partial failure" in entity["detail"]
        assert app._cap_reached is False
        assert app._repair_status == REPAIR_PENDING  # dwell started, no action
        assert _presses(app) == []


# ---------------------------------------------------------------------------
# 7. Exactly one action per evaluation
# ---------------------------------------------------------------------------


class TestOneActionPerEvaluation:
    def test_single_evaluation_presses_at_most_once(self):
        app = _make_app()
        _init_only(app)
        _recovers(app)
        app._unhealthy_since = _ago(minutes=60)

        _evaluate(app, _results())

        assert len(_presses(app)) == 1
        assert app.create_task.call_count == 1
        assert len(app._repair_attempts) == 1

    def test_no_second_press_while_a_repair_is_in_progress(self):
        app = _make_app()
        _init_only(app)
        _recovers(app)
        app._unhealthy_since = _ago(minutes=60)
        app._repair_status = REPAIR_IN_PROGRESS

        _evaluate(app, _results())

        assert _presses(app) == []
        assert app.captured_tasks == []
        assert app._repair_status == REPAIR_IN_PROGRESS
        assert app._repair_attempts == []


# ---------------------------------------------------------------------------
# 8. Post-action quiet period
# ---------------------------------------------------------------------------


class TestQuietPeriod:
    def test_quiet_period_blocks_the_next_evaluation(self):
        # Minimum interval removed so the quiet period is the ONLY gate that
        # can be responsible for the second evaluation not acting.
        app = _make_app({"repair_min_interval_s": 0})
        _init_only(app)
        _recovers(app)
        app._unhealthy_since = _ago(minutes=60)

        _evaluate(app, _results())
        assert len(_presses(app)) == 1
        assert app._quiet_until is not None
        assert app._quiet_until > datetime.datetime.now()

        # Everything would allow another restart — except the quiet period.
        app._unhealthy_since = _ago(minutes=60)
        _evaluate(app, _results())

        assert len(_presses(app)) == 1
        assert len(app._repair_attempts) == 1
        assert "Quiet period" in app._repair_detail


# ---------------------------------------------------------------------------
# 9./10. Persistence across an AppDaemon restart
# ---------------------------------------------------------------------------


class TestPersistenceAcrossRestart:
    def test_seeding_rebuilds_attempts_and_prunes_old_ones(self):
        app = _make_app()
        recent = [_ago(hours=2), _ago(hours=1)]
        app.controller_state = _controller_state(
            [_ago(seconds=ATTEMPT_WINDOW_S + 3600).isoformat(timespec="seconds")]
            + [a.isoformat(timespec="seconds") for a in recent]
        )
        _init_only(app)

        _run(app._seed_attempts_from_controller())

        assert len(app._repair_attempts) == 2
        assert app._last_repair_attempt == recent[-1].isoformat(timespec="seconds")

    def test_seeded_cap_survives_the_restart(self):
        """A deploy landing mid-outage must resume the ladder, not reset it."""
        app = _make_app()
        app.controller_state = _controller_state([
            _ago(hours=3).isoformat(timespec="seconds"),
            _ago(hours=2).isoformat(timespec="seconds"),
            _ago(hours=1).isoformat(timespec="seconds"),
        ])
        _init_only(app)
        _run(app._seed_attempts_from_controller())
        app._unhealthy_since = _ago(minutes=60)

        _evaluate(app, _results())

        assert _presses(app) == []
        assert app._cap_reached is True
        assert app._repair_status == REPAIR_FAILED
        assert "Cap reached" in app._repair_detail

    def test_repair_state_round_trips_through_the_published_sensor(self):
        app = _make_app()
        _init_only(app)
        app._repair_attempts = [_ago(hours=2), _ago(minutes=30)]

        published = app._build_repair_state()["repair_attempts"]
        assert published == [
            a.isoformat(timespec="seconds") for a in app._repair_attempts
        ]
        assert all(isinstance(s, str) for s in published)

        revived = _make_app()
        revived.controller_state = _controller_state(published)
        _init_only(revived)
        _run(revived._seed_attempts_from_controller())

        assert [
            a.isoformat(timespec="seconds") for a in revived._repair_attempts
        ] == published

    @pytest.mark.parametrize(
        "controller_state",
        [
            # AppDaemon 4.5.13 strips falsy attribute values, so an empty
            # attempt list comes back absent rather than as [].
            _controller_state(None),
            {"state": "ok", "attributes": {"checkers": {"zwave": {}}}},
            {"state": "ok", "attributes": {"checkers": {}}},
            {"state": "ok", "attributes": {}},
            None,
        ],
    )
    def test_absent_repair_attempts_is_handled(self, controller_state):
        app = _make_app()
        app.controller_state = controller_state
        _init_only(app)

        _run(app._seed_attempts_from_controller())

        assert app._repair_attempts == []
        assert app._last_repair_attempt is None
        # No swallowed exception behind the scenes.
        assert not [
            c for c in app.log.call_args_list if "Failed to seed" in str(c)
        ]

    def test_seeding_does_not_seed_the_dwell_clock(self):
        """The full dwell is always re-served after a restart, so a deploy
        mid-outage can never restart the board immediately."""
        app = _make_app()
        app.controller_state = _controller_state(
            [_ago(minutes=20).isoformat(timespec="seconds")]
        )
        _init_only(app)
        _run(app._seed_attempts_from_controller())

        assert app._unhealthy_since is None

        # The one seeded attempt is outside the 15 min interval and well
        # under the cap, so only the dwell can hold this restart back.
        _evaluate(app, _results())

        assert _presses(app) == []
        assert app._repair_status == REPAIR_PENDING
        assert app._unhealthy_since is not None


# ---------------------------------------------------------------------------
# 11./12./13. Repair execution outcomes
# ---------------------------------------------------------------------------


class TestRepairOutcome:
    def test_successful_repair_clears_the_ladder_but_keeps_the_attempt_log(self):
        app = _make_app()
        _init_only(app)
        _recovers(app)
        app._repair_status = REPAIR_IN_PROGRESS
        app._unhealthy_since = _ago(minutes=30)
        app._cap_reached = True

        _drive(app, app._execute_repair())

        assert app._repair_status == REPAIR_SUCCESS
        assert app._unhealthy_since is None
        assert app._cap_reached is False
        assert len(app._repair_attempts) == 1
        # The success edge event is queued on _pending_repair_events and
        # drained onto the report that carries the conclusion.
        final = _report_payloads(app)[-1]
        assert final["repair_events"] == [
            {"result": "success", "duration_s": REPAIR_POLL_INTERVAL_S}
        ]
        assert app._pending_repair_events == []

        # A later healthy cycle clears the status back to idle — but NOT the
        # attempt log: the 24 h cap counts restarts, not outages.
        _evaluate(app, _results(entity="ok"))
        assert app._repair_status == REPAIR_IDLE
        assert app._repair_detail == ""
        assert len(app._repair_attempts) == 1

    def test_failed_repair_still_counts_and_starts_the_quiet_period(self):
        app = _make_app({"repair_recovery_wait_s": 30})
        _init_only(app)
        _never_recovers(app)
        app._repair_status = REPAIR_IN_PROGRESS

        _drive(app, app._execute_repair())

        assert app._repair_status == REPAIR_FAILED
        # A failed attempt must never be a way around the cap.
        assert len(app._repair_attempts) == 1
        assert app._quiet_until is not None
        assert app._quiet_until > datetime.datetime.now()
        final = _report_payloads(app)[-1]
        assert final["repair_events"] == [{"result": "failed", "duration_s": 30}]

    def test_attempt_is_recorded_before_the_press(self):
        """A crash in the press path must not buy an extra restart."""
        app = _make_app()
        _init_only(app)
        app._repair_status = REPAIR_IN_PROGRESS
        app.call_service = MagicMock(side_effect=Exception("service unavailable"))

        _drive(app, app._execute_repair())

        assert len(app._repair_attempts) == 1
        assert app._repair_status == REPAIR_FAILED
        assert "Repair error" in app._repair_detail
        assert _report_payloads(app)[-1]["repair_events"] == [{"result": "failed"}]


# ---------------------------------------------------------------------------
# 14. Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_register_advertises_repair_support(self):
        app = _make_app()
        _startup(app)

        register_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "register_checker"
        ]
        assert len(register_calls) == 1
        assert register_calls[0][0][0] == "health_check_command"
        payload = json.loads(register_calls[0][1]["payload"])
        assert payload["checker_id"] == "zwave"
        assert payload["supports_repair"] is True
        assert isinstance(payload["repair_state"], dict)
        assert payload["repair_state"]["repair_max_per_24h"] == 3
        assert payload["repair_state"]["status"] == REPAIR_IDLE
        assert payload["check_names"] == [ENTITY_CHECK, PING_CHECK, WEB_CHECK]


# ---------------------------------------------------------------------------
# 15. Manual repair
# ---------------------------------------------------------------------------


class TestManualRepair:
    def test_manual_repair_is_refused_when_the_cap_is_spent(self):
        """Manual repair skips the dwell but NOT the hardware caps."""
        app = _make_app()
        _init_only(app)
        app._repair_attempts = [_ago(hours=3), _ago(hours=2), _ago(hours=1)]

        app._on_repair_command(
            "health_check_repair_zwave", {"action": "start_repair"}, {}
        )

        assert _presses(app) == []
        assert app.captured_tasks == []
        assert app._repair_status == REPAIR_FAILED
        assert "Cap reached" in app._repair_detail
        assert len(app._repair_attempts) == 3

    def test_manual_repair_runs_when_within_the_caps(self):
        app = _make_app()
        _init_only(app)
        _recovers(app)

        app._on_repair_command(
            "health_check_repair_zwave", {"action": "start_repair"}, {}
        )
        assert app._repair_status == REPAIR_IN_PROGRESS
        assert len(app.captured_tasks) == 1

        _drain(app)

        assert len(_presses(app)) == 1
        assert app._repair_status == REPAIR_SUCCESS


# ---------------------------------------------------------------------------
# 16. Cancelling a scheduled repair
# ---------------------------------------------------------------------------


class TestCancelRepair:
    def test_cancel_defers_by_restarting_the_dwell(self):
        """Cancel must be a real deferral, not a one-tick dismissal.

        This checker re-arms every cycle (unlike the shade gateway, which
        repairs once per episode), so dropping straight back to ``idle``
        would let the countdown fire again on the very next tick.
        """
        app = _make_app()
        _init_only(app)
        app._repair_status = REPAIR_PENDING
        app._unhealthy_since = _ago(minutes=60)

        app._on_repair_command(
            "health_check_repair_zwave", {"action": "cancel_repair"}, {}
        )

        assert app._repair_status == REPAIR_IDLE
        assert app._auto_repair_deadline is None
        # The dwell clock was restarted, so the next evaluation waits again.
        assert app._unhealthy_since > _ago(minutes=1)

        _run(app._evaluate_auto_repair(_results()))
        assert _presses(app) == []
        assert app._repair_status == REPAIR_PENDING

    def test_cancel_does_not_refund_rate_limit_budget(self):
        app = _make_app()
        _init_only(app)
        app._repair_attempts = [_ago(hours=1)]
        app._repair_status = REPAIR_PENDING

        app._on_repair_command(
            "health_check_repair_zwave", {"action": "cancel_repair"}, {}
        )

        assert len(app._repair_attempts) == 1

    def test_cancel_is_ignored_when_nothing_is_scheduled(self):
        app = _make_app()
        _init_only(app)
        app._repair_status = REPAIR_IN_PROGRESS

        app._on_repair_command(
            "health_check_repair_zwave", {"action": "cancel_repair"}, {}
        )

        assert app._repair_status == REPAIR_IN_PROGRESS


# ---------------------------------------------------------------------------
# 17. Escalation when no automatic fix can help
# ---------------------------------------------------------------------------


class TestOfflineRadioEscalation:
    def test_unreachable_radio_reports_critical_not_warning(self):
        """A board that is off the network must page, not warn.

        zwave-js-ui's own web UI answers happily while the radio is gone, so
        two of three checks pass and ``apply_cross_check`` masks a total
        outage as a partial failure — the same silence as 2026-09-09, on the
        other branch. Nothing automatic can fix an offline board, so the
        downgrade is reversed.
        """
        app = _make_app()
        _init_only(app)
        results = _results(entity="critical", ping="critical", web="ok")
        app._run_checks_only = AsyncMock(return_value=results)

        _drive(app, app._run_checks())

        entity = [r for r in results if r["name"] == ENTITY_CHECK][0]
        assert entity["status"] == "critical"
        assert "radio unreachable" in entity["detail"]
        # Still no restart — a software restart cannot reach an offline board.
        assert _presses(app) == []

    def test_reachable_radio_still_downgrades_to_warning(self):
        """The stale-client case keeps the normal downgrade until the cap."""
        app = _make_app()
        _init_only(app)
        results = _results(entity="critical", ping="ok", web="ok")
        app._run_checks_only = AsyncMock(return_value=results)

        _drive(app, app._run_checks())

        entity = [r for r in results if r["name"] == ENTITY_CHECK][0]
        assert entity["status"] == "warning"

    def test_escalation_clears_once_the_radio_is_back(self):
        app = _make_app()
        _init_only(app)
        _run(app._evaluate_auto_repair(
            _results(entity="critical", ping="critical")
        ))
        assert app._escalate_detail

        _run(app._evaluate_auto_repair(_results(entity="ok", ping="ok")))
        assert app._escalate_detail == ""


class TestSerialStateCoercion:
    def test_yaml_bool_on_is_coerced_back_to_the_string(self):
        """`repair_serial_connected_state: on` must not silently disable repair.

        PyYAML turns a bare `on` into True; without coercion `str(True)`
        never matches a binary_sensor state and every restart is refused
        forever — a safety guard that fails closed and silent.
        """
        app = _make_app({"repair_serial_connected_state": True})
        _init_only(app)
        assert app._repair_serial_state == "on"

    def test_yaml_bool_off_is_coerced_back_to_the_string(self):
        app = _make_app({"repair_serial_connected_state": False})
        _init_only(app)
        assert app._repair_serial_state == "off"
