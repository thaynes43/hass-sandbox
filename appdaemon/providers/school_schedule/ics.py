"""Minimal stdlib iCalendar (RFC 5545) reader for school event feeds.

Deliberately tiny: the Finalsite calendar feed we consume only uses all-day
``VEVENT`` entries with an optional ``RRULE:FREQ=DAILY`` and ``EXDATE``.  A full
iCalendar library (``icalendar``, ``dateutil.rrule``) is not in the AppDaemon
image, so this module implements just enough of the spec to expand that feed:

* line unfolding (RFC 5545 §3.1)
* ``NAME;PARAM=VALUE:value`` property parsing, quoted params honoured
* ``DTSTART;VALUE=DATE`` (floating all-day) and ``DTSTART;TZID=...`` (local
  wall-clock) — neither is ever routed through UTC, so a date never shifts
* ``SUMMARY`` text unescaping (``\\,`` ``\\;`` ``\\\\`` ``\\n``)
* ``RRULE:FREQ=DAILY`` with ``INTERVAL`` / ``UNTIL`` (inclusive) / ``COUNT``
* ``EXDATE`` (repeatable, comma-separated)

Anything else (weekly/monthly recurrence, ``BYDAY``, ``RDATE``) is logged at
WARNING and degraded to a single occurrence rather than raising: a school adding
an exotic event must never take the whole schedule sensor down.

``VTIMEZONE`` blocks are skipped entirely — they carry their own ``DTSTART`` and
``RRULE`` properties which would otherwise be mistaken for events.
"""

from __future__ import annotations

import datetime
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set

logger = logging.getLogger(__name__)

# Hard cap on generated occurrences per event — a malformed UNTIL must not spin.
MAX_OCCURRENCES = 1000


@dataclass
class IcsEvent:
    """A single ``VEVENT``, before recurrence expansion."""

    uid: str = ""
    summary: str = ""
    start: Optional[datetime.date] = None
    all_day: bool = True
    rrule: Dict[str, str] = field(default_factory=dict)
    exdates: Set[datetime.date] = field(default_factory=set)


# ---------------------------------------------------------------------------
# Lexing
# ---------------------------------------------------------------------------

def unfold(text: str) -> List[str]:
    """Undo RFC 5545 line folding.

    Continuation lines begin with a single space or tab; that character is
    stripped and the remainder appended to the previous logical line.
    """
    lines: List[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def split_property(line: str) -> tuple[str, Dict[str, str], str]:
    """Split ``NAME;PARAM=VALUE:value`` into ``(name, params, value)``.

    The value separator is the first unquoted ``:`` — property values routinely
    contain colons (URLs in ``DESCRIPTION``), and parameter values may be
    double-quoted and contain colons too.
    """
    in_quotes = False
    for idx, char in enumerate(line):
        if char == '"':
            in_quotes = not in_quotes
        elif char == ":" and not in_quotes:
            head, value = line[:idx], line[idx + 1:]
            break
    else:
        return line.upper(), {}, ""

    parts = head.split(";")
    name = parts[0].strip().upper()
    params: Dict[str, str] = {}
    for part in parts[1:]:
        if "=" not in part:
            continue
        key, _, val = part.partition("=")
        params[key.strip().upper()] = val.strip().strip('"')
    return name, params, value


def unescape_text(value: str) -> str:
    """Unescape an iCalendar TEXT value (``\\,`` ``\\;`` ``\\\\`` ``\\n``)."""
    out: List[str] = []
    idx = 0
    while idx < len(value):
        char = value[idx]
        if char == "\\" and idx + 1 < len(value):
            nxt = value[idx + 1]
            if nxt in ("n", "N"):
                out.append("\n")
            else:
                out.append(nxt)
            idx += 2
            continue
        out.append(char)
        idx += 1
    return "".join(out).strip()


_DATE_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})")


