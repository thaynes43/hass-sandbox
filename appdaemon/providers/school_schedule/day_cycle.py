"""Day-cycle client for a Finalsite Composer school calendar.

The school publishes its six-day rotation as ordinary calendar events titled
``Day 1`` .. ``Day 6``, alongside no-school and early-release annotations.  Two
fetch paths exist, both unauthenticated:

1. **ICS feed** (preferred) — one page GET to discover the calendar element,
   then one GET of ``/fs/calendar-manager/events.ics``.  That single ~50 KB file
   covers the whole school year, so the sensor knows every rotation day through
   June from one fetch a day.
2. **Month fragments** (fallback) — ``/fs/elements/<element_id>?cal_date=...``
   returns pre-expanded HTML for one month.  Used only when the ICS fetch or
   parse fails; covers the current month plus the next two.

All parsing helpers are pure functions so the tests exercise them against saved
fixtures with no network.

Ops notes: the site is Cloudflare-fronted and its ``ETag`` changes on every
response, so conditional GETs are pointless — we just fetch once a day with a
browser-like ``User-Agent``.
"""

from __future__ import annotations

import datetime
import logging
import re
from html import unescape
from typing import Dict, List, Optional
from urllib.parse import urlsplit

import aiohttp

from .ics import parse_events_by_date
from .types import CalendarElement, DayCycle

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

DEFAULT_TIMEOUT = 30
CYCLE_LENGTH = 6

# ``Day 3``, ``Day 1 (Repeat)`` — anchored so "Grade 6 Orientation" and
# "Field Day 2" never masquerade as rotation days.
DAY_NUMBER_RE = re.compile(r"^\s*Day\s*([1-6])\b", re.IGNORECASE)
CLOSURE_RE = re.compile(r"no\s*school", re.IGNORECASE)
NOTE_RE = re.compile(r"early\s*release|delay", re.IGNORECASE)

CALENDAR_ELEMENT_RE = re.compile(
    r'class="fsElement fsCalendar[^"]*" id="fsEl_(\d+)" '
    r'data-calendar-ids=([\d,]+) '
    r'data-calendars-feed-uuid="([0-9a-f-]+)"'
)
TITLE_RE = re.compile(r"<title>\s*(.*?)\s*</title>", re.IGNORECASE | re.DOTALL)

# Month-fragment fallback markers.
DAYBOX_SPLIT_RE = re.compile(r'<div class="fsCalendarDaybox')
DAYBOX_DATE_RE = re.compile(
    r'<div class="fsCalendarDate" data-day="(\d+)" data-year="(\d+)" data-month="(\d+)"'
)
DAYBOX_TITLE_RE = re.compile(
    r'class="(?:fsCalendarEventLink fsCalendarLongEventTitle|'
    r'fsCalendarTitle fsCalendarEventLink)"[^>]*>\s*(.*?)\s*</div>',
    re.DOTALL,
)
TAG_RE = re.compile(r"<[^>]+>")


# ---------------------------------------------------------------------------
# Pure parsing helpers
# ---------------------------------------------------------------------------

def discover_calendar(page_html: str, page_url: str = "") -> CalendarElement:
    """Find the calendar element and school name on the "all events" page.

    The element carries the numeric calendar ids and the feed UUID needed to
    build the ICS URL; ``<title>View All Events - <School></title>`` gives the
    school name, which is what auto-selects the right PowerSchool student.
    """
    element = CalendarElement()

    split = urlsplit(page_url) if page_url else None
    if split and split.scheme and split.netloc:
        element.base_url = f"{split.scheme}://{split.netloc}"

    match = CALENDAR_ELEMENT_RE.search(page_html)
    if match:
        element.element_id, element.calendar_ids, element.feed_uuid = match.groups()
    else:
        logger.warning(
            "No fsCalendar element found on the all-events page (%d bytes) — "
            "the site template may have changed",
            len(page_html),
        )

    title_match = TITLE_RE.search(page_html)
    if title_match:
        title = unescape(TAG_RE.sub("", title_match.group(1))).strip()
        # "View All Events - Example Middle School" -> "Example Middle School"
        element.school_name = title.split(" - ", 1)[-1].strip() if " - " in title else title

    return element


