"""One contract suite for ``shared/auto_repair_config.py``, run against all seven checkers.

The auto-repair toggle/delay machinery used to be copy-pasted into every
repairable checker, and so were its tests — thirty-five near-identical classes
across seven files.  The code now lives in :class:`AutoRepairConfigMixin`; this
module is the single place its behaviour is pinned, parametrised over the seven
hosts so a divergence in any one of them fails here.

Each checker contributes an adapter (:class:`Spec`) that knows only what is
genuinely per-checker: how to build an app, what "unhealthy long enough to
repair" looks like, how to run the evaluation, and what the real repair action
is.  Everything else — reading the helpers, the unreadable-helper guard, the
fail-closed rule, clamping, applying a card command — is the same contract for
all of them.

Per-checker *logic* (dwell ladders, rate limits, cross-checks, recovery
verification) stays in that checker's own test module.
"""

from __future__ import annotations

import asyncio
import datetime
import importlib
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Importing each checker's test module installs the `hassapi` mock and the
# sys.path entries the checker imports need, and gives us its app factory.
import test_fan_health_checker as t_fan
import test_protect_health_checker as t_protect
import test_repairable_device_checker as t_device
import test_repairable_device_group_checker as t_group
import test_repairable_network_protocol_checker as t_network
import test_shade_gateway_checker as t_shade
import test_spa_health_checker as t_spa

# ...and, once those have run their sys.path bootstrap, the mixin itself.
# The checkers reach it as ``shared.auto_repair_config``; so do we, so that
# TestTheStandDownConstantsMatchEveryChecker compares the very module object
# they are mixing in.
mixin_mod = importlib.import_module("shared.auto_repair_config")


# ---------------------------------------------------------------------------
# Service-call doubles — the shapes AppDaemon 4.5.13 really returns
# ---------------------------------------------------------------------------

#: What an awaited ``call_service`` returns on success: Home Assistant's
#: websocket result envelope with AppDaemon's own keys merged in
#: (``hassplugin.py:436``).
OK_RESULT: Dict[str, Any] = {
    "id": 7,
    "type": "result",
    "success": True,
    "result": {"context": {"id": "01J"}},
    "ad_status": "OK",
    "ad_duration": 0.012,
}

#: Websocket timeout — ``hassplugin.py:422-426``.
TIMEOUT_RESULT: Dict[str, Any] = {
    "success": False,
    "ad_status": "TIMEOUT",
    "ad_duration": 60.0,
}

#: Home Assistant rejected the write (e.g. a set_value outside the helper's
#: range): the raw HA error envelope, with ``ad_status`` still "OK".
HA_ERROR_RESULT: Dict[str, Any] = {
    "id": 8,
    "type": "result",
    "success": False,
    "error": {"code": "invalid_format", "message": "value out of range"},
    "ad_status": "OK",
    "ad_duration": 0.009,
}

#: The plugin was disconnected: ``@hass_check`` swallows the call and the
#: awaited value is ``None`` (``plugins/hass/utils.py:34-48``).
DISCONNECTED_RESULT = None

#: Every failure shape a write can come back as. None of them may update the
#: cache — a cached "off" that HA never accepted is a kill switch that isn't.
WRITE_FAILURES = [TIMEOUT_RESULT, HA_ERROR_RESULT, DISCONNECTED_RESULT]

#: Every state that means "no usable reading".
UNREADABLE = (None, "unavailable", "unknown", "none", "")

#: The repair statuses this contract reasons about, spelled out rather than
#: imported from any one checker — that is the whole point of
#: :class:`TestTheStandDownConstantsMatchEveryChecker`, which proves all eight
#: copies (seven checkers plus the mixin) still agree with these literals.
REPAIR_IDLE = "idle"
REPAIR_PENDING = "pending"
REPAIR_SUCCESS = "success"


class ServiceResult:
    """A stand-in for what ``call_service`` hands back.

    ``call_service`` is awaited in the config paths and deliberately *not*
    awaited at the repair call sites (``utils.sync_decorator`` returns a Task
    on the loop, so a bare call still runs).  The double therefore has to
    survive both: awaiting it yields ``value``, and discarding it is silent.
    A plain ``AsyncMock`` would emit "coroutine was never awaited" at every
    fire-and-forget site.  This file is kept free of that noise, checked from
    ``appdaemon/`` with::

        python -m pytest tests/test_auto_repair_config_contract.py -q \\
            -W error::RuntimeWarning

    which reports the run with **no warnings**; swapping this double for an
    ``AsyncMock`` turns that into eleven.  It has to be pytest's own ``-W``,
    not the interpreter's: ``tests/conftest.py`` installs a repo-wide
    ``ignore:coroutine .* was never awaited`` filter, and pytest applies its
    ``-W`` after the ini filters, so only the flag spelled this way wins.
    (The warning surfaces as ``PytestUnraisableExceptionWarning`` — it is
    raised during GC, so it is reported rather than failing the test; the
    positive check that the awaits really happen is ``ServiceResult.awaits``.)
    """

    def __init__(self, value: Any) -> None:
        self.value = value
        self.awaits = 0

    def __await__(self):
        self.awaits += 1
        yield from ()
        return self.value


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _ago(**kwargs) -> datetime.datetime:
    return datetime.datetime.now() - datetime.timedelta(**kwargs)


def _drain(app) -> None:
    """Await whatever the app handed to ``create_task`` (config commands)."""

    async def _inner():
        while app.captured:
            await app.captured.pop(0)

    _run(_inner())


def _drive(app, module: str) -> None:
    """Await captured repair coroutines with the poll sleeps patched out."""

    async def _inner():
        while app.captured:
            await app.captured.pop(0)

    with patch(f"{module}.asyncio.sleep", new=AsyncMock(return_value=None)):
        _run(_inner())


def _logs(app, level: str, *fragments: str) -> List[Any]:
    return [
        c
        for c in app.log.call_args_list
        if c[1].get("level") == level and all(f in str(c) for f in fragments)
    ]


def _service_calls(app, service: str) -> List[Any]:
    return [
        c
        for c in app.call_service.call_args_list
        if c.args and c.args[0] == service
    ]


# ---------------------------------------------------------------------------
# Per-checker adapters
# ---------------------------------------------------------------------------


