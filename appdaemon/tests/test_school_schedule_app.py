"""Unit tests for SchoolScheduleApp.

Mocks AppDaemon and both providers — no real network or HA access.  Secrets are
obvious placeholders (security rule S5) and the tests assert they never reach a
log line or a sensor attribute (S3, S6).
"""

from __future__ import annotations

import asyncio
import datetime
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Mock hassapi before importing the app
# ---------------------------------------------------------------------------
mock_hass = MagicMock()
mock_hass.Hass = type("_MockHass", (), {"__init__": lambda self, *a, **kw: None})
sys.modules["hassapi"] = mock_hass

_repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo_root / "apps"))
sys.path.insert(0, str(_repo_root))

from school_schedule_app.school_schedule_app import (  # noqa: E402
    DEFAULT_ICON,
    SENSOR_ENTITY_ID,
    SchoolScheduleApp,
    attach_periods,
    compile_icon_rules,
    compile_patterns,
    is_hidden,
    parse_time_of_day,
    resolve_icon,
    DEFAULT_HIDE_COURSES,
    DEFAULT_ICON_RULES,
)
from providers.school_schedule.types import ClassBlock, DayCycle, WeeklySchedule  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "school_schedule"

TODAY = datetime.date(2026, 9, 2)  # Wednesday, rotation Day 1

TEST_CALENDAR_URL = "https://calendar.example.org/page/view-all-events"
TEST_PORTAL_URL = "https://portal.example.org"
TEST_USER = "test-guardian"
TEST_PASSWORD = "test-password"

DEFAULT_ARGS: Dict[str, Any] = {
    "name": "Middle School",
    "day_cycle_url_env": "MIDDLE_ALL_EVENTS",
    "powerschool_url_env": "POWER_SCHOOL",
    "powerschool_user_env": "MIDDLE_USER",
    "powerschool_password_env": "MIDDLE_PASSWORD",
    "refresh_time": "05:00:00",
    "weeks_ahead": 3,
}


@pytest.fixture(autouse=True)
def _schedule_env(monkeypatch):
    monkeypatch.setenv("MIDDLE_ALL_EVENTS", TEST_CALENDAR_URL)
    monkeypatch.setenv("POWER_SCHOOL", TEST_PORTAL_URL)
    monkeypatch.setenv("MIDDLE_USER", TEST_USER)
    monkeypatch.setenv("MIDDLE_PASSWORD", TEST_PASSWORD)


# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

def _day_cycle() -> DayCycle:
    return DayCycle(
        school_name="Example Middle School",
        cycle_length=6,
        dates={
            "2026-09-02": 1,
            "2026-09-03": 2,
            "2026-09-04": 3,
            "2026-09-08": 4,
            "2026-09-24": 3,
        },
        closures={"2026-09-07": "Labor Day - No School"},
        notes={"2026-09-24": "Early Release - PD"},
        source="ics",
    )


def _blocks(*specs) -> List[ClassBlock]:
    return [
        ClassBlock(course=c, teacher="Adams, Alex", room=r, start=s, end=e)
        for c, r, s, e in specs
    ]


def _schedule() -> WeeklySchedule:
    return WeeklySchedule(
        days={
            "2026-09-02": _blocks(
                ("Advisory 6", "A206", "08:00", "08:20"),
                ("LA", "A209", "08:20", "09:15"),
                ("Lunch", "Cafe", "11:40", "12:05"),
                ("Advisory 6", "A206", "12:05", "12:25"),
                ("Theater Arts 6", "D106", "12:25", "13:20"),
            ),
            "2026-09-03": _blocks(("Science", "A206", "08:40", "09:35")),
            # A date the calendar knows nothing about — no `day` key.
            "2026-09-09": _blocks(("Math", "A207", "08:00", "09:00")),
        },
        cycle={
            1: [
                ClassBlock(period="ADV", course="Advisory 6", room="A206"),
                ClassBlock(period="6B1", course="LA", room="A209"),
                ClassBlock(period="6B5", course="Lunch", room="Cafe"),
                ClassBlock(period="6PA", course="Advisory 6", room="A206"),
                ClassBlock(period="6B6", course="Theater Arts 6", room="D106"),
            ],
            2: [ClassBlock(period="6B1", course="Science", room="A206")],
        },
        first_day="2026-09-02",
        last_day="2027-06-21",
        school_name="Example Middle School",
        student_id="10001",
    )


# ---------------------------------------------------------------------------
# Stub providers
# ---------------------------------------------------------------------------

