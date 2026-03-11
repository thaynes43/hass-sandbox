# Calendar from Schedule App

Reads a generic YAML maintenance schedule and populates a Home Assistant local calendar with recurring events using a rolling horizon. Periodically re-syncs to extend the horizon and detect schedule file changes.

## How it works

1. On startup, load the YAML schedule file and verify the HA calendar entity exists
2. Parse tasks (recurring by days/months, fixed-date, consumable triggers) into typed dataclasses
3. Expand tasks into concrete `EventInstance` objects within a configurable rolling horizon (default 90 days)
4. Diff desired events against the HA calendar using sync state (sync_key → HA event UID mapping)
5. Delete stale events, create new ones via `calendar/create_event` and `calendar/delete_event` services
6. Persist sync state (file hash, horizon date, event UIDs) to JSON for change detection
7. Re-sync periodically (default every 6 hours); skip when file hash and horizon are unchanged

## Layout

```
calendar_from_schedule_app/
├── calendar_schedule_app.py  — Main AppDaemon app (hass.Hass)
├── schedule_parser.py        — YAML loading + dataclasses + file hash
├── event_generator.py        — Expand tasks → concrete EventInstance list
├── calendar_sync.py          — Diff HA calendar vs desired; create/delete
└── sync_state.py             — JSON persistence for sync tracking
```

## Dependencies

- `providers.ha_provisioner.ha_rest_client` — REST API calls to fetch existing calendar events with UIDs
- `providers.secrets` — `resolve_secret()` for HA token resolution

## Self-provisioned entities

None. The calendar entity must be created manually (see Manual setup below).

## Config reference

| Key | Required | Default | Description |
|-----|----------|---------|-------------|
| `ha_url` or `ha_url_env` | Yes | — | Home Assistant base URL |
| `ha_token_env` | Yes | — | Env var name for HA long-lived access token |
| `schedule_dir` | No | `/media/calendar-schedules` | Directory containing schedule YAML files |
| `schedule_file` | Yes | — | Filename of the schedule YAML (e.g. `hot-tub-maintenance.yaml`) |
| `calendar_entity_id` | Yes | — | HA calendar entity to sync to (e.g. `calendar.hot_tub_maintenance`) |
| `state_dir` | No | `/media/calendar-schedules/sync-state` | Directory for sync state JSON files |
| `horizon_days` | No | `90` | Rolling horizon in days from today |
| `sync_interval_hours` | No | `6` | Hours between periodic re-syncs |

## Manual setup required

1. **Create local calendar entity** in HA: Settings → Devices & Services → Add Integration → Local Calendar → Name the calendar (e.g. "Hot Tub Maintenance"). The `local_calendar` integration uses a config entry flow that cannot be automated by the provisioner.
2. **Create schedule directory**: `mkdir -p /media/calendar-schedules/sync-state`
3. **Copy schedule file** to the schedule directory (e.g. `/media/calendar-schedules/hot-tub-maintenance.yaml`). The repo reference copy lives at `home-assistant/calendar-schedules/`.

## Schedule YAML schema

```yaml
schedule:
  name: "Schedule Name"
  plan_start: 2026-03-05       # Events before this date are skipped
  plan_end: 2027-03-19         # Optional; informational

tasks:
  # Recurring by days
  - id: unique_task_id
    title: "Human-readable title"
    frequency_days: 7
    first_due: 2026-03-12
    window: { before_days: 2, after_days: 2 }
    severity: HIGH
    checklist: ["Step 1", "Step 2"]

  # Recurring by months
  - id: monthly_task
    title: "Monthly task"
    frequency_months: 1
    first_due: 2026-04-05
    window: { before_days: 7, after_days: 7 }
    severity: MED

  # Fixed date
  - id: one_time_task
    title: "One-time task"
    due_date: 2026-06-05
    window: { start: 2026-05-22, end: 2026-06-19 }
    severity: HIGH
    procedure_ref: procedure_name   # optional

  # Consumable trigger (order when inventory low)
  - id: order_item
    title: "Order item"
    trigger: inventory_threshold
    threshold: { item: "filters", remaining_filters_lte: 1 }
    severity: LOW

consumables:
  filters:
    pack_size: 4
    starting_inventory_packs: 1

procedures:
  procedure_name:
    title: "Procedure Title"
    steps: ["Step 1", "Step 2"]
```

## Upstream/downstream dependencies

None — this is a standalone app with no dependencies on other AppDaemon apps.
