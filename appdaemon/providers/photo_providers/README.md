# Photo Providers

Abstraction layer for photo sources. Defines a `PhotoProvider` protocol and data types that any photo source must implement. Currently has one implementation: Immich.

## Architecture

```
photo_providers/
├── types.py               — Protocol + data types (PhotoProvider, PhotoFilter, PhotoMetadata, etc.)
├── immich_client.py        — Low-level Immich HTTP API client
├── immich_selectors.py     — Filter-to-API translation (all_photos, search, album)
├── immich_data_provider.py — ImmichDataProvider implementing PhotoProvider protocol
└── __init__.py             — Package exports
```

## PhotoProvider protocol

Any photo source must implement:

- `refresh_metadata()` — return people and albums available for filter configuration
- `fetch_photo_ids(filter, count, location_aliases=...)` — return matching photo IDs
- `download_photo(photo_id)` — download a photo's bytes

## PhotoFilter

Selection criteria for fetching photos:

| Field | Description |
|-------|-------------|
| `name` | Human-readable filter name |
| `selection` | `all_photos`, `search`, or `album` |
| `randomize` | Sample matching photos; always enabled for All Photos |
| `paused` | Keep a filter configured while skipping it during automatic rotation |
| `search_query` | Required smart-search text when selection is `search` |
| `search_pool_size` | Number of matching smart-search candidates to sample when randomizing |
| `people` | List of person names to filter by |
| `location` | Location text for search queries |
| `taken_after`, `taken_before` | Optional absolute ISO date/time limits |
| `recurring_date_ranges` | Inclusive OR list of `{start: "MM-DD", end: "MM-DD"}` ranges across years; omitted end means one day; descending ranges cross New Year |
| `rating` | Minimum rating filter |
| `favorites_only` | Only fetch favorites |
| `album_name` | Album name (for `album` selection type) |

## Immich implementation

`ImmichDataProvider` connects to an Immich server via REST API:

- Caches people name-to-ID mapping for filter resolution
- Resolves album names to IDs
- Rejects unresolved people and albums instead of broadening the filter
- Paginates matching assets before recurring-date filtering and sampling; uses `localDateTime` for birthdays/holidays, with `fileCreatedAt` as fallback
- Applies people, location, favorites, and date criteria to album selections too
- Supports `preview`, `fullsize`, and `original` download quality
- API key resolved via `providers.secrets.resolve_secret()`

## Dependencies

- `providers.secrets` — API key resolution
- `aiohttp` — asynchronous HTTP client (via `immich_client.py`)

## Used by

- `immich_fetcher` app — periodic photo fetching

## Extending

To add a new photo source (e.g. Google Photos, Apple Photos):

1. Create a new module (e.g. `google_photos_provider.py`) implementing the `PhotoProvider` protocol from `types.py`.
2. The consuming app selects the provider based on config.