class _StubClient:
    """Async-context-manager stub standing in for either provider client."""

    def __init__(self, result: Any = None, error: Optional[Exception] = None) -> None:
        self.result = result
        self.error = error
        self.calls: List[dict] = []

    async def __aenter__(self) -> "_StubClient":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def fetch(self) -> Any:
        self.calls.append({})
        if self.error:
            raise self.error
        return self.result

    async def fetch_schedule(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.result


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def _make_app(extra_args: dict | None = None) -> SchoolScheduleApp:
    app = SchoolScheduleApp(MagicMock(), MagicMock())

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
    app.run_daily = MagicMock()
    app.cancel_timer = MagicMock()
    app.log = MagicMock()
    app.create_task = MagicMock()
    app.date = MagicMock(return_value=TODAY)

    return app


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _startup(
    app: SchoolScheduleApp,
    day_client: _StubClient,
    portal_client: _StubClient,
) -> None:
    app.initialize()
    module = "school_schedule_app.school_schedule_app"
    with patch(f"{module}.DayCycleClient", return_value=day_client), \
         patch(f"{module}.PowerSchoolClient", return_value=portal_client):
        _run(app._async_startup())


def _published(app: SchoolScheduleApp) -> tuple[str, Dict[str, Any]]:
    assert app.set_state.called, "sensor was never published"
    args, kwargs = app.set_state.call_args
    assert args[0] == SENSOR_ENTITY_ID
    return kwargs["state"], kwargs["attributes"]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_compile_patterns_skips_invalid_regexes(caplog):
    with caplog.at_level("ERROR"):
        compiled = compile_patterns([r"\blunch\b", "[unclosed"])
    assert len(compiled) == 1
    assert "Ignoring invalid course pattern" in caplog.text


def test_compile_icon_rules_skips_blank_and_invalid_rules(caplog):
    with caplog.at_level("ERROR"):
        rules = compile_icon_rules([
            {"match": "", "icon": "mdi:x"},
            {"match": "[bad", "icon": "mdi:x"},
            {"match": "math", "icon": "mdi:calculator-variant", "short": "Math"},
        ])
    assert len(rules) == 1
    assert "Ignoring invalid icon rule" in caplog.text


@pytest.mark.parametrize(
    "course,icon,short",
    [
        # Order matters: theatre must win before the word-bounded art rule.
        ("Theater Arts 6", "mdi:drama-masks", "Theater"),
        ("Art", "mdi:palette", "Art"),
        ("LA", "mdi:book-open-page-variant", "LA"),
        ("SocStud", "mdi:earth", "Social Studies"),
        ("Science", "mdi:flask", "Science"),
        ("Math", "mdi:calculator-variant", "Math"),
        ("PhysEd", "mdi:run", "PE"),
        ("Health", "mdi:heart-pulse", "Health"),
        ("STEAM", "mdi:cog", "STEAM"),
        ("FLEX SGFL", "mdi:puzzle-outline", "FLEX"),
        ("Band", "mdi:trumpet", "Band"),
        # Nothing matches -> default icon, course name as the label.
        ("Underwater Basket Weaving", DEFAULT_ICON, "Underwater Basket Weaving"),
    ],
)
def test_resolve_icon(course, icon, short):
    assert resolve_icon(course, compile_icon_rules(DEFAULT_ICON_RULES)) == (icon, short)


def test_resolve_icon_falls_back_to_the_course_name_without_a_short():
    rules = compile_icon_rules([{"match": "math", "icon": "mdi:calculator-variant"}])
    assert resolve_icon("Math 6", rules) == ("mdi:calculator-variant", "Math 6")


@pytest.mark.parametrize(
    "course,hidden",
    [("Lunch", True), ("Advisory 6", True), ("Homeroom", True),
     ("LA", False), ("Science", False), ("Brunch Club", False)],
)
def test_is_hidden(course, hidden):
    assert is_hidden(course, compile_patterns(DEFAULT_HIDE_COURSES)) is hidden


def test_attach_periods_matches_repeated_courses_in_order():
    """Advisory meets twice a day: ADV first, 6PA second."""
    blocks = _blocks(
        ("Advisory 6", "A206", "08:00", "08:20"),
        ("LA", "A209", "08:20", "09:15"),
        ("Advisory 6", "A206", "12:05", "12:25"),
    )
    cycle = [
        ClassBlock(period="ADV", course="Advisory 6"),
        ClassBlock(period="6B1", course="LA"),
        ClassBlock(period="6PA", course="Advisory 6"),
    ]
    assert [b.period for b in attach_periods(blocks, cycle)] == ["ADV", "6B1", "6PA"]
    # Clock times survive the enrichment.
    assert [b.start for b in attach_periods(blocks, cycle)] == ["08:00", "08:20", "12:05"]


def test_attach_periods_leaves_unmatched_courses_alone():
    blocks = _blocks(("Chess Club", "B100", "08:00", "09:00"))
    assert attach_periods(blocks, [ClassBlock(period="ADV", course="LA")])[0].period == ""


def test_attach_periods_without_a_cycle_is_a_passthrough():
    blocks = _blocks(("LA", "A209", "08:20", "09:15"))
    assert attach_periods(blocks, []) == blocks


@pytest.mark.parametrize(
    "value,expected",
    [("05:00:00", datetime.time(5, 0)), ("06:30", datetime.time(6, 30)),
     ("", datetime.time(5, 0)), ("nonsense", datetime.time(5, 0))],
)
def test_parse_time_of_day(value, expected):
    assert parse_time_of_day(value, datetime.time(5, 0)) == expected


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def test_initialize_resolves_secrets_and_never_logs_them():
    app = _make_app()
    app.initialize()

    assert app._day_cycle_url == TEST_CALENDAR_URL
    assert app._powerschool_url == TEST_PORTAL_URL
    assert app._powerschool_user == TEST_USER
    assert app._powerschool_password == TEST_PASSWORD
    app.run_in.assert_called_once()

    logged = " ".join(str(c) for c in app.log.call_args_list)
    assert "****an" in logged  # masked username
    assert TEST_USER not in logged
    assert TEST_PASSWORD not in logged
    assert TEST_PORTAL_URL not in logged
    assert TEST_CALENDAR_URL not in logged


def test_initialize_uses_defaults_for_icons_hiding_and_timing():
    app = _make_app({"refresh_time": None, "weeks_ahead": None})
    app.initialize()
    assert app._refresh_time == datetime.time(5, 0)
    assert app._weeks_ahead == 3
    assert len(app._icon_rules) == len(DEFAULT_ICON_RULES)
    assert len(app._hide_courses) == len(DEFAULT_HIDE_COURSES)


def test_initialize_accepts_config_overrides():
    app = _make_app({
        "hide_courses": [r"\bstudy hall\b"],
        "icon_rules": [{"match": "chess", "icon": "mdi:chess-king", "short": "Chess"}],
        "refresh_time": "06:15:00",
        "weeks_ahead": 1,
    })
    app.initialize()
    assert app._refresh_time == datetime.time(6, 15)
    assert app._weeks_ahead == 1
    assert is_hidden("Study Hall", app._hide_courses)
    assert not is_hidden("Lunch", app._hide_courses)
    assert resolve_icon("Chess", app._icon_rules) == ("mdi:chess-king", "Chess")


def test_missing_env_var_is_logged_not_raised(monkeypatch):
    monkeypatch.delenv("POWER_SCHOOL", raising=False)
    app = _make_app()
    app.initialize()
    assert app._powerschool_url == ""
    assert any("Cannot resolve config" in str(c) for c in app.log.call_args_list)


def test_startup_schedules_the_daily_refresh_and_the_midnight_republish():
    app = _make_app()
    _startup(app, _StubClient(_day_cycle()), _StubClient(_schedule()))

    times = [c.args[1] for c in app.run_daily.call_args_list]
    assert times == [datetime.time(5, 0), datetime.time(0, 0, 30)]


def test_on_startup_launches_the_async_task():
    app = _make_app()
    app.initialize()
    app._on_startup({})
    app.create_task.assert_called_once()
    app.create_task.call_args[0][0].close()


def test_daily_refresh_creates_a_task():
    app = _make_app()
    app.initialize()
    app._daily_refresh({})
    app.create_task.assert_called_once()
    app.create_task.call_args[0][0].close()


# ---------------------------------------------------------------------------
# Sensor payload
# ---------------------------------------------------------------------------

def test_published_sensor_matches_the_card_contract():
    app = _make_app()
    _startup(app, _StubClient(_day_cycle()), _StubClient(_schedule()))
    state, attrs = _published(app)

    assert state == "ok"
    assert attrs["school"] == "Middle School"
    assert attrs["cycle_length"] == 6
    assert attrs["dates"]["2026-09-02"] == 1
    assert attrs["closures"] == {"2026-09-07": "Labor Day - No School"}
    assert attrs["notes"] == {"2026-09-24": "Early Release - PD"}
    assert attrs["today"] == {"date": "2026-09-02", "day": 1}
    assert attrs["next"] == {"date": "2026-09-03", "day": 2}
    assert attrs["last_updated"]
    assert attrs["sources"] == {
        "day_cycle": {"status": "ok", "fetched_at": attrs["sources"]["day_cycle"]["fetched_at"], "error": ""},
        "powerschool": {"status": "ok", "fetched_at": attrs["sources"]["powerschool"]["fetched_at"], "error": ""},
    }
    assert attrs["friendly_name"] == "Middle School Schedule"


def test_days_drop_hidden_courses_and_carry_icons_and_periods():
    app = _make_app()
    _startup(app, _StubClient(_day_cycle()), _StubClient(_schedule()))
    _, attrs = _published(app)

    entry = attrs["days"]["2026-09-02"]
    assert entry["day"] == 1
    assert [c["course"] for c in entry["classes"]] == ["LA", "Theater Arts 6"]
    assert [c["icon"] for c in entry["classes"]] == [
        "mdi:book-open-page-variant", "mdi:drama-masks"
    ]
    assert [c["short"] for c in entry["classes"]] == ["LA", "Theater"]
    # Periods come from the list view; times from the bell grid.
    assert [c["period"] for c in entry["classes"]] == ["6B1", "6B6"]
    assert entry["classes"][0]["start"] == "08:20"
    assert "note" not in entry


def test_days_carry_the_calendar_note_on_an_early_release_day():
    schedule = _schedule()
    schedule.days["2026-09-24"] = _blocks(("Science", "A206", "08:00", "09:00"))
    app = _make_app()
    _startup(app, _StubClient(_day_cycle()), _StubClient(schedule))
    _, attrs = _published(app)
    assert attrs["days"]["2026-09-24"]["note"] == "Early Release - PD"
    assert attrs["days"]["2026-09-24"]["day"] == 3


def test_days_without_a_calendar_day_number_omit_it():
    app = _make_app()
    _startup(app, _StubClient(_day_cycle()), _StubClient(_schedule()))
    _, attrs = _published(app)
    assert "day" not in attrs["days"]["2026-09-09"]
    assert attrs["days"]["2026-09-09"]["classes"][0]["course"] == "Math"


def test_days_before_today_are_not_published():
    schedule = _schedule()
    schedule.days["2026-09-01"] = _blocks(("LA", "A209", "08:00", "09:00"))
    app = _make_app()
    _startup(app, _StubClient(_day_cycle()), _StubClient(schedule))
    _, attrs = _published(app)
    assert "2026-09-01" not in attrs["days"]


def test_days_with_only_hidden_courses_are_omitted():
    schedule = _schedule()
    schedule.days["2026-09-10"] = _blocks(("Lunch", "Cafe", "11:40", "12:05"))
    app = _make_app()
    _startup(app, _StubClient(_day_cycle()), _StubClient(schedule))
    _, attrs = _published(app)
    assert "2026-09-10" not in attrs["days"]


def test_cycle_is_keyed_by_string_and_drops_hidden_courses():
    app = _make_app()
    _startup(app, _StubClient(_day_cycle()), _StubClient(_schedule()))
    _, attrs = _published(app)
    assert sorted(attrs["cycle"]) == ["1", "2"]
    assert [c["course"] for c in attrs["cycle"]["1"]] == ["LA", "Theater Arts 6"]
    assert attrs["cycle"]["1"][0]["period"] == "6B1"


def test_today_on_a_closure_day_has_no_day_number():
    app = _make_app()
    app.date = MagicMock(return_value=datetime.date(2026, 9, 7))
    _startup(app, _StubClient(_day_cycle()), _StubClient(_schedule()))
    _, attrs = _published(app)
    assert attrs["today"] == {
        "date": "2026-09-07", "note": "Labor Day - No School"
    }
    assert attrs["next"] == {"date": "2026-09-08", "day": 4}


def test_today_outside_the_calendar_reads_as_no_school():
    app = _make_app()
    app.date = MagicMock(return_value=datetime.date(2026, 9, 5))  # a Saturday
    _startup(app, _StubClient(_day_cycle()), _StubClient(_schedule()))
    _, attrs = _published(app)
    assert attrs["today"] == {"date": "2026-09-05", "note": "No school"}


def test_next_on_a_date_with_classes_but_no_day_number_is_not_no_school():
    app = _make_app()
    cycle = _day_cycle()
    cycle.dates.pop("2026-09-03")
    cycle.dates.pop("2026-09-04")
    cycle.dates.pop("2026-09-08")
    cycle.dates.pop("2026-09-24")
    _startup(app, _StubClient(cycle), _StubClient(_schedule()))
    _, attrs = _published(app)
    # 2026-09-03 has classes from the portal but no calendar day number.
    assert attrs["next"] == {"date": "2026-09-03"}


def test_next_falls_off_the_end_of_the_year():
    cycle = _day_cycle()
    cycle.dates = {"2026-09-02": 1}
    schedule = WeeklySchedule()
    app = _make_app()
    _startup(app, _StubClient(cycle), _StubClient(schedule))
    _, attrs = _published(app)
    assert attrs["next"] == {"date": "", "note": "No school days scheduled"}


def test_midnight_republish_rolls_today_over_without_fetching():
    day_client, portal_client = _StubClient(_day_cycle()), _StubClient(_schedule())
    app = _make_app()
    _startup(app, day_client, portal_client)

    app.date = MagicMock(return_value=datetime.date(2026, 9, 3))
    app._on_midnight({})

    _, attrs = _published(app)
    assert attrs["today"] == {"date": "2026-09-03", "day": 2}
    # No extra provider calls: the portal is logged into once a day, no more.
    assert len(day_client.calls) == 1
    assert len(portal_client.calls) == 1


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------

def test_powerschool_failure_keeps_stale_classes_and_reports_partial():
    app = _make_app()
    _startup(app, _StubClient(_day_cycle()), _StubClient(_schedule()))
    first_fetched = _published(app)[1]["sources"]["powerschool"]["fetched_at"]

    module = "school_schedule_app.school_schedule_app"
    with patch(f"{module}.DayCycleClient", return_value=_StubClient(_day_cycle())), \
         patch(f"{module}.PowerSchoolClient",
               return_value=_StubClient(error=RuntimeError("portal down"))):
        _run(app._refresh())

    state, attrs = _published(app)
    assert state == "partial"
    assert attrs["days"]["2026-09-02"]["classes"]  # stale data kept
    assert attrs["sources"]["powerschool"]["status"] == "error"
    assert attrs["sources"]["powerschool"]["error"] == "portal down"
    assert attrs["sources"]["powerschool"]["fetched_at"] == first_fetched
    assert attrs["sources"]["day_cycle"]["status"] == "ok"


def test_calendar_failure_keeps_stale_rotation_and_reports_partial():
    app = _make_app()
    _startup(app, _StubClient(_day_cycle()), _StubClient(_schedule()))

    module = "school_schedule_app.school_schedule_app"
    with patch(f"{module}.DayCycleClient",
               return_value=_StubClient(error=RuntimeError("calendar 503"))), \
         patch(f"{module}.PowerSchoolClient", return_value=_StubClient(_schedule())):
        _run(app._refresh())

    state, attrs = _published(app)
    assert state == "partial"
    assert attrs["dates"]["2026-09-02"] == 1
    assert attrs["sources"]["day_cycle"]["status"] == "error"


def test_both_sources_failing_on_a_cold_start_is_an_error():
    app = _make_app()
    _startup(
        app,
        _StubClient(error=RuntimeError("calendar down")),
        _StubClient(error=RuntimeError("portal down")),
    )
    state, attrs = _published(app)
    assert state == "error"
    assert attrs["dates"] == {}
    assert attrs["days"] == {}
    assert attrs["sources"]["day_cycle"]["fetched_at"] == ""


def test_error_details_redact_hosts_and_credentials():
    """aiohttp errors quote the URL they failed on; the frontend sees `sources`."""
    app = _make_app()
    boom = RuntimeError(
        f"Cannot connect to host portal.example.org:443 for {TEST_PORTAL_URL}"
        f" as {TEST_USER}/{TEST_PASSWORD}"
    )
    _startup(app, _StubClient(error=RuntimeError(TEST_CALENDAR_URL)), _StubClient(error=boom))

    _, attrs = _published(app)
    portal_error = attrs["sources"]["powerschool"]["error"]
    assert "<powerschool>" in portal_error and "<password>" in portal_error
    assert "portal.example.org" not in portal_error
    assert TEST_USER not in portal_error
    assert "<calendar>" in attrs["sources"]["day_cycle"]["error"]

    logged = " ".join(str(c) for c in app.log.call_args_list)
    assert TEST_PASSWORD not in logged
    assert "portal.example.org" not in logged


def test_long_error_messages_are_truncated():
    app = _make_app()
    _startup(app, _StubClient(_day_cycle()), _StubClient(error=RuntimeError("x" * 500)))
    _, attrs = _published(app)
    assert len(attrs["sources"]["powerschool"]["error"]) == 200


def test_unconfigured_sources_are_reported_without_network_calls(monkeypatch):
    monkeypatch.delenv("MIDDLE_ALL_EVENTS", raising=False)
    monkeypatch.delenv("MIDDLE_PASSWORD", raising=False)
    app = _make_app()
    day_client, portal_client = _StubClient(_day_cycle()), _StubClient(_schedule())
    _startup(app, day_client, portal_client)

    state, attrs = _published(app)
    assert state == "error"
    assert day_client.calls == [] and portal_client.calls == []
    assert "day_cycle_url" in attrs["sources"]["day_cycle"]["error"]
    assert "credentials" in attrs["sources"]["powerschool"]["error"]


def test_credentials_never_reach_the_sensor_attributes():
    app = _make_app()
    _startup(app, _StubClient(_day_cycle()), _StubClient(_schedule()))
    _, attrs = _published(app)
    blob = json.dumps(attrs)
    assert TEST_PASSWORD not in blob
    assert TEST_USER not in blob
    assert TEST_PORTAL_URL not in blob
    assert TEST_CALENDAR_URL not in blob


# ---------------------------------------------------------------------------
# Provider wiring
# ---------------------------------------------------------------------------

def test_powerschool_receives_the_calendar_school_name_and_window():
    portal = _StubClient(_schedule())
    app = _make_app({"weeks_ahead": 2})
    _startup(app, _StubClient(_day_cycle()), portal)

    assert portal.calls[0] == {
        "school_name": "Example Middle School",
        "student_id": "",
        "weeks_ahead": 2,
        "today": TODAY,
    }


def test_resolved_student_id_is_cached_for_later_runs():
    app = _make_app()
    _startup(app, _StubClient(_day_cycle()), _StubClient(_schedule()))
    assert app._student_id == "10001"

    portal = _StubClient(_schedule())
    module = "school_schedule_app.school_schedule_app"
    with patch(f"{module}.DayCycleClient", return_value=_StubClient(_day_cycle())), \
         patch(f"{module}.PowerSchoolClient", return_value=portal):
        _run(app._refresh())
    assert portal.calls[0]["student_id"] == "10001"


def test_an_explicit_student_id_is_passed_through():
    portal = _StubClient(_schedule())
    app = _make_app({"powerschool_student_id": "20002"})
    _startup(app, _StubClient(_day_cycle()), portal)
    assert portal.calls[0]["student_id"] == "20002"


# ---------------------------------------------------------------------------
# Payload budget
# ---------------------------------------------------------------------------

def test_a_realistic_full_year_payload_stays_well_under_the_limit():
    """A whole school year of dates plus three weeks of classes must fit."""
    from providers.school_schedule import day_cycle as dc, powerschool as ps
    from providers.school_schedule.ics import parse_events_by_date

    cycle = dc.build_day_cycle(
        parse_events_by_date((FIXTURES / "events.ics").read_text(encoding="utf-8")),
        school_name="Example Middle School",
    )
    rows = ps.parse_list_schedule((FIXTURES / "myschedule.html").read_text(encoding="utf-8"))
    week, _ = ps.parse_bell_schedule(
        (FIXTURES / "myschedule_bellsched.html").read_text(encoding="utf-8")
    )
    days = {
        (TODAY + datetime.timedelta(days=offset)).isoformat(): list(week["2026-09-02"])
        for offset in range(21)
    }
    schedule = WeeklySchedule(days=days, cycle=ps.build_cycle(rows, TODAY))

    app = _make_app()
    _startup(app, _StubClient(cycle), _StubClient(schedule))
    _, attrs = _published(app)

    assert len(attrs["dates"]) == 181
    assert len(attrs["days"]) == 21
    assert len(json.dumps(attrs).encode("utf-8")) < 48 * 1024


def test_an_oversized_payload_logs_a_warning():
    app = _make_app()
    _startup(app, _StubClient(_day_cycle()), _StubClient(_schedule()))
    app.log.reset_mock()

    with patch("school_schedule_app.school_schedule_app.ATTRIBUTE_WARN_BYTES", 10):
        app._publish_sensor()

    assert any(
        "approaching the" in str(c) and c.kwargs.get("level") == "WARNING"
        for c in app.log.call_args_list
    )
