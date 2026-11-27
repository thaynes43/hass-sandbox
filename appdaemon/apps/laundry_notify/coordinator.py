"""
Pure washer/dryer completion logic for laundry_notify.

No AppDaemon dependency: the app feeds state changes in and acts on the returned
actions, so every combining path is unit-testable.

Rules (Tom, 2026-11-27):

- Washer finishes while the dryer is running: hold, then send one combined
  "Washer & dryer complete" when the dryer finishes. A wet load can't move until
  the dryer is free, so a washer-only push has no value then.
- Dryer finishes while the washer is running: send "Dryer complete" right away
  (``dryer_first_policy: notify_now``, the default) so it can be emptied, or hold
  for one combined push (``wait_for_washer``).
- Only one machine running: that machine's own notification.
- Washer reminder: if the washer is still sitting in ``end`` a configurable time
  after a notification that told someone it was done, send a reminder that the
  wet clothes still need switching. The washer leaving ``end`` (door opened, it
  powers off, or a new cycle starts) cancels it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

WASHER = "washer"
DRYER = "dryer"

END_STATE = "end"

# States that are not a cycle in progress. Everything else LG ThinQ reports
# (running, rinsing, spinning, drying, cooling, wrinkle_care, pause, ...) counts
# as active, so a new LG state defaults to "running" rather than being missed.
IDLE_STATES = frozenset({"power_off", END_STATE, "initial", "error", "reserved"})

# Connectivity noise: ignored entirely so running -> unavailable -> end is still
# seen as running -> end, and unavailable -> end after a restart is not a finish.
IGNORED_STATES = frozenset({"unavailable", "unknown", "none", ""})

DONE_ALIASES = frozenset({"wrinkle_care"})

POLICY_NOTIFY_NOW = "notify_now"
POLICY_WAIT_FOR_WASHER = "wait_for_washer"
POLICIES = (POLICY_NOTIFY_NOW, POLICY_WAIT_FOR_WASHER)

# Notification kinds
WASHER_COMPLETE = "washer_complete"
DRYER_COMPLETE = "dryer_complete"
BOTH_COMPLETE = "both_complete"
WASHER_REMINDER = "washer_reminder"


@dataclass(frozen=True)
class Action:
    """Something the app must do. ``kind`` is notify / arm_reminder / cancel_reminder."""

    kind: str
    notification: Optional[str] = None


def is_active(state: Optional[str]) -> bool:
    if state is None:
        return False
    s = str(state).strip().lower()
    return s not in IDLE_STATES and s not in IGNORED_STATES


def normalize(state: Optional[str]) -> Optional[str]:
    """Return the lower-cased state, or None for connectivity noise."""
    if state is None:
        return None
    s = str(state).strip().lower()
    if s in IGNORED_STATES:
        return None
    # The dryer's wrinkle-care tumble runs after the load is dry: treat it as
    # done, so cooling -> wrinkle_care is the finish and wrinkle_care <-> end
    # is not a second one.
    if s in DONE_ALIASES:
        return END_STATE
    return s


@dataclass
class LaundryCoordinator:
    dryer_first_policy: str = POLICY_NOTIFY_NOW
    last_state: dict = field(default_factory=lambda: {WASHER: None, DRYER: None})
    holding_washer: bool = False  # washer done, waiting for the dryer
    holding_dryer: bool = False  # dryer done, waiting for the washer (wait policy)
    reminder_armed: bool = False

    def __post_init__(self) -> None:
        if self.dryer_first_policy not in POLICIES:
            raise ValueError(
                f"dryer_first_policy must be one of {POLICIES}, got {self.dryer_first_policy!r}"
            )

    # ------------------------------------------------------------------ seeding
    def seed(self, washer_state: Optional[str], dryer_state: Optional[str]) -> None:
        """Record current states at startup without emitting notifications."""
        self.last_state[WASHER] = normalize(washer_state)
        self.last_state[DRYER] = normalize(dryer_state)

    # ------------------------------------------------------------------ updates
    def update(self, machine: str, new_state: Optional[str]) -> list[Action]:
        if machine not in (WASHER, DRYER):
            raise ValueError(f"unknown machine {machine!r}")
        new = normalize(new_state)
        if new is None:
            return []
        old = self.last_state.get(machine)
        if new == old:
            return []
        self.last_state[machine] = new

        if machine == WASHER:
            return self._washer_changed(old, new)
        return self._dryer_changed(old, new)

    def _other_active(self, machine: str) -> bool:
        other = DRYER if machine == WASHER else WASHER
        return is_active(self.last_state.get(other))

    def _washer_changed(self, old: Optional[str], new: str) -> list[Action]:
        actions: list[Action] = []

        # Washer left "end": someone opened it / it powered off / new cycle.
        if old == END_STATE:
            self.holding_washer = False
            if self.reminder_armed:
                self.reminder_armed = False
                actions.append(Action("cancel_reminder"))

        finished = new == END_STATE and is_active(old)
        stopped = is_active(old) and not is_active(new) and not finished

        if finished:
            if self.holding_dryer:
                self.holding_dryer = False
                actions += self._notify_with_washer(BOTH_COMPLETE)
            elif self._other_active(WASHER):
                self.holding_washer = True
            else:
                actions += self._notify_with_washer(WASHER_COMPLETE)
        elif stopped and self.holding_dryer:
            # Washer cycle cancelled while the dryer push was held: release it.
            self.holding_dryer = False
            actions.append(Action("notify", DRYER_COMPLETE))
        return actions

    def _dryer_changed(self, old: Optional[str], new: str) -> list[Action]:
        actions: list[Action] = []

        # Dryer left "end": it was emptied (or a new cycle started).
        if old == END_STATE:
            self.holding_dryer = False

        finished = new == END_STATE and is_active(old)
        stopped = is_active(old) and not is_active(new) and not finished

        if finished:
            if self.holding_washer:
                self.holding_washer = False
                actions += self._notify_with_washer(BOTH_COMPLETE)
            elif self._other_active(DRYER) and self.dryer_first_policy == POLICY_WAIT_FOR_WASHER:
                self.holding_dryer = True
            else:
                actions.append(Action("notify", DRYER_COMPLETE))
        elif stopped and self.holding_washer:
            # Dryer cycle cancelled while the washer push was held: release it.
            self.holding_washer = False
            actions += self._notify_with_washer(WASHER_COMPLETE)
        return actions

    def _notify_with_washer(self, notification: str) -> list[Action]:
        """Notify, then arm the reminder if the washer is still holding wet clothes."""
        actions = [Action("notify", notification)]
        if self.last_state.get(WASHER) == END_STATE:
            self.reminder_armed = True
            actions.append(Action("arm_reminder"))
        return actions

    # ------------------------------------------------------------------ reminder
    def reminder_due(self) -> list[Action]:
        """Called when the reminder timer fires."""
        if not self.reminder_armed:
            return []
        self.reminder_armed = False
        if self.last_state.get(WASHER) != END_STATE:
            return []
        return [Action("notify", WASHER_REMINDER)]

    def restore_reminder(self) -> bool:
        """At startup: re-arm the reminder if the washer is sitting in ``end``."""
        if self.last_state.get(WASHER) == END_STATE:
            self.reminder_armed = True
            return True
        return False