class Spec:
    """Everything the contract needs to know about one checker."""

    def __init__(
        self,
        name: str,
        module: str,
        factory: Callable[[dict], Any],
        checker_id: str,
        delay_min: int,
        delay_max: int,
        step: int,
        arm: Callable[[Any, int], None],
        fire: Callable[[Any], None],
        actions: Callable[[Any], List[Any]],
        base_states: Optional[Dict[str, Any]] = None,
        extra_args: Optional[Dict[str, Any]] = None,
        reported_status: Optional[Callable[[Any], str]] = None,
    ) -> None:
        self.name = name
        self.module = module
        self.factory = factory
        self.checker_id = checker_id
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.step = step
        self._arm = arm
        self._fire = fire
        self._actions = actions
        self.base_states = base_states or {}
        self.extra_args = extra_args or {}
        self._reported_status = reported_status or (lambda app: app._repair_status)

    # -- entity ids ----------------------------------------------------
    @property
    def toggle(self) -> str:
        return f"input_boolean.{self.checker_id}_health_auto_repair"

    @property
    def delay(self) -> str:
        return f"input_number.{self.checker_id}_health_auto_repair_delay"

    # -- app construction ----------------------------------------------
    def app(
        self,
        *,
        enabled_default: bool = False,
        delay_default: Optional[int] = None,
        states: Optional[Dict[str, Any]] = None,
        extra_args: Optional[Dict[str, Any]] = None,
    ):
        args = dict(self.extra_args)
        args["auto_repair_enabled_default"] = enabled_default
        args["auto_repair_delay_min_default"] = (
            self.delay_min if delay_default is None else delay_default
        )
        args["repair_recovery_wait_s"] = 0
        if extra_args:
            args.update(extra_args)

        app = self.factory(args)

        app.entity_states = dict(self.base_states)
        app.entity_states.update(states or {})
        app.get_state = AsyncMock(
            side_effect=lambda entity_id=None, **kw: app.entity_states.get(entity_id)
        )
        app.call_service = MagicMock(return_value=ServiceResult(OK_RESULT))
        app.add_entity = MagicMock(return_value=ServiceResult(None))
        app.captured = []
        app.create_task = MagicMock(side_effect=app.captured.append)
        app.log = MagicMock()
        app.initialize()
        return app

    # -- behaviour ------------------------------------------------------
    def refresh(self, app) -> None:
        _run(app._refresh_auto_repair_config())

    def command(self, app, **payload) -> None:
        app._on_repair_command(
            f"health_check_repair_{self.checker_id}",
            {"action": "update_repair_config", **payload},
            {},
        )
        _drain(app)

    def arm(self, app, minutes_ago: int = 400) -> None:
        """Mark the checker unhealthy since *minutes_ago*.

        400 minutes is past every checker's maximum delay, so the default arms
        a repair that is due now. A small value instead leaves it counting
        down — the ``pending`` state the card and the paging hold react to.
        """
        self._arm(app, minutes_ago)

    def fire(self, app) -> None:
        """Run the auto-repair evaluation and let any repair it started run."""
        self._fire(app)

    def actions(self, app) -> List[Any]:
        """The real repair actions taken — service calls, not cached flags."""
        return self._actions(app)

    def reported_status(self, app) -> str:
        """The repair status the controller (and so the card) is told about."""
        return self._reported_status(app)

    def cancel(self, app) -> None:
        app._on_repair_command(
            f"health_check_repair_{self.checker_id}",
            {"action": "cancel_repair"},
            {},
        )


# -- device checker (printer) ------------------------------------------------

MOD_DEVICE = "health_checks.checker_apps.device_checker.repairable_device_checker"

_DEVICE_BAD = [
    {"name": "Ping", "status": "critical", "detail": "timeout"},
    {"name": "Status", "status": "critical", "detail": "unavailable"},
]


def _device_arm(app, minutes_ago: int) -> None:
    app._unhealthy_since = _ago(minutes=minutes_ago)
    app._run_checks_only = AsyncMock(return_value=_DEVICE_BAD)


def _device_fire(app) -> None:
    app._evaluate_auto_repair(_DEVICE_BAD)
    _drive(app, MOD_DEVICE)


# -- device group checker (cielo) --------------------------------------------

MOD_GROUP = (
    "health_checks.checker_apps.device_group_checker."
    "repairable_device_group_checker"
)

_GROUP_BAD = [
    {"name": "Movie Room Status", "status": "critical", "detail": "off"},
    {"name": "Movie Room Ping", "status": "critical", "detail": "timeout"},
    {"name": "Rumpus Room Status", "status": "ok", "detail": "on"},
    {"name": "Rumpus Room Ping", "status": "ok", "detail": "5ms"},
]


def _group_arm(app, minutes_ago: int) -> None:
    app._unhealthy_since = _ago(minutes=minutes_ago)
    app._run_checks_only = AsyncMock(return_value=_GROUP_BAD)


def _group_fire(app) -> None:
    app._evaluate_auto_repair(_GROUP_BAD)
    _drive(app, MOD_GROUP)


# -- fan checker (fans) ------------------------------------------------------

MOD_FAN = "health_checks.checker_apps.fan_health_checker.fan_health_checker"


def _fan_arm(app, minutes_ago: int) -> None:
    app._fan_unhealthy_since["Pink Room"] = _ago(minutes=minutes_ago)


def _fan_fire(app) -> None:
    app._evaluate_auto_repair(t_fan._pink_down_results())
    _drive(app, MOD_FAN)


# -- spa checker (spa) -------------------------------------------------------

MOD_SPA = "health_checks.checker_apps.spa_health_checker.spa_health_checker"

_SPA_BAD = [
    {"name": "Gateway Ping", "status": "critical", "detail": "timeout"},
    {"name": "Overall Connection", "status": "critical", "detail": "State: off"},
]


def _spa_arm(app, minutes_ago: int) -> None:
    app._unhealthy_since = _ago(minutes=minutes_ago)


def _spa_fire(app) -> None:
    app._evaluate_auto_repair(_SPA_BAD)
    _drive(app, MOD_SPA)


# -- shade gateway checker (shade_gateway) -----------------------------------

MOD_SHADE = (
    "health_checks.checker_apps.shade_gateway_checker.shade_gateway_checker"
)


def _shade_arm(app, minutes_ago: int) -> None:
    t_shade._start_episode(
        app,
        "sensor.test_shade_a_battery",
        "sensor.test_shade_b_battery",
        minutes_ago=minutes_ago,
    )


def _shade_fire(app) -> None:
    app._evaluate_auto_repair()
    _drive(app, MOD_SHADE)


# -- Protect checker (protect) -----------------------------------------------

MOD_PROTECT = (
    "health_checks.checker_apps.protect_health_checker.protect_health_checker"
)

_PROTECT_BAD = [{"name": "Camera Events", "status": "critical", "detail": "frozen"}]


def _protect_arm(app, minutes_ago: int) -> None:
    app._unhealthy_since = _ago(minutes=minutes_ago)
    admin = t_protect._make_mock_admin()
    admin.list_config_entries = AsyncMock(
        return_value=[t_protect.IGNORED_ENTRY, t_protect.LOADED_ENTRY]
    )
    app._admin = admin
    app._any_event_after = AsyncMock(return_value="binary_sensor.driveway_motion")
    app._repair_recovery_wait_s = 0


def _protect_fire(app) -> None:
    app._evaluate_auto_repair(_PROTECT_BAD)
    _drive(app, MOD_PROTECT)


def _protect_actions(app) -> List[Any]:
    admin = getattr(app, "_admin", None)
    if admin is None:
        return []
    return list(admin.reload_config_entry.call_args_list)


# -- network protocol checker (zwave) ----------------------------------------

MOD_NETWORK = (
    "health_checks.checker_apps.network_protocol_checker."
    "repairable_network_protocol_checker"
)


