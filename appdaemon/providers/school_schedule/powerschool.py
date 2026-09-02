"""PowerSchool guardian-portal client (scraping — the portal offers no API).

Everything here is HTML scraping over ``aiohttp`` with a cookie jar.  The
portal has no usable public API for guardians: ``pearson-rest`` wants the mobile
app's digest credential, ``/ws/xte`` is 404, and there is no ICS export.

**Concurrent guardian sessions are forbidden.**  Logging in evicts any other
session for the same account — a parent already signed in gets bounced, and a
parent signing in mid-run kills ours.  So the client logs in exactly once,
fetches everything it needs, and always logs off in a ``finally``.  A mid-run
eviction is detected (the portal serves the login form with HTTP 200, so status
codes are useless) and retried once, whole flow.

Two schedule views are scraped:

* ``myschedule_bellsched.html`` — the weekly grid, date x time.  Authoritative:
  the server has already resolved rotation day, term changes and holidays.  It
  carries clock times but, notably, **no period label**.
* ``myschedule.html`` — the list view.  Carries the period expression
  (``Exp``: ``ADV(1-6) 6PA(1,3,5)``) which expands to the per-rotation-day
  fallback used for dates beyond the fetched weekly window.
"""

from __future__ import annotations

import datetime
import logging
import re
from html import unescape
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import aiohttp

from .types import ClassBlock, ScheduleRow, Student, WeeklySchedule

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 30

# The portal serves the login form with HTTP 200 when a session has been
# evicted, so this marker — not the status code — is the session check.
LOGIN_FORM_MARKER = "LoginForm"
BAD_CREDENTIALS_MARKER = "Invalid Username or Password"
SESSION_COOKIE = "psaid"

TAG_RE = re.compile(r"<[^>]+>")
BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
TD_RE = re.compile(r"<td\b[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)
TR_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)

PRINT_SCHOOL_RE = re.compile(
    r'<div id="print-school">(.*?)</div>', re.IGNORECASE | re.DOTALL
)
SWITCH_STUDENT_RE = re.compile(r"switchStudent\((\d+)\)")

# Bell-schedule grid.
GRID_RE = re.compile(
    r'<table id="tableStudentSchedMatrix".*?</table>', re.IGNORECASE | re.DOTALL
)
GRID_HEADER_DATE_RE = re.compile(
    r'<td[^>]*class="scheduleHeader"[^>]*>\s*<b>\s*[A-Za-z]+\s*<br\s*/?>\s*'
    r"(\d{2}/\d{2}/\d{4})\s*</b>",
    re.IGNORECASE,
)
GRID_CELL_RE = re.compile(
    r'<td[^>]*class="scheduleClass\d+(?:Tick)?"[^>]*name="attCell(\d{8})"[^>]*>'
    r"(.*?)</td>",
    re.IGNORECASE | re.DOTALL,
)
YEAR_BOUND_RE = re.compile(r"psc_(firstDay|lastDay)\s*=\s*parseDate\('(\d{8})'\)")

# List view: `ADV(1-6) 6PA(1,3,5)` -> [("ADV", [1..6]), ("6PA", [1, 3, 5])]
EXP_TOKEN_RE = re.compile(r"([A-Za-z0-9]+)\(([0-9,\-\s]+)\)")
TIME_RANGE_RE = re.compile(
    r"(\d{1,2}:\d{2}\s*[AP]M)\s*-\s*(\d{1,2}:\d{2}\s*[AP]M)", re.IGNORECASE
)

LIST_COLUMNS = ("exp", "term", "course_section", "course", "teacher", "room",
                "enroll", "leave")


class PowerSchoolError(RuntimeError):
    """Any unrecoverable PowerSchool failure."""


class SessionEvicted(PowerSchoolError):
    """The portal served the login form mid-flow — our session was taken."""


def mask_user(user: str) -> str:
    """Mask a username for logging (security rule S6): ``****ab``."""
    user = (user or "").strip()
    if not user:
        return "<unset>"
    return f"****{user[-2:]}" if len(user) > 2 else "****"


