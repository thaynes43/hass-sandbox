"""Power-cycle a repair switch and confirm it came back on.

Shared by ``RepairableDeviceChecker`` and ``RepairableDeviceGroupChecker``
(``appdaemon/apps/`` may not hold shared libraries, with
``health_checks/shared/`` as the package's established exception — see
``auto_repair_config.py``).  Import it the same way::

    from shared.switch_power_cycle import power_cycle_switch, switch_not_on_detail

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
``False``: the caller ends the repair as failed and skips the recovery wait.

What counts as "on"
-------------------
Home Assistant reports the outlet late, so a read taken seconds after the
``turn_on`` can still be the ``on`` from *before* the cycle — HA has not yet
reported the ``off``.  Taking that at face value would confirm a switch whose
``turn_on`` was lost.  So an ``on`` confirms early only when its
``last_changed`` is at or after the moment the cycle started: a transition HA
registered after the ``turn_off``.  An ``on`` that has not changed since
before the cycle is accepted only at the end of the window, by when HA has had
far longer than the PDU's re-provision to report the ``off``; it means the
switch never went off at all (the ``turn_off`` was lost or coalesced), so the
device is powered but may not have been cycled — logged as a WARNING.

``call_service`` stays un-awaited, as at every other repair call site: on the
event loop AppDaemon's ``sync_decorator`` returns a Task, so a bare call still
runs (``shared/auto_repair_config.py`` ``_service_ok`` has the evidence).  An
exception raised by it propagates, and both callers already turn that into a
failed repair.
"""

from __future__ import annotations

import asyncio
import datetime
from typing import Any, Optional, Tuple

#: How long to wait for the switch to report ``on`` after each ``turn_on``.
SWITCH_CONFIRM_TIMEOUT_S = 60

#: How often to read the switch while waiting.
SWITCH_CONFIRM_POLL_S = 5


def switch_not_on_detail(switch: str) -> str:
    """The repair detail for a switch that never came back on."""
    return f"{switch} did not turn back on — check the outlet"


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _parse_changed(value: Any) -> Optional[datetime.datetime]:
    """Parse a ``last_changed`` timestamp; None when absent or not tz-aware."""
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # Cannot be compared with an aware clock — treat as "no evidence".
        return None
    return parsed


async def _read_switch(
    app: Any, switch: str
) -> Tuple[Optional[str], Optional[datetime.datetime]]:
    """Return ``(state, last_changed)``; ``(None, None)`` when unreadable."""
    try:
        full = await app.get_state(switch, attribute="all")
    except Exception as exc:
        app.log(f"Could not read {switch}: {exc!r}", level="DEBUG")
        return None, None
    if full is None:
        return None, None
    if not isinstance(full, dict):
        return str(full), None
    state = full.get("state")
    return (
        None if state is None else str(state),
        _parse_changed(full.get("last_changed")),
    )


async def _confirm_on(
    app: Any, switch: str, cycle_started: datetime.datetime
) -> Tuple[bool, Optional[str]]:
    """Wait up to ``SWITCH_CONFIRM_TIMEOUT_S`` for *switch* to report ``on``.

    Returns ``(confirmed, last_state_read)``.  See the module docstring for
    why an unchanged ``on`` only counts at the end of the window.
    """
    timeout_s = SWITCH_CONFIRM_TIMEOUT_S
    poll_s = max(1, SWITCH_CONFIRM_POLL_S)
    waited = 0
    state: Optional[str] = None
    while waited < timeout_s:
        await asyncio.sleep(poll_s)
        waited += poll_s
        state, changed = await _read_switch(app, switch)
        if state == "on" and changed is not None and changed >= cycle_started:
            app.log(f"{switch} confirmed on after {waited}s", level="INFO")
            return True, state
    if state == "on":
        app.log(
            f"{switch} reads on but has not changed since before the power "
            f"cycle — Home Assistant never saw it go off, so the device may "
            f"not have been power cycled",
            level="WARNING",
        )
        return True, state
    return False, state


async def power_cycle_switch(
    app: Any, switch: str, off_duration_s: float, target: str = ""
) -> bool:
    """Turn *switch* off, wait, turn it back on and confirm it is on.

    *app* is the calling AppDaemon app (for ``call_service``, ``get_state``
    and ``log``); *target* names the device in log lines when one switch is
    one of several (the device group checker).

    Returns True once the switch is confirmed on — the caller may start its
    recovery wait.  Returns False when it is still not on after a second
    ``turn_on``; the ERROR is already logged, and the caller must end the
    repair as failed without waiting for recovery.
    """
    label = f"{switch} for {target}" if target else switch
    cycle_started = _utcnow()

    app.log(f"Turning off {label}", level="INFO")
    app.call_service("switch/turn_off", entity_id=switch)

    await asyncio.sleep(off_duration_s)

    app.log(f"Turning on {label}", level="INFO")
    app.call_service("switch/turn_on", entity_id=switch)
    confirmed, state = await _confirm_on(app, switch, cycle_started)
    if confirmed:
        return True

    app.log(
        f"{label} did not report on within {SWITCH_CONFIRM_TIMEOUT_S}s "
        f"(state: {state!r}) — turning it on again",
        level="WARNING",
    )
    app.call_service("switch/turn_on", entity_id=switch)
    confirmed, state = await _confirm_on(app, switch, cycle_started)
    if confirmed:
        return True

    missing = (
        " — the entity is missing; reload the integration that owns it"
        if state is None
        else ""
    )
    app.log(
        f"{label} still not on after a second turn_on (state: {state!r})"
        f"{missing} — ending the repair without waiting for recovery",
        level="ERROR",
    )
    return False
