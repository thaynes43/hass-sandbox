# Media Dashboard App

AppDaemon app that aggregates media content from Tautulli (Plex), TMDb, and SerpApi (Google Showtimes) into a Wall Display dashboard card. Shows what's new on Plex, what's currently in theaters, and what's coming soon — with poster art, showtimes at local theaters, and a thumbs-up/down preference system.

## How It Works

1. On startup, provisions a relay script and two sensors, creates required filesystem directories, and loads persisted user preferences from disk.
2. Runs an initial fetch from all three sources: Tautulli (recently added + popular stats), TMDb (now playing pages 1-3 + upcoming + trending), and SerpApi (showtimes via Google search, locale pinned to US English).
3. Applies popularity filtering (TMDb score + vote count thresholds), genre filtering, and user preference boosts/hides to rank each category.
4. Downloads poster images for each item to a shared `/media/` directory, then calls a `shell_command` to sync them to `/config/www/` where HA can serve them.
5. Composes the In Theaters row from the TMDb result *and* the showtime cache (see [In Theaters semantics](#in-theaters-semantics)), then publishes the four categories (In Theaters, Plex Movies, Plex Shows, Coming Soon) to `sensor.media_dashboard_status` with local poster URLs and metadata.
6. When a user taps a poster, the card sends `get_detail` via relay. The app reads full metadata and cached showtimes from disk (no API call) and publishes to `sensor.media_dashboard_detail`.
7. Scheduled timers refresh each source on its own cadence (Tautulli: 2h, TMDb: 12h, showtimes: 24h). On partial upstream failure, the app retains last-known-good data per category.
8. Thumbs-down (`dismiss`) and thumbs-up (`like`) commands persist to a JSON preferences file and take effect on the next sensor publish.

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
├── serpapi_fetcher.py              # Fetcher: showtime search and parsing
└── mdblist_client.py               # HTTP client for MDbList ratings aggregator (IMDb, RT, Metacritic)

appdaemon/tests/
├── test_tautulli_fetcher.py
├── test_tmdb_fetcher.py
├── test_serpapi_client.py          # Locale params, session handling
├── test_serpapi_fetcher.py
├── test_mdblist_client.py
├── test_media_dashboard_app.py     # Composition, freshness, title matching, preferences
└── test_media_dashboard_relay.py
```

## Data Sources

| Source | Purpose | Refresh |
|--------|---------|---------|
| **Tautulli** | Recently added movies/shows from Plex, watch popularity stats, poster images via `pms_image_proxy` (hides Plex token) | Every 2 hours |
| **TMDb** | Now-playing list (pages 1-3), upcoming releases, trending movies, popularity/vote metadata, poster images from CDN | Every 12 hours |
| **SerpApi** (Google Showtimes) | Theater showtimes via Google search, one search per configured theater, locale pinned to US English | Once per calendar day |
| **MDbList** | External ratings aggregator — IMDb score, Rotten Tomatoes critics/audience, Metacritic; fetched per item (1 req/s) | On each TMDb/Tautulli refresh |

## Content Categories

| Category | Key | Icon | Content | Subtitle Format |
|----------|-----|------|---------|-----------------|
| In Theaters | `in_theaters` | Film | Movies with showtimes at the configured theaters this week; TMDb now-playing when showtime data is unusable | "PG-13 · 2h 15m" |
| Plex Movies | `plex_movies` | TV | Tautulli recently-added movies, cross-referenced with TMDb popularity | "Added 2 days ago" |
| Plex Shows | `plex_shows` | TV | Tautulli recently-added TV shows, quality scored with 50min runtime normalization | "Added 2 days ago" |
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
| Compact view | `cards/media-dashboard-card.js` | Shows 3 posters per row with category tabs; auto-rotates; paginate by 3 with chevron arrows; dismiss button per poster |
| Detail popup | `cards/media-dashboard-detail-card.js` | Popup with all four category rows; scroll arrows on poster rows; tap poster for inline detail with synopsis and showtimes; like/unlike toggle; hidden items restore |

## Dependencies

| Provider | Usage |
|----------|-------|
| `providers.media_providers.tautulli_fetcher` | Plex recently-added items, popularity cross-reference, poster download |
| `providers.media_providers.tmdb_fetcher` | In-theaters and coming-soon data, mainstream filter, poster download |
| `providers.media_providers.serpapi_fetcher` | Theater showtime search and parsing |
| `providers.media_providers.mdblist_client` | External ratings aggregator — IMDb, Rotten Tomatoes, Metacritic |
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
  tautulli_url_env: TAUTULLI_URL
  tautulli_api_key_env: TAUTULLI_API_KEY
  tmdb_api_key_env: TMDB_API_KEY
  serpapi_api_key_env: SERPAPI_KEY
  mdblist_api_key_env: MDBLIST_API_KEY
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
| `max_items_per_category` | `20` | Max items published per category in the main sensor |
| `popularity_threshold` | `5.0` | TMDb popularity minimum to pass the filter |
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
| `unlike` | `{"id": "tmdb-11111"}` | Remove a previously liked item from the liked list |
| `undo_dismiss` | `{"id": "tmdb-11111"}` | Remove a previously dismissed item from the hidden list |

## Sensor Schema

### `sensor.media_dashboard_status`

State: `ok` | `degraded` | `error`

```json
{
  "last_updated": "2026-03-29T18:00:00",
  "categories": {
    "in_theaters": [
      {
        "id": "tmdb-11111",
        "title": "Project Hail Mary",
        "year": 2026,
        "poster": "/local/media-dashboard/posters/tmdb-11111.jpg",
        "media_type": "movie",
        "subtitle": "PG-13 · 2h 7m",
        "rating": "PG-13",
        "runtime_min": 127,
        "tmdb_score": 7.8,
        "genres": "Action, Adventure",
        "has_showtimes": true,
        "liked": true
      }
    ],
    "plex_movies": [ ... ],
    "plex_shows": [ ... ],
    "coming_soon": [ ... ]
  },
  "hidden_eligible": {
    "in_theaters": [
      {
        "id": "tmdb-22222",
        "title": "Hidden Movie",
        "poster": "/local/media-dashboard/posters/tmdb-22222.jpg",
        "hidden": true
      }
    ]
  },
  "fetch_status": {
    "tautulli": "ok",
    "tmdb": "ok",
    "serpapi": "ok"
  },
  "showtimes_date": "2026-03-29",
  "friendly_name": "Media Dashboard Status",
  "icon": "mdi:movie-roll"
}
```

`showtimes_date` is the date stamped on the showtime cache the In Theaters row was last composed against (empty string when there is no cache) — the one attribute that says whether the row is showtime-backed or the TMDb now-playing fallback.

The `hidden_eligible` attribute contains items that passed quality/stale filters but were dismissed by the user. The detail card shows these in a collapsible "Hidden (N)" section with a restore button per category.

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

**Size budget**: The main sensor targets ~8KB (10 items × 4 categories × ~200 bytes each). Showtimes and synopsis are only in the detail sensor, keeping both sensors well under the HA WebSocket 16KB limit.

## In Theaters Semantics

"In Theaters" means **playing at the configured `theaters` this week**, not "recently released somewhere".

`TmdbFetcher.fetch_in_theaters()` returns a *pool*: `/movie/now_playing` pages 1-3 (one page is 20 titles — fewer than a multiplex actually screens) plus `/trending/movie/week`. Each item records where it came from in `release_type`:

| `release_type` | Meaning |
|----------------|---------|
| `in_theaters` | On TMDb's US now-playing list |
| `trending` | Only trending this week — streaming hits and months-old releases live here too |

The app keeps that pool in memory (`_tmdb_in_theaters_pool`) and `_compose_in_theaters()` builds the published row from it against the showtime cache. Both the TMDb refresh and the showtime refresh (its cached-skip path *and* its fetched path) call it, so a TMDb refresh can no longer drop the `has_showtimes` flags the showtime refresh set.

Two tiers:

1. **Showtime data usable** — the row is every pool item whose title matches a cached film with a screening today or later, ranked as usual. Trending-only titles are included when they are genuinely playing (a 4K re-release, for instance).
2. **Showtime data unusable** — the row falls back to pool items with `release_type == "in_theaters"` only. Trending-only titles are never shown without showtimes; that is what put months-old films on the wall display.

One INFO line per composition says which tier ran:

```
In Theaters: 12 of 41 TMDb titles have showtimes at Cinemark Rockingham Park and XD or AMC Methuen 20 (cache 2026-09-22)
In Theaters: no usable showtime data (cache is dated 2026-09-19); showing TMDb now-playing (23)
```

### Title matching

TMDb and Google spell titles differently, so `_showtime_title_matches()` normalises both sides (case-fold, `&` → `and`, drop everything that is not a letter/digit/space, collapse whitespace) and matches on equality **or** when the cached title starts with the TMDb title followed by a space ("Ghost in the Shell 30th Anniversary 4K" matches "Ghost in the Shell"). It is deliberately not a substring test — that let "Hope" match "Hopeless" and any foreign-language title containing the word.

## Showtime Caching

Showtimes are batch-fetched daily and cached to `{media_fs_root}/{showtime_cache_subdir}/showtime-cache.json`. The `get_detail` command reads from this disk cache — no API call on user interaction. SerpApi is queried once per calendar day (one search per configured theater), and the on-disk cache date is the guard, so restarts and card refreshes do not spend quota.

### Locale

`SerpApiClient.get_showtimes()` always sends `hl=en`, `gl=us`, `google_domain=google.com`. Without them SerpApi's exit node decides the locale: on 2026-09-22 one theater came back entirely in Lithuanian ("absoliutus blogis" for "Resident Evil"), which matches no TMDb title, and its day labels ("Šiandien") resolved to nothing. When a day label cannot be resolved the fetcher logs one WARNING per theater naming the raw label and falls back to today, so a locale regression is visible in the log instead of silently stamping every screening with today's date.

### Freshness is a calendar-day comparison

`cache.date` is stamped with the *local* date, so freshness compares calendar days — never an hour count (parsing the date-only stamp as UTC midnight called a cache fetched this morning "stale" by 20:00 local, and dropped it entirely the next evening although it was one day old).

| Cache date | Behaviour |
|------------|-----------|
| Today | Fresh — showtimes shown, `has_showtimes` set |
| Yesterday | Usable — showtimes shown with `stale: true` and the note `Showtimes were fetched yesterday (YYYY-MM-DD)` |
| Older / missing / unparseable | Not usable — In Theaters falls back to TMDb now-playing, the detail popup says `Showtimes not available (last fetched YYYY-MM-DD)` |

A cache is only usable if it also carries at least one screening dated today or later; entries dated before today are never shown or flagged (yesterday's fetch includes yesterday's screenings).

Detail-popup notes when an In Theaters item has no entries:

| Situation | Note |
|-----------|------|
| Cache usable, film absent | `Not playing at <theater> or <theater> this week` |
| Cache unusable, date known | `Showtimes not available (last fetched YYYY-MM-DD)` |
| No cache at all | `No showtime data available` |

`media-dashboard-detail-card.js::_renderShowtimes()` renders `note` only when there are no entries, so the yesterday note travels in the sensor (`stale: true` + `note`) but is not drawn above a populated showtime list. Rendering it there needs a card change and a `?v=N` bump.

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

Preferences are loaded on startup and written back on each `dismiss`, `like`, `unlike`, or `undo_dismiss` command. Timestamps enable future cleanup of stale preferences (items dismissed more than 90 days ago).

## Failure Modes

| Failure | Behavior |
|---------|----------|
| Tautulli unreachable | Retain last-known-good `plex_movies` and `plex_shows` items; set `fetch_status.tautulli.status = "error"` |
| TMDb unreachable | Retain last-known-good `in_theaters` and `coming_soon`; set `fetch_status.tmdb.status = "error"` |
| SerpApi unreachable | Retain cached showtimes on disk; set `fetch_status.serpapi = "error"`. Yesterday's cache is still used (with a "fetched yesterday" note); anything older drops In Theaters to the TMDb now-playing fallback |
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
| `TAUTULLI_URL` | Tautulli base URL (e.g. `http://192.168.1.10:8181`) |
| `TAUTULLI_API_KEY` | Tautulli API key (Settings > Web Interface > API Key) |
| `TMDB_API_KEY` | TMDb v3 API key (free tier, from themoviedb.org/settings/api) |
| `SERPAPI_KEY` | SerpApi API key (from serpapi.com/manage-api-key) |
| `MDBLIST_API_KEY` | MDbList API key (from mdblist.com/api/) — optional; disables external ratings if absent |
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
