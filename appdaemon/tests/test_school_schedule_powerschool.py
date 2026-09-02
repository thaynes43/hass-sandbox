"""Unit tests for the PowerSchool guardian-portal provider.

Offline: no network, no real portal.  The HTTP layer is a fake session and
every page comes from the sanitized fixtures in
``tests/fixtures/school_schedule/``.  Credentials are obvious placeholders
(security rule S5).

The load-bearing test is ``test_build_cycle_reproduces_the_rotation_oracle``:
the list view must expand to exactly the six-day rotation captured from the
live portal.
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

from providers.school_schedule import powerschool as ps
from providers.school_schedule.types import ClassBlock, ScheduleRow

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "school_schedule"

BASE_URL = "https://portal.example.org"
TEST_USER = "test-guardian"
TEST_PASSWORD = "test-password"
TODAY = datetime.date(2026, 9, 2)


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Fake HTTP + cookie jar
# ---------------------------------------------------------------------------

class _Cookie:
    def __init__(self, key: str) -> None:
        self.key = key


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
    """Serves fixtures by URL substring and fakes the portal's session cookie.

    ``login_ok=False`` makes the login POST return the login form, which is how
    the real portal signals both bad credentials and an evicted session.
    """

    def __init__(self, routes: dict, *, login_ok: bool = True) -> None:
        self.routes = routes
        self.login_ok = login_ok
        self.requests: list[str] = []
        self.posts: list[tuple[str, dict]] = []
        self.cookie_jar: list[_Cookie] = []
        self.closed = False

    def _serve(self, url: str) -> _FakeResponse:
        for fragment, value in self.routes.items():
            if fragment in url:
                if callable(value):
                    value = value(url)
                body, status = value if isinstance(value, tuple) else (value, 200)
                return _FakeResponse(body, status)
        return _FakeResponse("not found", 404)

    def get(self, url: str, **_: object) -> _FakeResponse:
        self.requests.append(url)
        return self._serve(url)

    def post(self, url: str, data: dict | None = None, **_: object) -> _FakeResponse:
        self.posts.append((url, data or {}))
        if not self.login_ok:
            return _FakeResponse(_fixture("login_page.html"))
        self.cookie_jar.append(_Cookie("psaid"))
        return _FakeResponse(_fixture("guardian_home.html"))

    async def close(self) -> None:
        self.closed = True


def _routes(**overrides) -> dict:
    routes = {
        "myschedule_bellsched.html": _fixture("myschedule_bellsched.html"),
        "myschedule.html": _fixture("myschedule.html"),
        "home.html": _fixture("guardian_home.html"),
    }
    routes.update(overrides)
    return routes


def _client(session: _FakeSession) -> ps.PowerSchoolClient:
    return ps.PowerSchoolClient(BASE_URL, TEST_USER, TEST_PASSWORD, session=session)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "user,expected",
    [("guardian", "****an"), ("ab", "****"), ("a", "****"), ("", "<unset>")],
)
def test_mask_user(user, expected):
    assert ps.mask_user(user) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [("08:20 AM", "08:20"), ("01:20 PM", "13:20"), ("12:05 PM", "12:05"),
     ("12:00 AM", "00:00"), ("13:20", "13:20"), ("nonsense", "")],
)
def test_to_24h(raw, expected):
    assert ps.to_24h(raw) == expected


def test_parse_year_bounds():
    assert ps.parse_year_bounds(_fixture("myschedule_bellsched.html")) == (
        "2026-09-02",
        "2027-06-21",
    )


def test_parse_year_bounds_missing():
    assert ps.parse_year_bounds("<html></html>") == ("", "")


def test_parse_school_name_reads_the_span_not_the_district():
    assert ps.parse_school_name(_fixture("myschedule.html")) == "Example Middle School"
    assert ps.parse_school_name("<html></html>") == ""


def test_parse_student_ids_dedupes_and_keeps_order():
    assert ps.parse_student_ids(_fixture("guardian_home.html")) == ["10001", "10002"]
    assert ps.parse_student_ids("<html></html>") == []


def test_text_strips_nbsp_and_comments():
    assert ps._text("Advisory 6&nbsp;") == "Advisory 6"
    assert ps._text("26-27<!-- 3600 3260330 -->") == "26-27"


# ---------------------------------------------------------------------------
# Bell schedule grid
# ---------------------------------------------------------------------------

def test_parse_bell_schedule_reads_every_block_in_time_order():
    days, columns = ps.parse_bell_schedule(_fixture("myschedule_bellsched.html"))

    # The grid rendered four date columns; the first is a no-school day.
    assert columns == ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]
    assert "2026-09-01" not in days
    assert {k: len(v) for k, v in days.items()} == {
        "2026-09-02": 9, "2026-09-03": 8, "2026-09-04": 9
    }

    first = days["2026-09-02"][0]
    assert (first.course, first.room, first.start, first.end) == (
        "Advisory 6", "A206", "08:00", "08:20"
    )
    assert first.teacher.count(",") == 1  # "Last, First"
    assert first.period == ""  # the grid carries no period label

    assert [b.start for b in days["2026-09-03"]] == [
        "08:00", "08:40", "09:35", "10:30", "11:15", "12:00", "12:25", "13:20"
    ]
    assert [b.course for b in days["2026-09-03"]] == [
        "Advisory 6", "Science", "Math", "FLEX SGFL", "Art", "Lunch", "LA", "SocStud"
    ]


def test_parse_bell_schedule_keeps_the_lunch_pseudo_teacher():
    days, _ = ps.parse_bell_schedule(_fixture("myschedule_bellsched.html"))
    lunch = [b for b in days["2026-09-02"] if b.course == "Lunch"][0]
    assert lunch.teacher == "Lunch, MS"
    assert lunch.room == "Cafe"


def test_parse_bell_schedule_raises_when_the_grid_is_gone():
    with pytest.raises(ps.PowerSchoolError, match="not found"):
        ps.parse_bell_schedule("<html><body>Signed out</body></html>")


def test_parse_bell_schedule_tolerates_a_cell_without_times(caplog):
    html = (
        '<table id="tableStudentSchedMatrix">'
        '<tr><td class="scheduleHeader"><b>Monday<br>09/07/2026</b></td></tr>'
        '<tr><td class="scheduleClass1" name="attCell20260907">Math&nbsp;<br>'
        'Adams, Alex<br>A207<br></td></tr>'
        "</table>"
    )
    days, columns = ps.parse_bell_schedule(html)
    assert columns == ["2026-09-07"]
    block = days["2026-09-07"][0]
    assert (block.course, block.teacher, block.room, block.start) == (
        "Math", "Adams, Alex", "A207", ""
    )


def test_parse_bell_schedule_skips_unparseable_dates(caplog):
    html = (
        '<table id="tableStudentSchedMatrix">'
        '<td class="scheduleClass1" name="attCell20261332">X&nbsp;<br>T<br>R<br>'
        "08:00 AM - 09:00 AM</td></table>"
    )
    with caplog.at_level("WARNING"):
        days, _ = ps.parse_bell_schedule(html)
    assert days == {}
    assert "Unparseable attCell date" in caplog.text


# ---------------------------------------------------------------------------
# Exp expansion + period ordering
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "exp,expected",
    [
        ("ADV(1-6) 6PA(1,3,5)", [("ADV", [1, 2, 3, 4, 5, 6]), ("6PA", [1, 3, 5])]),
        ("6B4(1-3)", [("6B4", [1, 2, 3])]),
        ("6B4(4)", [("6B4", [4])]),
        ("6B4(5-6)", [("6B4", [5, 6])]),
        ("6B1(1,3,5) 6B6(2,4,6)", [("6B1", [1, 3, 5]), ("6B6", [2, 4, 6])]),
        ("6B1(3-1)", [("6B1", [1, 2, 3])]),  # reversed range
        ("", []),
        ("no parens here", []),
    ],
)
def test_expand_exp(exp, expected):
    assert ps.expand_exp(exp) == expected


def test_expand_exp_warns_on_garbage_days(caplog):
    with caplog.at_level("WARNING"):
        assert ps.expand_exp("6B1(1-)") == []
    assert "Unparseable Exp day range" in caplog.text


def test_period_sort_key_orders_the_matrix_header():
    periods = ["6B8", "6PA", "ADV", "6B6", "6B1", "ZZZ", "6B5"]
    ordered = sorted(periods, key=lambda p: ps.period_sort_key(p, periods.index(p)))
    assert ordered == ["ADV", "6B1", "6B5", "6PA", "6B6", "6B8", "ZZZ"]


# ---------------------------------------------------------------------------
# List view
# ---------------------------------------------------------------------------

def test_parse_list_schedule_reads_all_rows_and_strips_comments():
    rows = ps.parse_list_schedule(_fixture("myschedule.html"))
    assert len(rows) == 15

    first = rows[0]
    assert first.as_tuple() == (
        "ADV(1-6) 6PA(1,3,5)", "26-27", "6825-206", "Advisory 6",
        "Jordan, Jamie", "A206", "09/02/2026", "06/22/2027",
    )
    assert first.periods == [("ADV", [1, 2, 3, 4, 5, 6]), ("6PA", [1, 3, 5])]

    # Terms are not sorted in the source; all four quarters of FLEX are present.
    flex_terms = sorted(r.term for r in rows if r.course.startswith("FLEX"))
    assert flex_terms == ["Q1", "Q2", "Q3", "Q4"]


def test_parse_list_schedule_ignores_the_plugin_drawer_table():
    rows = ps.parse_list_schedule(_fixture("myschedule.html"))
    assert all("Forms" != row.course for row in rows)


def test_parse_list_schedule_raises_when_the_table_is_gone():
    with pytest.raises(ps.PowerSchoolError, match="not found"):
        ps.parse_list_schedule("<html><body>Signed out</body></html>")


def test_rows_active_on_filters_by_enrolment_window():
    rows = ps.parse_list_schedule(_fixture("myschedule.html"))

    september = {r.course for r in ps.rows_active_on(rows, datetime.date(2026, 9, 2))}
    assert "FLEX SGFL" in september and "Art" in september
    assert "FLEX LSGF" not in september and "Health" not in september

    january = {r.course for r in ps.rows_active_on(rows, datetime.date(2027, 1, 5))}
    assert "FLEX LSGF" in january and "Health" in january
    assert "Art" not in january


def test_rows_active_on_keeps_rows_with_unparseable_dates():
    row = ScheduleRow(course="Mystery", enroll="", leave="not-a-date")
    assert ps.rows_active_on([row], datetime.date(2026, 9, 2)) == [row]


def test_build_cycle_reproduces_the_rotation_oracle():
    """The whole point of the list view: all six rotation days, in order."""
    expected = json.loads(_fixture("cycle_by_day.json"))
    cycle = ps.build_cycle(ps.parse_list_schedule(_fixture("myschedule.html")), TODAY)

    assert sorted(cycle) == [1, 2, 3, 4, 5, 6]
    for day, entries in expected.items():
        actual = cycle[int(day)]
        assert len(actual) == len(entries), day
        for block, entry in zip(actual, entries):
            assert (block.period, block.course, block.teacher, block.room) == (
                entry["period"], entry["course"], entry["teacher"], entry["room"]
            )


def test_build_cycle_follows_the_term_calendar():
    rows = ps.parse_list_schedule(_fixture("myschedule.html"))
    spring = ps.build_cycle(rows, datetime.date(2027, 4, 20))
    courses = {b.course for b in spring[1]}
    assert "FLEX FLSG" in courses  # Q4
    assert "STEAM" in courses      # T3
    assert "FLEX SGFL" not in courses


def test_build_cycle_drops_out_of_range_rotation_days(caplog):
    row = ScheduleRow(course="Math", exp="6B1(9)")
    row.periods = ps.expand_exp(row.exp)
    with caplog.at_level("WARNING"):
        assert ps.build_cycle([row], TODAY) == {}
    assert "out-of-range rotation day" in caplog.text


# ---------------------------------------------------------------------------
# Client: login, session handling, logoff
# ---------------------------------------------------------------------------

def test_fetch_schedule_logs_in_fetches_and_logs_off():
    session = _FakeSession(_routes())
    schedule = _run(
        _client(session).fetch_schedule(
            school_name="Example Middle School", weeks_ahead=0, today=TODAY
        )
    )

    assert schedule.student_id == "10001"
    assert schedule.first_day == "2026-09-02"
    assert schedule.last_day == "2027-06-21"
    assert schedule.school_name == "Example Middle School"
    assert sorted(schedule.days) == ["2026-09-02", "2026-09-03", "2026-09-04"]
    assert sorted(schedule.cycle) == [1, 2, 3, 4, 5, 6]

    # Exactly one login POST, and a logoff on the way out.
    assert len(session.posts) == 1
    url, form = session.posts[0]
    assert url == f"{BASE_URL}/guardian/home.html"
    assert form["account"] == TEST_USER
    assert form["pw"] == TEST_PASSWORD == form["dbpw"]
    assert form["serviceName"] == "PS Parent Portal"
    assert session.requests[-1] == f"{BASE_URL}/guardian/home.html?ac=logoff"


def test_fetch_schedule_selects_the_student_matching_the_calendar_school():
    def _schedule_page(url: str) -> str:
        if "selected_student_id=10002" in url:
            return _fixture("myschedule.html").replace(
                "Example Middle School", "Example High School"
            )
        return _fixture("myschedule.html")

    session = _FakeSession(_routes(**{"myschedule.html": _schedule_page}))
    schedule = _run(
        _client(session).fetch_schedule(
            school_name="Example High School", weeks_ahead=0, today=TODAY
        )
    )
    assert schedule.student_id == "10002"


def test_fetch_schedule_uses_an_explicit_student_id_without_probing():
    session = _FakeSession(_routes())
    _run(
        _client(session).fetch_schedule(
            student_id="10002", weeks_ahead=0, today=TODAY
        )
    )
    # No per-student probe fetches: straight to that student's schedule.
    assert not any("selected_student_id=10001" in url for url in session.requests)


def test_fetch_schedule_takes_the_only_student_when_there_is_one():
    home = _fixture("guardian_home.html")
    home = home[: home.index('<li >')] + "</ul>"
    session = _FakeSession(_routes(**{"home.html": home}))
    schedule = _run(
        _client(session).fetch_schedule(weeks_ahead=0, today=TODAY)
    )
    assert schedule.student_id == "10001"


def test_fetch_schedule_errors_without_naming_students_when_nothing_matches():
    session = _FakeSession(_routes())
    with pytest.raises(ps.PowerSchoolError) as exc:
        _run(
            _client(session).fetch_schedule(
                school_name="Somewhere Else", weeks_ahead=0, today=TODAY
            )
        )
    assert "Example Middle School" in str(exc.value)
    assert "Student One" not in str(exc.value)
    # Still logged off despite the failure.
    assert session.requests[-1].endswith("ac=logoff")


def test_login_failure_reports_the_masked_user_only():
    session = _FakeSession(_routes(), login_ok=False)
    with pytest.raises(ps.PowerSchoolError) as exc:
        _run(
            _client(session).fetch_schedule(
                student_id="10001", weeks_ahead=0, today=TODAY
            )
        )
    assert "login form returned" in str(exc.value)
    assert TEST_USER not in str(exc.value)
    assert TEST_PASSWORD not in str(exc.value)
    # Even a failed login logs off: a stranded session evicts the next sign-in.
    assert session.requests[-1].endswith("ac=logoff")


def test_login_reports_bad_credentials_distinctly():
    session = _FakeSession(_routes())
    session.post = lambda url, data=None, **kw: _FakeResponse(  # type: ignore[assignment]
        "<html>Invalid Username or Password</html>"
    )
    with pytest.raises(ps.PowerSchoolError, match="rejected the credentials"):
        _run(
            _client(session).fetch_schedule(
                student_id="10001", weeks_ahead=0, today=TODAY
            )
        )


def test_login_requires_the_session_cookie():
    session = _FakeSession(_routes())
    session.post = lambda url, data=None, **kw: _FakeResponse(  # type: ignore[assignment]
        _fixture("guardian_home.html")
    )
    with pytest.raises(ps.PowerSchoolError, match="psaid"):
        _run(
            _client(session).fetch_schedule(
                student_id="10001", weeks_ahead=0, today=TODAY
            )
        )


def test_mid_run_eviction_retries_the_whole_flow_once(caplog):
    """A parent logging in mid-run bounces us; one retry, then success."""
    state = {"evict": True}

    def _bellsched(url: str) -> str:
        if state["evict"]:
            state["evict"] = False
            return _fixture("login_page.html")
        return _fixture("myschedule_bellsched.html")

    session = _FakeSession(_routes(**{"myschedule_bellsched.html": _bellsched}))
    with caplog.at_level("WARNING"):
        schedule = _run(
            _client(session).fetch_schedule(
                student_id="10001", weeks_ahead=0, today=TODAY
            )
        )

    assert schedule.days
    assert "session evicted mid-fetch" in caplog.text
    assert len(session.posts) == 2  # exactly one retry
    assert sum(1 for u in session.requests if u.endswith("ac=logoff")) == 2


def test_a_second_eviction_is_not_retried_again():
    session = _FakeSession(
        _routes(**{"myschedule_bellsched.html": _fixture("login_page.html")})
    )
    with pytest.raises(ps.SessionEvicted):
        _run(
            _client(session).fetch_schedule(
                student_id="10001", weeks_ahead=0, today=TODAY
            )
        )
    assert len(session.posts) == 2


def test_http_errors_are_reported_by_path_not_url():
    session = _FakeSession(_routes(**{"myschedule_bellsched.html": ("boom", 503)}))
    with pytest.raises(ps.PowerSchoolError) as exc:
        _run(
            _client(session).fetch_schedule(
                student_id="10001", weeks_ahead=0, today=TODAY
            )
        )
    assert "/guardian/myschedule_bellsched.html returned HTTP 503" in str(exc.value)
    assert BASE_URL not in str(exc.value)


def test_missing_credentials_fail_before_any_request():
    session = _FakeSession(_routes())
    client = ps.PowerSchoolClient(BASE_URL, "", "", session=session)
    with pytest.raises(ps.PowerSchoolError, match="username/password not configured"):
        _run(client.fetch_schedule(weeks_ahead=0, today=TODAY))
    assert session.requests == []


def test_missing_base_url_fails_before_any_request():
    session = _FakeSession(_routes())
    client = ps.PowerSchoolClient("", TEST_USER, TEST_PASSWORD, session=session)
    with pytest.raises(ps.PowerSchoolError, match="No PowerSchool base URL"):
        _run(client.fetch_schedule(weeks_ahead=0, today=TODAY))


# ---------------------------------------------------------------------------
# Weekly window
# ---------------------------------------------------------------------------

def test_weeks_ahead_falls_back_to_one_request_per_week():
    session = _FakeSession(_routes())
    _run(
        _client(session).fetch_schedule(
            student_id="10001", weeks_ahead=2, today=TODAY
        )
    )
    bell = [u for u in session.requests if "bellsched" in u]
    # One full-range attempt, then Mondays after the covered week.
    assert "startdate=09/02/2026&enddate=09/16/2026" in bell[0]
    assert [u.split("startdate=")[1] for u in bell[1:]] == [
        "09/07/2026&enddate=09/11/2026",
        "09/14/2026&enddate=09/18/2026",
    ]


def test_weeks_ahead_zero_makes_a_single_request():
    session = _FakeSession(_routes())
    _run(
        _client(session).fetch_schedule(
            student_id="10001", weeks_ahead=0, today=TODAY
        )
    )
    assert len([u for u in session.requests if "bellsched" in u]) == 1


def test_monday_on_or_after():
    assert ps._monday_on_or_after(datetime.date(2026, 9, 5)) == datetime.date(2026, 9, 7)
    assert ps._monday_on_or_after(datetime.date(2026, 9, 7)) == datetime.date(2026, 9, 7)
    assert ps._monday_on_or_after(datetime.date(2026, 9, 2)) == datetime.date(2026, 9, 7)


def test_session_property_requires_a_session():
    with pytest.raises(RuntimeError, match="no active session"):
        _ = ps.PowerSchoolClient(BASE_URL, TEST_USER, TEST_PASSWORD).session


def test_client_does_not_close_a_session_it_does_not_own():
    session = _FakeSession(_routes())

    async def _use() -> None:
        async with ps.PowerSchoolClient(
            BASE_URL, TEST_USER, TEST_PASSWORD, session=session
        ):
            pass

    _run(_use())
    assert session.closed is False


def test_class_block_as_dict_drops_empty_fields():
    block = ClassBlock(course="Math", period="6B7", start="13:20", end="14:20")
    assert block.as_dict() == {
        "course": "Math", "period": "6B7", "start": "13:20", "end": "14:20"
    }