def _network_arm(app, minutes_ago: int) -> None:
    app._unhealthy_since = _ago(minutes=minutes_ago)


def _network_fire(app) -> None:
    async def _inner():
        await app._evaluate_auto_repair(t_network._results())
        while app.captured:
            await app.captured.pop(0)

    with patch(f"{MOD_NETWORK}.asyncio.sleep", new=AsyncMock(return_value=None)):
        _run(_inner())


SPECS = [
    Spec(
        "device", MOD_DEVICE, t_device._make_app, "printer", 1, 60, 1,
        _device_arm, _device_fire,
        lambda app: _service_calls(app, "switch/turn_off"),
    ),
    Spec(
        "device_group", MOD_GROUP, t_group._make_app, "cielo", 1, 60, 1,
        _group_arm, _group_fire,
        lambda app: _service_calls(app, "switch/turn_off"),
        reported_status=lambda app: app._aggregate_repair_status(),
    ),
    Spec(
        "fan", MOD_FAN, t_fan._make_app, "fans", 1, 60, 1,
        _fan_arm, _fan_fire,
        lambda app: _service_calls(app, "script/turn_on"),
        reported_status=lambda app: app._aggregate_repair_status(),
    ),
    Spec(
        "spa", MOD_SPA, t_spa._make_app, "spa", 1, 60, 1,
        _spa_arm, _spa_fire,
        lambda app: _service_calls(app, "switch/turn_off"),
    ),
    Spec(
        "shade_gateway", MOD_SHADE, t_shade._make_app, "shade_gateway",
        15, 360, 15,
        _shade_arm, _shade_fire,
        lambda app: _service_calls(app, "button/press"),
    ),
    Spec(
        "protect", MOD_PROTECT, t_protect._make_app, "protect", 1, 60, 1,
        _protect_arm, _protect_fire, _protect_actions,
    ),
    Spec(
        "network_protocol", MOD_NETWORK, t_network._make_app, "zwave",
        1, 60, 1,
        _network_arm, _network_fire,
        lambda app: _service_calls(app, "button/press"),
        base_states={
            t_network.DEFAULT_ARGS["entity_id"]: "driver_failed",
            t_network.SERIAL_ENTITY: "on",
        },
    ),
]


#: For the handful of contracts that are genuinely one checker's shape.
SPEC_BY_NAME = {s.name: s for s in SPECS}


@pytest.fixture(params=SPECS, ids=lambda s: s.name)
def spec(request) -> Spec:
    return request.param


# ---------------------------------------------------------------------------
# The unreadable-helper window
# ---------------------------------------------------------------------------


class TestUnreadableBeforeFirstSight:
    """A helper AppDaemon has not seen yet must not disable auto-repair.

    ``ensure_helper`` creates the helper over the REST API; AppDaemon only
    learns of it at the next full plugin-state refresh (``refresh_delay``,
    10 minutes by default), so until then ``get_state`` returns None.
    ``str(None) == "on"`` is False, and taking that as a real read is what
    shipped 1.17.0 inert: the toggle was `on` in HA, the checker had it cached
    as disabled, and nothing in the logs said so.
    """

    @pytest.mark.parametrize("raw", UNREADABLE)
    def test_keeps_the_configured_default(self, spec, raw):
        app = spec.app(enabled_default=True, states={spec.toggle: raw})

        spec.refresh(app)

        assert app._cached_auto_repair_enabled is True

    def test_a_real_off_still_disables(self, spec):
        """"Unknown" is not evidence; an actual "off" is."""
        app = spec.app(enabled_default=True, states={spec.toggle: "off"})

        spec.refresh(app)

        assert app._cached_auto_repair_enabled is False

    def test_the_repair_really_fires(self, spec):
        """The end-to-end consequence, not just the cached flag."""
        app = spec.app(enabled_default=True, states={spec.toggle: None})
        spec.refresh(app)
        spec.arm(app)

        spec.fire(app)

        assert len(spec.actions(app)) == 1

    def test_an_unreadable_delay_keeps_the_cached_one(self, spec):
        app = spec.app(delay_default=spec.delay_min, states={spec.delay: None})

        spec.refresh(app)

        assert app._cached_auto_repair_delay_min == spec.delay_min


class TestUnreadableAfterFirstSight:
    """Once the helper has been read, losing it must fail CLOSED.

    Keeping the cached value forever is right only while the helper has never
    been seen. A helper that WAS readable and now is not has been deleted or
    gone unavailable — and a kill switch nobody can reach must not keep
    repairing. `main` failed closed here (an unreadable read was taken as
    "not on"); the first-run guard must not quietly take that away.
    """

    @pytest.mark.parametrize("raw", UNREADABLE)
    def test_toggle_fails_closed(self, spec, raw):
        app = spec.app(enabled_default=True, states={spec.toggle: "on"})
        spec.refresh(app)
        assert app._cached_auto_repair_enabled is True

        app.entity_states[spec.toggle] = raw
        spec.refresh(app)

        assert app._cached_auto_repair_enabled is False

    def test_failing_closed_names_the_entity(self, spec):
        app = spec.app(enabled_default=True, states={spec.toggle: "on"})
        spec.refresh(app)

        app.entity_states[spec.toggle] = None
        spec.refresh(app)

        assert _logs(app, "WARNING", spec.toggle)

    def test_failing_closed_stops_the_repair(self, spec):
        app = spec.app(enabled_default=True, states={spec.toggle: "on"})
        spec.refresh(app)
        app.entity_states[spec.toggle] = "unavailable"
        spec.refresh(app)
        spec.arm(app)

        spec.fire(app)

        assert spec.actions(app) == []

    def test_delay_keeps_its_last_good_value(self, spec):
        """There is no safe "closed" delay — the last good one stands."""
        good = min(spec.delay_min + spec.step, spec.delay_max)
        app = spec.app(delay_default=spec.delay_min, states={spec.delay: str(good)})
        spec.refresh(app)
        assert app._cached_auto_repair_delay_min == good

        app.entity_states[spec.delay] = None
        spec.refresh(app)

        assert app._cached_auto_repair_delay_min == good


class TestTransitionLogging:
    """An operator needs the window to open and close audibly — once each."""

    def test_a_clean_start_is_silent(self, spec):
        app = spec.app(
            enabled_default=True,
            states={spec.toggle: "on", spec.delay: str(spec.delay_min)},
        )

        spec.refresh(app)

        assert _logs(app, "WARNING", spec.toggle) == []
        assert _logs(app, "INFO", "readable again") == []

    def test_the_window_opening_warns_once(self, spec):
        app = spec.app(enabled_default=True, states={spec.toggle: None})

        spec.refresh(app)
        spec.refresh(app)
        spec.refresh(app)

        assert len(_logs(app, "WARNING", spec.toggle, "not readable")) == 1

    def test_the_window_closing_logs_info(self, spec):
        app = spec.app(enabled_default=True, states={spec.toggle: None})
        spec.refresh(app)

        app.entity_states[spec.toggle] = "on"
        spec.refresh(app)

        assert len(_logs(app, "INFO", spec.toggle, "readable again")) == 1

    def test_the_delay_window_opening_warns_once(self, spec):
        """The delay helper owes the same courtesy as the toggle.

        Its branch is a separate ``try`` with its own transition flag, so it
        can regress on its own: a warning per cycle here is one line every
        ``check_interval_s``, for every checker whose delay helper is missing,
        for as long as it stays missing — the kind of noise that gets an
        operator to stop reading the log the toggle's warning also lives in.
        """
        app = spec.app(delay_default=spec.delay_min, states={spec.delay: None})

        spec.refresh(app)
        spec.refresh(app)
        spec.refresh(app)

        assert len(_logs(app, "WARNING", spec.delay, "not readable")) == 1


