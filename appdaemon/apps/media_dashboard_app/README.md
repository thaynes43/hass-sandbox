# Media Dashboard App

AppDaemon app that aggregates media content from Tautulli (Plex), TMDb, and SerpApi (Google Showtimes) into a Wall Display dashboard card. Shows what's new on Plex, what's currently in theaters, and what's coming soon — with poster art, showtimes at local theaters, and a thumbs-up/down preference system.

## How It Works

1. On startup, provisions a relay script and two sensors, creates required filesystem directories, and loads persisted user preferences from disk.
2. Runs an initial fetch from all three sources: Tautulli (recently added + popular stats), TMDb (now playing + upcoming + trending), and SerpApi (showtimes via Google search).
3. Applies popularity filtering (TMDb score + vote count thresholds), genre filtering, and user preference boosts/hides to rank each category.
5. Downloads poster images for each item to a shared `/media/` directory, then calls a `shell_command` to sync them to `/config/www/` where HA can serve them.
6. Publishes the three categories (In Theaters, New on Plex, Coming Soon) to `sensor.media_dashboard_status` with local poster URLs and metadata.
7. When a user taps a poster, the card sends `get_detail` via relay. The app reads full metadata and cached showtimes from disk (no API call) and publishes to `sensor.media_dashboard_detail`.
8. Scheduled timers refresh each source on its own cadence (Tautulli: 2h, TMDb: 12h, showtimes: 24h). On partial upstream failure, the app retains last-known-good data per category.
9. Thumbs-down (`dismiss`) and thumbs-up (`like`) commands persist to a JSON preferences file and take effect on the next sensor publish.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  media_dashboard_app (AppDaemon)                    │
│                                                     │
│  ┌───────────────┐ ┌──────────────┐ ┌────────────┐ │
│  │ tautulli_     │ │ tmdb_        │ │ serpapi_   │ │
│  │ fetcher.py    │ │ fetcher.py   │ │ fetcher.py │ │
│  │               │ │              │ │            │ │
│  │ recently_     │ │ now_playing  │ │ showtimes/ │ │
│  │ added         │ │ upcoming     │ │ theater    │ │
│  │ home_stats    │ │ trending     │ │ (SerpApi)  │ │
│  │ pms_image_    │ │ discover     │ │            │ │
│  │ proxy (imgs)  │ │ image CDN    │ │            │ │
│  └───────┬───────┘ └──────┬───────┘ └─────┬──────┘ │
│          └────────────────┼───────────────┘         │
│                           ▼                         │
│              ┌────────────────────┐                 │
│              │ Poster cache       │                 │
│              │ /media/media-      │                 │
│              │ dashboard/posters/ │                 │
│              └────────┬───────────┘                 │
│                       │ shell_command sync           │
│                       ▼                             │
│              /config/www/media-dashboard/posters/   │
│                                                     │
│  ┌─────────────────────┐  ┌──────────────────────┐  │
│  │ sensor.media_       │  │ sensor.media_        │  │
│  │ dashboard_status    │  │ dashboard_detail     │  │
│  │ (categories + meta) │  │ (selected item +     │  │
│  │                     │  │  showtimes)          │  │
│  └─────────┬───────────┘  └───────────┬──────────┘  │
└────────────┼──────────────────────────┼─────────────┘
             │ HA WebSocket             │ HA WebSocket
             ▼                          ▼
┌─────────────────────────────────────────────────────┐
│  Lovelace Cards                                     │
│  - media-dashboard-card.js (compact view)           │
│  - media-dashboard-detail-card.js (popup/detail)    │
│                                                     │
│  Reads: sensor attributes (metadata + poster URLs)  │
│  Sends: commands via script.media_dashboard_relay   │
└─────────────────────────────────────────────────────┘
```

### File Layout

```
appdaemon/apps/media_dashboard_app/
├── __init__.py
├── media_dashboard_app.py          # Main app — lifecycle, fetcher orchestration, sensor publish
├── cards/
│   ├── media-dashboard-card.js     # Compact card: 3 posters, category tabs, dismiss button
│   └── media-dashboard-detail-card.js  # Detail popup: all categories, inline expand, showtimes
└── README.md

appdaemon/providers/media_providers/
├── __init__.py
├── types.py                        # Shared dataclasses: MediaItem, ShowtimeEntry, FetchResult, etc.
├── tautulli_client.py              # HTTP client for Tautulli REST API
├── tautulli_fetcher.py             # Fetcher: recently added, popular stats, poster download
├── tmdb_client.py                  # HTTP client for TMDb v3 API
├── tmdb_fetcher.py                 # Fetcher: now playing, upcoming, trending, poster download
├── serpapi_client.py               # HTTP client for SerpApi Google Showtimes
└── serpapi_fetcher.py              # Fetcher: showtime search and parsing

