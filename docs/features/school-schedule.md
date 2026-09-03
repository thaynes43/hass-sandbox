# School Day Rotation

Which day of the middle school's six-day rotation is it today, and which classes are on? A compact card under the lunch menu answers both at a glance for today and the next school day, on the wall display where the family checks it on the way out the door.

<!-- TODO: Add screenshot of school-schedule-card under the school lunch card on the wall display -->

## Overview

The school rotates a six-day schedule (Day 1 through Day 6) that skips weekends, holidays, and vacation days, so the day number never lines up with the weekday. The `school_schedule_app` AppDaemon app rebuilds the answer every morning from two sources:

- **Which day number a date is** comes from the school's public events calendar. The calendar publishes the whole year's rotation as recurring events, plus no-school days, early releases, and delays.
- **Which classes fall on each date** comes from PowerSchool's weekly schedule page, behind the guardian login. It already accounts for terms and holidays, and the class list view supplies the six-day cycle as a fallback.

Both are scraped at startup and again every morning at 5:00 AM, the same cadence as the [School Lunch Menu](school-lunch.md), because the two feeds come from the same school and change together (a new term, a schedule change, a snow day). The results are merged into one Home Assistant sensor that the card reads.

## Data flow

```
Finalsite events calendar (iCal feed)        PowerSchool guardian portal
        │  Day 1..6 per date,                        │  login, then the
        │  no-school days, early releases            │  weekly schedule grid + class list
        ▼                                            ▼
  providers/school_schedule/day_cycle.py    providers/school_schedule/powerschool.py
        └───────────────────┬────────────────────────┘
                            ▼
                  school_schedule_app (AppDaemon)
                  · merges the two sources
                  · maps each course to an icon
                  · keeps the last good data if a source fails
                            ▼
                  sensor.school_schedule
                            ▼
                  school-schedule-card (Lovelace)
```

The app publishes the full year of date to day-number mappings, three weeks of per-date class lists, and the six-day cycle as a fallback. The card works out "today" and "the next school day" itself from the browser's clock, so it stays correct across midnight and on weekends without waiting for the next scrape.

## The card

Two fixed-height rows, one for today and one for the next school day:

- **Kicker and date**: "Today · Wed 9/3", then "Tomorrow" or the weekday name when the next school day is further out (Friday shows "Monday").
- **Day badge**: the rotation day number. On a weekend or holiday the badge is blank and the row reads "No school" or the calendar's own label ("Labor Day - No School").
- **Class icons**: one icon per class in period order. Icons shrink as the class count grows so the row never wraps.

The card is built for the 1920x1080 UniFi Connect wall display, where the column under the lunch menu has only about 130 pixels to spare before the bottom button row is pushed off screen. The card is 112 pixels tall and never grows with content.

## The matrix view

Tapping the card opens the full six-day rotation as a matrix: every block of the day including advisory and lunch, period numbers down the side, Day 1 through Day 6 across, and in every cell the same icon as the compact card next to the class name, teacher, and room. Today's column is outlined and the next school day is tinted, and a legend under the grid spells out what each icon means. On the wall display it opens as a popup, matching the lunch menu; on the lighter unifi-connect dashboard it is its own page with a back arrow, which the display's older Android webview handles better than popups.

## Icons

Courses are matched to Material Design icons by keyword rules in the app config (math, science, ELA, Spanish, art, band, PE, and so on). Anything unmatched gets a generic school icon. Lunch, advisory, and homeroom blocks are flagged so the compact card shows only real classes, while the matrix view keeps them for the full picture. Rules are overridable per deployment without touching code.

## Secrets

The calendar URL, the PowerSchool root URL, and the guardian username and password live in 1Password and reach the app as environment variables through the cluster's ExternalSecret. The app config names the variables (`day_cycle_url_env`, `powerschool_url_env`, `powerschool_user_env`, `powerschool_password_env`) and never the values, following the same pattern as every other app in this repo.

## Configuration

The app is configured in `apps-prod.yaml`. Key fields:

| Key | Description |
|-----|-------------|
| `school_name` | Display name of the school (shown in the sensor) |
| `day_cycle_url_env` | Env var holding the school's events calendar page URL |
| `powerschool_url_env` / `powerschool_user_env` / `powerschool_password_env` | Env vars holding the PowerSchool root URL and guardian credentials |
| `refresh_time` | Daily scrape time (default `"05:00:00"`, matching the lunch menu) |
| `icon_rules` | Ordered keyword to icon rules for course names |
| `hide_courses` | Course name fragments to leave off the card (lunch, advisory) |

See `appdaemon/apps/school_schedule_app/README.md` for the full configuration reference, the sensor attribute schema, and the Lovelace resource registration step.

## See also

- [School Lunch Menu](school-lunch.md), the card this one sits under and shares a refresh cadence with
- [AppDaemon Apps](../apps/index.md)