# ---------------------------------------------------------------------------
# Card commands
# ---------------------------------------------------------------------------


class TestCommandUpdatesTheCache:
    """An explicit choice must take effect even while the helper is unreadable.

    The command used to rely on the next ``get_state`` picking the value up —
    but during the provisioning window that read is still None. An operator
    turning auto-repair off would see it off in the card and in HA while the
    checker went on repairing.
    """

    def test_turning_off_takes_effect_immediately(self, spec):
        app = spec.app(enabled_default=True, states={spec.toggle: None})
        spec.refresh(app)
        assert app._cached_auto_repair_enabled is True

        spec.command(app, auto_repair_enabled=False)

        assert app._cached_auto_repair_enabled is False
        assert _service_calls(app, "input_boolean/turn_off")

    def test_turning_off_suppresses_the_action(self, spec):
        """The end-to-end consequence of the command, mid-window."""
        app = spec.app(enabled_default=True, states={spec.toggle: None})
        spec.refresh(app)
        spec.command(app, auto_repair_enabled=False)
        spec.arm(app)

        spec.fire(app)

        assert spec.actions(app) == []

    def test_turning_on_takes_effect_immediately(self, spec):
        app = spec.app(enabled_default=False, states={spec.toggle: None})
        spec.refresh(app)
        assert app._cached_auto_repair_enabled is False

        spec.command(app, auto_repair_enabled=True)

        assert app._cached_auto_repair_enabled is True
        assert _service_calls(app, "input_boolean/turn_on")

    def test_delay_change_takes_effect_immediately(self, spec):
        wanted = min(spec.delay_min + spec.step, spec.delay_max)
        app = spec.app(delay_default=spec.delay_min, states={spec.delay: None})
        spec.refresh(app)

        spec.command(app, auto_repair_delay_min=wanted)

        assert app._cached_auto_repair_delay_min == wanted
        assert _service_calls(app, "input_number/set_value")

    def test_the_already_at_desired_branch_caches_without_a_write(self, spec):
        """The helper already holds it: proof enough, and no service call.

        This branch had no coverage at all before — and it is the one that
        runs whenever the card echoes back a value HA already has.
        """
        app = spec.app(enabled_default=False, states={spec.toggle: "on"})

        spec.command(app, auto_repair_enabled=True)

        assert app._cached_auto_repair_enabled is True
        assert _service_calls(app, "input_boolean/turn_on") == []

    def test_the_already_at_desired_delay_caches_without_a_write(self, spec):
        wanted = min(spec.delay_min + spec.step, spec.delay_max)
        app = spec.app(
            delay_default=spec.delay_min, states={spec.delay: str(wanted)}
        )

        spec.command(app, auto_repair_delay_min=wanted)

        assert app._cached_auto_repair_delay_min == wanted
        assert _service_calls(app, "input_number/set_value") == []

    def test_the_card_is_refreshed_at_the_end(self, spec):
        """Without this the card re-renders from the stale controller sensor.

        The controller only refreshes its copy of ``repair_state`` on
        ``report_status``, so a toggle flipped from the card would spring back
        for up to a check interval.
        """
        app = spec.app(enabled_default=False, states={spec.toggle: None})
        app._report_repair_status_only = MagicMock()

        spec.command(app, auto_repair_enabled=True)

        assert app._report_repair_status_only.call_count == 1

    def test_an_unknown_action_is_logged(self, spec):
        app = spec.app()

        app._on_repair_command(
            f"health_check_repair_{spec.checker_id}",
            {"action": "wat"},
            {},
        )

        assert _logs(app, "WARNING", "Unknown repair action")


class TestTheCommandTellsProvisioningLagFromAVanishedHelper:
    """Both look like ``get_state`` → None. They need opposite answers.

    Home Assistant answers ``success: true`` for a service call that matched
    zero entities — ``helpers/service.py`` logs a warning and returns — so
    ``_service_ok`` cannot tell a write that landed from one that hit nothing.
    The pre-read is the only evidence available, and its ``None`` is
    ambiguous: before the helper has ever been seen it means the REST-created
    entity has not reached AppDaemon's local state yet (write, it exists);
    after it has been seen it means the helper was deleted (do not write, and
    above all do not cache a change that never happened).
    """

    def test_a_write_to_a_vanished_helper_is_skipped(self, spec):
        app = spec.app(enabled_default=True, states={spec.toggle: "on"})
        spec.refresh(app)
        app.entity_states[spec.toggle] = None  # somebody deleted the helper

        spec.command(app, auto_repair_enabled=False)

        assert _service_calls(app, "input_boolean/turn_off") == []
        assert _service_calls(app, "input_boolean/turn_on") == []
        assert app._cached_auto_repair_enabled is True
        assert _logs(app, "WARNING", spec.toggle, "not available")

    def test_a_write_during_provisioning_lag_still_goes_through(self, spec):
        """The other side of the same ``None`` — and the reason it is subtle.

        Here the helper genuinely exists in HA (``ensure_helper`` just made
        it); only AppDaemon's copy of the namespace is behind. Refusing to
        write would strand the operator's very first toggle for up to
        ``refresh_delay``.
        """
        app = spec.app(enabled_default=True, states={spec.toggle: None})
        spec.refresh(app)
        assert app._toggle_ever_readable is False

        spec.command(app, auto_repair_enabled=False)

        assert len(_service_calls(app, "input_boolean/turn_off")) == 1
        assert app._cached_auto_repair_enabled is False


class TestAReadableCommandPreReadCountsAsASighting:
    """The card path sees the helper too, and must say so.

    ``_refresh_auto_repair_config`` is not the only place a helper is read:
    ``_apply_toggle_command`` reads it before every write. If only the refresh
    armed ``_toggle_ever_readable``, a helper first seen by a command would
    still count as never-seen — and the fail-closed rule, which is the whole
    kill switch, would not apply to it.
    """

    def test_a_helper_first_seen_by_a_command_still_fails_closed(self, spec):
        app = spec.app(enabled_default=True, states={spec.toggle: None})
        spec.refresh(app)  # unreadable so far: the configured default stands

        app.entity_states[spec.toggle] = "on"  # the plugin refresh landed
        spec.command(app, auto_repair_enabled=True)  # ...and the card read it

        app.entity_states[spec.toggle] = None  # now the helper is deleted
        spec.refresh(app)

        assert app._cached_auto_repair_enabled is False

        # The end-to-end consequence: no repair runs behind a kill switch
        # nobody can reach.
        spec.arm(app)
        spec.fire(app)

        assert spec.actions(app) == []