appdaemon/tests/
├── test_media_types.py
├── test_tautulli_fetcher.py
├── test_tmdb_fetcher.py
├── test_serpapi_fetcher.py
├── test_media_dashboard_app.py
├── test_media_dashboard_relay.py
└── test_media_dashboard_showtime_cache.py
```

## Data Sources

| Source | Purpose | Refresh |
|--------|---------|---------|
| **Tautulli** | Recently added movies/shows from Plex, watch popularity stats, poster images via `pms_image_proxy` (hides Plex token) | Every 2 hours |
| **TMDb** | Now-playing theaters list, upcoming releases, trending movies, popularity/vote metadata, poster images from CDN | Every 12 hours |
| **SerpApi** (Google Showtimes) | Theater showtimes via Google search; filters results to configured local theaters | Once daily |

## Content Categories

| Category | Key | Icon | Content | Subtitle Format |
|----------|-----|------|---------|-----------------|
| In Theaters | `in_theaters` | Film | TMDb now-playing filtered by popularity | "PG-13 · 2h 15m" |
| New on Plex | `plex_new` | TV | Tautulli recently-added, cross-referenced with TMDb popularity | "Added 2 days ago" |
| Coming Soon | `coming_soon` | Popcorn | TMDb upcoming theatrical + streaming releases, sorted by release date | "Opens Apr 18" / "Netflix Apr 2" |

## Self-Provisioned Entities

| Entity | Type | Purpose |
|--------|------|---------|
| `sensor.media_dashboard_status` | Virtual sensor (`set_state`) | Primary sensor — categories with items, poster URLs, fetch status. State: `ok` / `degraded` / `error`. |
| `sensor.media_dashboard_detail` | Virtual sensor (`set_state`) | On-demand detail for the selected item — full synopsis, showtimes from disk cache. State: selected item ID. |
| `script.media_dashboard_relay` | Script | Relay card commands to the `media_dashboard_command` AppDaemon event |

## Associated Cards

| Card | File | Purpose |
|------|------|---------|
| Compact view | `cards/media-dashboard-card.js` | Shows 3 posters per row with category tabs at the bottom; auto-rotates; dismiss button per poster |
| Detail popup | `cards/media-dashboard-detail-card.js` | Full-screen popup with all three category rows; tap poster to expand inline detail with synopsis and showtimes |

## Dependencies

| Provider | Usage |
|----------|-------|
| `providers.media_providers.tautulli_fetcher` | Plex recently-added items, popularity cross-reference, poster download |
| `providers.media_providers.tmdb_fetcher` | In-theaters and coming-soon data, mainstream filter, poster download |
| `providers.media_providers.serpapi_fetcher` | Theater showtime search and parsing |
| `providers.media_providers.types` | `MediaItem`, `FetchResult`, `ShowtimeCache`, `ShowtimeEntry`, `CinemaInfo` dataclasses |
| `providers.ha_provisioner.HAProvisioner` | Creates relay script and sensors on startup |
| `providers.secrets.resolve_arg_secret` | Resolves `_env`-suffix config keys to actual values at runtime |

## Upstream / Downstream Dependencies

This app is standalone — it fetches from external APIs and publishes sensors for cards to consume. No other AppDaemon app depends on it.

## Config Reference (`apps-prod.yaml`)

### Required

```yaml
media_dashboard_app:
  module: media_dashboard_app.media_dashboard_app
  class: MediaDashboardApp
  ha_url: !secret ha_url
  ha_token_env: TOKEN
  tautulli_url: !secret tautulli_url
  tautulli_api_key_env: TAUTULLI_API_KEY
  tmdb_api_key_env: TMDB_API_KEY
  serpapi_api_key_env: SERPAPI_KEY
  location: "Westford, MA"
  theaters:
    - AMC Tyngsboro 12
    - Showcase Cinema de Lux Lowell
    - AMC Methuen 20
    - Cinemark Rockingham Park and XD
    - AMC Burlington Cinema 10
  media_fs_root_env: MEDIA_FS_ROOT
