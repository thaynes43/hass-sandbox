"""Unit tests for the Finalsite day-cycle provider.

Offline: the HTTP layer is a fake session and every page comes from the
sanitized fixtures in ``tests/fixtures/school_schedule/``.

The load-bearing test here is ``test_real_feed_reproduces_the_day_number_oracle``:
the fixture feed must expand to exactly the 181 ``{date: day number}`` pairs
captured from the live calendar.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import sys
from pathlib import Path

import pytest

_repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo_root / "apps"))
sys.path.insert(0, str(_repo_root))

from providers.school_schedule import day_cycle as dc
from providers.school_schedule.ics import parse_events_by_date

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "school_schedule"
PAGE_URL = "https://calendar.example.org/page/view-all-events"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Fake HTTP
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, text: str, status: int = 200) -> None:
        self._text = text
        self.status = status

    async def text(self) -> str:
        return self._text

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSession:
    """Routes GETs by URL substring; records every URL requested."""

    def __init__(self, routes: dict[str, tuple[str, int]]) -> None:
        self.routes = routes
        self.requests: list[str] = []
        self.closed = False

    def get(self, url: str, **_: object) -> _FakeResponse:
        self.requests.append(url)
        for fragment, (body, status) in self.routes.items():
            if fragment in url:
                return _FakeResponse(body, status)
        return _FakeResponse("not found", 404)

    async def close(self) -> None:
        self.closed = True


def _client(routes: dict[str, tuple[str, int]]) -> tuple[dc.DayCycleClient, _FakeSession]:
    session = _FakeSession(routes)
    return dc.DayCycleClient(PAGE_URL, session=session), session


ROUTES_OK = {
    "view-all-events": (_fixture("all_events_page.html"), 200),
    "events.ics": (_fixture("events.ics"), 200),
    "/fs/elements/": (_fixture("calendar_fragment_2026_09.html"), 200),
}


# ---------------------------------------------------------------------------
# discover_calendar
# ---------------------------------------------------------------------------

def test_discover_calendar_reads_ids_and_school_name():
    element = dc.discover_calendar(_fixture("all_events_page.html"), PAGE_URL)
    assert element.element_id == "13538"
    assert element.calendar_ids == "15"
    assert element.feed_uuid == "8ae13486-9583-4342-91ca-99187126f1e0"
    assert element.school_name == "Example Middle School"
    assert element.base_url == "https://calendar.example.org"


def test_discover_calendar_warns_when_the_element_is_missing(caplog):
    with caplog.at_level("WARNING"):
        element = dc.discover_calendar("<html><title>Nope</title></html>", PAGE_URL)
    assert not element
    assert element.school_name == "Nope"
    assert "No fsCalendar element" in caplog.text


def test_discover_calendar_survives_a_title_without_a_dash():
    element = dc.discover_calendar("<title>Events</title>", PAGE_URL)
    assert element.school_name == "Events"


# ---------------------------------------------------------------------------
# build_day_cycle
# ---------------------------------------------------------------------------

def test_build_day_cycle_classifies_days_closures_and_notes():
    cycle = dc.build_day_cycle(
        {
            datetime.date(2026, 9, 2): ["Day 1 (Repeat)", "School Begins"],
            datetime.date(2026, 9, 7): ["Labor Day - No School"],
            datetime.date(2026, 9, 24): ["Day 3", "Early Release - PD"],
            datetime.date(2026, 11, 4): ["Day 5", "2 Hour Delay - Conferences"],
        },
        school_name="Example Middle School",
        source="ics",
    )
    assert cycle.dates == {"2026-09-02": 1, "2026-09-24": 3, "2026-11-04": 5}
    assert cycle.closures == {"2026-09-07": "Labor Day - No School"}
    assert cycle.notes == {
        "2026-09-24": "Early Release - PD",
        "2026-11-04": "2 Hour Delay - Conferences",
    }
    assert cycle.school_name == "Example Middle School"
    assert cycle.cycle_length == 6
    assert bool(cycle) is True


def test_build_day_cycle_ignores_titles_that_merely_contain_day():
    cycle = dc.build_day_cycle(
        {
            datetime.date(2026, 9, 1): ["Grade 6 Orientation", "Field Day 2"],
            datetime.date(2026, 9, 3): ["Day 7"],
        }
    )
    assert cycle.dates == {}


def test_build_day_cycle_ignores_weekends():
    cycle = dc.build_day_cycle(
        {
            datetime.date(2026, 9, 5): ["Day 4", "No School"],  # Saturday
        }
    )
    assert cycle.dates == {}
    assert cycle.closures == {}


def test_build_day_cycle_lets_a_closure_win_over_a_rotation_day(caplog):
    with caplog.at_level("WARNING"):
        cycle = dc.build_day_cycle(
            {datetime.date(2026, 11, 6): ["Day 3", "Elections-No School"]}
        )
    assert cycle.dates == {}
    assert cycle.closures == {"2026-11-06": "Elections-No School"}
    assert "both a rotation day and a no-school event" in caplog.text


def test_empty_day_cycle_is_falsey():
    assert not dc.build_day_cycle({})


# ---------------------------------------------------------------------------
# The oracle
# ---------------------------------------------------------------------------

def test_real_feed_reproduces_the_day_number_oracle():
    """The whole point of the ICS path: 181 dates, byte-for-byte."""
    expected = json.loads(_fixture("day_numbers.json"))
    cycle = dc.build_day_cycle(parse_events_by_date(_fixture("events.ics")))
    assert cycle.dates == expected
    assert len(cycle.dates) == 181


def test_real_feed_closures_and_notes():
    cycle = dc.build_day_cycle(parse_events_by_date(_fixture("events.ics")))
    assert cycle.closures["2026-09-07"] == "Labor Day - No School"
    assert cycle.closures["2026-11-11"] == "Veterans Day-No School"
    assert cycle.notes["2026-09-24"] == "Early Release - PD"
    assert cycle.notes["2026-11-04"] == "2 Hour Delay - Conferences"
    # A closure date never also carries a rotation day.
    assert not set(cycle.dates) & set(cycle.closures)


# ---------------------------------------------------------------------------
# Month fragments (fallback path)
# ---------------------------------------------------------------------------

def test_parse_month_fragment_reads_zero_based_months_and_dedupes():
    by_date = dc.parse_month_fragment(_fixture("calendar_fragment_2026_09.html"))
    # data-month="8" is September, not August.
    assert datetime.date(2026, 9, 1) in by_date
    assert by_date[datetime.date(2026, 9, 1)] == ["Day 1", "Grade 6 Orientation"]
    # Every event is rendered twice per daybox; titles must not double up.
    assert by_date[datetime.date(2026, 9, 4)] == ["Day 3"]


def test_parse_month_fragment_yields_the_same_day_numbers_as_the_feed():
    cycle = dc.build_day_cycle(
        dc.parse_month_fragment(_fixture("calendar_fragment_2026_09.html"))
    )
    expected = json.loads(_fixture("day_numbers.json"))
    assert cycle.dates == {k: v for k, v in expected.items() if k in cycle.dates}
    assert cycle.dates["2026-09-02"] == 1
    assert cycle.closures["2026-09-07"] == "Labor Day - No School"


def test_parse_month_fragment_skips_impossible_dates(caplog):
    html = (
        '<div class="fsCalendarDaybox">'
        '<div class="fsCalendarDate" data-day="31" data-year="2026" data-month="12">'
    )
    with caplog.at_level("WARNING"):
        assert dc.parse_month_fragment(html) == {}
    assert "impossible date" in caplog.text


def test_month_starts_rolls_over_the_year():
    assert dc.month_starts(datetime.date(2026, 11, 20), 3) == [
        datetime.date(2026, 11, 1),
        datetime.date(2026, 12, 1),
        datetime.date(2027, 1, 1),
    ]


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

def test_fetch_uses_the_ics_feed():
    client, session = _client(ROUTES_OK)
    cycle = _run(client.fetch())

    assert cycle.source == "ics"
    assert len(cycle.dates) == 181
    assert cycle.school_name == "Example Middle School"
    assert session.requests == [
        PAGE_URL,
        "https://calendar.example.org/fs/calendar-manager/events.ics?calendar_ids[]=15",
    ]


def test_fetch_builds_one_query_parameter_per_calendar_id():
    page = _fixture("all_events_page.html").replace(
        "data-calendar-ids=15", "data-calendar-ids=15,22"
    )
    client, session = _client({**ROUTES_OK, "view-all-events": (page, 200)})
    _run(client.fetch())
    assert session.requests[1].endswith("calendar_ids[]=15&calendar_ids[]=22")


def test_fetch_falls_back_to_month_fragments_when_the_feed_fails(caplog):
    routes = {**ROUTES_OK, "events.ics": ("gateway timeout", 504)}
    client, session = _client(routes)

    with caplog.at_level("WARNING"):
        cycle = _run(client._fetch_fragments(
            dc.discover_calendar(_fixture("all_events_page.html"), PAGE_URL),
            today=datetime.date(2026, 9, 2),
            months=1,
        ))

    assert cycle.source == "html"
    assert cycle.dates["2026-09-02"] == 1
    assert session.requests == [
        "https://calendar.example.org/fs/elements/13538?cal_date=2026-09-01"
    ]


def test_fetch_end_to_end_falls_back_on_a_broken_feed(caplog):
    routes = {**ROUTES_OK, "events.ics": ("nope", 500)}
    client, _ = _client(routes)
    with caplog.at_level("WARNING"):
        cycle = _run(client.fetch())
    assert cycle.source == "html"
    assert "falling back to month fragments" in caplog.text


def test_fetch_raises_when_the_feed_has_no_rotation_days():
    routes = {**ROUTES_OK, "events.ics": ("BEGIN:VCALENDAR\nEND:VCALENDAR\n", 200)}
    client, _ = _client(routes)
    element = dc.discover_calendar(_fixture("all_events_page.html"), PAGE_URL)
    with pytest.raises(ValueError, match="no 'Day N' rotation events"):
        _run(client._fetch_ics(element))


def test_fetch_requires_a_configured_url():
    client = dc.DayCycleClient("", session=_FakeSession({}))
    with pytest.raises(ValueError, match="No all-events URL"):
        _run(client.fetch())


def test_fetch_fragments_needs_an_element_id():
    client, _ = _client(ROUTES_OK)
    with pytest.raises(ValueError, match="No calendar element id"):
        _run(client._fetch_fragments(dc.CalendarElement(base_url="https://x")))


def test_session_property_requires_a_session():
    with pytest.raises(RuntimeError, match="no active session"):
        _ = dc.DayCycleClient(PAGE_URL).session


def test_client_does_not_close_a_session_it_does_not_own():
    session = _FakeSession(ROUTES_OK)

    async def _use() -> None:
        async with dc.DayCycleClient(PAGE_URL, session=session):
            pass

    _run(_use())
    assert session.closed is False
