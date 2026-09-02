"""School Schedule AppDaemon app.

Publishes ``sensor.school_schedule`` so a wall-display card can show which day
of the school's six-day rotation today and the next school day are, with one
icon per class.

Two independent sources, both scraped once a day (startup + ``refresh_time``,
same cadence as ``school_lunch_app``):

* the school's public Finalsite calendar — which rotation day each date is,
  plus no-school and early-release annotations;
* the PowerSchool guardian portal — which classes the student actually has.

Either source failing leaves the previous good data in place and marks that
source as errored in the sensor's ``sources`` attribute, so a portal outage
degrades the card rather than blanking it.

All external HTTP lives in ``providers/school_schedule/`` (security rule S2);
this module only ever sees env var *names* for credentials (S7) and never logs
them (S6).
"""

from __future__ import annotations

import datetime
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

# AppDaemon only adds `appdaemon/apps` to sys.path. Our shared libraries
# live at `appdaemon/providers`, so add the AppDaemon root directory.
sys.path.append(str(Path(__file__).resolve().parents[2]))

import hassapi as hass

from providers.school_schedule.day_cycle import DayCycleClient
from providers.school_schedule.powerschool import PowerSchoolClient, mask_user
from providers.school_schedule.types import ClassBlock, DayCycle, WeeklySchedule
from providers.secrets import resolve_arg_secret

logger = logging.getLogger(__name__)

SENSOR_ENTITY_ID = "sensor.school_schedule"

DEFAULT_REFRESH_TIME = "05:00:00"
DEFAULT_WEEKS_AHEAD = 3
DEFAULT_ICON = "mdi:school"

# Courses that carry no useful signal on a nine-icon row.  Also removes the
# duplicate Advisory block that odd rotation days carry twice.
DEFAULT_HIDE_COURSES = [r"\blunch\b", r"\badvisory\b", r"\bhomeroom\b"]

# First match wins, so the order matters: "Theater Arts 6" must reach the
# theatre rule before the (word-bounded) art rule sees it.
DEFAULT_ICON_RULES: List[Dict[str, str]] = [
    {"match": r"theat|drama", "icon": "mdi:drama-masks", "short": "Theater"},
    {"match": r"\bart\b", "icon": "mdi:palette", "short": "Art"},
    {"match": r"\bmath|algebra|geometry", "icon": "mdi:calculator-variant", "short": "Math"},
    {
        "match": r"^la\b|language arts|\bela\b|english|\breading\b|\bwriting\b",
        "icon": "mdi:book-open-page-variant",
        "short": "LA",
    },
    {"match": r"science", "icon": "mdi:flask", "short": "Science"},
    {
        "match": r"soc\s*stud|social|history|civics|geography",
        "icon": "mdi:earth",
        "short": "Social Studies",
    },
    {
        "match": r"spanish|french|latin|world lang|\bwl\b",
        "icon": "mdi:translate",
        "short": "Language",
    },
    {"match": r"phys\s*ed|physical|\bpe\b|\bgym\b", "icon": "mdi:run", "short": "PE"},
    {"match": r"health|wellness", "icon": "mdi:heart-pulse", "short": "Health"},
    {"match": r"steam|stem|engineering|robot", "icon": "mdi:cog", "short": "STEAM"},
    {"match": r"tech|computer|coding|digital", "icon": "mdi:laptop", "short": "Tech"},
    {"match": r"\bband\b", "icon": "mdi:trumpet", "short": "Band"},
    {"match": r"chorus|choir", "icon": "mdi:microphone-variant", "short": "Chorus"},
    {"match": r"orchestra|strings", "icon": "mdi:violin", "short": "Orchestra"},
    {"match": r"music", "icon": "mdi:music", "short": "Music"},
    {"match": r"library|media center", "icon": "mdi:library", "short": "Library"},
    {"match": r"\bflex\b", "icon": "mdi:puzzle-outline", "short": "FLEX"},
]

# The sensor attribute payload has a hard 64 KB ceiling in HA; warn well before.
ATTRIBUTE_WARN_BYTES = 48 * 1024


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested directly)
# ---------------------------------------------------------------------------

