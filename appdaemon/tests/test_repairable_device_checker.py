"""Unit tests for RepairableDeviceChecker."""

from __future__ import annotations

import asyncio
import datetime
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

from health_checks.checker_apps.device_checker.repairable_device_checker import (
    RepairableDeviceChecker,
    REPAIR_FAILED,
    REPAIR_IDLE,
    REPAIR_IN_PROGRESS,
    REPAIR_PENDING,
    REPAIR_SUCCESS,
)


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

    @staticmethod
    def _turn_on_calls(app):
        return [
            c for c in app.call_service.call_args_list
            if c.args[:1] == ("input_boolean/turn_on",)
            and c.kwargs.get("entity_id") == "input_boolean.printer_health_auto_repair"
        ]

    def test_provisioning_enables_auto_repair_default_when_created(self):
        """A freshly created input_boolean is off — seed it, or the default lies.

        _refresh_auto_repair_config honours auto_repair_enabled_default for the
        whole first run (the helper is invisible to AppDaemon until the next
        restart). Without this seed the helper would still read "off" once it
        became visible, so auto-repair would silently switch itself off one
        deploy after it was configured on.
        """
        app = _make_app({"auto_repair_enabled_default": True})
        mock_prov = _make_mock_provisioner()
        mock_prov.ensure_helper = AsyncMock(return_value=True)  # created

        _startup(app, mock_prov)

        assert len(self._turn_on_calls(app)) == 1

    def test_provisioning_does_not_enable_when_helper_already_exists(self):
        """ensure_helper returns False — never clobber a later manual "off"."""
        app = _make_app({"auto_repair_enabled_default": True})
        mock_prov = _make_mock_provisioner()  # returns False

        _startup(app, mock_prov)

        assert self._turn_on_calls(app) == []

    def test_provisioning_does_not_enable_when_default_disabled(self):
        app = _make_app({"auto_repair_enabled_default": False})
        mock_prov = _make_mock_provisioner()
        mock_prov.ensure_helper = AsyncMock(return_value=True)  # created

        _startup(app, mock_prov)

        assert self._turn_on_calls(app) == []

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
        app.create_task = MagicMock()

        app._start_repair()
        assert app._repair_status == REPAIR_IN_PROGRESS

    def test_no_repair_switch_fails(self):
        app = _make_app({"repair_switch": ""})
        _init_only(app)

        app._start_repair()
        assert app._repair_status == REPAIR_FAILED
        assert "No repair switch" in app._repair_detail


class TestRepairExecution:
    def test_successful_repair(self):
        app = _make_app({"repair_recovery_wait_s": 10, "repair_off_duration_s": 0})
        _init_only(app)
        app._repair_status = REPAIR_IN_PROGRESS

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
        app.create_task = MagicMock()

        app._on_repair_command(
            "health_check_repair_printer",
            {"action": "start_repair"},
            {},
        )
        assert app._repair_status == REPAIR_IN_PROGRESS

    def test_update_config_command(self):
        app = _make_app()
        _init_only(app)

        app._on_repair_command(
            "health_check_repair_printer",
            {"action": "update_repair_config", "auto_repair_enabled": True},
            {},
        )
        calls = [c[0][0] for c in app.call_service.call_args_list]
        assert "input_boolean/turn_on" in calls


# ---------------------------------------------------------------------------
# Helper-readability harness
# ---------------------------------------------------------------------------
#
# AppDaemon loads its entity list at startup, so an input_boolean/input_number
# this app provisions in `_provision_repair_helpers` is unknown to `get_state`
# for the whole rest of the process — every read returns None. The old idiom
# `str(enabled_state) == "on"` folded that None into a deliberate "off" and
# shipped the Z-Wave auto-repair inert in v1.17.0. The classes below lock that
# behaviour down for the printer's power-cycle repair.

MODULE = "health_checks.checker_apps.device_checker.repairable_device_checker"

TOGGLE = f"input_boolean.{DEFAULT_ARGS['checker_id']}_health_auto_repair"
DELAY = f"input_number.{DEFAULT_ARGS['checker_id']}_health_auto_repair_delay"
REPAIR_SWITCH = DEFAULT_ARGS["repair_switch"]

#: Every read is unreadable unless a test says otherwise — the first-run state
#: these tests exist for.
UNREADABLE = (None, "unavailable", "unknown", "none", "")


