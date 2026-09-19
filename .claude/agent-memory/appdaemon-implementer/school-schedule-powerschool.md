---
name: school-schedule-powerschool
description: Never log into PowerSchool from the pod (it evicts the family's live session), plus the fixture oracles, the sanitisation mapping, and the no-bs4 parsing constraint
metadata:
  type: feedback
---

# school_schedule_app / providers/school_schedule (2026-09-01)

**Never log into PowerSchool from the pod during development.**

**Why:** the guardian portal forbids concurrent sessions — every login evicts
the family's live session, and a parent signing in mid-run kills the app's.
This is a real-people-affected constraint, not a rate limit.

**How to apply:** build parsers against the saved fixtures. If a login is truly
unavoidable, do it once and `GET /guardian/home.html?ac=logoff` immediately.
The app's own refresh sits at 05:00 for the same reason — never trigger extra
scrapes.

## Fixtures

- `appdaemon/tests/fixtures/school_schedule/` is the repo's first test-fixture directory.
- Two files are **oracles** captured live and must keep matching exactly: `day_numbers.json` (181 ICS day numbers) and `cycle_by_day.json` (the six-day rotation from the PowerSchool list view).
- Fixtures are sanitised: school/district → "Example …", teachers → placeholder `Last, First` names, student ids → 10001/10002. Keep the mapping **global across fixture files** or the oracle stops matching.

## Parsing + errors

- No bs4/lxml/icalendar/dateutil in the AppDaemon image — parse with `re` + `html.unescape` only.
- Redact configured hosts and credentials out of exception strings before putting them in `set_state` attributes: aiohttp errors quote the URL they failed on, and the frontend renders `sources.*.error`.