class TestWriteFailuresDoNotUpdateTheCache:
    """A cache the write never reached is a lie the operator cannot see.

    Every one of these shapes is a *failed* write (see ``_service_ok``'s
    docstring for where each comes from in the AppDaemon source), so none of
    them may move the cached value.
    """

    @pytest.mark.parametrize("result", WRITE_FAILURES)
    def test_a_failed_toggle_write_is_not_cached(self, spec, result):
        app = spec.app(enabled_default=True, states={spec.toggle: None})
        spec.refresh(app)
        app.call_service = MagicMock(return_value=ServiceResult(result))

        spec.command(app, auto_repair_enabled=False)

        assert app._cached_auto_repair_enabled is True

    @pytest.mark.parametrize("result", WRITE_FAILURES)
    def test_a_failed_delay_write_is_not_cached(self, spec, result):
        wanted = min(spec.delay_min + spec.step, spec.delay_max)
        app = spec.app(delay_default=spec.delay_min, states={spec.delay: None})
        spec.refresh(app)
        app.call_service = MagicMock(return_value=ServiceResult(result))

        spec.command(app, auto_repair_delay_min=wanted)

        assert app._cached_auto_repair_delay_min == spec.delay_min

    def test_a_raised_write_is_not_cached(self, spec):
        app = spec.app(enabled_default=True, states={spec.toggle: None})
        spec.refresh(app)
        app.call_service = MagicMock(side_effect=RuntimeError("ws closed"))

        spec.command(app, auto_repair_enabled=False)

        assert app._cached_auto_repair_enabled is True
        assert _logs(app, "ERROR", "auto-repair toggle")

    def test_a_raised_delay_write_is_not_cached(self, spec):
        """The delay write has its own ``except`` arm, and its own way to lie.

        A cached delay the helper never took is quieter than a cached toggle
        but no less wrong: the card shows the number the operator typed, the
        helper still holds the old one, and the dwell the checker actually
        counts is neither of the two a human can see.
        """
        wanted = min(spec.delay_min + spec.step, spec.delay_max)
        app = spec.app(delay_default=spec.delay_min, states={spec.delay: None})
        spec.refresh(app)
        app.call_service = MagicMock(side_effect=RuntimeError("ws closed"))

        spec.command(app, auto_repair_delay_min=wanted)

        assert app._cached_auto_repair_delay_min == spec.delay_min
        assert _logs(app, "ERROR", "auto-repair delay")

    def test_a_successful_write_is_cached(self, spec):
        """The control: the same path with the real success shape."""
        app = spec.app(enabled_default=True, states={spec.toggle: None})
        spec.refresh(app)

        spec.command(app, auto_repair_enabled=False)

        assert app._cached_auto_repair_enabled is False

    def test_service_ok_reads_the_documented_shapes(self, spec):
        app = spec.app()
        assert app._service_ok(OK_RESULT) is True
        assert app._service_ok(TIMEOUT_RESULT) is False
        assert app._service_ok(HA_ERROR_RESULT) is False
        assert app._service_ok(None) is False


class TestDelayParsing:
    """Both the helper and the card hand over hand-editable strings."""

    def test_float_shaped_delays_are_accepted(self, spec):
        wanted = min(spec.delay_min + spec.step, spec.delay_max)
        app = spec.app(delay_default=spec.delay_min, states={spec.delay: None})

        spec.command(app, auto_repair_delay_min=f"{wanted}.0")

        assert app._cached_auto_repair_delay_min == wanted

    @pytest.mark.parametrize("hostile", ["abc", "", None, "inf", "nan", [1]])
    def test_an_unparseable_command_delay_is_ignored(self, spec, hostile):
        """``int(float("inf"))`` raises OverflowError, not ValueError."""
        app = spec.app(delay_default=spec.delay_min, states={spec.delay: None})
        spec.refresh(app)

        spec.command(app, auto_repair_delay_min=hostile)

        assert app._cached_auto_repair_delay_min == spec.delay_min
        assert _service_calls(app, "input_number/set_value") == []

    def test_an_unparseable_delay_does_not_skip_the_toggle(self, spec):
        app = spec.app(enabled_default=False, states={spec.toggle: None})
        spec.refresh(app)

        spec.command(app, auto_repair_enabled=True, auto_repair_delay_min="abc")

        assert app._cached_auto_repair_enabled is True

    @pytest.mark.parametrize("hostile", ["abc", "inf", "nan"])
    def test_an_unparseable_helper_delay_keeps_the_cached_one(self, spec, hostile):
        app = spec.app(delay_default=spec.delay_min, states={spec.delay: hostile})

        spec.refresh(app)

        assert app._cached_auto_repair_delay_min == spec.delay_min


