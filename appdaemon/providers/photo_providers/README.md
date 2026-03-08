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

- `get_people()` — list available people (name + ID)
- `get_albums()` — list available albums (name + ID)
- `fetch_photos(filter, num_photos, output_dir, quality)` — download photos matching a filter
- `get_photo_metadata(asset_id)` — get metadata for a specific photo

## PhotoFilter

Selection criteria for fetching photos:

| Field | Description |
|-------|-------------|
| `name` | Human-readable filter name |
| `selection_type` | `all_photos`, `search`, or `album` |
| `people` | List of person names to filter by |
| `location` | Location text for search queries |
| `date_range` | Date range filter |
| `min_rating` | Minimum rating filter |
| `favorites_only` | Only fetch favorites |
| `album_name` | Album name (for `album` selection type) |

## Immich implementation

`ImmichDataProvider` connects to an Immich server via REST API:

- Caches people name-to-ID mapping for filter resolution
- Resolves album names to IDs
- Supports `preview`, `fullsize`, and `original` download quality
- API key resolved via `providers.secrets.resolve_secret()`

## Dependencies

- `providers.secrets` — API key resolution
- `aiohttp` or `requests` — HTTP client (via `immich_client.py`)

## Used by

- `immich_fetcher` app — periodic photo fetching

## Extending

To add a new photo source (e.g. Google Photos, Apple Photos):

1. Create a new module (e.g. `google_photos_provider.py`) implementing the `PhotoProvider` protocol from `types.py`.
2. The consuming app selects the provider based on config.
