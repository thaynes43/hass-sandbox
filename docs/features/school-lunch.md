# School Lunch Menu

Daily school lunch menus from all configured schools, surfaced on any Lovelace dashboard and through the voice assistants. An at-a-glance card shows tomorrow's entrees for selected schools; a detail popup provides a full weekly view, month-by-month calendar navigation, and per-school selection settings; and "Okay Nabu, what's for lunch tomorrow?" reads the menu out loud.

<!-- TODO: Add screenshot of school-lunch-card and school-lunch-detail-card -->

## Overview

The `school_lunch_app` AppDaemon app fetches menus from the [School Nutrition and Fitness](https://www.schoolnutritionandfitness.com) platform. Multiple schools can be configured in a single app instance. Menus refresh automatically every morning at 5:00 AM and are published to a Home Assistant virtual sensor that both Lovelace cards and the voice tool read from.

## Data flow

```
School Nutrition and Fitness API
  ├─ downloadMenu.php (HTTP 302)  — resolves download ID → MongoDB ObjectId
  └─ GraphQL API                  — returns structured menu items per day
       + content overlay REST API — adds holiday / early-release notices
            │
            ▼
  school_menu provider (aiohttp client)
       │  returns MenuMonth dataclasses
       ▼
  school_lunch_app (AppDaemon)
       │  publishes structured JSON to
       ▼
  sensor.school_lunch_menu          ← Lovelace cards and the voice tool read from here
  input_text.school_lunch_selected_schools
            │
            ├──▶ school-lunch-card.js         — compact at-a-glance card
            ├──▶ school-lunch-detail-card.js  — full detail popup card
            └──▶ script.voice_school_lunch    — voice tool: reads the sensor (not the selection)
                                                and input_text.school_lunch_buildings
```

## Cards

### At-a-glance card (`custom:school-lunch-card`)

A compact card showing the next relevant lunch menu for all selected schools. The display depends on the configurable `show_tomorrow_after` cutoff time:

| Time of day | Weekday | Header shown |
|-------------|---------|-------------|
| Before cutoff | Mon–Fri | **Today's Lunch** |
| After cutoff | Mon–Thu | **Tomorrow's Lunch** |
| After cutoff | Friday | **Monday's Lunch** |
| Any time | Sat/Sun | **Monday's Lunch** |

On the wall display the card is pinned to a fixed height (`height: 296` in the card config) so a long menu can never push the dashboard's bottom button row off the screen. When the menu does not fit, the menu area scrolls by touch, with a fade and chevron at the bottom while more is hidden below.

Menu items are split into numbered **options** (main entree choices) and an **Includes** line for daily items like fruit and milk (auto-classified by the app based on which items appear on 75%+ of days).

School selection persists in `input_text.school_lunch_selected_schools` so the choice survives reloads and is shared across all dashboard instances.

```yaml
type: custom:school-lunch-card
status_entity: sensor.school_lunch_menu
selection_entity: input_text.school_lunch_selected_schools
navigation_path: '#school-lunch-popup'   # tapping the card opens the detail popup
height: 296                              # optional: fixed height, scrollable menu
```

### Detail card (`custom:school-lunch-detail-card`)

A popup/dialog card with three tabs:

- **This week** — full item list (entrees and sides) for each school day in the current week
- **Calendar** — month view per school with prev/next month navigation
- **Settings** — checkboxes to choose which schools appear in the at-a-glance card

Month navigation sends a `fetch_month` command via `script.school_lunch_relay` to fetch adjacent months on demand without a full refresh.

```yaml
type: custom:school-lunch-detail-card
status_entity: sensor.school_lunch_menu
selection_entity: input_text.school_lunch_selected_schools
relay_script: school_lunch_relay
```

## Relay commands

Card → AppDaemon communication uses the standard relay script pattern:

| Command | Triggered by | Effect |
|---------|-------------|--------|
| `select_schools` | Settings tab save | Updates `input_text.school_lunch_selected_schools`; at-a-glance card updates immediately |
| `fetch_month` | Prev/next month button | Fetches the adjacent month for a specific school and updates its entry in the sensor attributes |

## Ask a voice assistant

The same menus answer out loud. "Okay Nabu, what's for lunch tomorrow?" on any voice box (or the
Phone Assist on Tom's phone) reads the options for both kids. Other questions work too: "What is
Penelope having on Friday?", "What did Jackson have last Friday?", "What's the high school lunch on
Tuesday?". Penelope's lunch is the elementary menu and Jackson's is the middle school menu. That
mapping lives in the voice tool itself, so every assistant gives the same answer without its own
instructions.

The tool is the Home Assistant script `script.voice_school_lunch`
(`home-assistant/scripts/voice/voice_school_lunch.yaml`). It is read-only: it reads
`sensor.school_lunch_menu` and the buildings helper described below, and changes nothing.

- With no day named it gives the next school lunch. That's today's until the `show_tomorrow_after`
  cutoff, then the next day's, skipping weekends and no-school days, as the at-a-glance card does.
- A day that has no lunch comes back as a notice: no school, a holiday, the weekend, or "not
  published yet" for a month the district has not posted.
- Some days the district lists different options for different buildings. The voice answer drops
  the options printed for another building and keeps everything else: options with no building
  label, and labels it doesn't recognise. If the filter would leave nothing, or a school has no
  entry in the helper (the high school, for one), every option is read out with its labels. The
  kids' buildings are set in the live helper `input_text.school_lunch_buildings`, which is kept out
  of this repository on purpose.

## Configuration

The app is configured in `apps-prod.yaml`. Key fields:

| Key | Description |
|-----|-------------|
| `menu_url_env` | Env var holding the district's menu URL; the site ID (`sid`) is parsed from it so the district never appears in the repo (production: `SCHOOL_LUNCH`, sourced from 1Password) |
| `menus` | List of `{name, download_id}` — one entry per school |
| `default_selected` | School names pre-selected in the at-a-glance card |
| `show_tomorrow_after` | `HH:MM:SS` cutoff time — before this, cards (and the voice tool's default day) show today's lunch; after, tomorrow's (default `"15:00:00"`; set to `"12:00:00"` in this house) |

Where the pieces live in this repository:

| Area | Path |
|------|------|
| Voice tool script | `home-assistant/scripts/voice/` |
| App and cards | `appdaemon/apps/school_lunch_app/` |

See `appdaemon/apps/school_lunch_app/README.md` for the full configuration reference, sensor attribute schema, relay command payload format, and manual setup steps (Lovelace resource registration).

## Holiday and closure handling

Days with no menu items (holidays, school closures, early releases) appear as notice entries in the calendar view. The `school_menu` provider fetches content overlay data from the API and maps announcement text (`NO SCHOOL`, `EARLY RELEASE`, etc.) to the correct weekday using calendar grid geometry. Notice days are shown in the calendar with their announcement text in place of menu items.