```

### Optional (with defaults)

| Key | Default | Description |
|-----|---------|-------------|
| `tautulli_refresh_interval` | `7200` | Seconds between Tautulli refreshes (2 hours) |
| `tmdb_refresh_interval` | `43200` | Seconds between TMDb refreshes (12 hours) |
| `showtimes_refresh_interval` | `86400` | Seconds between showtime refreshes (24 hours) |
| `max_items_per_category` | `10` | Max items published per category in the main sensor |
| `popularity_threshold` | `10.0` | TMDb popularity minimum to pass the filter |
| `vote_count_threshold` | `50` | TMDb vote count minimum to pass the filter |
| `stale_ttl_days` | `7` | Days before an item is evicted from a category for staleness |
| `genre_filter` | `[]` | Optional genre allow-list. Empty = all genres pass through |
| `poster_media_subdir` | `media-dashboard/posters` | Subdir under `media_fs_root` where posters are stored |
| `poster_www_subdir` | `media-dashboard/posters` | Subdir under `/config/www/` where posters are served |
| `preferences_file_subdir` | `media-dashboard` | Subdir under `media_fs_root` where `preferences.json` lives |
| `showtime_cache_subdir` | `media-dashboard` | Subdir under `media_fs_root` where `showtime-cache.json` lives |
| `poster_width` | `342` | Poster download width in pixels (TMDb `w342` size) |
| `poster_sync_shell_command` | `media_dashboard_sync_posters` | Name of the HA shell command that copies posters to `/config/www/` |

## Relay Commands

Cards communicate with the app via `hass.callService("script", "media_dashboard_relay", { command, payload })`. The relay script fires a `media_dashboard_command` event that the app listens for.

| Command | Payload | Description |
|---------|---------|-------------|
| `refresh` | `{"source": "all"\|"tautulli"\|"tmdb"\|"showtimes"}` | Force refresh from one or all sources |
| `get_detail` | `{"id": "tmdb-11111"}` | Read full metadata + showtimes from cache; publish to `sensor.media_dashboard_detail` |
| `dismiss` | `{"id": "tmdb-11111"}` | Hide an item (thumbs down); persist to preferences file |
| `like` | `{"id": "tmdb-11111"}` | Boost an item to the top (thumbs up); persist to preferences file |
| `undo_dismiss` | `{"id": "tmdb-11111"}` | Remove a previously dismissed item from the hidden list |

## Sensor Schema

### `sensor.media_dashboard_status`

State: `ok` | `degraded` | `error`

```json
{
  "last_updated": "2026-03-29T18:00:00",
  "categories": {
    "in_theaters": {
      "label": "In Theaters",
      "items": [
        {
          "id": "tmdb-11111",
          "title": "Thunderbolts*",
          "year": 2025,
          "poster": "/local/media-dashboard/posters/tmdb-11111.jpg",
          "type": "movie",
          "subtitle": "PG-13 · 2h 7m",
          "rating": "PG-13",
          "runtime_min": 127,
          "tmdb_score": 7.8,
          "genres": "Action, Adventure",
          "has_showtimes": true
        }
      ]
    },
    "plex_new": {
      "label": "New on Plex",
      "items": [
        {
          "id": "plex-12345",
          "title": "Movie Title",
          "year": 2026,
          "poster": "/local/media-dashboard/posters/plex-12345.jpg",
          "type": "movie",
          "subtitle": "Added 2 days ago",
          "rating": "PG-13",
          "genres": "Action, Sci-Fi"
        }
      ]
    },
    "coming_soon": {
      "label": "Coming Soon",
      "items": [
        {
          "id": "tmdb-22222",
          "title": "Movie Title",
          "poster": "/local/media-dashboard/posters/tmdb-22222.jpg",
          "type": "movie",
          "subtitle": "Opens May 2",
          "release_date": "2026-05-02",
          "release_type": "theatrical",
          "genres": "Animation, Comedy"
        }
      ]
    }
  },
  "fetch_status": {
    "tautulli": {"last_ok": "2026-03-29T17:00:00", "status": "ok"},
    "tmdb": {"last_ok": "2026-03-29T12:00:00", "status": "ok"},
    "serpapi": {"last_ok": "2026-03-29T06:00:00", "status": "ok"}
  },
  "friendly_name": "Media Dashboard",
  "icon": "mdi:movie-open-outline"
}
```

### `sensor.media_dashboard_detail`

State: selected item ID (e.g., `tmdb-11111`), or `none` when nothing is selected.

```json
{
  "id": "tmdb-11111",
  "title": "Thunderbolts*",
  "year": 2025,
  "poster": "/local/media-dashboard/posters/tmdb-11111.jpg",
  "rating": "PG-13",
  "runtime_min": 127,
  "genres": "Action, Adventure",
  "tmdb_score": 7.8,
  "summary": "A team of antiheroes is recruited by Valentina Allegra de Fontaine...",
  "release_type": "in_theaters",
  "showtimes": {
    "AMC Tyngsboro 12": ["13:30", "16:15", "19:00", "21:45"],
    "Showcase Lowell": ["13:00", "15:45", "18:30", "21:15"],
    "AMC Methuen 20": ["14:00", "17:30", "20:15", "22:30"],
    "Cinemark Salem XD": ["13:15", "16:00", "19:30"]
  },
  "showtimes_date": "2026-03-29",
  "friendly_name": "Media Dashboard Detail",
  "icon": "mdi:movie-open-outline"
}
```

**Size budget**: The main sensor targets ~6KB (10 items × 3 categories × ~200 bytes each). Showtimes and synopsis are only in the detail sensor, keeping both sensors well under the HA WebSocket 16KB limit.

## Showtime Caching

Showtimes are batch-fetched daily and cached to `{media_fs_root}/{showtime_cache_subdir}/showtime-cache.json`. The `get_detail` command reads from this disk cache — no API call on user interaction. SerpApi is queried once daily (one search covers all configured theaters).

Staleness handling:
- Cache older than 24h and refresh failed: showtimes shown with "Showtimes from yesterday" note
- Cache older than 48h: showtimes omitted from detail view; `has_showtimes` set to `false`

## User Preferences

Preferences are persisted to `{media_fs_root}/{preferences_file_subdir}/preferences.json`:

```json
{
  "hidden": ["tmdb-12345", "plex-67890"],
  "liked": ["tmdb-11111"],
  "hidden_at": {"tmdb-12345": "2026-03-29T10:00:00"},
  "liked_at": {"tmdb-11111": "2026-03-28T15:00:00"}
}
```

Preferences are loaded on startup and written back on each `dismiss`, `like`, or `undo_dismiss` command. Timestamps enable future cleanup of stale preferences (items dismissed more than 90 days ago).

## Failure Modes

| Failure | Behavior |
|---------|----------|
| Tautulli unreachable | Retain last-known-good `plex_new` items; set `fetch_status.tautulli.status = "error"` |
| TMDb unreachable | Retain last-known-good `in_theaters` and `coming_soon`; set `fetch_status.tmdb.status = "error"` |
| SerpApi unreachable | Retain cached showtimes on disk; set `fetch_status.serpapi = "error"`; detail view shows stale data with note |
| Source returns empty | Clear that category's items (genuinely empty is valid); set status to `"ok"` |

## Manual Setup Required

These cannot be auto-provisioned and must be configured manually.

### 1. Shell Command (`configuration.yaml`)

Add this to HA's `configuration.yaml` and restart HA:

```yaml
shell_command:
  media_dashboard_sync_posters: >-
    /bin/sh -c 'set -e;
    src="/media/media-dashboard/posters";
    dest="/config/www/media-dashboard/posters";
    mkdir -p "$dest";
    [ -d "$src" ] && [ -n "$(ls -A "$src" 2>/dev/null)" ] || exit 0;
    find "$dest" -type f | while read f; do
      [ -f "$src/$(basename "$f")" ] || rm -f "$f";
    done;
    cp -a "$src"/. "$dest"/'