def compile_patterns(patterns: Sequence[str]) -> List[re.Pattern]:
    """Compile case-insensitive regexes, skipping (and logging) bad ones."""
    compiled: List[re.Pattern] = []
    for pattern in patterns or []:
        try:
            compiled.append(re.compile(pattern, re.IGNORECASE))
        except re.error as exc:
            logger.error("Ignoring invalid course pattern %r: %s", pattern, exc)
    return compiled


def compile_icon_rules(
    rules: Sequence[Dict[str, str]],
) -> List[Tuple[re.Pattern, str, str]]:
    """Compile ``[{match, icon, short}]`` into ordered match rules."""
    compiled: List[Tuple[re.Pattern, str, str]] = []
    for rule in rules or []:
        pattern = str(rule.get("match", "")).strip()
        if not pattern:
            continue
        try:
            compiled.append(
                (
                    re.compile(pattern, re.IGNORECASE),
                    str(rule.get("icon") or DEFAULT_ICON),
                    str(rule.get("short") or ""),
                )
            )
        except re.error as exc:
            logger.error("Ignoring invalid icon rule %r: %s", pattern, exc)
    return compiled


def resolve_icon(
    course: str, rules: Sequence[Tuple[re.Pattern, str, str]]
) -> Tuple[str, str]:
    """Return ``(icon, short label)`` for a course name — first rule wins.

    A rule without a ``short`` falls back to the course name, so an unmatched
    or unlabelled course still gets a readable tooltip on the card.
    """
    name = (course or "").strip()
    for pattern, icon, short in rules:
        if pattern.search(name):
            return icon, (short or name)
    return DEFAULT_ICON, name


def is_hidden(course: str, patterns: Sequence[re.Pattern]) -> bool:
    """True when a course should be left off the card."""
    name = (course or "").strip()
    return any(pattern.search(name) for pattern in patterns)


def attach_periods(
    blocks: Sequence[ClassBlock],
    cycle_blocks: Sequence[ClassBlock],
) -> List[ClassBlock]:
    """Fill in the period label the bell-schedule grid does not carry.

    The grid gives clock times but no period; the list view gives periods but
    no times.  They are matched by course name **in occurrence order**, which
    is what disambiguates a course that meets twice in one day (Advisory sits
    at both ``ADV`` and ``6PA``).  Anything unmatched keeps an empty period.
    """
    if not cycle_blocks:
        return list(blocks)

    remaining: Dict[str, List[str]] = {}
    for block in cycle_blocks:
        remaining.setdefault(block.course.strip().lower(), []).append(block.period)

    filled: List[ClassBlock] = []
    for block in blocks:
        period = block.period
        if not period:
            queue = remaining.get(block.course.strip().lower())
            if queue:
                period = queue.pop(0)
        filled.append(
            ClassBlock(
                period=period,
                course=block.course,
                teacher=block.teacher,
                room=block.room,
                start=block.start,
                end=block.end,
            )
        )
    return filled