# ---------------------------------------------------------------------------
# Pure parsing helpers
# ---------------------------------------------------------------------------

def _text(fragment: str) -> str:
    """Strip tags and entities from an HTML fragment, collapsing whitespace.

    ``&nbsp;`` decodes to U+00A0, which ``str.strip()`` alone leaves behind, so
    it is normalised to a plain space before collapsing.
    """
    plain = unescape(TAG_RE.sub(" ", fragment)).replace("\xa0", " ")
    return " ".join(plain.split())


def parse_school_name(page_html: str) -> str:
    """Read the school name from ``div#print-school``.

    The div holds ``<district><br><span><school></span>``; the span is the
    school, which is what we match against the calendar's school name.
    """
    match = PRINT_SCHOOL_RE.search(page_html)
    if not match:
        return ""
    inner = match.group(1)
    span = re.search(r"<span[^>]*>(.*?)</span>", inner, re.IGNORECASE | re.DOTALL)
    return _text(span.group(1) if span else inner)


def parse_student_ids(page_html: str) -> List[str]:
    """Extract the guardian's student ids from ``switchStudent(<id>)`` links.

    Order is preserved and duplicates removed; names are never captured.
    """
    seen: List[str] = []
    for sid in SWITCH_STUDENT_RE.findall(page_html):
        if sid not in seen:
            seen.append(sid)
    return seen


def parse_year_bounds(page_html: str) -> Tuple[str, str]:
    """Read ``psc_firstDay`` / ``psc_lastDay`` as ISO dates (``""`` if absent)."""
    found = {name: value for name, value in YEAR_BOUND_RE.findall(page_html)}

    def _iso(compact: str) -> str:
        try:
            return datetime.datetime.strptime(compact, "%Y%m%d").date().isoformat()
        except ValueError:
            return ""

    return _iso(found.get("firstDay", "")), _iso(found.get("lastDay", ""))


def to_24h(value: str) -> str:
    """``08:20 AM`` / ``01:20 PM`` -> ``08:20`` / ``13:20`` (``""`` if unparseable)."""
    cleaned = " ".join(value.split()).upper()
    for fmt in ("%I:%M %p", "%H:%M"):
        try:
            return datetime.datetime.strptime(cleaned, fmt).strftime("%H:%M")
        except ValueError:
            continue
    return ""


def parse_bell_schedule(page_html: str) -> Tuple[Dict[str, List[ClassBlock]], List[str]]:
    """Parse the weekly bell-schedule grid.

    Returns ``({ISO date: [ClassBlock, ...]}, [ISO date, ...])`` — the map holds
    only dates that actually have classes; the list is every date column the
    grid rendered, so the caller can tell "no school" apart from "not fetched".

    Each class cell is ``COURSE&nbsp;<br>TEACHER<br>ROOM<br>START - END`` with
    no period label; ``ClassBlock.period`` is therefore left empty here and
    filled in later from the list view.
    """
    grid_match = GRID_RE.search(page_html)
    if not grid_match:
        raise PowerSchoolError(
            "Bell schedule grid (table#tableStudentSchedMatrix) not found — "
            "the portal template may have changed"
        )
    grid = grid_match.group(0)

    columns: List[str] = []
    for raw_date in GRID_HEADER_DATE_RE.findall(grid):
        try:
            iso = datetime.datetime.strptime(raw_date, "%m/%d/%Y").date().isoformat()
        except ValueError:
            logger.warning("Unparseable grid header date %r — skipping column", raw_date)
            continue
        if iso not in columns:
            columns.append(iso)

    days: Dict[str, List[ClassBlock]] = {}
    for compact_date, body in GRID_CELL_RE.findall(grid):
        try:
            iso = datetime.datetime.strptime(compact_date, "%Y%m%d").date().isoformat()
        except ValueError:
            logger.warning("Unparseable attCell date %r — skipping cell", compact_date)
            continue

        parts = [_text(chunk) for chunk in BR_RE.split(body)]
        parts += [""] * (4 - len(parts))
        course, teacher, room, times = parts[0], parts[1], parts[2], parts[3]

        start = end = ""
        time_match = TIME_RANGE_RE.search(times)
        if time_match:
            start, end = to_24h(time_match.group(1)), to_24h(time_match.group(2))
        elif times:
            logger.debug("No time range in bell cell for %s: %r", iso, times)

        if not course:
            logger.debug("Skipping bell cell with no course name on %s", iso)
            continue

        days.setdefault(iso, []).append(
            ClassBlock(course=course, teacher=teacher, room=room, start=start, end=end)
        )

    for iso, blocks in days.items():
        # Document order is already chronological; sorting is cheap insurance
        # against a future template that emits columns row-major.
        blocks.sort(key=lambda b: b.start or "99:99")
        if iso not in columns:
            columns.append(iso)

    return days, sorted(columns)


