# school_schedule_app

Publishes `sensor.school_schedule` so a wall-display card can show which day of
the school's six-day rotation today and the next school day are, with one icon
per class on each of those days.

## How it works

1. **Startup** — `initialize()` reads config, logs one masked line (never the
   username, password or either host), and defers to `run_in(…, 0)`.
2. **Calendar first** — `DayCycleClient` fetches the school's public Finalsite
   calendar: one page GET to discover the calendar element, then one GET of the
   ICS feed (falling back to month fragments). That yields `{date: 1..6}` for
   the whole school year plus no-school and early-release annotations, and the
   school's name.
3. **Portal second** — `PowerSchoolClient` logs into the guardian portal once,
   selects the student (by the calendar's school name unless an id is
   configured), scrapes the weekly bell-schedule grid for
   `today .. today + weeks_ahead*7` and the list view for the per-rotation-day
   fallback, then always logs off.
4. **Merge and publish** — every class is published; the ones matching
   `hide_courses` are *marked* `hidden: "true"` rather than removed. Icons and
   short labels are resolved server-side, period labels from the list view are
   stitched onto the timed bell-schedule blocks, and everything is written to
   `sensor.school_schedule` with `set_state`.
5. **Schedules** — a daily refresh at `refresh_time` (05:00 by default, the same
   cadence as `school_lunch_app`) and a **network-free** republish at 00:00:30 so
   `today` / `next` roll over at midnight without a second portal login.

A source that fails leaves its previous data in place and is marked `error` in
the sensor's `sources` attribute; the sensor state becomes `partial`. Only a
cold start where nothing at all is available publishes `error`.

## Dependencies

| Dependency | Use |
|------------|-----|
| `providers/school_schedule/` | All HTTP and HTML/ICS parsing (security rule S2) — see its README |
| `providers/secrets.py` | `resolve_arg_secret()` for the `*_env` config keys |

No `ha_provisioner` use: the card sends no commands, so there is no relay script
and there are no helpers to create.

## Self-provisioned entities

| Entity | Type | Purpose |
|--------|------|---------|
| `sensor.school_schedule` | virtual sensor (`set_state`) | The whole payload the card reads |

`set_state` sensors are created on first publish and do not need provisioning.
There are **no** helpers, scripts or relay entities.

### Sensor contract

State: `ok` (both sources fresh), `partial` (one or both failed, previous data
kept), `error` (nothing usable), `unknown` before the first fetch.

```jsonc
{
  "school": "Middle School",
  "cycle_length": 6,
  "dates":    { "2026-09-02": 1, "2026-09-03": 2, … },   // whole year, ~181 keys
  "closures": { "2026-09-07": "Labor Day - No School", … },
  "notes":    { "2026-09-24": "Early Release - PD", … }, // these keep a day number
  "days": {                                              // today .. weeks_ahead
    "2026-09-03": {
      "day": 2,                                          // omitted when unknown
      "note": "Early Release - PD",                      // omitted when empty
      "classes": [
        { "course": "Science", "short": "Science", "icon": "mdi:flask",
          "period": "6B1", "start": "08:40", "end": "09:35",
          "teacher": "…", "room": "…" },
        { "course": "Lunch", "short": "Lunch", "icon": "mdi:food",
          "period": "6B5", "start": "11:40", "end": "12:05",
          "hidden": "true" }                               // compact card skips it
      ]
    }
  },
  "cycle": { "1": [ … ], …, "6": [ … ] },                // per-rotation-day fallback
  "today": { "date": "2026-09-02", "day": 1 },           // `note` instead of `day` on a closure
  "next":  { "date": "2026-09-03", "day": 2 },
  "last_updated": "2026-09-02T05:00:12-04:00",
  "sources": {
    "day_cycle":   { "status": "ok", "fetched_at": "…", "error": "" },
    "powerschool": { "status": "ok", "fetched_at": "…", "error": "" }
  }
}
```

Class lists are chronological and complete — **every** class the portal reports
is published, icons resolved. `days` contains every date that has at least one
named class, from today forward; weekends and holidays are simply absent.

#### The `hidden` marker

