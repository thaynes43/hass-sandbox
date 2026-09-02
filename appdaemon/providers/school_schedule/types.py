"""Data types shared by the school schedule providers.

Two independent sources feed one sensor:

* :class:`DayCycle` — the school's public Finalsite calendar, which says which
  rotation day (1..6) each calendar date is, plus no-school and early-release
  annotations.
* :class:`WeeklySchedule` — the PowerSchool guardian portal, which says which
  classes the student actually has, per date and per rotation day.

Both are plain dataclasses with no AppDaemon or HTTP dependency so the app can
serialise them into sensor attributes and the tests can build them by hand.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class ClassBlock:
    """One scheduled class period.

    ``start``/``end`` are 24-hour ``HH:MM`` strings in school-local time, or
    ``""`` when the source view does not carry clock times (the list view is
    per rotation day, not per date, so it has none).
    """

    period: str = ""
    course: str = ""
    teacher: str = ""
    room: str = ""
    start: str = ""
    end: str = ""

    def as_dict(self) -> Dict[str, str]:
        """Serialise for sensor attributes, dropping empty fields.

        AppDaemon's ``set_state`` drops falsy attribute values anyway, so empty
        strings only bloat the payload — the 64 KB attribute budget is real.
        """
        out = {
            "course": self.course,
            "period": self.period,
            "start": self.start,
            "end": self.end,
            "teacher": self.teacher,
            "room": self.room,
        }
        return {k: v for k, v in out.items() if v}


@dataclass
class DayCycle:
    """The school calendar's view of the year.

    ``dates``, ``closures`` and ``notes`` are keyed by ISO ``YYYY-MM-DD`` so
    they drop straight into sensor attributes.  A date never appears in both
    ``dates`` and ``closures``.
    """

    school_name: str = ""
    cycle_length: int = 6
    dates: Dict[str, int] = field(default_factory=dict)
    closures: Dict[str, str] = field(default_factory=dict)
    notes: Dict[str, str] = field(default_factory=dict)
    source: str = ""  # "ics" or "html" — which fetch path produced this

    def __bool__(self) -> bool:
        return bool(self.dates)


@dataclass
class WeeklySchedule:
    """The student's classes, as scraped from the PowerSchool guardian portal.

    ``days`` is the authoritative per-date view (the bell-schedule grid already
    resolves rotation, terms and holidays server-side).  ``cycle`` is the
    per-rotation-day fallback derived from the list view, used for dates beyond
    the fetched window.
    """

    days: Dict[str, List[ClassBlock]] = field(default_factory=dict)
    cycle: Dict[int, List[ClassBlock]] = field(default_factory=dict)
    first_day: str = ""  # school year bounds reported by the portal (ISO)
    last_day: str = ""
    school_name: str = ""
    student_id: str = ""

    def __bool__(self) -> bool:
        return bool(self.days or self.cycle)


@dataclass
class ScheduleRow:
    """One row of the PowerSchool list view (``myschedule.html``)."""

    exp: str = ""
    term: str = ""
    course_section: str = ""
    course: str = ""
    teacher: str = ""
    room: str = ""
    enroll: str = ""  # MM/DD/YYYY as rendered
    leave: str = ""
    periods: List[Any] = field(default_factory=list)  # list[tuple[str, list[int]]]

    def as_tuple(self) -> tuple:
        return (
            self.exp,
            self.term,
            self.course_section,
            self.course,
            self.teacher,
            self.room,
            self.enroll,
            self.leave,
        )


@dataclass
class Student:
    """A student on the guardian account."""

    student_id: str
    school: str = ""


@dataclass
class CalendarElement:
    """The Finalsite calendar element discovered on the "all events" page."""

    element_id: str = ""
    calendar_ids: str = ""
    feed_uuid: str = ""
    school_name: str = ""
    base_url: str = ""

    def __bool__(self) -> bool:
        return bool(self.calendar_ids or self.feed_uuid)


__all__ = [
    "CalendarElement",
    "ClassBlock",
    "DayCycle",
    "ScheduleRow",
    "Student",
    "WeeklySchedule",
]