def build_day_cycle(
    events_by_date: Dict[datetime.date, List[str]],
    *,
    school_name: str = "",
    source: str = "",
) -> DayCycle:
    """Classify ``{date: [summary, ...]}`` into rotation days, closures and notes.

    * ``Day N`` (anchored) sets the rotation day for that date.
    * ``no school`` marks a closure — weekdays only, because the feed also
      titles weekend/holiday-break spans that way and a Saturday closure tells
      the card nothing.
    * ``early release`` / ``delay`` becomes a note; the date keeps its day
      number because school still happens.

    A closure wins over a rotation day if the feed ever emits both for one date
    (it normally uses ``EXDATE`` to prevent that).
    """
    dates: Dict[str, int] = {}
    closures: Dict[str, str] = {}
    notes: Dict[str, str] = {}

    for day, summaries in sorted(events_by_date.items()):
        key = day.isoformat()
        is_weekday = day.weekday() < 5
        for summary in summaries:
            day_match = DAY_NUMBER_RE.match(summary)
            if day_match and is_weekday:
                dates[key] = int(day_match.group(1))
            if CLOSURE_RE.search(summary) and is_weekday:
                closures[key] = summary
            if NOTE_RE.search(summary):
                notes[key] = summary

    conflicts = sorted(set(dates) & set(closures))
    for key in conflicts:
        dates.pop(key, None)
    if conflicts:
        logger.warning(
            "%d date(s) carried both a rotation day and a no-school event — "
            "treating them as closures: %s",
            len(conflicts),
            ", ".join(conflicts),
        )

    return DayCycle(
        school_name=school_name,
        cycle_length=CYCLE_LENGTH,
        dates=dates,
        closures=closures,
        notes=notes,
        source=source,
    )


def parse_month_fragment(fragment_html: str) -> Dict[datetime.date, List[str]]:
    """Parse a Finalsite month fragment into ``{date: [summary, ...]}``.

    ``data-month`` in the fragment is **0-based** (September is ``8``).  Each
    event is rendered twice per daybox (once for the grid, once for the hidden
    day view), so titles are deduped.
    """
    by_date: Dict[datetime.date, List[str]] = {}

    for chunk in DAYBOX_SPLIT_RE.split(fragment_html)[1:]:
        date_match = DAYBOX_DATE_RE.search(chunk)
        if not date_match:
            continue
        day_num, year, month0 = (int(g) for g in date_match.groups())
        try:
            day = datetime.date(year, month0 + 1, day_num)
        except ValueError:
            logger.warning(
                "Skipping daybox with an impossible date: day=%s year=%s month0=%s",
                day_num, year, month0,
            )
            continue

        bucket = by_date.setdefault(day, [])
        for raw_title in DAYBOX_TITLE_RE.findall(chunk):
            title = unescape(TAG_RE.sub("", raw_title)).strip()
            if title and title not in bucket:
                bucket.append(title)

    return dict(sorted(by_date.items()))


