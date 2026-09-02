# school_schedule provider

Two read-only scrapers behind one small package: the school's public
**Finalsite** calendar (which rotation day each date is) and the **PowerSchool**
guardian portal (which classes the student has). Consumed by
`appdaemon/apps/school_schedule_app/`.

All external HTTP for the school schedule lives here — never in app code
(security rule S2). Nothing in this package reads environment variables: the app
resolves credentials via `providers.secrets.resolve_arg_secret` and passes plain
values in.

## Modules

| Module | What it does |
|--------|--------------|
| `types.py` | `ClassBlock`, `DayCycle`, `WeeklySchedule`, `ScheduleRow`, `Student`, `CalendarElement` — plain dataclasses, no I/O |
| `ics.py` | A ~250-line stdlib iCalendar reader: unfolding, property/param parsing, `DTSTART` DATE + `TZID`, `SUMMARY` unescaping, `RRULE:FREQ=DAILY` with `INTERVAL`/`UNTIL`/`COUNT`, `EXDATE` |
| `day_cycle.py` | `DayCycleClient` — discovers the calendar element, fetches the ICS feed (or month fragments), classifies rotation days / closures / notes |
| `powerschool.py` | `PowerSchoolClient` — login, student selection, weekly bell-schedule grid, list-view rotation, logoff |

## Contracts

```python
from providers.school_schedule.day_cycle import DayCycleClient
from providers.school_schedule.powerschool import PowerSchoolClient

async with DayCycleClient(all_events_url) as client:
    cycle = await client.fetch()          # -> DayCycle

async with PowerSchoolClient(base_url, user, password) as client:
    schedule = await client.fetch_schedule(   # -> WeeklySchedule
        school_name=cycle.school_name,        # auto-selects the student
        student_id="",                        # or an explicit id
        weeks_ahead=3,
        today=datetime.date.today(),
    )
```

Both clients are async context managers and accept an injected
`aiohttp.ClientSession` (which they will not close). Both log request **paths**
only — the hosts are secrets.

### `DayCycle`

| Field | Meaning |
|-------|---------|
| `school_name` | From `<title>View All Events - <School></title>`; used to pick the PowerSchool student |
| `dates` | `{"YYYY-MM-DD": 1..6}` for every school day the feed knows (a full year, ~181 keys) |
| `closures` | `{"YYYY-MM-DD": "<label>"}` — weekday no-school days |
| `notes` | `{"YYYY-MM-DD": "<label>"}` — early release / delay; these dates keep their day number |
| `source` | `"ics"` or `"html"` — which fetch path produced the data |

### `WeeklySchedule`

| Field | Meaning |
|-------|---------|
| `days` | `{"YYYY-MM-DD": [ClassBlock, ...]}` in time order — from the bell-schedule grid |
| `cycle` | `{1..6: [ClassBlock, ...]}` in matrix-header period order — from the list view |
| `first_day` / `last_day` | School-year bounds the portal reports (`psc_firstDay` / `psc_lastDay`) |
| `school_name`, `student_id` | For logging and for caching the resolved student |

## Day-cycle source (Finalsite Composer)

1. `GET <all_events_url>` — server-rendered HTML. The calendar element is found
   with a regex on `class="fsElement fsCalendar…" id="fsEl_<n>"
   data-calendar-ids=<ids> data-calendars-feed-uuid="<uuid>"`.
2. `GET <host>/fs/calendar-manager/events.ics?calendar_ids[]=<id>` — one
   unauthenticated ~50 KB file covering the whole school year.
   (`?feed_id=<uuid>` is equivalent.)
3. Classification, per `(date, summary)`:
   - rotation day: `^\s*Day\s*([1-6])\b` — anchored, so "Grade 6 Orientation"
     and "Field Day 2" do not match while "Day 1 (Repeat)" does;
   - closure: `no\s*school` (weekdays only);
   - note: `early\s*release|delay` — the date keeps its day number.
   A closure always wins over a rotation day on the same date.

**Fallback** when the ICS fetch or parse fails: `GET
<host>/fs/elements/<element_id>?cal_date=YYYY-MM-01` for the current month plus
the next two. These fragments are pre-expanded HTML; note `data-month` is
**0-based**, and each event is rendered twice per daybox (deduped by title).

### RRULE support

Only `FREQ=DAILY` with `INTERVAL` / `UNTIL` (inclusive; both `YYYYMMDD` and
`YYYYMMDDTHHMMSSZ` forms) / `COUNT` is expanded, plus `EXDATE`. Anything else —
`FREQ=WEEKLY`, any `BY…` part, or an unbounded rule — logs a WARNING and
degrades to the single `DTSTART` occurrence rather than inventing dates.
Expansion is capped at `MAX_OCCURRENCES` (1000) per event.

All-day dates never go through UTC, so an event never slides a day.

