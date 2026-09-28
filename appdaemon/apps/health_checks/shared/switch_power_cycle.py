"""Power-cycle a repair switch and confirm it came back on.

Shared by ``RepairableDeviceChecker``, ``RepairableDeviceGroupChecker`` and
``SpaHealthChecker``
(``appdaemon/apps/`` may not hold shared libraries, with
``health_checks/shared/`` as the package's established exception — see
``auto_repair_config.py``).  Import it the same way::

    from shared.switch_power_cycle import power_cycle_switch, switch_not_on_detail

    result = await power_cycle_switch(self, switch, off_duration_s)
    if not result.switch_on:
        ...  # fail the repair now, no recovery wait
    ...      # recovery wait; on failure append f" ({result.note})" if any

Why the turn-on is confirmed
----------------------------
The repair used to be fire-and-forget: ``switch/turn_off``, sleep,
``switch/turn_on``, then straight into the recovery wait.  Nothing checked
that the switch really came back on, and on a UniFi USP PDU outlet that is not
safe (seen on 2026-09-28 with the Movie Room Sonos Port on outlet 21):

* toggling an outlet re-provisions the whole PDU for about 40 s;
* Home Assistant took more than 10 s to report the outlet ``on`` after
  ``switch.turn_on``;
* once, around a re-provision, the unifi integration dropped every outlet
  entity of that PDU until the integration was reloaded.

So a ``turn_on`` can be lost, and the old repair then left the device
unpowered — through its recovery wait and after it, since a failed repair is
not retried — reporting only "did not recover", with nothing saying the outlet
was the reason.

What this does
--------------
Turn off, wait ``off_duration_s``, turn on, then read the switch every
``SWITCH_CONFIRM_POLL_S`` for up to ``SWITCH_CONFIRM_TIMEOUT_S``.  If it is not
on, log a WARNING and ``turn_on`` once more, then confirm again for the same
window.  Still not on (the entity missing included) → log an ERROR and return
``switch_on=False``: the caller ends the repair as failed and skips the
recovery wait.

What counts as "on"
-------------------
Home Assistant reports the outlet late, so a read taken seconds after the
``turn_on`` can still be the ``on`` from *before* the cycle — HA has not yet
reported the ``off``.  Taking that at face value would confirm a switch whose
``turn_on`` was lost.  So an ``on`` confirms early only when its
``last_changed`` is at or after the moment the cycle started: a transition HA
registered after the ``turn_off``.  An ``on`` that has not changed since
before the cycle is accepted only at the end of the window, by when HA has had
far longer than the PDU's re-provision to report the ``off``; it means HA
never saw the switch go off (the ``turn_off`` was lost or coalesced, or HA
simply missed the ``off``).  The device is powered, so that is not a failure,
but it may not have been cycled: the result carries ``NEVER_OFF_NOTE``, which
the callers append to the detail if the recovery wait then fails, so the card
and the Alertmanager description say it.

``call_service`` stays un-awaited, as at every other repair call site: on the
event loop AppDaemon's ``sync_decorator`` returns a Task, so a bare call still
runs (``shared/auto_repair_config.py`` ``_service_ok`` has the evidence).  An
exception raised by it propagates, and both callers already turn that into a
failed repair.
"""

from __future__ import annotations

import asyncio
import datetime
from dataclasses import dataclass
from typing import Any, Optional, Tuple

#: How long to wait for the switch to report ``on`` after each ``turn_on``.
SWITCH_CONFIRM_TIMEOUT_S = 60

#: How often to read the switch while waiting.
SWITCH_CONFIRM_POLL_S = 5

#: Carried by a result whose ``on`` was never preceded by a reported ``off``.
NEVER_OFF_NOTE = "the outlet never reported off — it may not have been power cycled"


@dataclass(frozen=True)
class PowerCycleResult:
    """What :func:`power_cycle_switch` observed.

    ``switch_on`` — the switch is confirmed on; the caller may start its
    recovery wait.  False means it is still not on after a second
    ``turn_on``: fail the repair now.
    ``note`` — something a human should know if the repair then fails (empty
    when there is nothing to add).  Never a failure by itself.
    """

    switch_on: bool
    note: str = ""


#: Carried by a failed result whose switch could not be read at all (get_state
#: raised — most likely the AppDaemon↔HA plugin was disconnected, which also
#: drops the un-awaited turn_off/turn_on). Not "the entity is missing".
READ_ERROR_NOTE = "the switch could not be read — see the AppDaemon log"

#: Carried by a failed result whose switch entity was missing from Home
#: Assistant (the unifi integration dropping a PDU's outlet entities): the
#: fix is reloading the integration, not the outlet.
MISSING_ENTITY_NOTE = (
    "the entity is missing from Home Assistant — reload the integration "
    "that owns it"
)


def switch_not_on_detail(switch: str, note: str = "") -> str:
    """The repair detail for a switch that never came back on.

    *note* is the failed :class:`PowerCycleResult`'s note:
    ``MISSING_ENTITY_NOTE`` or ``READ_ERROR_NOTE`` replaces "check the
    outlet", which would send the operator to the wrong place for either.
    """
    return f"{switch} did not turn back on — {note or 'check the outlet'}"


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _parse_changed(value: Any) -> Optional[datetime.datetime]:
    """Parse a ``last_changed`` into an aware UTC datetime; None when unusable.

    Same shape as ``protect_health_checker._parse_iso_utc``, which reads this
    same attribute off the same ``get_state(attribute="all")`` dict: AppDaemon
    can hand it back as a ``datetime`` rather than a string, and a naive
    timestamp from Home Assistant is UTC.  Dropping either would make every
    cycle miss the early confirm and end on the unchanged-``on`` path.
    """
    if isinstance(value, datetime.datetime):
        parsed = value
    elif value:
        try:
            parsed = datetime.datetime.fromisoformat(str(value))
        except (ValueError, TypeError):
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