def parse_date_value(value: str) -> Optional[datetime.date]:
    """Read the calendar date out of a DATE or DATE-TIME value.

    ``20260901`` and ``20260901T080000`` both yield ``date(2026, 9, 1)``.  A
    trailing ``Z`` is intentionally ignored: the feed emits local wall-clock
    times with a ``TZID`` and converting through UTC is exactly how an all-day
    event slides onto the wrong day.
    """
    match = _DATE_RE.match(value.strip())
    if not match:
        return None
    try:
        return datetime.date(int(match[1]), int(match[2]), int(match[3]))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_events(text: str) -> List[IcsEvent]:
    """Parse every ``VEVENT`` in an iCalendar document.

    ``VTIMEZONE`` blocks (and their nested ``STANDARD`` / ``DAYLIGHT``
    sub-components) are skipped so their ``DTSTART``/``RRULE`` never leak in.
    Events without a usable ``DTSTART`` are dropped with a WARNING.
    """
    events: List[IcsEvent] = []
    current: Optional[IcsEvent] = None
    in_timezone = False

    for line in unfold(text):
        if not line.strip():
            continue
        name, params, value = split_property(line)

        if name == "BEGIN":
            token = value.strip().upper()
            if token == "VTIMEZONE":
                in_timezone = True
            elif token == "VEVENT" and not in_timezone:
                current = IcsEvent()
            continue

        if name == "END":
            token = value.strip().upper()
            if token == "VTIMEZONE":
                in_timezone = False
            elif token == "VEVENT" and current is not None:
                if current.start is None:
                    logger.warning(
                        "Skipping VEVENT with no usable DTSTART (uid=%s, summary=%r)",
                        current.uid or "<none>",
                        current.summary,
                    )
                else:
                    events.append(current)
                current = None
            continue

        if in_timezone or current is None:
            continue

        if name == "UID":
            current.uid = value.strip()
        elif name == "SUMMARY":
            current.summary = unescape_text(value)
        elif name == "DTSTART":
            current.start = parse_date_value(value)
            current.all_day = params.get("VALUE", "").upper() == "DATE"
        elif name == "RRULE":
            current.rrule = _parse_rrule(value)
        elif name == "EXDATE":
            for chunk in value.split(","):
                exdate = parse_date_value(chunk)
                if exdate is not None:
                    current.exdates.add(exdate)

    if current is not None:
        logger.warning("iCalendar document ended inside a VEVENT — dropping it")

    return events


def _parse_rrule(value: str) -> Dict[str, str]:
    rule: Dict[str, str] = {}
    for part in value.split(";"):
        key, _, val = part.partition("=")
        key = key.strip().upper()
        if key:
            rule[key] = val.strip()
    return rule


# ---------------------------------------------------------------------------
# Expansion
# ---------------------------------------------------------------------------

def expand(event: IcsEvent) -> List[datetime.date]:
    """Expand an event to the list of dates it occurs on.

    Only ``FREQ=DAILY`` is expanded.  Any other ``FREQ``, or a ``BYxxx`` part we
    do not model, degrades to the single ``DTSTART`` occurrence with a WARNING —
    a partially-understood rule must never silently invent dates.
    """
    if event.start is None:
        return []

    rule = event.rrule
    if not rule:
        return [event.start]

    freq = rule.get("FREQ", "").upper()
    unsupported = [k for k in rule if k.startswith("BY")]
    if freq != "DAILY" or unsupported:
        logger.warning(
            "Unsupported RRULE %r on event %r — using the single DTSTART occurrence",
            rule,
            event.summary,
        )
        return [event.start]

    try:
        interval = max(1, int(rule.get("INTERVAL", "1")))
    except ValueError:
        logger.warning(
            "Bad RRULE INTERVAL %r on event %r — defaulting to 1",
            rule.get("INTERVAL"),
            event.summary,
        )
        interval = 1

    until = parse_date_value(rule["UNTIL"]) if rule.get("UNTIL") else None
    if rule.get("UNTIL") and until is None:
        logger.warning(
            "Bad RRULE UNTIL %r on event %r — using the single DTSTART occurrence",
            rule.get("UNTIL"),
            event.summary,
        )
        return [event.start]

    count: Optional[int] = None
    if rule.get("COUNT"):
        try:
            count = max(1, int(rule["COUNT"]))
        except ValueError:
            logger.warning(
                "Bad RRULE COUNT %r on event %r — ignoring it",
                rule.get("COUNT"),
                event.summary,
            )

    if until is None and count is None:
        logger.warning(
            "Unbounded RRULE on event %r — using the single DTSTART occurrence",
            event.summary,
        )
        return [event.start]

    dates: List[datetime.date] = []
    cursor = event.start
    step = datetime.timedelta(days=interval)
    generated = 0
    while generated < MAX_OCCURRENCES:
        if until is not None and cursor > until:
            break
        if cursor not in event.exdates:
            dates.append(cursor)
        generated += 1
        if count is not None and generated >= count:
            break
        cursor += step
    else:
        logger.warning(
            "RRULE on event %r hit the %d-occurrence cap — truncating",
            event.summary,
            MAX_OCCURRENCES,
        )

    return dates


def events_by_date(events: Iterable[IcsEvent]) -> Dict[datetime.date, List[str]]:
    """Expand events into ``{date: [summary, ...]}``, deduped, insertion ordered."""
    by_date: Dict[datetime.date, List[str]] = {}
    for event in events:
        if not event.summary:
            continue
        for occurrence in expand(event):
            bucket = by_date.setdefault(occurrence, [])
            if event.summary not in bucket:
                bucket.append(event.summary)
    return dict(sorted(by_date.items()))


def parse_events_by_date(text: str) -> Dict[datetime.date, List[str]]:
    """Convenience: parse an iCalendar document straight to ``{date: summaries}``."""
    return events_by_date(parse_events(text))
