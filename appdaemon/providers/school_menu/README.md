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
3. Maps noticed to the correct calendar weekday by grid position (row/column geometry).
4. Appends `MenuDay` entries with `notice` set and, for `GRAB AND GO` days, a synthetic `MenuItem`.

Notice matching is heuristic (grid geometry is empirically derived) and may misalign on unusual calendar layouts.

## Limitations

- No authentication — only works with publicly accessible school sites on the School Nutrition and Fitness platform.
- Notice mapping is position-based; schools with non-standard calendar templates may get incorrect day assignments.
- No rate limiting or retry logic — callers are responsible for back-off if needed.
- `month` field in the API response is 0-indexed; always use `MenuMonth.display_month` for human-facing output.

## Dependencies

- `aiohttp` — async HTTP client
- No AppDaemon or HA dependencies — fully testable in isolation

## Used by

- `school_lunch_app` — fetches menus on startup and daily refresh, publishes to `sensor.school_lunch_menu`