def expand_exp(exp: str) -> List[Tuple[str, List[int]]]:
    """Expand a PowerSchool ``Exp`` expression to ``[(period, [day, ...])]``.

    ``ADV(1-6) 6PA(1,3,5)`` -> ``[("ADV", [1,2,3,4,5,6]), ("6PA", [1,3,5])]``.
    Day numbers are the rotation cycle directly.
    """
    out: List[Tuple[str, List[int]]] = []
    for period, day_spec in EXP_TOKEN_RE.findall(exp or ""):
        days: List[int] = []
        for chunk in day_spec.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            if "-" in chunk:
                low, _, high = chunk.partition("-")
                try:
                    lo, hi = int(low.strip()), int(high.strip())
                except ValueError:
                    logger.warning("Unparseable Exp day range %r in %r", chunk, exp)
                    continue
                if lo > hi:
                    lo, hi = hi, lo
                days.extend(range(lo, hi + 1))
            else:
                try:
                    days.append(int(chunk))
                except ValueError:
                    logger.warning("Unparseable Exp day %r in %r", chunk, exp)
        deduped = sorted(dict.fromkeys(days))
        if deduped:
            out.append((period, deduped))
    return out


def period_sort_key(period: str, index: int) -> Tuple[int, int, int]:
    """Order periods the way the school's matrix header does.

    ``ADV`` first, then ``{grade}B1``..``{grade}B5``, ``{grade}PA``,
    ``{grade}B6``..``{grade}B8``; anything unrecognised sorts last in the order
    it was seen.
    """
    token = (period or "").strip().upper()
    if token == "ADV":
        return (0, 0, index)
    block = re.match(r"^\d*B(\d+)$", token)
    if block:
        number = int(block.group(1))
        # PA sits between B5 and B6, so B6+ shifts one slot later.
        return (1, number if number <= 5 else number + 1, index)
    if re.match(r"^\d*PA$", token):
        return (1, 6, index)
    return (2, 0, index)


def parse_list_schedule(page_html: str) -> List[ScheduleRow]:
    """Parse the list view (``table#results``) into rows.

    Columns are ``Exp | Trm | Crs-Sec | Course Name | Teacher | Room | Enroll |
    Leave``.  Rows with fewer cells (spacers, totals) are skipped.
    """
    table = _extract_table(page_html, "results")
    if table is None:
        raise PowerSchoolError(
            "Schedule list table (table#results) not found — "
            "the portal template may have changed"
        )

    rows: List[ScheduleRow] = []
    for row_html in TR_RE.findall(table):
        if "<th" in row_html.lower():
            continue
        cells = [_text(cell) for cell in TD_RE.findall(row_html)]
        if len(cells) < len(LIST_COLUMNS):
            continue
        values = dict(zip(LIST_COLUMNS, cells[: len(LIST_COLUMNS)]))
        if not values["course"]:
            continue
        row = ScheduleRow(**values)
        row.periods = expand_exp(row.exp)
        rows.append(row)

    return rows


def _extract_table(page_html: str, table_id: str) -> Optional[str]:
    """Slice out ``<table id="...">...</table>``, honouring nested tables."""
    start = re.search(
        rf'<table\b[^>]*\bid="{re.escape(table_id)}"', page_html, re.IGNORECASE
    )
    if not start:
        return None
    depth = 0
    for token in re.finditer(r"</?table\b", page_html[start.start():], re.IGNORECASE):
        if token.group(0).lower() == "<table":
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                return page_html[start.start(): start.start() + token.end() + 1]
    return page_html[start.start():]


