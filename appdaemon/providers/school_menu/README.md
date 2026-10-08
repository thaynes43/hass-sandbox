# school_menu Provider

Async HTTP client for the School Nutrition and Fitness platform. Fetches structured monthly lunch menus for configured schools and exposes them as typed Python dataclasses.

## Package layout

```
school_menu/
├── types.py    — MenuMonth, MenuDay, MenuItem dataclasses
├── client.py   — SchoolMenuClient (aiohttp-based, no AppDaemon dependency)
└── __init__.py — Package exports
```

## API contract

### Data types (`types.py`)

| Type | Fields | Notes |
|------|--------|-------|
| `MenuItem` | `name`, `category`, `is_ancillary` | Single food item. `is_ancillary=True` for milk/condiments. |
| `MenuDay` | `day`, `month`, `year`, `items`, `notice` | One calendar day. `notice` holds holiday/closure text when present. |
| `MenuMonth` | `menu_id`, `menu_type_name`, `month`, `year`, `days`, `previous_month_id`, `next_month_id` | Full month. `month` is 0-indexed (API convention); use `display_month` property for 1-indexed. |

### `SchoolMenuClient`

Async context manager. Pass the site ID (`sid`) from the school district's menu URL (the `sid=` query parameter). The district id is treated as a secret — apps read it from the `SCHOOL_LUNCH` env var rather than hardcoding it.

```python
async with SchoolMenuClient(sid="<district-site-id>") as client:
    # Step 1: resolve a human-facing numeric download ID to a MongoDB ObjectId
    resolved = await client.resolve_menu_id("853700")
    # -> {"id": "69652cd2...", "site_code": "244"}

    # Step 2: fetch menu data for the resolved ID
    menu: MenuMonth = await client.fetch_menu(resolved["id"])
```

You can also inject an existing `aiohttp.ClientSession` to share connections across calls:

```python
async with aiohttp.ClientSession() as session:
    client = SchoolMenuClient(sid=sid, session=session)
    menu = await client.fetch_menu(menu_id)
```

## External APIs used

| Endpoint | Protocol | Purpose |
|----------|----------|---------|
| `https://www.schoolnutritionandfitness.com/downloadMenu.php/{sid}/{download_id}` | HTTP GET (follow 302) | Resolves numeric download ID → MongoDB ObjectId + site code |
| `https://api.schoolnutritionandfitness.com/graphql` | HTTP POST (GraphQL) | Fetches structured menu items by MongoDB ID |
| `https://www.schoolnutritionandfitness.com/webmenus2/api/menuController.php/open-raw?id={menu_id}` | HTTP GET (REST/JSON) | Fetches content overlays (holiday notices, early releases) |

No authentication is required — all endpoints are publicly accessible.

## Notice/holiday handling

The GraphQL endpoint only returns days that have menu items. Days with no items (holidays, early releases) appear as positioned HTML text overlays in the content overlay API. `SchoolMenuClient.fetch_menu()` automatically:

1. Fetches content overlays after the GraphQL response.
2. Parses overlay HTML for notice keywords (`NO SCHOOL`, `EARLY RELEASE`, `HOLIDAY`, etc.).
3. Places each notice in the calendar cell where its box starts: the week row from the box's top edge, the weekday column from the centre of its first column's worth of width, so a banner over several days lands on its first day (the `GRID_*` constants in `client.py`).
4. Appends `MenuDay` entries with `notice` set and, for `GRAB AND GO` days, a synthetic `MenuItem`. A notice is kept only when its cell is a weekday of the month that has no menu items. It is never moved to another day. A notice over a day that has a menu is logged at INFO and dropped. A notice with an unreadable position, outside the calendar grid, over a cell outside the month, or over a day that already has a notice is logged at WARNING and dropped.

Before 1.25.2 the notices of a week went left to right onto that week's days without a menu. A holiday that has no notice box therefore took the next day's early-release notice: October 12, 2026 got the notice for October 13, and June 19, 2025 got the one for June 20.

The grid geometry is empirical (calibrated on the district's 2025-2026 designs) and may not fit an unusual calendar layout.

## Limitations

- No authentication — only works with publicly accessible school sites on the School Nutrition and Fitness platform.
- Notice mapping is position-based; on a non-standard calendar template a notice can be dropped (a WARNING in the log).
- No rate limiting or retry logic — callers are responsible for back-off if needed.
- `month` field in the API response is 0-indexed; always use `MenuMonth.display_month` for human-facing output.

## Dependencies

- `aiohttp` — async HTTP client
- No AppDaemon or HA dependencies — fully testable in isolation

## Used by

- `school_lunch_app` — fetches menus on startup and daily refresh, publishes to `sensor.school_lunch_menu`