def month_starts(today: datetime.date, count: int = 3) -> List[datetime.date]:
    """First-of-month dates for ``today``'s month and the following ones."""
    starts: List[datetime.date] = []
    year, month = today.year, today.month
    for _ in range(max(1, count)):
        starts.append(datetime.date(year, month, 1))
        month += 1
        if month > 12:
            month = 1
            year += 1
    return starts


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class DayCycleClient:
    """Fetches the six-day rotation from a Finalsite school calendar.

    Use as an async context manager, or pass an existing
    ``aiohttp.ClientSession``::

        async with DayCycleClient(all_events_url) as client:
            cycle = await client.fetch()
    """

    def __init__(
        self,
        all_events_url: str,
        session: Optional[aiohttp.ClientSession] = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self.all_events_url = (all_events_url or "").strip()
        self._session = session
        self._owns_session = session is None
        self._timeout = timeout

    # -- Context manager ---------------------------------------------------

    async def __aenter__(self) -> "DayCycleClient":
        if self._session is None:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": USER_AGENT},
                timeout=aiohttp.ClientTimeout(total=self._timeout),
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
                "DayCycleClient has no active session — "
                "use 'async with DayCycleClient(...)' or pass a session"
            )
        return self._session

    # -- Fetch -------------------------------------------------------------

    async def _get_text(self, url: str, what: str) -> str:
        """GET ``url`` and return its body.  Logs the path only, never the host."""
        path = urlsplit(url).path or "/"
        async with self.session.get(
            url, headers={"User-Agent": USER_AGENT}
        ) as resp:
            body = await resp.text()
        logger.debug(
            "Fetched %s (path=%s, status=%s, %d bytes)", what, path, resp.status, len(body)
        )
        if resp.status >= 400:
            raise RuntimeError(f"{what} returned HTTP {resp.status} for {path}")
        return body

    async def fetch(self) -> DayCycle:
        """Fetch and classify the school year's rotation days.

        Tries the ICS feed first and falls back to month fragments.  Raises when
        neither path yields any rotation days — the app keeps its previous data
        and marks the source as errored.
        """
        if not self.all_events_url:
            raise ValueError("No all-events URL configured for the day cycle")

        page = await self._get_text(self.all_events_url, "all-events page")
        element = discover_calendar(page, self.all_events_url)
        if not element.base_url:
            raise ValueError("Could not determine the calendar host from the all-events URL")

        if element.calendar_ids:
            try:
                return await self._fetch_ics(element)
            except Exception as exc:  # noqa: BLE001 — fall back, never abort
                logger.warning(
                    "ICS feed unusable (%s) — falling back to month fragments", exc
                )
        else:
            logger.warning(
                "No calendar ids discovered — going straight to month fragments"
            )

        return await self._fetch_fragments(element)

    async def _fetch_ics(self, element: CalendarElement) -> DayCycle:
        query = "&".join(
            f"calendar_ids[]={cid.strip()}"
            for cid in element.calendar_ids.split(",")
            if cid.strip()
        )
        url = f"{element.base_url}/fs/calendar-manager/events.ics?{query}"
        body = await self._get_text(url, "calendar ICS feed")

        cycle = build_day_cycle(
            parse_events_by_date(body),
            school_name=element.school_name,
            source="ics",
        )
        if not cycle.dates:
            # After the last day of school the feed is legitimately empty; the
            # caller decides whether that is stale-but-fine or a real problem.
            raise ValueError("ICS feed contained no 'Day N' rotation events")

        logger.info(
            "Day cycle from ICS: %d school days, %d closures, %d notes (school=%r)",
            len(cycle.dates), len(cycle.closures), len(cycle.notes), cycle.school_name,
        )
        return cycle

    async def _fetch_fragments(
        self,
        element: CalendarElement,
        today: Optional[datetime.date] = None,
        months: int = 3,
    ) -> DayCycle:
        if not element.element_id:
            raise ValueError("No calendar element id — cannot fetch month fragments")

        today = today or datetime.date.today()
        merged: Dict[datetime.date, List[str]] = {}
        failures = 0

        for start in month_starts(today, months):
            url = (
                f"{element.base_url}/fs/elements/{element.element_id}"
                f"?cal_date={start.isoformat()}"
            )
            try:
                body = await self._get_text(url, f"calendar fragment {start:%Y-%m}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                logger.warning("Month fragment %s failed: %s", f"{start:%Y-%m}", exc)
                continue
            for day, titles in parse_month_fragment(body).items():
                bucket = merged.setdefault(day, [])
                for title in titles:
                    if title not in bucket:
                        bucket.append(title)

        cycle = build_day_cycle(
            merged, school_name=element.school_name, source="html",
        )
        if not cycle.dates:
            raise ValueError(
                f"Month fragments yielded no rotation days ({failures} fetch failure(s))"
            )

        logger.info(
            "Day cycle from month fragments: %d school days, %d closures, %d notes",
            len(cycle.dates), len(cycle.closures), len(cycle.notes),
        )
        return cycle