### Ops notes

- Cloudflare-fronted; no bot protection at one fetch a day, but use the
  browser-like `USER_AGENT` this module already sends.
- The `ETag` changes on every response — conditional GETs are pointless.
- `robots.txt` disallows `/fs/` for crawlers. This is a single personal fetch
  per day, not a crawl.
- Coverage seen 2026-09-01 → 2027-06-21 (181 school days). After the last day
  the feed is simply empty — not an error, but `_fetch_ics` raises so the app
  keeps its previous data rather than publishing an empty rotation.

## PowerSchool source

Modern PowerSchool with **no `pstoken`, no HMAC on `dbpw`, no `contextData`, no
MFA and no captcha** for guardians on this deployment (`signin-guardian-saml-login = 0`).
Imperva sits in front, so the client sends a realistic UA and never probes URLs
speculatively.

```
GET  /guardian/home.html                → redirects to the public login, seeds cookies
POST /guardian/home.html                → account, pw, dbpw, serviceName=PS Parent Portal, …
GET  /guardian/myschedule.html?selected_student_id=<id>
GET  /guardian/myschedule_bellsched.html?selected_student_id=<id>&startdate=…&enddate=…
GET  /guardian/home.html?ac=logoff      → always, in a finally
```

### Session rules (the important part)

- **Concurrent guardian sessions are forbidden.** Logging in evicts any other
  session for the account: a parent already signed in gets bounced, and a parent
  signing in mid-run kills ours. The client therefore logs in **once** per
  `fetch_schedule`, does all its fetches, and always logs off.
- **Status codes are useless** for session state: an evicted session gets the
  login form with HTTP 200. Every fetch checks for the `LoginForm` marker and
  raises `SessionEvicted`, which `fetch_schedule` retries exactly once (whole
  login + fetch), never more.
- Login success is `psaid` cookie present **and** no `LoginForm` in the body.
  Landing on `/guardian/home_not_available.html` still counts as logged in.
- **Never use `?frn=`** to select a student — it kills the session.
  `?selected_student_id=<id>` switches and renders in one request.

### Student selection

`powerschool_student_id` (explicit) → the only student if there is one → the
student whose `div#print-school > span` matches the calendar's school name
(case-insensitive substring, either direction). Failing all three, the client
raises listing **schools only** — never student names. The app caches the
resolved id in memory so later runs skip the per-student probe fetches.

### The two schedule views, and why both

| View | Gives | Lacks |
|------|-------|-------|
| `myschedule_bellsched.html` | Per-date classes with clock times; rotation, terms and holidays already resolved server-side | **No period label at all** — the cell is `COURSE<br>TEACHER<br>ROOM<br>START - END` |
| `myschedule.html` | `Exp` period expressions (`ADV(1-6) 6PA(1,3,5)`) → the per-rotation-day fallback, plus term windows | No clock times, no per-date resolution |

The app stitches them: periods from the list view are attached to bell-schedule
blocks by course name **in occurrence order**, which is what disambiguates a
course that meets twice in one day (Advisory at both `ADV` and `6PA`).

Period ordering follows the school's matrix header: `ADV`, `{g}B1`..`{g}B5`,
`{g}PA`, `{g}B6`..`{g}B8`, unknown tokens last in stable order.

`myschedule_matrixsched.html` is deliberately **not** used — heavy rowspan
layout, no extra information.

### Weekly window

`fetch_schedule` asks for `today .. today + weeks_ahead*7` in one request. The
portal is only known to honour a single week, so when the grid comes back short
the client falls back to one Monday-to-Friday request per remaining week, capped
at `weeks_ahead + 2` requests.

### No API alternative

`pearson-rest` wants the mobile app's digest credential, `/ws/xte` is 404, and
there is no ICS export. Scraping is the only option.

## Testing

Everything is offline. Pure parsing helpers (`discover_calendar`,
`build_day_cycle`, `parse_month_fragment`, `parse_bell_schedule`,
`parse_list_schedule`, `expand_exp`, `build_cycle`, …) are module-level
functions tested directly against sanitized fixtures in
`appdaemon/tests/fixtures/school_schedule/`; the clients are tested with a fake
`aiohttp` session.

Two fixtures are **oracles** captured from the live sources and must keep
matching exactly:

- `day_numbers.json` — 181 `{date: day number}` pairs the ICS feed must expand to;
- `cycle_by_day.json` — the six-day rotation the list view must produce.

```bash
source .venv/bin/activate && cd appdaemon
python -m pytest tests/test_school_schedule_ics.py \
                 tests/test_school_schedule_day_cycle.py \
                 tests/test_school_schedule_powerschool.py -v
```

## Dependencies

`aiohttp` only. No `bs4` / `lxml` / `icalendar` / `dateutil` — none of them are
in the AppDaemon image, so parsing is `re` + `html.unescape`.