```

### 2. Lovelace Resources

Copy both card JS files to `/config/www/media-dashboard/` and register them as Lovelace resources:

```yaml
- url: /local/media-dashboard/media-dashboard-card.js?v=1
  type: module
- url: /local/media-dashboard/media-dashboard-detail-card.js?v=1
  type: module
```

Bump the `?v=N` query param after each card update.

### 3. Secrets

Configure the following environment variables (dev: `.env` file; prod: Kubernetes ExternalSecret):

| Variable | Description |
|----------|-------------|
| `TAUTULLI_API_KEY` | Tautulli API key (Settings > Web Interface > API Key) |
| `TMDB_API_KEY` | TMDb v3 API key (free tier, from themoviedb.org/settings/api) |
| `SERPAPI_KEY` | SerpApi API key (from serpapi.com/manage-api-key) |
| `MEDIA_FS_ROOT` | Filesystem root for `/media/` (default: `/media`; override in dev) |
| `TOKEN` | Home Assistant long-lived access token |

### 4. Dashboard Cards

Add both cards to the Wall Display dashboard. Compact card goes in the right column below the calendar:

```yaml
# Compact card
type: custom:media-dashboard-card
status_entity: sensor.media_dashboard_status
relay_script: media_dashboard_relay

# Detail popup card
type: custom:media-dashboard-detail-card
status_entity: sensor.media_dashboard_status
detail_entity: sensor.media_dashboard_detail
relay_script: media_dashboard_relay
```