class TestDelayIsClamped:
    """All three paths that can set the cached delay obey the helper's bounds.

    HA silently rejects a ``set_value`` outside an input_number's range
    without AppDaemon raising, so an unclamped cache would hold a value the
    helper never accepted — and a delay of 0 collapses the dwell gate.
    """

    def test_the_config_default_is_clamped(self, spec):
        app = spec.app(delay_default=0)

        assert app._cached_auto_repair_delay_min == spec.delay_min
        assert _logs(app, "WARNING", "outside the permitted")

    def test_a_helper_value_is_clamped(self, spec):
        app = spec.app(delay_default=spec.delay_min, states={spec.delay: "9000"})

        spec.refresh(app)

        assert app._cached_auto_repair_delay_min == spec.delay_max

    def test_the_helper_clamp_warns_once_per_episode(self, spec):
        app = spec.app(delay_default=spec.delay_min, states={spec.delay: "9000"})

        spec.refresh(app)
        spec.refresh(app)
        spec.refresh(app)

        assert len(_logs(app, "WARNING", "outside the permitted")) == 1

    def test_the_latch_resets_when_the_value_comes_back_in_range(self, spec):
        app = spec.app(delay_default=spec.delay_min, states={spec.delay: "9000"})
        spec.refresh(app)
        app.entity_states[spec.delay] = str(spec.delay_min)
        spec.refresh(app)
        app.entity_states[spec.delay] = "9000"

        spec.refresh(app)

        assert len(_logs(app, "WARNING", "outside the permitted")) == 2

    @pytest.mark.parametrize("sent", [0, -5, 9000])
    def test_a_command_delay_is_clamped(self, spec, sent):
        app = spec.app(delay_default=spec.delay_min, states={spec.delay: None})
        expected = max(spec.delay_min, min(spec.delay_max, sent))

        spec.command(app, auto_repair_delay_min=sent)

        assert app._cached_auto_repair_delay_min == expected

    def test_a_command_clamp_is_always_loud(self, spec):
        """The read path latches its warning; an operator's typo must not.

        Same out-of-range value twice from the card, two warnings — otherwise
        the second override is silent and the card shows a number the checker
        is not using.
        """
        app = spec.app(delay_default=spec.delay_min, states={spec.delay: None})

        spec.command(app, auto_repair_delay_min=9000)
        spec.command(app, auto_repair_delay_min=9000)

        assert len(_logs(app, "WARNING", "outside the permitted")) == 2

    def test_a_loud_clamp_never_touches_the_read_paths_latch(self, spec):
        """The latch is the read path's alone — a loud clamp must not arm it.

        An out-of-range ``auto_repair_delay_min_default`` left in the app YAML
        is clamped once at startup, loudly. If that clamp also set the latch,
        the once-per-episode warning the read path owes about the *helper's*
        value would be permanently spent before the helper had ever been read
        — the operator would be told about the YAML they can see and never
        about the helper they cannot. Two independently wrong values must
        produce two independent warnings.
        """
        # Out of range for every spec: 9000 is above both maxima, and
        # delay_max * 2 is above whichever maximum this checker has.
        app = spec.app(
            delay_default=9000, states={spec.delay: str(spec.delay_max * 2)}
        )
        assert len(_logs(app, "WARNING", "outside the permitted")) == 1

        spec.refresh(app)

        assert len(_logs(app, "WARNING", "outside the permitted")) == 2
        assert app._cached_auto_repair_delay_min == spec.delay_max

        # ...and the read path's own latch still works: the same out-of-range
        # helper value on the next cycle stays quiet.
        spec.refresh(app)

        assert len(_logs(app, "WARNING", "outside the permitted")) == 2

    def test_a_zero_configured_default_cannot_collapse_the_dwell(self, spec):
        """The end-to-end consequence of clamping the *config* default.

        ``auto_repair_delay_min_default: 0`` in the app YAML, with the delay
        helper not yet readable, is a checker that repairs the instant it
        first sees an outage — no dwell, no chance for a transient to clear
        itself, and (for the fan and spa) mains power cycled off a single bad
        poll. Only the clamped cache stands between that YAML and the action,
        so the evaluation has to read the cache and nothing else.
        """
        app = spec.app(
            enabled_default=True,
            delay_default=0,
            states={spec.toggle: "on", spec.delay: None},
        )
        spec.refresh(app)
        assert app._cached_auto_repair_enabled is True  # nothing else is stopping it
        spec.arm(app, minutes_ago=0)  # unhealthy as of right now

        spec.fire(app)

        assert spec.actions(app) == []
        assert spec.reported_status(app) == REPAIR_PENDING


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------


def _prov(created: bool) -> MagicMock:
    prov = MagicMock()
    prov.ensure_helper = AsyncMock(return_value=created)
    return prov


class TestProvisioning:
    """Creating a helper must also make it readable and seed its value."""

    def test_both_helpers_are_ensured(self, spec):
        app = spec.app()
        prov = _prov(False)

        _run(app._provision_auto_repair_helpers(prov))

        kinds = [c.args[0] for c in prov.ensure_helper.call_args_list]
        assert kinds == ["input_boolean", "input_number"]

    def test_the_delay_helper_is_created_with_this_checkers_bounds(self, spec):
        app = spec.app()
        prov = _prov(False)

        _run(app._provision_auto_repair_helpers(prov))

        kwargs = prov.ensure_helper.call_args_list[1].kwargs
        assert kwargs["min"] == spec.delay_min
        assert kwargs["max"] == spec.delay_max
        assert kwargs["step"] == spec.step

    def test_a_created_helper_is_added_to_appdaemons_local_state(self, spec):
        """This is what closes the unreadable window.

        Without ``add_entity`` AppDaemon does not see the new helper until its
        next full plugin-state refresh — ``refresh_delay``, 10 minutes by
        default. With it, the very next ``get_state`` works.
        """
        app = spec.app(enabled_default=True)
        prov = _prov(True)

        _run(app._provision_auto_repair_helpers(prov))

        added = {c.args[0]: c.args[1] for c in app.add_entity.call_args_list}
        # Registered AFTER the seed with the state HA confirmed: "on" here,
        # because the seed succeeded. Registering "off" first would be read
        # back one line later and start a default-on checker one interval
        # inert; registering "on" blind would trust an unconfirmed write.
        assert added[spec.toggle] == "on"
        assert added[spec.delay] == spec.delay_min
        # ...and it was awaited, not left as a dangling coroutine.
        assert app.add_entity.return_value.awaits == 2

    def test_a_created_toggle_is_seeded_from_the_config_default(self, spec):
        app = spec.app(enabled_default=True)
        prov = _prov(True)

        _run(app._provision_auto_repair_helpers(prov))

        assert len(_service_calls(app, "input_boolean/turn_on")) == 1

    def test_a_created_delay_is_seeded_from_the_config_default(self, spec):
        app = spec.app(delay_default=spec.delay_min)
        prov = _prov(True)

        _run(app._provision_auto_repair_helpers(prov))

        calls = _service_calls(app, "input_number/set_value")
        assert len(calls) == 1
        assert calls[0].kwargs["value"] == spec.delay_min

    def test_an_existing_helper_is_never_seeded(self, spec):
        """A later manual "off" must survive every restart."""
        app = spec.app(enabled_default=True)
        prov = _prov(False)

        _run(app._provision_auto_repair_helpers(prov))

        assert _service_calls(app, "input_boolean/turn_on") == []
        assert _service_calls(app, "input_number/set_value") == []
        assert app.add_entity.call_args_list == []

    def test_a_disabled_default_does_not_turn_the_toggle_on(self, spec):
        app = spec.app(enabled_default=False)
        prov = _prov(True)

        _run(app._provision_auto_repair_helpers(prov))

        assert _service_calls(app, "input_boolean/turn_on") == []

    def test_a_rejected_seed_is_reported(self, spec):
        app = spec.app(enabled_default=True)
        app.call_service = MagicMock(return_value=ServiceResult(TIMEOUT_RESULT))
        prov = _prov(True)

        _run(app._provision_auto_repair_helpers(prov))

        assert _logs(app, "WARNING", "did not accept the initial value")

    def test_a_failed_toggle_creation_still_provisions_the_delay(self, spec):
        app = spec.app()
        prov = MagicMock()
        prov.ensure_helper = AsyncMock(
            side_effect=[RuntimeError("boom"), False]
        )

        _run(app._provision_auto_repair_helpers(prov))

        assert prov.ensure_helper.call_count == 2
        assert _logs(app, "ERROR", "provision auto-repair toggle")


# ---------------------------------------------------------------------------
# Standing a pending repair down
# ---------------------------------------------------------------------------


def _pending(spec: Spec):
    """A checker counting down to a repair that has not fired yet."""
    app = spec.app(
        enabled_default=True,
        delay_default=spec.delay_max,
        states={spec.toggle: "on", spec.delay: str(spec.delay_max)},
    )
    spec.refresh(app)
    spec.arm(app, minutes_ago=1)
    spec.fire(app)
    assert spec.reported_status(app) == REPAIR_PENDING
    assert spec.actions(app) == []
    return app