async def _read_switch(
    app: Any, switch: str
) -> Tuple[Optional[str], Optional[datetime.datetime], bool]:
    """Return ``(state, last_changed, read_ok)``.

    ``state`` is None both when the entity is missing (``read_ok`` True) and
    when the read raised (``read_ok`` False) — only the first means "reload
    the integration that owns it".
    """
    try:
        full = await app.get_state(switch, attribute="all")
    except Exception as exc:
        # WARNING, not DEBUG: most likely the HASS plugin is disconnected (the
        # un-awaited turn_off/turn_on were dropped too), and this is the only
        # record of it.
        app.log(f"Could not read {switch}: {exc!r}", level="WARNING")
        return None, None, False
    if full is None:
        return None, None, True
    if not isinstance(full, dict):
        return str(full), None, True
    state = full.get("state")
    return (
        None if state is None else str(state),
        _parse_changed(full.get("last_changed")),
        True,
    )


async def _confirm_on(
    app: Any,
    switch: str,
    cycle_started: datetime.datetime,
    saw_off: bool = False,
) -> Tuple[bool, Optional[str], bool, bool, bool]:
    """Wait up to ``SWITCH_CONFIRM_TIMEOUT_S`` for *switch* to report ``on``.

    Returns ``(confirmed, last_state_read, never_off, saw_off, read_ok)``;
    ``read_ok`` is False when the last read raised.  An ``on``
    confirms early when its ``last_changed`` is after the cycle started, or
    when an ``off`` was read earlier in this cycle (*saw_off*, carried across
    both windows): either proves HA registered the cycle, whatever the
    timestamp says.  ``never_off`` is True when the confirmation was an ``on``
    with neither proof, accepted only at the end of the window — see the
    module docstring.
    """
    timeout_s = SWITCH_CONFIRM_TIMEOUT_S
    poll_s = max(1, SWITCH_CONFIRM_POLL_S)
    waited = 0
    state: Optional[str] = None
    read_ok = True
    while waited < timeout_s:
        await asyncio.sleep(poll_s)
        waited += poll_s
        state, changed, read_ok = await _read_switch(app, switch)
        if state == "off":
            saw_off = True
        fresh = changed is not None and changed >= cycle_started
        if state == "on" and (fresh or saw_off):
            app.log(f"{switch} confirmed on after {waited}s", level="INFO")
            return True, state, False, saw_off, read_ok
    if state == "on":
        # Not a failure (the device is powered, and the outlet may still have
        # cycled while HA missed the `off`), so not an ERROR; WARNING because
        # an `off` HA never reported is an unexpected-but-recoverable
        # condition, and on a *successful* repair this log line is the only
        # record — the note only reaches the detail if recovery then fails.
        app.log(
            f"{switch} reads on but has not changed since before the power "
            f"cycle — Home Assistant never saw it go off; accepting it as on",
            level="WARNING",
        )
        return True, state, True, saw_off, read_ok
    return False, state, False, saw_off, read_ok


async def power_cycle_switch(
    app: Any, switch: str, off_duration_s: float, target: str = ""
) -> PowerCycleResult:
    """Turn *switch* off, wait, turn it back on and confirm it is on.

    *app* is the calling AppDaemon app (for ``call_service``, ``get_state``
    and ``log``); *target* names the device in log lines when one switch is
    one of several (the device group checker).

    ``result.switch_on`` is True once the switch is confirmed on — the caller
    may start its recovery wait — and False when it is still not on after a
    second ``turn_on``; the ERROR is already logged, and the caller must end
    the repair as failed without waiting for recovery.  ``result.note``:
    with ``switch_on`` True, ``NEVER_OFF_NOTE`` when the ``on`` was never
    preceded by a reported ``off`` (else empty); with ``switch_on`` False,
    ``READ_ERROR_NOTE`` when the last read raised, ``MISSING_ENTITY_NOTE``
    when the entity is absent, else empty (the outlet itself).
    """
    label = f"{switch} for {target}" if target else switch
    cycle_started = _utcnow()

    app.log(f"Turning off {label}", level="INFO")
    app.call_service("switch/turn_off", entity_id=switch)

    await asyncio.sleep(off_duration_s)

    app.log(f"Turning on {label}", level="INFO")
    app.call_service("switch/turn_on", entity_id=switch)
    confirmed, state, never_off, saw_off, read_ok = await _confirm_on(
        app, switch, cycle_started
    )
    if confirmed:
        return _confirmed(never_off)

    app.log(
        f"{label} did not report on within {SWITCH_CONFIRM_TIMEOUT_S}s "
        f"(state: {state!r}) — turning it on again",
        level="WARNING",
    )
    app.call_service("switch/turn_on", entity_id=switch)
    confirmed, state, never_off, saw_off, read_ok = await _confirm_on(
        app, switch, cycle_started, saw_off
    )
    if confirmed:
        return _confirmed(never_off)

    if not read_ok:
        note = READ_ERROR_NOTE
    elif state is None:
        note = MISSING_ENTITY_NOTE
    else:
        note = ""
    app.log(
        f"{label} still not on after a second turn_on (state: {state!r})"
        f"{' — ' + note if note else ''} — ending the repair without waiting "
        f"for recovery",
        level="ERROR",
    )
    return PowerCycleResult(switch_on=False, note=note)


def _confirmed(never_off: bool) -> PowerCycleResult:
    return PowerCycleResult(
        switch_on=True, note=NEVER_OFF_NOTE if never_off else ""
    )


def with_note(detail: str, note: str) -> str:
    """*detail* with the power cycle's note appended, if it has one."""
    return f"{detail} ({note})" if note else detail