A class whose course name matches `hide_courses` (Lunch, Advisory, Homeroom by
default) carries the extra key `hidden: "true"`. Nothing is removed from the
payload on account of it — the two cards decide for themselves:

| Card | Behaviour |
|------|-----------|
| `school-schedule-card` (compact) | Skips classes with `hidden`, so the icon row stays readable |
| `school-schedule-detail-card` (matrix) | Draws everything, so the day reads as it is actually lived — Advisory at both `ADV` and `6PA`, Lunch in the middle |

The value is the **string** `"true"`, not a boolean, and a visible class has no
`hidden` key at all. AppDaemon's `set_state` drops falsy attribute values, so a
`False` would arrive indistinguishable from an absent key — present-and-`"true"`
versus absent is the only unambiguous encoding. Consumers should test for
presence (`"hidden" in cls`), never for truthiness of a boolean.

**Empty values are omitted, not published as `""`/`null`/`0`.** AppDaemon's
`set_state` drops falsy attribute values anyway (see
`agent-docs/appdaemon-testing.md`), and the card already treats a missing key as
"unknown". Nothing in the payload is a boolean.

## Associated cards

Both live in this directory; register each as a Lovelace resource and bump
the `?v=N` query parameter after any edit.

- `school-schedule-card.js` — the compact card: today and the next school day,
  rotation day badge, one icon per class, **skipping anything marked `hidden`**.
  Fixed 112px height for the wall display. `navigation_path` opens the detail
  view.
- `school-schedule-detail-card.js` — the six-day rotation as a matrix (periods
  with times down, Day 1..6 across, icon + class + teacher + room per cell),
  **including the hidden blocks** so the full day is visible,
  today/next columns highlighted, icon legend. Read-only. On `wall-display` it
  sits inside a bubble-card pop-up (`#school-schedule-popup`); on
  `unifi-connect` it is its own `subview` panel page (`/unifi-connect/school-schedule`).

```yaml
type: custom:school-schedule-card
status_entity: sensor.school_schedule   # default
navigation_path: /lovelace/school       # optional; tapping the card navigates
today_label: Today
tomorrow_label: Tomorrow
max_lookahead_days: 14
```

The card reads `dates` + `days` first and falls back to `cycle` / `today` /
`next`, so every attribute above matters.

## Config reference

| Key | Required | Default | Purpose |
|-----|----------|---------|---------|
| `module` / `class` | yes | — | `school_schedule_app.school_schedule_app` / `SchoolScheduleApp` |
| `school_name` | no | `"School"` | Label published as the sensor's `school` attribute and friendly name |
| `day_cycle_url_env` | yes | — | Env var holding the Finalsite "view all events" page URL |
| `powerschool_url_env` | yes | — | Env var holding the PowerSchool guardian portal root |
| `powerschool_user_env` | yes | — | Env var holding the guardian username |
| `powerschool_password_env` | yes | — | Env var holding the guardian password |
| `refresh_time` | no | `"05:00:00"` | Daily scrape time (`HH:MM:SS` or `HH:MM`) |
| `weeks_ahead` | no | `3` | How far forward to fetch the bell-schedule grid |
| `powerschool_student_id` (or `_env`) | no | auto | Skip student auto-selection |
| `hide_courses` | no | lunch / advisory / homeroom | Case-insensitive regexes; matching courses are published with `hidden: "true"` and skipped by the compact card (never removed from the payload) |
| `icon_rules` | no | see below | Ordered `{match, icon, short}`; first match wins |
| `ha_url` / `ha_token_env` | no | — | Carried for consistency; this app provisions nothing |

Every credential key uses the `_env` suffix and names an **environment
variable**, never a value (security rules S1/S7). A direct `day_cycle_url` /
`powerschool_url` etc. is accepted for local dev but must not be committed.

### Default icon rules

Order matters — `Theater Arts 6` must reach the theatre rule before the
word-bounded `\bart\b` rule sees it. Lunch and Advisory lead the table: they are
hidden on the compact card but still drawn on the matrix, and they need real
icons there.

