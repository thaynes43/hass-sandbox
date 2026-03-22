# Calendar Summary

Vestaboard automation that watches a Home Assistant calendar entity, fetches all upcoming events, and rotates them on the board with dynamic countdowns. Supports an urgent reminder threshold that can force-push to the board when events are imminent.

Supports multiple instances: configure one YAML entry per calendar. The display name is derived from the app key (e.g. `calendar_summary_family` → "Calendar: Family").

## How it works

### Event collection

1. On each trigger (state change or periodic interval), fetches all events within the `time_before_event_hours` window using the HA Calendar REST API (`GET /api/calendars/{entity_id}`) via `HaRestClient`.
2. Falls back to reading entity state attributes (current + next event) if the service call fails.
3. Events are sorted by start time (soonest first).

### Partitioning: urgent vs upcoming

Events are split into two buckets:

- **Urgent**: start time is within `reminder_threshold_minutes` (default 30 min). These events have already started or are about to.
- **Upcoming**: start time is beyond the reminder threshold but within `time_before_event_hours`.

If **any** urgent events exist, **only** urgent events are displayed. If `force_push_at_reminder` is enabled, these frames override the board's current TTL (force-push). Otherwise, all upcoming events are displayed normally.

After a non-urgent push, a cooldown timer (random duration between `cooldown_min_minutes` and `cooldown_max_minutes`) is started. Re-triggers during the cooldown window are suppressed. Urgent events always bypass the cooldown.

### Rotation logic

The automation calculates how to divide the TTL window across multiple events:

```
max_slots      = floor(TTL / FLOOR)           # Max events that fit in the window
shown_events   = min(N, max_slots)            # Events actually displayed
display_time   = max(FLOOR, TTL / N)          # Time per event in minutes
overflow       = max(0, N - max_slots)        # Events dropped from rotation
```

Where:
- `TTL` = `ttl_minutes` config value (default 60 min)
- `N` = number of events to display
- `FLOOR` = `rotation_floor_minutes` config value (default 10 min)

#### Example (TTL = 60 min, FLOOR = 10 min)

| Events (N) | Shown | Display Time | Overflow | Notes |
|------------|-------|--------------|----------|-------|
| 1 | 1 | 60 min | 0 | Static full hour |
| 2 | 2 | 30 min | 0 | Comfortable |
| 3 | 3 | 20 min | 0 | Solid |
| 4 | 4 | 15 min | 0 | Clean split |
| 5 | 5 | 12 min | 0 | Still above floor |
| 6 | 6 | 10 min | 0 | Exactly at floor |
| 7 | 6 | 10 min | 1 | 1 event dropped |
| 10 | 6 | 10 min | 4 | Prioritize by start |

#### Overflow strategy

When `N > max_slots`, events are prioritized by soonest start time. The last rotation slot is reserved for a summary frame showing "+N MORE" on row 2 and "EVENTS UPCOMING" (or "EVENT UPCOMING" for exactly 1 overflow) on row 3, so users know there are additional events.

### Countdown logic

Each displayed event shows a dynamic countdown that updates at varying intervals:

| Time to event | Update frequency | Example displays |
|---------------|-----------------|------------------|
| > 30 min | Every 15 min (aligned to 15-min boundaries) | "90 MIN", "75 MIN", "60 MIN" |
| 30 → 15 min | Every 5 min | "30 MIN", "25 MIN", "20 MIN" |
| 15 → 0 min | Every 1 min | "15 MIN", "14 MIN", ... "1 MIN" |
| 0 → -5 min | Static | "NOW" |
| -5 → -30 min | Every 5 min | "5 MIN AGO", "10 MIN AGO" |
| > -30 min | Every 15 min | "45 MIN AGO", "60 MIN AGO" |

The first update when > 30 min out is aligned so `remaining_minutes % 15 == 0`, giving clean values like "90 MIN" → "75 MIN". This means the first wait may be longer than 15 minutes.

Countdown updates re-push the frame with the same source, so the controller replaces the displayed frame without requiring a TTL override.

### Frame layout (6×22 grid)