def _helper_app(
    extra_args: dict | None = None, states: dict | None = None
) -> RepairableDeviceChecker:
    """`_make_app` plus a state-aware async `get_state` and task capture.

    `_make_app`'s default `get_state` is a plain MagicMock, which the async
    `_refresh_auto_repair_config` cannot await — every read would land in its
    `except` branch and prove nothing about readability. Backing it with a
    dict a test can rewrite mid-run is the whole point here: the helper going
    from unknown to known is the transition under test.

    `create_task` captures the repair coroutine instead of scheduling it, so a
    test decides when (and whether) the switch is really power-cycled.
    """
    app = _make_app(extra_args)
    app.entity_states = dict(states or {})
    app.get_state = AsyncMock(
        side_effect=lambda entity_id=None, **kw: app.entity_states.get(entity_id)
    )
    app.captured_tasks = []
    app.create_task = MagicMock(side_effect=app.captured_tasks.append)
    return app


def _drain(app) -> None:
    """Run any repair coroutine handed to `create_task`.

    `asyncio.sleep` is patched out so the off-duration and the recovery poll
    loop run instantly instead of taking `repair_recovery_wait_s` seconds.
    """

    async def _inner():
        while app.captured_tasks:
            await app.captured_tasks.pop(0)

    with patch(f"{MODULE}.asyncio.sleep", new=AsyncMock(return_value=None)):
        _run(_inner())


def _evaluate(app, results) -> None:
    """Evaluate auto-repair and let any repair it started actually run."""
    app._evaluate_auto_repair(results)
    _drain(app)


def _bad_results() -> list[dict]:
    return [
        {"name": "Ping", "status": "critical", "detail": "timeout"},
        {"name": "Status", "status": "critical", "detail": "unavailable"},
    ]


def _never_recovers(app) -> None:
    """The repair's recovery poll never passes — it only ever times out.

    Keeps the end-to-end tests about *whether the switch was cycled*, not
    about what happened afterwards.
    """
    app._run_checks_only = AsyncMock(return_value=_bad_results())


def _turn_offs(app) -> list:
    return [
        c for c in app.call_service.call_args_list
        if c[0] and c[0][0] == "switch/turn_off"
    ]


def _set_values(app) -> list:
    return [
        c for c in app.call_service.call_args_list
        if c[0] and c[0][0] == "input_number/set_value"
    ]


def _warnings(app, *needles) -> list:
    return [
        c for c in app.log.call_args_list
        if c[1].get("level") == "WARNING" and all(n in str(c) for n in needles)
    ]


def _clamp_warnings(app) -> list:
    return _warnings(app, "outside the permitted")


def _ago(**kwargs) -> datetime.datetime:
    return datetime.datetime.now() - datetime.timedelta(**kwargs)


def _cmd(app, **payload) -> None:
    app._on_repair_command(
        f"health_check_repair_{DEFAULT_ARGS['checker_id']}",
        {"action": "update_repair_config", **payload},
        {},
    )


class TestUnreadableToggleKeepsTheDefault:
    """A helper AppDaemon cannot see must not silently disable auto-repair.

    AppDaemon loads its entity list at startup, so for the whole first run
    after `_provision_repair_helpers` creates them, `get_state` returns None.
    Treating that as a real read (`str(None) == "on"` → False) shipped 1.17.0
    inert: the toggle was `on` in HA, the checker had it cached as disabled,
    and nothing in the logs said so.
    """

    @pytest.mark.parametrize("raw", UNREADABLE)
    def test_unreadable_toggle_keeps_the_cached_value(self, raw):
        app = _helper_app(
            {"auto_repair_enabled_default": True}, states={TOGGLE: raw}
        )
        app.initialize()

        _run(app._refresh_auto_repair_config())

        assert app._cached_auto_repair_enabled is True

    def test_a_real_off_still_disables(self):
        """"Unknown" is not evidence; an actual "off" is."""
        app = _helper_app(
            {"auto_repair_enabled_default": True}, states={TOGGLE: "off"}
        )
        app.initialize()

        _run(app._refresh_auto_repair_config())

        assert app._cached_auto_repair_enabled is False

    def test_unreadable_toggle_still_permits_repair(self):
        """The end-to-end consequence: the printer really is power-cycled."""
        app = _helper_app(
            {"auto_repair_enabled_default": True}, states={TOGGLE: None}
        )
        app.initialize()
        _run(app._refresh_auto_repair_config())
        _never_recovers(app)
        app._unhealthy_since = _ago(minutes=30)

        _evaluate(app, _bad_results())

        assert len(_turn_offs(app)) == 1
        assert _turn_offs(app)[0][1]["entity_id"] == REPAIR_SWITCH