class TestDisableStandsDownAPendingRepair:
    """Turning auto-repair off must clear the countdown, not just skip it.

    A ``pending`` left standing is not cosmetic. The card keeps counting down
    to "Starting repair…" forever, and ``alertmanager_bridge`` holds the
    critical page on ``pending`` for up to ``repair_hold_cap_s`` (1800 s) —
    so disabling auto-repair mid-outage would *suppress the page* for the
    outage the operator has just declared will not self-heal.
    """

    def test_the_countdown_is_visible_first(self, spec):
        """Guards the guard: a pending repair the card can actually see.

        ``device_group`` and ``fan`` keep the countdown in a global flag while
        reporting a per-device aggregate, so an aggregate that ignores the
        flag hides the countdown from both the card and the paging hold.
        """
        app = _pending(spec)

        assert app._auto_repair_deadline is not None

    def test_disabling_clears_the_pending_state(self, spec):
        app = _pending(spec)

        app.entity_states[spec.toggle] = "off"
        spec.refresh(app)
        spec.fire(app)

        assert spec.reported_status(app) == REPAIR_IDLE
        assert app._auto_repair_deadline is None

    def test_disabling_from_the_card_clears_it_too(self, spec):
        """The same stand-down through the path an operator actually uses."""
        app = _pending(spec)

        spec.command(app, auto_repair_enabled=False)
        spec.fire(app)

        assert spec.reported_status(app) == REPAIR_IDLE
        assert app._auto_repair_deadline is None

    def test_disabling_from_the_card_clears_it_without_waiting_for_a_tick(
        self, spec
    ):
        """No ``spec.fire`` here — that is the entire point of this one.

        ``_update_repair_config`` republishes the repair state the instant the
        command is applied, so whatever the card is handed at that moment is
        what the operator sees. If only the next ``_evaluate_auto_repair``
        stood the ladder down, the card would re-render a live countdown and a
        Cancel button next to a box the operator has just unchecked — and
        ``alertmanager_bridge`` would go on withholding the critical page —
        for up to a whole ``check_interval_s``. The sibling test above proves
        the eventual state; this one proves the immediate one.
        """
        app = _pending(spec)

        spec.command(app, auto_repair_enabled=False)

        assert spec.reported_status(app) == REPAIR_IDLE
        assert app._auto_repair_deadline is None


class TestTheStandDownConstantsMatchEveryChecker:
    """Eight copies of three strings, and the mixin cannot import any of them.

    Each checker owns its own ``REPAIR_*`` constants — its own test module
    imports them from there, and the mixin importing a checker would be a
    cycle (every checker imports the mixin, and the checker modules drag
    ``hassapi`` and their own ``sys.path`` bootstrap in with them). So
    ``shared/auto_repair_config.py`` repeats the three literals it needs and
    this test is what makes the duplication safe: the moment any checker's
    copy drifts, ``_stand_down_pending_repair`` would silently stop matching
    that checker's status strings and leave the countdown — and the paging
    hold — standing, with nothing else to notice.
    """

    @pytest.mark.parametrize(
        "const", ["REPAIR_IDLE", "REPAIR_PENDING", "REPAIR_SUCCESS"]
    )
    def test_the_checker_agrees_with_the_mixin(self, spec, const):
        checker = importlib.import_module(spec.module)

        assert getattr(checker, const) == getattr(mixin_mod, const)

    @pytest.mark.parametrize(
        "const,literal",
        [
            ("REPAIR_IDLE", REPAIR_IDLE),
            ("REPAIR_PENDING", REPAIR_PENDING),
            ("REPAIR_SUCCESS", REPAIR_SUCCESS),
        ],
    )
    def test_this_suite_agrees_with_the_mixin(self, const, literal):
        """Otherwise a shared drift would move both sides and pass."""
        assert getattr(mixin_mod, const) == literal


class TestTheDisabledCheckRunsBeforeTheEarlyReturns:
    """Where the toggle is read decides whether it can ever be obeyed.

    Every one of these checkers has at least one guard that returns before the
    dwell ladder is touched — parked at ``success``, budget spent, nothing
    critical this cycle. Read the toggle *after* one of those and the
    stand-down is unreachable for exactly the outages that take that return
    every cycle: the state stands for the rest of the outage, and with it the
    bridge's repair hold on the critical page.
    """

    def test_a_checker_parked_at_success_still_stands_down(self, spec):
        """``success`` is a hold state, and a stale one is the worst kind.

        The repair ran, HA looked healthy for a cycle, and then the outage
        came back. ``device`` and ``shade_gateway`` take a bare ``return`` on
        ``success`` before the ladder is touched at all; the other five reach
        the same place past guards of their own. Either way ``success`` is one
        of the two states ``alertmanager_bridge`` withholds the critical page
        on, so it is precisely the state that must not outlive the toggle
        being switched off.
        """
        app = _pending(spec)
        app._repair_status = REPAIR_SUCCESS

        app.entity_states[spec.toggle] = "off"
        spec.refresh(app)
        spec.fire(app)

        assert app._repair_status == REPAIR_IDLE
        assert spec.reported_status(app) == REPAIR_IDLE
        assert app._auto_repair_deadline is None

    def test_device_group_with_every_device_already_attempted_stands_down(self):
        """Not parametrised: only device_group has this early return.

        Its ladder is per-device on top of a global countdown. Once every
        failing device has had its one attempt, ``repairable`` is empty and
        ``_evaluate_auto_repair`` returns there on every cycle — so with the
        toggle read after that return, the *global* ``pending`` (which is the
        only thing that ever holds ``pending``, and therefore the only thing
        the card's countdown and the bridge's hold are reading) survived being
        switched off for the rest of the outage.

        The per-device ``failed`` deliberately survives the stand-down: it is
        the truth about that device and it is a state the bridge releases on,
        so the aggregate goes from ``pending`` to ``failed`` — a page, which
        is exactly what an operator who has just disabled auto-repair on a
        dead device should get.
        """
        spec = SPEC_BY_NAME["device_group"]
        app = _pending(spec)
        # Movie Room is the failing device in _GROUP_BAD; it has had its one
        # attempt and did not recover, which is what leaves `repairable` empty.
        app._device_repair_states["Movie Room"]["status"] = t_group.REPAIR_FAILED

        app.entity_states[spec.toggle] = "off"
        spec.refresh(app)
        spec.fire(app)

        assert app._repair_status == REPAIR_IDLE
        assert app._auto_repair_deadline is None
        assert spec.reported_status(app) == t_group.REPAIR_FAILED