```

    TEAM MEETING


      10:30 AM
       14 MIN
```

- Row 0: blank
- Rows 1–2: event name (centered, up to 44 chars across two lines)
- Row 3: blank
- Row 4: event start time (e.g. "10:30 AM")
- Row 5: countdown string

### Timer architecture

Four independent timers run during active display:

1. **Interval timer** (periodic, `check_interval_s`): Polls the calendar and re-runs the full cycle. Detects new/changed/removed events.
2. **Rotation timer** (`display_time_s`): Fires when it's time to show the next event in rotation.
3. **Countdown timer** (variable): Fires at the next countdown display update point for the currently shown event. Recalculated after each update.
4. **Cooldown timer** (random, `cooldown_min_minutes`–`cooldown_max_minutes`): Started after every non-urgent push. While active, subsequent non-urgent pushes are suppressed to avoid overwhelming the board. Urgent events bypass the cooldown entirely.

All timers except the cooldown timer are cancelled and restarted when the event set changes.

## Architecture

```
CalendarSummaryApp
  → initialize()
    → register_with_controller() via HA events
  → listen_state(calendar_entity) + run_every(check_interval_s)
    → _run_cycle()
      → _fetch_upcoming_events() via HA Calendar REST API
      → partition into urgent / upcoming
      → calculate rotation parameters
      → _push_current_event()
        → push_frame() → fire_event("vestaboard_controller_command", command="push_automation_frame")
        → schedule rotation timer (next event)
        → schedule countdown timer (next update)
  → _on_rotation_timer()
    → _rotate_to_next_event() → _push_current_event()
  → _on_countdown_timer()
    → _update_countdown() → push_frame() (same source replaces displayed)
```

## Dependencies

- `providers.vestaboard.character_encoding` — character encoding and blank grid
- `vestaboard_apps._shared.base.VestaboardAutomation` — controller registration and frame push API
- `providers.ha_provisioner.ha_rest_client.HaRestClient` — HA Calendar REST API calls
- `providers.secrets` — env var secret resolution for `ha_url_env` / `ha_token_env`
- `providers.ai_providers.registry` — AI summarization of long event names (optional, when `ai_provider_conf` is set)

## Self-provisioned entities

None. The controller provisions all shared entities.

## Config reference

### YAML config keys

| Key | Required | Default | Description |
|-----|----------|---------|-------------|
| `module` | Yes | — | `vestaboard_apps.automations.calendar_summary.calendar_summary_app` |
| `class` | Yes | — | `CalendarSummaryApp` |
| `calendar_entity` | Yes | — | HA entity ID of the calendar to watch (e.g. `calendar.family`) |
| `ha_url_env` | Yes | — | Env var name holding the HA base URL (needed for calendar REST API) |
| `ha_token_env` | Yes | — | Env var name holding the HA long-lived access token |
| `check_interval_s` | No | `300` | How often (seconds) to poll the calendar entity regardless of state changes |
| `ai_provider_conf` | No | — | AI provider capability bundle for summarizing long calendar event names. If omitted, event names are used as-is. |

### UI-editable config (stored in controller's `automation_config_path`)

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | bool | `false` | Whether the automation is active |
| `ttl_minutes` | int | `60` | Total display window for the rotation cycle |
| `should_expire` | bool | `true` | If `true`, frames are dropped after TTL rather than moved to fallback |
| `time_before_event_hours` | int | `240` | Hours before an event to start showing it on the board (240 = 10 days) |
| `reminder_threshold_minutes` | int | `30` | Minutes before event start to enter urgent mode |
| `force_push_at_reminder` | bool | `true` | Override board TTL when in urgent mode (force-push to board) |
| `rotation_floor_minutes` | int | `10` | Minimum display time per event in rotation (minutes) |
| `cooldown_min_minutes` | int | `30` | Minimum cooldown duration (minutes) after a non-urgent push |
| `cooldown_max_minutes` | int | `120` | Maximum cooldown duration (minutes) after a non-urgent push |

### YAML example (single calendar)