def parse_time_of_day(value: str, default: datetime.time) -> datetime.time:
    """Parse ``HH:MM:SS`` / ``HH:MM`` from config, falling back on garbage."""
    text = str(value or "").strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    logger.warning("Unparseable refresh_time %r — using %s", value, default)
    return default


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class SchoolScheduleApp(hass.Hass):
    """Publishes the six-day rotation and each day's classes to one sensor."""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        args = self.args or {}

        self._school_label: str = str(args.get("name") or "School")
        self._weeks_ahead: int = int(args.get("weeks_ahead") or DEFAULT_WEEKS_AHEAD)
        self._refresh_time: datetime.time = parse_time_of_day(
            args.get("refresh_time", DEFAULT_REFRESH_TIME), datetime.time(5, 0)
        )

        self._hide_courses = compile_patterns(
            args.get("hide_courses", DEFAULT_HIDE_COURSES)
        )
        self._icon_rules = compile_icon_rules(
            args.get("icon_rules", DEFAULT_ICON_RULES)
        )

        # Credentials arrive as env var NAMES (security rule S7).
        self._day_cycle_url: str = self._resolve("day_cycle_url")
        self._powerschool_url: str = self._resolve("powerschool_url")
        self._powerschool_user: str = self._resolve("powerschool_user")
        self._powerschool_password: str = self._resolve("powerschool_password")
        self._student_id: str = self._resolve("powerschool_student_id")

        # Last good data, kept across failed refreshes.
        self._day_cycle: Optional[DayCycle] = None
        self._schedule: Optional[WeeklySchedule] = None
        self._sources: Dict[str, Dict[str, str]] = {
            "day_cycle": {"status": "unknown", "fetched_at": "", "error": ""},
            "powerschool": {"status": "unknown", "fetched_at": "", "error": ""},
        }

        self.log(
            f"SchoolScheduleApp initialising: school={self._school_label!r}, "
            f"powerschool_user={mask_user(self._powerschool_user)}, "
            f"weeks_ahead={self._weeks_ahead}, refresh_time={self._refresh_time}, "
            f"calendar_configured={bool(self._day_cycle_url)}, "
            f"portal_configured={bool(self._powerschool_url)}",
            level="INFO",
        )

        self.run_in(self._on_startup, 0)

    def _resolve(self, key: str) -> str:
        """Resolve a ``<key>`` / ``<key>_env`` config value, never raising."""
        try:
            return str(resolve_arg_secret(self.args or {}, key, default="") or "")
        except ValueError as exc:
            self.log(f"Cannot resolve config '{key}': {exc}", level="ERROR")
            return ""

    def _redact(self, text: Any) -> str:
        """Strip configured secrets out of a message before it is logged or published.

        Exceptions raised *below* our own code — an ``aiohttp`` connection error,
        for instance — quote the URL they failed on, and both hosts identify the
        family's school.  Errors land in the sensor's ``sources`` attribute,
        which the frontend renders, so this runs on every one of them (S3/S6).
        """
        out = str(text)
        for value, label in (
            (self._powerschool_password, "<password>"),
            (self._powerschool_url, "<powerschool>"),
            (self._day_cycle_url, "<calendar>"),
            (self._powerschool_user, "<user>"),
        ):
            if not value:
                continue
            out = out.replace(value, label)
            host = urlsplit(value).netloc if "//" in value else ""
            if host:
                out = out.replace(host, label)
        return out

    def _on_startup(self, kwargs: Any) -> None:
        """run_in callback — launches the async startup coroutine."""
        self.create_task(self._async_startup())

    async def _async_startup(self) -> None:
        await self._refresh()

        self.run_daily(self._daily_refresh, self._refresh_time)
        # Roll `today` / `next` over at midnight without touching the network:
        # the portal must not be logged into more than once a day.
        self.run_daily(self._on_midnight, datetime.time(0, 0, 30))

        self.log(
            f"SchoolScheduleApp started: daily refresh at {self._refresh_time}, "
            f"midnight republish at 00:00:30",
            level="INFO",
        )

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def _daily_refresh(self, kwargs: Any) -> None:
        self.log("Daily school schedule refresh starting", level="INFO")
        self.create_task(self._refresh())

    def _on_midnight(self, kwargs: Any) -> None:
        """Re-publish so ``today``/``next`` roll over — no network involved."""
        self.log("Midnight rollover — republishing school schedule", level="DEBUG")
        self._publish_sensor()

    async def _refresh(self) -> None:
        """Fetch both sources, keeping the previous data for whichever fails.

        The calendar runs first: its school name is what auto-selects the right
        student on a multi-student guardian account.
        """
        await self._refresh_day_cycle()
        await self._refresh_powerschool()
        self._publish_sensor()

    async def _refresh_day_cycle(self) -> None:
        if not self._day_cycle_url:
            self._mark_source(
                "day_cycle", "error", "No day_cycle_url configured (set day_cycle_url_env)"
            )
            self.log(
                "No school calendar URL configured — rotation days unavailable. "
                "Set day_cycle_url_env in apps.yaml and reload.",
                level="ERROR",
            )
            return

        try:
            async with DayCycleClient(self._day_cycle_url) as client:
                day_cycle = await client.fetch()
        except Exception as exc:  # noqa: BLE001 — one source must not kill the app
            detail = self._redact(str(exc) or exc.__class__.__name__)
            self._mark_source("day_cycle", "error", detail[:200])
            self.log(
                f"School calendar fetch failed ({exc.__class__.__name__}): {detail} "
                f"— keeping previous rotation data",
                level="ERROR",
            )
            return

        self._day_cycle = day_cycle
        self._mark_source("day_cycle", "ok", "")
        self.log(
            f"School calendar refreshed via {day_cycle.source}: "
            f"{len(day_cycle.dates)} school days, {len(day_cycle.closures)} closures, "
            f"{len(day_cycle.notes)} notes, school={day_cycle.school_name!r}",
            level="INFO",
        )

    async def _refresh_powerschool(self) -> None:
        if not (self._powerschool_url and self._powerschool_user and self._powerschool_password):
            self._mark_source(
                "powerschool", "error", "PowerSchool URL/credentials not configured"
            )
            self.log(
                "PowerSchool URL or credentials not configured — class lists "
                "unavailable. Set powerschool_url_env / powerschool_user_env / "
                "powerschool_password_env in apps.yaml and reload.",
                level="ERROR",
            )
            return

        school_name = self._day_cycle.school_name if self._day_cycle else ""
        try:
            async with PowerSchoolClient(
                self._powerschool_url,
                self._powerschool_user,
                self._powerschool_password,
            ) as client:
                schedule = await client.fetch_schedule(
                    school_name=school_name,
                    student_id=self._student_id,
                    weeks_ahead=self._weeks_ahead,
                    today=self._today(),
                )
        except Exception as exc:  # noqa: BLE001
            detail = self._redact(str(exc) or exc.__class__.__name__)
            self._mark_source("powerschool", "error", detail[:200])
            self.log(
                f"PowerSchool fetch failed for user {mask_user(self._powerschool_user)} "
                f"({exc.__class__.__name__}): {detail} — keeping previous class data",
                level="ERROR",
            )
            return

        self._schedule = schedule
        # Cache the resolved id so later runs skip the per-student probe fetches.
        self._student_id = schedule.student_id or self._student_id
        self._mark_source("powerschool", "ok", "")
        self.log(
            f"PowerSchool refreshed: {len(schedule.days)} dates with classes, "
            f"{len(schedule.cycle)} rotation days, year "
            f"{schedule.first_day or '?'}..{schedule.last_day or '?'}",
            level="INFO",
        )

    def _mark_source(self, name: str, status: str, error: str) -> None:
        """Record a source's outcome, preserving ``fetched_at`` across failures."""
        entry = self._sources.setdefault(
            name, {"status": "unknown", "fetched_at": "", "error": ""}
        )
        entry["status"] = status
        entry["error"] = error
        if status == "ok":
            entry["fetched_at"] = self._now_iso()

    # ------------------------------------------------------------------
    # Sensor publication
    # ------------------------------------------------------------------

    def _today(self) -> datetime.date:
        """Today's date in AppDaemon's timezone (mockable in tests)."""
        try:
            value = self.date()
        except Exception:  # noqa: BLE001 — AppDaemon not fully up yet
            return datetime.date.today()
        return value if isinstance(value, datetime.date) else datetime.date.today()

    @staticmethod
    def _now_iso() -> str:
        return datetime.datetime.now().astimezone().isoformat(timespec="seconds")

    def _class_payload(self, block: ClassBlock) -> Dict[str, str]:
        icon, short = resolve_icon(block.course, self._icon_rules)
        payload = block.as_dict()
        payload["icon"] = icon
        payload["short"] = short
        return payload

    def _visible(self, blocks: Sequence[ClassBlock]) -> List[ClassBlock]:
        return [b for b in blocks if b.course and not is_hidden(b.course, self._hide_courses)]

    def _build_attributes(self) -> Tuple[str, Dict[str, Any]]:
        """Assemble the sensor state and attributes from the last good data."""
        day_cycle = self._day_cycle or DayCycle()
        schedule = self._schedule or WeeklySchedule()

        dates = dict(day_cycle.dates)
        closures = dict(day_cycle.closures)
        notes = dict(day_cycle.notes)

        cycle_payload: Dict[str, List[Dict[str, str]]] = {}
        for day_number, blocks in sorted(schedule.cycle.items()):
            visible = self._visible(blocks)
            if visible:
                cycle_payload[str(day_number)] = [self._class_payload(b) for b in visible]

        today = self._today()
        today_iso = today.isoformat()

        days_payload: Dict[str, Dict[str, Any]] = {}
        for iso, blocks in sorted(schedule.days.items()):
            if iso < today_iso:
                continue  # the card only ever looks forward
            visible = self._visible(blocks)
            if not visible:
                continue
            day_number = dates.get(iso)
            enriched = attach_periods(visible, schedule.cycle.get(day_number or 0, []))
            entry: Dict[str, Any] = {
                "classes": [self._class_payload(b) for b in enriched]
            }
            if day_number is not None:
                entry["day"] = day_number
            note = notes.get(iso, "")
            if note:
                entry["note"] = note
            days_payload[iso] = entry

        known_dates = sorted(set(dates) | set(days_payload))
        next_iso = next((iso for iso in known_dates if iso > today_iso), "")

        attrs: Dict[str, Any] = {
            "school": self._school_label,
            "cycle_length": day_cycle.cycle_length,
            "dates": dates,
            "closures": closures,
            "notes": notes,
            "days": days_payload,
            "cycle": cycle_payload,
            "today": self._day_summary(today_iso, dates, notes, closures, days_payload),
            "next": self._day_summary(next_iso, dates, notes, closures, days_payload),
            "last_updated": self._now_iso(),
            "sources": {
                name: dict(entry) for name, entry in sorted(self._sources.items())
            },
            "friendly_name": f"{self._school_label} Schedule",
            "icon": "mdi:calendar-clock",
        }

        ok_sources = sum(1 for e in self._sources.values() if e["status"] == "ok")
        if ok_sources == len(self._sources):
            state = "ok"
        elif dates or days_payload or cycle_payload:
            state = "partial"
        else:
            state = "error"

        return state, attrs

    @staticmethod
    def _day_summary(
        iso: str,
        dates: Dict[str, int],
        notes: Dict[str, str],
        closures: Dict[str, str],
        days: Dict[str, Any],
    ) -> Dict[str, Any]:
        """The ``today`` / ``next`` fallback block the card reads when ``dates`` is thin.

        "No school" is only asserted when nothing knows of the date — a rotation
        day the calendar missed but the portal scheduled classes on is still a
        school day.
        """
        if not iso:
            return {"date": "", "note": "No school days scheduled"}

        summary: Dict[str, Any] = {"date": iso}
        day_number = dates.get(iso)
        if day_number is not None:
            summary["day"] = day_number
            note = notes.get(iso, "")
        elif iso in days:
            note = notes.get(iso, "")
        else:
            note = notes.get(iso) or closures.get(iso) or "No school"
        if note:
            summary["note"] = note
        return summary

    def _publish_sensor(self) -> None:
        """Publish (or update) ``sensor.school_schedule``."""
        state, attrs = self._build_attributes()

        try:
            size = len(json.dumps(attrs, default=str).encode("utf-8"))
        except (TypeError, ValueError):
            size = -1
        if size > ATTRIBUTE_WARN_BYTES:
            self.log(
                f"School schedule attributes are {size} bytes — approaching the "
                f"64 KB entity attribute limit; consider lowering weeks_ahead",
                level="WARNING",
            )

        self.log(
            f"Publishing {SENSOR_ENTITY_ID}: state={state}, "
            f"dates={len(attrs['dates'])}, days={len(attrs['days'])}, "
            f"cycle_days={len(attrs['cycle'])}, bytes={size}",
            level="DEBUG",
        )
        self.set_state(SENSOR_ENTITY_ID, state=state, attributes=attrs)