class TestCancelRepair:
    """Every repair-capable checker must honour the card's Cancel button.

    The controller forwards ``cancel_repair`` to any checker registered with
    ``supports_repair`` (``health_check_controller._handle_cancel_repair``),
    and the card offers it for anything sitting at ``pending`` — so a checker
    without the arm accepted the tap and silently dropped it.
    """

    def test_cancel_clears_the_pending_state(self, spec):
        app = _pending(spec)

        spec.cancel(app)

        assert spec.reported_status(app) == REPAIR_IDLE
        assert app._auto_repair_deadline is None

    def test_cancel_really_defers_the_next_attempt(self, spec):
        """A bare "idle" would re-arm and fire on the very next tick.

        These checkers re-evaluate every cycle off a clock the cancel does not
        move, so cancelling has to restart the dwell (or defer past it) to be
        a deferral at all rather than a one-tick pause.

        The elapsed deadline matters as much as the elapsed dwell: ``device``
        and ``spa`` fire from their ``pending`` arm only when
        ``_auto_repair_deadline`` is set and past, so without it here the
        repair would not have run with or without the cancel and the test
        would assert nothing at all.
        """
        app = spec.app(
            enabled_default=True,
            delay_default=spec.delay_max,
            states={spec.toggle: "on", spec.delay: str(spec.delay_max)},
        )
        spec.refresh(app)
        spec.arm(app, minutes_ago=400)  # long past due
        app._repair_status = REPAIR_PENDING
        app._auto_repair_deadline = _ago(minutes=1)

        spec.cancel(app)
        spec.fire(app)

        assert spec.actions(app) == []

    def test_cancel_when_nothing_is_pending_warns(self, spec):
        app = spec.app(enabled_default=True, states={spec.toggle: "on"})
        spec.refresh(app)

        spec.cancel(app)

        assert _logs(app, "WARNING", "Cannot cancel repair")


class TestLocalRegistrationFollowsTheSeed:
    """The local copy must reflect what HA confirmed, never a guess.

    One rule, symmetric across both helpers: register the value only when the
    write that set it came back accepted. A successful seed registers what was
    seeded, so the read one line later agrees with HA and the checker is not
    inert for its first interval. A *failed* seed of either helper registers
    nothing at all — leaving the entity unreadable, which the read guard
    already handles by keeping the configured default and saying so every
    cycle. The only case that registers without a seed is a toggle whose
    configured default is off: nothing was written, so a freshly created
    input_boolean really is off, and "off" is a truth rather than a guess.
    """

    def test_a_failed_toggle_seed_registers_nothing(self, spec):
        """"off" after a failed ``turn_on`` would be a fabrication.

        The commonest failure here is a websocket TIMEOUT, where HA very
        probably DID execute the turn_on and only the reply was lost — so
        "off" is not the safe reading of a failure, it is a coin flip
        recorded as fact. Registering it caches auto-repair disabled for a
        default-enabled checker, latches ``_toggle_ever_readable`` on that
        fiction (so the next unreadable read "fails closed" on a lie), and
        logs nothing, because as far as the read path can tell the helper is
        perfectly readable.
        """
        app = spec.app(enabled_default=True)
        app.call_service = MagicMock(
            return_value=ServiceResult({"success": False, "ad_status": "TIMEOUT"})
        )

        _run(app._provision_auto_repair_helpers(_prov(True)))

        added = {c.args[0] for c in app.add_entity.call_args_list}
        assert spec.toggle not in added

        # The consequence: the helper stays unreadable, so the configured
        # default still stands and every refresh says so out loud until the
        # next plugin state refresh brings HA's real answer.
        spec.refresh(app)

        assert app._cached_auto_repair_enabled is True
        assert _logs(app, "WARNING", spec.toggle, "not readable")

    def test_failed_delay_seed_registers_nothing(self, spec):
        app = spec.app(enabled_default=False)
        app.call_service = MagicMock(
            return_value=ServiceResult({"success": False, "ad_status": "TIMEOUT"})
        )
        _run(app._provision_auto_repair_helpers(_prov(True)))
        added = {c.args[0] for c in app.add_entity.call_args_list}
        assert spec.delay not in added
        # ...and the toggle (not seeded — default off) is registered truthfully.
        assert app.add_entity.call_args_list[0].args[1] == "off"


# ---------------------------------------------------------------------------
# What the card is told
# ---------------------------------------------------------------------------


class TestPublishedRepairState:
    """``repair_state`` is the card's only source for the delay input.

    The card used to hard-code ``min=1 max=60 step=1`` on that input, which is
    right for six checkers and wrong for the shade gateway (15/360/15).  From
    the card, a spinner nudge on the shade row took 120 down to 60 (the
    browser clamps ``stepDown`` to the input's ``max``) and nothing above 60
    could be entered at all.  Nothing unsafe reached the backend — it clamps
    to the real bounds either way — but the value the operator saw was not
    the value they had.  Each checker therefore publishes its own bounds.

    The two keys that were already there keep their names, their types and
    their meaning: the card and the Shepherd runbooks read them.
    """

    def test_publishes_its_own_delay_bounds(self, spec):
        bounds = spec.app()._build_repair_state()["auto_repair_delay_bounds"]

        assert bounds == {
            "min": spec.delay_min,
            "max": spec.delay_max,
            "step": spec.step,
        }

    def test_the_two_existing_keys_are_unchanged(self, spec):
        """Same names, same types, same cached values as before the merge."""
        app = spec.app(enabled_default=True, delay_default=spec.delay_max)
        spec.refresh(app)

        state = app._build_repair_state()

        assert state["auto_repair_enabled"] is True
        assert state["auto_repair_delay_min"] == spec.delay_max
        assert isinstance(state["auto_repair_delay_min"], int)

    def test_the_two_existing_keys_track_the_cache(self, spec):
        """A card command must be visible in the next published state."""
        app = spec.app(enabled_default=False, delay_default=spec.delay_min)
        spec.refresh(app)
        wanted = min(spec.delay_min + spec.step, spec.delay_max)

        spec.command(
            app, auto_repair_enabled=True, auto_repair_delay_min=wanted
        )
        state = app._build_repair_state()

        assert state["auto_repair_enabled"] is True
        assert state["auto_repair_delay_min"] == wanted

    def test_the_published_bounds_are_the_range_the_backend_accepts(self, spec):
        """So a card rendering them can never offer a value that gets clamped."""
        app = spec.app()
        bounds = app._build_repair_state()["auto_repair_delay_bounds"]

        assert app._clamp_delay(bounds["min"]) == bounds["min"]
        assert app._clamp_delay(bounds["max"]) == bounds["max"]
        assert app._clamp_delay(bounds["min"] - 1) == bounds["min"]
        assert app._clamp_delay(bounds["max"] + 1) == bounds["max"]

    def test_no_bound_is_falsy(self, spec):
        """AppDaemon 4.5.13's ``set_state`` drops falsy attribute values.

        A ``0`` bound would vanish from the published attributes and the card
        would silently fall back to 1/60/1 — so the contract is that none of
        these is ever 0.  (None legitimately can be: a zero delay collapses
        the dwell gate, and a zero step is meaningless.)
        """
        bounds = spec.app()._build_repair_state()["auto_repair_delay_bounds"]

        assert all(bounds.values())

    def test_the_repair_state_keys_the_card_reads_are_all_present(self, spec):
        """The merge must not have dropped a key on the way in."""
        state = spec.app()._build_repair_state()

        assert {
            "status",
            "auto_repair_enabled",
            "auto_repair_delay_min",
            "auto_repair_delay_bounds",
            "auto_repair_deadline",
            "last_repair_attempt",
        } <= set(state)