class TestRepairConfigCommandUpdatesTheCache:
    """An explicit user choice must take effect even while the helper is unreadable.

    `_update_repair_config` writes the helper and used to rely on the next
    `get_state` to pick the value up — but during the unreadable window that
    read stays None for the rest of the run. An operator turning auto-repair
    off from the card would see it off in the card and in HA, and the printer
    would still get power-cycled.
    """

    def test_turning_off_takes_effect_immediately(self):
        app = _helper_app(
            {"auto_repair_enabled_default": True}, states={TOGGLE: None}
        )
        app.initialize()
        _run(app._refresh_auto_repair_config())
        assert app._cached_auto_repair_enabled is True

        _cmd(app, auto_repair_enabled=False)

        assert app._cached_auto_repair_enabled is False
        # ...and the power cycle really does not happen.
        _never_recovers(app)
        app._unhealthy_since = _ago(minutes=30)
        _evaluate(app, _bad_results())
        assert _turn_offs(app) == []

    def test_turning_on_takes_effect_immediately(self):
        app = _helper_app(
            {"auto_repair_enabled_default": False}, states={TOGGLE: None}
        )
        app.initialize()
        _run(app._refresh_auto_repair_config())

        _cmd(app, auto_repair_enabled=True)

        assert app._cached_auto_repair_enabled is True

    def test_delay_change_takes_effect_immediately(self):
        app = _helper_app(states={DELAY: None})
        app.initialize()
        _run(app._refresh_auto_repair_config())

        _cmd(app, auto_repair_delay_min=17)

        assert app._cached_auto_repair_delay_min == 17


class TestUnreadableToggleLogging:
    def test_unreadable_warns_once_then_recovers_at_info(self):
        """The window needs a visible open and close, not a message per cycle."""
        app = _helper_app(states={TOGGLE: None, DELAY: "5"})
        app.initialize()

        _run(app._refresh_auto_repair_config())
        _run(app._refresh_auto_repair_config())

        assert len(_warnings(app, TOGGLE, "not readable")) == 1, (
            "should warn on the transition, not every cycle"
        )

        app.entity_states[TOGGLE] = "on"
        _run(app._refresh_auto_repair_config())

        infos = [
            c for c in app.log.call_args_list
            if c[1].get("level") == "INFO" and "readable again" in str(c)
        ]
        assert len(infos) == 1

    def test_clean_start_does_not_announce_a_window_that_never_opened(self):
        """`is readable again` is the phrase for the *close* of a window."""
        app = _helper_app(states={TOGGLE: "on", DELAY: "5"})
        app.initialize()

        _run(app._refresh_auto_repair_config())

        assert not [
            c for c in app.log.call_args_list if "readable again" in str(c)
        ]
        assert not _warnings(app, "not readable")
        assert app._toggle_readable is True
        assert app._delay_readable is True


class TestRepairConfigWriteFailures:
    def test_a_failed_toggle_write_does_not_update_the_cache(self):
        """Caching a write that failed asserts the opposite of HA."""
        app = _helper_app(
            {"auto_repair_enabled_default": True}, states={TOGGLE: "on"}
        )
        app.initialize()
        _run(app._refresh_auto_repair_config())
        app.call_service = MagicMock(side_effect=RuntimeError("boom"))

        _cmd(app, auto_repair_enabled=False)

        assert app._cached_auto_repair_enabled is True

    def test_a_failed_delay_write_does_not_update_the_cache(self):
        app = _helper_app(states={DELAY: "5"})
        app.initialize()
        _run(app._refresh_auto_repair_config())
        app.call_service = MagicMock(side_effect=RuntimeError("boom"))

        _cmd(app, auto_repair_delay_min=42)

        assert app._cached_auto_repair_delay_min == 5

    @pytest.mark.parametrize("value", ["17.0", 17.0, 17])
    def test_float_shaped_delays_from_the_card_are_accepted(self, value):
        """The helper read uses int(float(...)); the card path must match."""
        app = _helper_app(states={DELAY: None})
        app.initialize()
        _run(app._refresh_auto_repair_config())

        _cmd(app, auto_repair_delay_min=value)

        assert app._cached_auto_repair_delay_min == 17

    def test_an_unparseable_delay_is_ignored_not_fatal(self):
        app = _helper_app()
        app.initialize()
        _run(app._refresh_auto_repair_config())
        before = app._cached_auto_repair_delay_min

        _cmd(app, auto_repair_delay_min="soon")

        assert app._cached_auto_repair_delay_min == before
        assert _warnings(app, "unparseable auto_repair_delay_min")
        assert _set_values(app) == []

    def test_unreadable_delay_helper_warns_on_the_transition(self):
        """logging-standards puts "config key missing (using default)" at WARNING."""
        app = _helper_app(states={DELAY: None, TOGGLE: "on"})
        app.initialize()

        _run(app._refresh_auto_repair_config())
        _run(app._refresh_auto_repair_config())

        assert len(_warnings(app, "auto_repair_delay", "not readable")) == 1

    def test_unparseable_delay_does_not_skip_the_rest_of_the_command(self):
        """A bare return here would silently skip anything appended later."""
        app = _helper_app(states={TOGGLE: "on", DELAY: "5"})
        app.initialize()
        _run(app._refresh_auto_repair_config())

        _cmd(app, auto_repair_enabled=False, auto_repair_delay_min="soon")

        # The toggle half of the same command still applied.
        assert app._cached_auto_repair_enabled is False
        # ...and the bad delay changed nothing.
        assert app._cached_auto_repair_delay_min == 5
        assert _set_values(app) == []


