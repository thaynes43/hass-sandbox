# School Lunch App

Fetches daily lunch menus from the School Nutrition and Fitness API for multiple configured schools and publishes structured menu data to a Home Assistant virtual sensor. Handles Lovelace card commands for school selection and per-school month navigation.

## How it works

1. **Startup** — provisions the `input_text.school_lunch_selected_schools` helper and the `script.school_lunch_relay` relay script if they don't already exist.
2. **ID resolution** — resolves the human-readable numeric `download_id` for each configured school to an internal MongoDB ObjectId (used by the GraphQL API).
3. **Initial fetch** — fetches the current month's menu for all configured schools. If a resolved menu is behind the current calendar month (download IDs are month-specific), the app follows the `nextMonthPublished` chain to advance automatically. The next month's data is also pre-fetched and merged so cross-month week views (e.g., last week of March showing April days) display real menu data. Publishes the sensor.
4. **Daily refresh** — at 5:00 AM, re-resolves and re-fetches menus for all schools (with the same auto-advance and next-month pre-fetch). If a school fetch fails, the stale data for that school is preserved.
5. **Command routing** — listens for `school_lunch_command` events (fired by the relay script) to handle `select_schools` and `fetch_month` commands from the Lovelace detail card.

## Dependencies

- `providers/school_menu/client.py` — `SchoolMenuClient` (HTTP calls to the School Nutrition and Fitness API)
- `providers/school_menu/types.py` — `MenuMonth`, `MenuDay`, `MenuItem` data classes
- `providers/ha_provisioner` — idempotent HA entity creation

## Self-provisioned entities

| Entity ID | Type | Purpose |
|-----------|------|---------|
| `sensor.school_lunch_menu` | Virtual sensor (via `set_state`) | Main data store; attributes contain the full `schools` list with menu items per day |
| `input_text.school_lunch_selected_schools` | `input_text` helper | JSON array of school names selected for display in the at-a-glance card |
| `script.school_lunch_relay` | HA script | Relays `command`/`payload` from the Lovelace card to AppDaemon via the `school_lunch_command` event |

## Associated cards

- `school-lunch-card.js` — compact at-a-glance card showing tomorrow's entrees for selected schools
- `school-lunch-detail-card.js` — popup detail card with weekly view, full month calendars, and settings tab

## Config reference

| Key | Required | Default | Description |
|-----|----------|---------|-------------|
| `ha_url` | Yes | — | Home Assistant base URL (use `!secret ha_url`) |
| `ha_token_env` | Yes | — | Env var name containing the HA long-lived access token (e.g. `TOKEN`) |
| `sid` | Yes | — | Site ID for the School Nutrition and Fitness API (numeric string) |
| `menus` | Yes | — | List of school menu configs: `name` (display name) and `download_id` (numeric string from the site URL) |
| `default_selected` | No | `[]` | List of school names selected by default when the helper is first created |
| `show_tomorrow_after` | No | `"15:00:00"` | `HH:MM:SS` time after which cards flip from "Today's Lunch" to "Tomorrow's Lunch" (or Monday on Fri evenings/weekends) |

### Example config

```yaml
school_lunch_app:
  module: school_lunch_app.school_lunch_app
  class: SchoolLunchApp
  disable: true
  ha_url: !secret ha_url
  ha_token_env: TOKEN
  sid: "0802121850414637"
  menus:
    - name: "Elementary"
      download_id: "853700"
    - name: "Middle School"
      download_id: "854234"
    - name: "High School"
      download_id: "854323"
  default_selected:
    - "Elementary"
    - "Middle School"
  show_tomorrow_after: "12:00:00"
```

## Relay commands

The Lovelace detail card sends commands via `hass.callService("script", "school_lunch_relay", { command, payload })`.

### `select_schools`

Update which schools are displayed in the at-a-glance card.

```json
{
  "command": "select_schools",
  "payload": "{\"schools\": [\"Elementary\", \"High School\"]}"
}
```

Constraints: `schools` must be a non-empty list. Rejected with a WARNING log if empty.

### `fetch_month`

Fetch a specific month for a given school (used for prev/next month navigation in the detail card).

```json
{
  "command": "fetch_month",
  "payload": "{\"school\": \"Elementary\", \"menu_id\": \"abc123def456\"}"
}
```

`menu_id` is the MongoDB ObjectId from `prev_month_id` or `next_month_id` in the current sensor attributes.

## Sensor attribute schema

```json
{
  "schools": [
    {
      "name": "Elementary",
      "month": 3,
      "year": 2026,
      "days": [
        {
          "day": 3,
          "month": 3,
          "year": 2026,
          "items": [
            {"name": "Chicken Nuggets, Sweet Potato Fries, Garden Salad Cups", "role": "option"},
            {"name": "Grilled Cheese Sandwich", "role": "option"},
            {"name": "Chilled Fruit", "role": "includes"},
            {"name": "Milk Choice", "role": "includes"}
          ]
        }
      ],
      "prev_month_id": "abc123",
      "next_month_id": "def456"
    }
  ],
  "show_tomorrow_after": "12:00:00",
  "last_updated": "2026-03-15T10:00:00"
}
```

Notes:
- School-level `month` is **1-indexed** (1 = January, 12 = December) and represents the primary loaded month.
- Each day also carries its own `month` and `year` (1-indexed) to support cross-month lookups (e.g., next month's days appended for week views spanning a month boundary).
- `days` only includes school days that have menu data (weekends and holidays are absent). May include days from the next month when pre-fetched.
- Items have a `role` field: `"option"` for menu choices, `"includes"` for items appearing daily (auto-classified by the app based on 75%+ day frequency).
- Items starting with "OR " have the prefix stripped; all options are presented without it.
- `show_tomorrow_after` is the configured cutoff time. Cards read this to determine whether to show today's or tomorrow's lunch.
- `prev_month_id` / `next_month_id` are MongoDB ObjectIds or `null` if no adjacent month is published.

## Manual setup required

The following cannot be provisioned automatically:

| Item | Action |
|------|--------|
| Lovelace resource: `school-lunch-card.js` | Register via HA UI (Settings > Dashboards > Resources) or MCP `ha_config_set_dashboard_resource`. URL depends on how the JS file is served (e.g., via `/local/` or a HACS-like path). |
| Lovelace resource: `school-lunch-detail-card.js` | Same as above. |

## Upstream/downstream dependencies

This app is standalone — it has no upstream dependencies.

The Lovelace cards (`school-lunch-card.js`, `school-lunch-detail-card.js`) read from:
- `sensor.school_lunch_menu` (produced by this app)
- `input_text.school_lunch_selected_schools` (provisioned by this app)