| Match | Icon | Short |
|-------|------|-------|
| `\blunch\b` | `mdi:food` | Lunch |
| `advisory\|homeroom\|\badv\b` | `mdi:account-group` | Advisory |
| `theat\|drama` | `mdi:drama-masks` | Theater |
| `\bart\b` | `mdi:palette` | Art |
| `\bmath\|algebra\|geometry` | `mdi:calculator-variant` | Math |
| `^la\b\|language arts\|\bela\b\|english\|\breading\b\|\bwriting\b` | `mdi:book-open-page-variant` | LA |
| `science` | `mdi:flask` | Science |
| `soc\s*stud\|social\|history\|civics\|geography` | `mdi:earth` | Social Studies |
| `spanish\|french\|latin\|world lang\|\bwl\b` | `mdi:translate` | Language |
| `phys\s*ed\|physical\|\bpe\b\|\bgym\b` | `mdi:run` | PE |
| `health\|wellness` | `mdi:heart-pulse` | Health |
| `steam\|stem\|engineering\|robot` | `mdi:cog` | STEAM |
| `tech\|computer\|coding\|digital` | `mdi:laptop` | Tech |
| `\bband\b` | `mdi:trumpet` | Band |
| `chorus\|choir` | `mdi:microphone-variant` | Chorus |
| `orchestra\|strings` | `mdi:violin` | Orchestra |
| `music` | `mdi:music` | Music |
| `library\|media center` | `mdi:library` | Library |
| `\bflex\b` | `mdi:puzzle-outline` | FLEX |

Anything unmatched gets `mdi:school` and the course name as its label.

## Manual setup required

1. **Environment variables** — add the four secrets to the Kubernetes
   `ExternalSecret` (and to `appdaemon/.env` for local dev; see `.env.example`).
2. **Lovelace resources** — register `school-schedule-card.js` and
   `school-schedule-detail-card.js` under Settings → Dashboards → Resources as
   JavaScript modules (served from `/local/school-schedule/`), and bump `?v=N`
   after each update. The provisioner cannot create Lovelace resources.

Nothing else: no shell commands, no `local_file` cameras, no helpers.

## Gotchas

- **One PowerSchool login a day, and never two at once.** The portal evicts any
  other session for the same account on login. If this app runs while a parent
  is signed in, that parent gets bounced — and vice versa, which is why the
  refresh sits at **05:00**, before anyone is awake. The client logs in once,
  fetches everything, and always logs off; a mid-run eviction is retried exactly
  once. Do not add extra refresh triggers, and do not lower `refresh_time` into
  waking hours.
- **The midnight republish never touches the network** — it only recomputes
  `today` / `next` from data already in memory. That is deliberate: rolling the
  date over must not cost a login.
- **The bell-schedule grid carries no period label.** Periods are stitched on
  from the list view by course name in occurrence order. A course the list view
  does not know about publishes with an empty `period` — harmless, the card does
  not use it.
- **Failure is sticky-by-design.** A failed source keeps its previous data and
  its previous `fetched_at`; check `sources.*.status` and `last_updated` in
  Developer Tools before assuming the scrape is fine.
- **Payload budget.** HA caps entity attributes at 64 KB. A full year of
  `dates` plus three weeks of classes lands around 43 KB now that Lunch and
  Advisory are published rather than dropped (it was ~30 KB when they were
  removed — the fixture-backed budget test measures exactly this). The app logs
  a WARNING past 48 KB, so the headroom is real but thin: if it ever trips,
  lower `weeks_ahead` before adding anything to the per-class payload.
- **After the last day of school the ICS feed goes empty.** That raises rather
  than publishing an empty rotation, so the sensor keeps showing the year that
  just ended until the school publishes the next one.

## Upstream / downstream dependencies

Standalone. Nothing else in this repo reads `sensor.school_schedule`, and this
app listens to no events. It sits next to `school_lunch_app` on the wall
dashboard but shares no code or state with it.

## Testing

```bash
source .venv/bin/activate && cd appdaemon
python -m pytest tests/test_school_schedule_app.py -v
```

All offline, with both providers stubbed. See the provider README for the two
fixture oracles that guard the parsers.