def rows_active_on(
    rows: Iterable[ScheduleRow], on_date: datetime.date
) -> List[ScheduleRow]:
    """Filter list rows to those whose enrolment window contains ``on_date``.

    A row with an unparseable or missing date is kept — dropping a class because
    a date failed to parse is worse than showing one that has ended.
    """
    kept: List[ScheduleRow] = []
    for row in rows:
        enroll = _parse_us_date(row.enroll)
        leave = _parse_us_date(row.leave)
        if enroll and on_date < enroll:
            continue
        if leave and on_date > leave:
            continue
        kept.append(row)
    return kept


def _monday_on_or_after(day: datetime.date) -> datetime.date:
    """The Monday of ``day``'s week, or ``day`` itself when it is a Monday."""
    return day + datetime.timedelta(days=(7 - day.weekday()) % 7)


def _parse_us_date(value: str) -> Optional[datetime.date]:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.datetime.strptime(value, "%m/%d/%Y").date()
    except ValueError:
        return None


def build_cycle(
    rows: Sequence[ScheduleRow],
    on_date: Optional[datetime.date] = None,
    cycle_length: int = 6,
) -> Dict[int, List[ClassBlock]]:
    """Build ``{rotation day: [ClassBlock, ...]}`` from list rows.

    Only rows active on ``on_date`` are used (terms change mid-year), and each
    day's blocks come out in matrix-header period order.
    """
    on_date = on_date or datetime.date.today()
    active = rows_active_on(rows, on_date)

    buckets: Dict[int, List[Tuple[Tuple[int, int, int], ClassBlock]]] = {}
    for index, row in enumerate(active):
        for period, days in row.periods:
            key = period_sort_key(period, index)
            block = ClassBlock(
                period=period,
                course=row.course,
                teacher=row.teacher,
                room=row.room,
            )
            for day in days:
                if not 1 <= day <= cycle_length:
                    logger.warning(
                        "Ignoring out-of-range rotation day %s in Exp %r", day, row.exp
                    )
                    continue
                buckets.setdefault(day, []).append((key, block))

    return {
        day: [block for _, block in sorted(entries, key=lambda pair: pair[0])]
        for day, entries in sorted(buckets.items())
    }


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class PowerSchoolClient:
    """Scrapes one student's schedule out of the PowerSchool guardian portal.

    ::

        async with PowerSchoolClient(base_url, user, password) as client:
            schedule = await client.fetch_schedule(school_name="Example Middle")

    ``fetch_schedule`` owns the whole session lifecycle: one login, all fetches,
    then a logoff — see the module docstring on session eviction.
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        session: Optional[aiohttp.ClientSession] = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self._username = username or ""
        self._password = password or ""
        self._session = session
        self._owns_session = session is None
        self._timeout = timeout
        self._student_id = ""

    # -- Context manager ---------------------------------------------------

    async def __aenter__(self) -> "PowerSchoolClient":
        if self._session is None:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": USER_AGENT},
                timeout=aiohttp.ClientTimeout(total=self._timeout),
                cookie_jar=aiohttp.CookieJar(),
            )
            self._owns_session = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError(
                "PowerSchoolClient has no active session — "
                "use 'async with PowerSchoolClient(...)' or pass a session"
            )
        return self._session

    @property
    def student_id(self) -> str:
        """The student id resolved on the last successful fetch (cached)."""
        return self._student_id

    # -- HTTP --------------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    async def _get(self, path: str, *, allow_login_form: bool = False) -> str:
        """GET a guardian page.  Logs the path only — never the secret host."""
        async with self.session.get(self._url(path)) as resp:
            body = await resp.text()
        logger.debug("GET %s -> %s (%d bytes)", path.split("?")[0], resp.status, len(body))
        if resp.status >= 400:
            raise PowerSchoolError(f"GET {path.split('?')[0]} returned HTTP {resp.status}")
        if not allow_login_form and LOGIN_FORM_MARKER in body:
            raise SessionEvicted(
                f"Login form served for {path.split('?')[0]} — session evicted"
            )
        return body

    def _require_config(self) -> None:
        """Fail before any network call when the portal is not configured."""
        if not self.base_url:
            raise PowerSchoolError("No PowerSchool base URL configured")
        if not self._username or not self._password:
            raise PowerSchoolError("PowerSchool username/password not configured")

    async def _login(self) -> None:
        """Log in.  Plaintext form POST — this portal uses no pstoken/HMAC/MFA."""
        self._require_config()

        # Seed cookies; this lands on the public login page by design.
        await self._get("/guardian/home.html", allow_login_form=True)

        form = {
            "account": self._username,
            "pw": self._password,
            "dbpw": self._password,
            "translatorpw": "",
            "translator_username": "",
            "translator_password": "",
            "translator_ldappassword": "",
            "returnUrl": "",
            "serviceTicket": "",
            "pcasServerUrl": "/",
            "serviceName": "PS Parent Portal",
            "credentialType": "User Id and Password Credential",
        }
        async with self.session.post(self._url("/guardian/home.html"), data=form) as resp:
            body = await resp.text()
            status = resp.status

        if BAD_CREDENTIALS_MARKER in body:
            raise PowerSchoolError(
                f"PowerSchool rejected the credentials for user {mask_user(self._username)}"
            )
        if LOGIN_FORM_MARKER in body:
            raise PowerSchoolError(
                f"PowerSchool login did not take (HTTP {status}) — login form returned"
            )
        if not self._has_session_cookie():
            raise PowerSchoolError(
                f"PowerSchool login did not set the {SESSION_COOKIE} cookie "
                f"(HTTP {status})"
            )

        logger.info("PowerSchool login OK for user %s", mask_user(self._username))

    def _has_session_cookie(self) -> bool:
        """True when the jar holds the portal's session cookie.

        Iterating the jar (rather than ``filter_cookies``) avoids depending on
        how the portal scopes the cookie's domain across its login redirects.
        """
        try:
            return any(
                cookie.key == SESSION_COOKIE for cookie in self.session.cookie_jar
            )
        except TypeError:  # a stub jar in tests need not be iterable
            return False

    async def _logoff(self) -> None:
        """Best-effort logoff — a stale session would evict the family's next login."""
        try:
            await self._get("/guardian/home.html?ac=logoff", allow_login_form=True)
            logger.debug("PowerSchool session logged off")
        except Exception as exc:  # noqa: BLE001 — never mask the real error
            logger.warning("PowerSchool logoff failed: %s", exc)

    # -- Student selection -------------------------------------------------

    async def _resolve_student(self, school_name: str, preferred_id: str = "") -> str:
        """Pick which student on the guardian account we are scraping.

        Order: an explicitly configured id, then the single student if there is
        only one, then the student whose school matches the calendar's school
        name.  Errors list schools only — never student names.
        """
        if preferred_id:
            logger.debug("Using configured PowerSchool student id")
            return preferred_id

        home = await self._get("/guardian/home.html")
        ids = parse_student_ids(home)
        if not ids:
            raise PowerSchoolError(
                "No students found on the guardian account (no switchStudent links)"
            )
        if len(ids) == 1:
            logger.info("Guardian account has one student — selecting it")
            return ids[0]

        students: List[Student] = []
        for sid in ids:
            page = await self._get(f"/guardian/myschedule.html?selected_student_id={sid}")
            students.append(Student(student_id=sid, school=parse_school_name(page)))

        wanted = (school_name or "").strip().lower()
        if wanted:
            for student in students:
                school = student.school.strip().lower()
                if school and (school in wanted or wanted in school):
                    logger.info(
                        "Matched PowerSchool student by school %r", student.school
                    )
                    return student.student_id

        raise PowerSchoolError(
            "Could not decide which student to use — "
            f"calendar school {school_name!r} matched none of "
            f"{[s.school for s in students]}. Set powerschool_student_id."
        )

    # -- Schedule fetching -------------------------------------------------

    async def fetch_schedule(
        self,
        *,
        school_name: str = "",
        student_id: str = "",
        weeks_ahead: int = 3,
        today: Optional[datetime.date] = None,
    ) -> WeeklySchedule:
        """Log in, scrape the schedule, log off.  Retries once on eviction."""
        today = today or datetime.date.today()
        try:
            return await self._fetch_once(school_name, student_id, weeks_ahead, today)
        except SessionEvicted as exc:
            logger.warning(
                "PowerSchool session evicted mid-fetch (%s) — retrying once", exc
            )
        return await self._fetch_once(school_name, student_id, weeks_ahead, today)

    async def _fetch_once(
        self,
        school_name: str,
        student_id: str,
        weeks_ahead: int,
        today: datetime.date,
    ) -> WeeklySchedule:
        self._require_config()
        try:
            await self._login()
            chosen = await self._resolve_student(
                school_name, student_id or self._student_id
            )
            self._student_id = chosen

            list_page = await self._get(
                f"/guardian/myschedule.html?selected_student_id={chosen}"
            )
            rows = parse_list_schedule(list_page)
            cycle = build_cycle(rows, today)
            logger.info(
                "PowerSchool list view: %d rows, %d rotation days populated",
                len(rows), len(cycle),
            )

            days, first_day, last_day = await self._fetch_weeks(
                chosen, today, weeks_ahead
            )

            return WeeklySchedule(
                days=days,
                cycle=cycle,
                first_day=first_day,
                last_day=last_day,
                school_name=parse_school_name(list_page),
                student_id=chosen,
            )
        finally:
            # Always log off, even after a failed login: a stranded session
            # would evict the family's next sign-in.
            await self._logoff()

    async def _fetch_weeks(
        self,
        student_id: str,
        today: datetime.date,
        weeks_ahead: int,
    ) -> Tuple[Dict[str, List[ClassBlock]], str, str]:
        """Fetch the bell-schedule grid from ``today`` forward.

        Asks for the whole range in one request first.  The portal is only known
        to honour a single week per request, so when the response stops short
        the remaining weeks are fetched Monday-to-Friday, one request each,
        starting from the Monday after whatever the first response covered.
        """
        weeks = max(0, int(weeks_ahead))
        end = today + datetime.timedelta(days=weeks * 7)

        days: Dict[str, List[ClassBlock]] = {}

        page = await self._get(self._bellsched_path(student_id, today, end))
        first_day, last_day = parse_year_bounds(page)
        week_days, columns = parse_bell_schedule(page)
        days.update(week_days)

        covered = max(columns) if columns else today.isoformat()
        if end.isoformat() <= covered:
            logger.info(
                "Bell schedule: one request covered through %s (%d dates with classes)",
                covered, len(days),
            )
            return days, first_day, last_day

        logger.debug(
            "Bell schedule range request covered only through %s — "
            "falling back to one request per week", covered,
        )
        cursor = _monday_on_or_after(
            datetime.date.fromisoformat(covered) + datetime.timedelta(days=1)
        )
        # Defensive cap: a portal that always returns the same week must not
        # turn into an unbounded request loop against the family's account.
        for _ in range(weeks + 2):
            if cursor > end:
                break
            page = await self._get(
                self._bellsched_path(student_id, cursor, cursor + datetime.timedelta(days=4))
            )
            week_days, _ = parse_bell_schedule(page)
            days.update(week_days)
            cursor += datetime.timedelta(days=7)

        logger.info(
            "Bell schedule: %d dates with classes through %s", len(days), end.isoformat()
        )
        return days, first_day, last_day

    @staticmethod
    def _bellsched_path(
        student_id: str, start: datetime.date, end: datetime.date
    ) -> str:
        return (
            "/guardian/myschedule_bellsched.html"
            f"?selected_student_id={student_id}"
            f"&startdate={start:%m/%d/%Y}&enddate={end:%m/%d/%Y}"
        )