class TestDelayIsClamped:
    """A delay of 0 collapses the dwell gate, power-cycling on the first bad cycle.

    Reachable through the card, which does not guard a *typed* 0. HA rejects
    `set_value(0)` without AppDaemon raising, so an unclamped cache would hold
    a value the helper never accepted — and during the unreadable window
    nothing can correct it.
    """

    @pytest.mark.parametrize(
        "sent,expected", [(0, 1), (-5, 1), (900, 60), (17, 17)]
    )
    def test_command_delays_are_clamped(self, sent, expected):
        app = _helper_app(states={DELAY: None})
        app.initialize()
        _run(app._refresh_auto_repair_config())

        _cmd(app, auto_repair_delay_min=sent)

        assert app._cached_auto_repair_delay_min == expected

    def test_a_zero_delay_cannot_collapse_the_dwell(self):
        """The end-to-end consequence: no power cycle on the first bad cycle."""
        app = _helper_app(
            {"auto_repair_enabled_default": True},
            states={TOGGLE: None, DELAY: None},
        )
        app.initialize()
        _run(app._refresh_auto_repair_config())
        _never_recovers(app)

        _cmd(app, auto_repair_delay_min=0)
        _evaluate(app, _bad_results())

        assert _turn_offs(app) == []
        assert app._repair_status == REPAIR_PENDING

    def test_an_out_of_range_helper_value_is_clamped(self):
        app = _helper_app(states={DELAY: "0"})
        app.initialize()

        _run(app._refresh_auto_repair_config())

        assert app._cached_auto_repair_delay_min == 1

    @pytest.mark.parametrize("configured,expected", [(0, 1), (-1, 1), (999, 60)])
    def test_the_configured_default_is_clamped_too(self, configured, expected):
        """The config seed is the other door to the same dwell collapse."""
        app = _helper_app({"auto_repair_delay_min_default": configured})
        app.initialize()

        assert app._auto_repair_delay_min_default == expected
        assert app._cached_auto_repair_delay_min == expected

    def test_a_zero_configured_default_cannot_collapse_the_dwell(self):
        app = _helper_app(
            {
                "auto_repair_delay_min_default": 0,
                "auto_repair_enabled_default": True,
            },
            states={TOGGLE: None, DELAY: None},
        )
        app.initialize()
        _run(app._refresh_auto_repair_config())
        _never_recovers(app)

        _evaluate(app, _bad_results())

        assert _turn_offs(app) == []
        assert app._repair_status == REPAIR_PENDING

    def test_clamping_is_logged(self):
        """Overriding what an operator asked for must not be silent."""
        app = _helper_app({"auto_repair_delay_min_default": 0})
        app.initialize()

        assert _clamp_warnings(app)

    def test_in_range_values_are_not_logged(self):
        app = _helper_app({"auto_repair_delay_min_default": 5})
        app.initialize()

        assert not _clamp_warnings(app)

    def test_the_clamp_warning_is_once_per_episode_not_once_per_cycle(self):
        """The helper read runs every check_interval_s — the warning must not.

        An out-of-range value sitting in the helper would otherwise emit a
        WARNING every cycle for as long as it sat there. `_delay_clamped_logged`
        latches the warning and clears again on the first in-range read, so a
        *new* out-of-range episode is still announced.
        """
        app = _helper_app(states={DELAY: "0"})
        app.initialize()

        for _ in range(3):
            _run(app._refresh_auto_repair_config())

        assert len(_clamp_warnings(app)) == 1

        # An in-range read closes the episode...
        app.entity_states[DELAY] = "5"
        _run(app._refresh_auto_repair_config())
        assert len(_clamp_warnings(app)) == 1

        # ...so the next out-of-range value gets its own warning.
        app.entity_states[DELAY] = "900"
        _run(app._refresh_auto_repair_config())
        assert len(_clamp_warnings(app)) == 2