```yaml
calendar_summary_family:
  module: vestaboard_apps.automations.calendar_summary.calendar_summary_app
  class: CalendarSummaryApp
  disable: true
  calendar_entity: calendar.family
  ha_url_env: HA_URL
  ha_token_env: TOKEN
```

### YAML example (multiple calendars)

```yaml
calendar_summary_family:
  module: vestaboard_apps.automations.calendar_summary.calendar_summary_app
  class: CalendarSummaryApp
  disable: true
  calendar_entity: calendar.family
  ha_url_env: HA_URL
  ha_token_env: TOKEN

calendar_summary_work:
  module: vestaboard_apps.automations.calendar_summary.calendar_summary_app
  class: CalendarSummaryApp
  disable: true
  calendar_entity: calendar.work
  ha_url_env: HA_URL
  ha_token_env: TOKEN
```

Each instance registers with the controller under its own app key and appears separately in the configuration card's automation list with the display name derived from the key suffix.

## Manual setup required

- The HA calendar integration must be configured and the `calendar_entity` must exist before the app starts.

## Testing procedure

Use the HA MCP server to create calendar events and trigger different modes:

### Test 1: Single upcoming event (normal mode)

```
Create a calendar event on the target calendar:
- Summary: "Test Meeting"
- Start: 2 hours from now
- End: 3 hours from now
```

Expected: Single frame with "TEST MEETING", start time, and countdown (e.g. "2 HRS"). No rotation.

### Test 2: Multiple upcoming events (rotation mode)

```
Create 3 calendar events on the target calendar:
- "Morning Standup" — 1 hour from now, 30 min duration
- "Design Review" — 2 hours from now, 1 hour duration
- "Team Lunch" — 3 hours from now, 1 hour duration
```

Expected: Events rotate with `display_time = max(FLOOR, TTL / 3)` = 20 min each (with default 60 min TTL, 10 min floor).

### Test 3: Urgent event (force push)

```
Create a calendar event:
- Summary: "Fire Drill"
- Start: 15 minutes from now
- End: 30 minutes from now
```

Expected: With default 30 min reminder threshold, this event enters urgent mode. If `force_push_at_reminder` is enabled, it force-pushes to the board overriding any current TTL.

### Test 4: Countdown progression

```
Create a calendar event:
- Summary: "Big Presentation"
- Start: 45 minutes from now
- End: 90 minutes from now
```

Expected countdown sequence:
- Initial: "45 MIN" (aligned to 15-min boundary)
- After ~15 min: "30 MIN" (switches to 5-min updates)
- "25 MIN", "20 MIN", "15 MIN" (switches to 1-min updates)
- "14 MIN", "13 MIN", ... "1 MIN"
- "NOW" (for 5 minutes)
- "5 MIN AGO", "10 MIN AGO", ...

### Test 5: Overflow

```
Create 8+ calendar events within the next few hours.
```

Expected: With default 10 min floor and 60 min TTL, max 6 events shown. Last slot shows "+N MORE" / "EVENTS UPCOMING" (or "EVENT UPCOMING" for 1 overflow).

### Debugging via logs

Key log messages to watch:

| Log message pattern | What it means |
|---------------------|---------------|
| `URGENT: N event(s) within reminder threshold` | Entered urgent mode |
| `Upcoming: N event(s) within Xh window` | Normal mode with event list |
| `Rotation plan: N events shown, display_time=Xs` | Rotation parameters calculated |
| `Pushing event [i/N]: "name"` | Event frame pushed to board |
| `Countdown update: "name" → "X MIN"` | Countdown display refreshed |
| `Rotating to slot i/N` | Advancing to next event |
| `Calendar REST API returned N event(s)` | Multi-event REST call succeeded |
| `Calendar REST API failed ... falling back` | Using single-event entity state fallback |

## Upstream/downstream dependencies

- **Upstream**: `vestaboard_controller` — must be running and listening for events before this app starts. Registration happens via HA events; no AppDaemon `dependencies:` entry is needed. The app also listens for `vestaboard_controller_ready` and re-registers automatically if the controller restarts.
- **Downstream**: None.
