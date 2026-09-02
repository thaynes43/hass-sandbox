"""Unit tests for the minimal iCalendar reader used by school_schedule.

Offline: everything runs against the sanitized fixture feed in
``tests/fixtures/school_schedule/``.
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path

import pytest

_repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo_root / "apps"))
sys.path.insert(0, str(_repo_root))

from providers.school_schedule import ics

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "school_schedule"


@pytest.fixture(scope="module")
def feed_text() -> str:
    return (FIXTURES / "events.ics").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Lexing
# ---------------------------------------------------------------------------

def test_unfold_joins_continuation_lines():
    text = "SUMMARY:A very long\r\n  summary line\r\nUID:abc\r\n"
    assert ics.unfold(text) == ["SUMMARY:A very long summary line", "UID:abc", ""]


def test_unfold_accepts_tab_continuations_and_bare_lf():
    text = "SUMMARY:one\n\ttwo\nUID:x"
    assert ics.unfold(text) == ["SUMMARY:onetwo", "UID:x"]


def test_split_property_parses_params():
    name, params, value = ics.split_property("DTSTART;VALUE=DATE:20260901")
    assert (name, params, value) == ("DTSTART", {"VALUE": "DATE"}, "20260901")


def test_split_property_ignores_colons_inside_quoted_params():
    name, params, value = ics.split_property(
        'DTSTART;TZID="America/New_York":20260901T080000'
    )
    assert name == "DTSTART"
    assert params["TZID"] == "America/New_York"
    assert value == "20260901T080000"


def test_split_property_keeps_colons_in_the_value():
    _, _, value = ics.split_property("DESCRIPTION:see https://example.org/a:b")
    assert value == "see https://example.org/a:b"


def test_split_property_handles_a_line_with_no_colon():
    assert ics.split_property("GARBAGE") == ("GARBAGE", {}, "")


@pytest.mark.parametrize(
    "raw,expected",
    [
        (r"Full Cast\, Choreo", "Full Cast, Choreo"),
        (r"A\;B", "A;B"),
        (r"back\\slash", "back\\slash"),
        (r"line\none", "line\none"),
    ],
)
def test_unescape_text(raw, expected):
    assert ics.unescape_text(raw) == expected


def test_parse_date_value_handles_date_and_datetime():
    assert ics.parse_date_value("20260901") == datetime.date(2026, 9, 1)
    assert ics.parse_date_value("20260901T080000") == datetime.date(2026, 9, 1)
    # A trailing Z must not shift the calendar day.
    assert ics.parse_date_value("20260901T000000Z") == datetime.date(2026, 9, 1)
    assert ics.parse_date_value("nonsense") is None
    assert ics.parse_date_value("20261301") is None


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

MINIMAL = """\
BEGIN:VCALENDAR
BEGIN:VTIMEZONE
TZID:America/New_York
BEGIN:DAYLIGHT
DTSTART:20260308T030000
RRULE:FREQ=YEARLY;BYDAY=2SU;BYMONTH=3
END:DAYLIGHT
END:VTIMEZONE
BEGIN:VEVENT
UID:one
DTSTART;VALUE=DATE:20260901
SUMMARY:Day 1
END:VEVENT
END:VCALENDAR
"""


def test_parse_events_skips_vtimezone_blocks():
    events = ics.parse_events(MINIMAL)
    assert [e.uid for e in events] == ["one"]
    assert events[0].summary == "Day 1"
    assert events[0].all_day is True
    assert events[0].rrule == {}


def test_parse_events_drops_events_without_dtstart(caplog):
    text = "BEGIN:VEVENT\nUID:x\nSUMMARY:No start\nEND:VEVENT\n"
    with caplog.at_level("WARNING"):
        assert ics.parse_events(text) == []
    assert "no usable DTSTART" in caplog.text


def test_parse_events_reads_timed_events_without_crashing():
    text = (
        "BEGIN:VEVENT\nUID:t\n"
        "DTSTART;TZID=America/New_York:20260901T080000\n"
        "SUMMARY:Grade 6 Orientation\nEND:VEVENT\n"
    )
    (event,) = ics.parse_events(text)
    assert event.start == datetime.date(2026, 9, 1)
    assert event.all_day is False


def test_parse_events_collects_multiple_exdates():
    text = (
        "BEGIN:VEVENT\nUID:e\nDTSTART;VALUE=DATE:20260901\nSUMMARY:Day 1\n"
        "EXDATE;VALUE=DATE:20260908,20260915\nEXDATE;VALUE=DATE:20260922\n"
        "END:VEVENT\n"
    )
    (event,) = ics.parse_events(text)
    assert event.exdates == {
        datetime.date(2026, 9, 8),
        datetime.date(2026, 9, 15),
        datetime.date(2026, 9, 22),
    }


# ---------------------------------------------------------------------------
# Expansion
# ---------------------------------------------------------------------------

def _event(**kw) -> ics.IcsEvent:
    base = dict(uid="e", summary="Day 1", start=datetime.date(2026, 9, 1))
    base.update(kw)
    return ics.IcsEvent(**base)


def test_expand_without_rrule_is_a_single_date():
    assert ics.expand(_event()) == [datetime.date(2026, 9, 1)]


def test_expand_daily_interval_until_is_inclusive():
    event = _event(rrule={"FREQ": "DAILY", "INTERVAL": "8", "UNTIL": "20260917T000000Z"})
    assert ics.expand(event) == [
        datetime.date(2026, 9, 1),
        datetime.date(2026, 9, 9),
        datetime.date(2026, 9, 17),
    ]


def test_expand_honours_exdate():
    event = _event(
        rrule={"FREQ": "DAILY", "INTERVAL": "8", "UNTIL": "20260917"},
        exdates={datetime.date(2026, 9, 9)},
    )
    assert ics.expand(event) == [datetime.date(2026, 9, 1), datetime.date(2026, 9, 17)]


def test_expand_supports_count():
    event = _event(rrule={"FREQ": "DAILY", "COUNT": "3"})
    assert ics.expand(event) == [
        datetime.date(2026, 9, 1),
        datetime.date(2026, 9, 2),
        datetime.date(2026, 9, 3),
    ]


@pytest.mark.parametrize(
    "rrule",
    [
        {"FREQ": "WEEKLY", "UNTIL": "20261001"},
        {"FREQ": "DAILY", "BYDAY": "MO", "UNTIL": "20261001"},
        {"FREQ": "DAILY"},  # unbounded
        {"FREQ": "DAILY", "UNTIL": "not-a-date"},
    ],
)
def test_expand_degrades_unsupported_rrules_to_one_occurrence(rrule, caplog):
    with caplog.at_level("WARNING"):
        assert ics.expand(_event(rrule=rrule)) == [datetime.date(2026, 9, 1)]
    assert caplog.records


def test_expand_bad_interval_falls_back_to_one_day(caplog):
    event = _event(rrule={"FREQ": "DAILY", "INTERVAL": "x", "COUNT": "2"})
    with caplog.at_level("WARNING"):
        assert ics.expand(event) == [
            datetime.date(2026, 9, 1),
            datetime.date(2026, 9, 2),
        ]


def test_expand_is_capped(monkeypatch):
    monkeypatch.setattr(ics, "MAX_OCCURRENCES", 5)
    event = _event(rrule={"FREQ": "DAILY", "UNTIL": "20270901"})
    assert len(ics.expand(event)) == 5


def test_events_by_date_dedupes_and_sorts():
    events = [
        ics.IcsEvent(summary="Day 1", start=datetime.date(2026, 9, 2)),
        ics.IcsEvent(summary="Day 1", start=datetime.date(2026, 9, 2)),
        ics.IcsEvent(summary="Assembly", start=datetime.date(2026, 9, 1)),
        ics.IcsEvent(summary="", start=datetime.date(2026, 9, 1)),
    ]
    assert ics.events_by_date(events) == {
        datetime.date(2026, 9, 1): ["Assembly"],
        datetime.date(2026, 9, 2): ["Day 1"],
    }


# ---------------------------------------------------------------------------
# Against the real (sanitized) feed
# ---------------------------------------------------------------------------

def test_real_feed_parses_every_event(feed_text):
    events = ics.parse_events(feed_text)
    assert len(events) == 196
    assert all(e.start is not None for e in events)


def test_real_feed_expands_the_full_school_year(feed_text):
    by_date = ics.parse_events_by_date(feed_text)
    assert min(by_date) == datetime.date(2026, 9, 1)
    assert max(by_date) == datetime.date(2027, 6, 21)
    assert by_date[datetime.date(2026, 9, 1)] == ["Day 1", "Grade 6 Orientation"]
    # The one EXDATE in the feed removes an election-day rotation occurrence.
    assert "Day 3" not in by_date.get(datetime.date(2026, 11, 6), [])


def test_real_feed_unescapes_commas(feed_text):
    by_date = ics.parse_events_by_date(feed_text)
    assert any(
        "Full Cast, Choreo" in summary
        for summaries in by_date.values()
        for summary in summaries
    )
