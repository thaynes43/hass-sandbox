# Media Dashboard Card — Requirements & Wireframes

## Overview

A new AppDaemon-powered dashboard card that displays media content across four categories: what's new on the family Plex server, what's releasing soon to streaming/on-demand, what's currently in movie theaters, and what's coming to theaters soon. The card lives on the Wall Display dashboard, positioned below the calendar in the right column. Tapping a poster opens a detail popup with showtimes, synopsis, and ratings. Users can thumbs-up/down items to train the filter over time.

## Data Sources

### 1. Tautulli — "New on Plex"

**API**: Tautulli REST API (wraps Plex, hides Plex token server-side)

**Endpoints used**:
- `get_recently_added` — recently added movies/shows (title, year, rating, genres, summary, `thumb` path, `added_at`, `rating_key`, `guids` which include TMDb IDs)
- `get_home_stats` with `stat_id=popular_movies` / `popular_tv` — most-watched by unique users on the server (helps gauge family interest)
- `pms_image_proxy` — serves poster images proxied from Plex without exposing the Plex token; supports resize (`width`/`height` params)

**Poster images**: AppDaemon downloads posters via `pms_image_proxy` to the configurable `poster_media_dir` (default: `{media_fs_root}/media-dashboard/posters/plex-{rating_key}.jpg`), then syncs to `/config/www/` via `shell_command`. Card references `/local/media-dashboard/posters/plex-{rating_key}.jpg`. See [Image Caching](#image-caching) and [Filesystem Paths](#filesystem-paths) for the full flow.

**Filtering**: Tautulli returns *everything* added to Plex, including obscure content. We cross-reference each item's TMDb ID (from Plex `guids` field) against TMDb `popularity` score to filter out non-mainstream releases. Items below a configurable popularity threshold are hidden by default but still fetchable.

**Refresh cadence**: Every 2 hours

### 2. TMDb — "Coming Soon to Streaming" + Theater Metadata

**API**: TMDb v3 REST API (free tier, requires API key signup)

**Endpoints used**:
- `GET /3/movie/upcoming?region=US` — upcoming theatrical releases
- `GET /3/movie/now_playing?region=US` — currently in theaters
- `GET /3/trending/movie/week` — trending movies (good mainstream filter)
- `GET /3/discover/movie` — filtered discovery with `with_release_type=4` (digital), `primary_release_date.gte/lte`, `vote_count.gte=50`, `popularity.gte=10`
- `GET /3/trending/tv/week` — trending TV shows
- Movie/show detail endpoints for synopsis, runtime, genres, ratings

**Poster images**: TMDb image CDN is public — `https://image.tmdb.org/t/p/w342/{poster_path}`. AppDaemon downloads to `{poster_media_dir}/tmdb-{id}.jpg` and syncs to `/config/www/` via `shell_command`. Size `w342` is good for dashboard cards (~342px wide).

**Mainstream filter**: TMDb `popularity` field (float, higher = more mainstream) + `vote_count` threshold. Combined with Plex watch history cross-reference to boost items matching family's taste.

**Refresh cadence**: Every 12 hours

### 3. SerpApi — Theater Showtimes

**API**: SerpApi Google Showtimes (paid plan required; single API key, no per-theater calls)

**Endpoint used**:
- `GET /search?engine=google&q=showtimes+near+{location}&location={location}&api_key={key}` — returns `showtimes_results` structured data grouping movies by theater

**Signup**: SerpApi account required. API key stored as `SERPAPI_KEY` env var.

**Coverage**: Uses Google's live showtime data — covers all major US chains (AMC, Cinemark, Showcase, Regal, etc.).

**Refresh cadence**: Once daily (one search covers all configured theaters)

**Theater filtering**: Response includes many theaters; results are filtered to the configured `theaters` list using case-insensitive substring matching (e.g. configured `"Showcase Cinema de Lux Lowell"` matches API name `"Showcase Cinema de Lux Lowell"`).

**Fallback**: If SerpApi returns no results or fails, TMDb still provides "now playing" and "upcoming" theater data (just no showtimes). The card degrades gracefully — movie posters and metadata still show, just without specific showtime data.

### Configured Theaters (01886 area)

| Theater | Location | Priority |
|---------|----------|----------|
| AMC Tyngsboro 12 | 440 Middlesex Rd, Tyngsborough, MA 01879 | High |
| Showcase Cinema de Lux Lowell | 32 Reiss Ave, Lowell, MA 01851 | High |
| AMC Methuen 20 (The Loop) | 90 Pleasant Valley St, Methuen, MA 01844 | High |
| Cinemark Rockingham Park and XD | 99 Rockingham Park Blvd, Salem, NH 03079 | Medium |
| AMC Burlington Cinema 10 | 20 South Ave, Burlington, MA 01803 | Low |

## Content Filtering & Thumbs Up/Down

### Problem
Plex servers accumulate huge libraries with many obscure releases. TMDb trending lists are broad. The family wants to see what's relevant to *them*, not everything.

### Approach — Layered Filtering

1. **Popularity gate** (automatic): TMDb `popularity` score + `vote_count` threshold. Only items above a configurable threshold appear by default. This removes the long tail of obscure content.

2. **Plex history boost** (automatic): Cross-reference TMDb genre/franchise data against the family's Plex watch history (via `get_home_stats`). Content matching frequently-watched genres gets a boost.

3. **Thumbs up/down** (user-driven): Each poster on the card has a small thumbs-down button (or swipe gesture). Dismissed items are persisted to a JSON file on the shared `/media/` filesystem. Thumbs-up items get pinned to the top. This trains a simple preference model over time.

4. **Genre config** (optional): YAML config can whitelist genres (`["Action", "Animation", "Comedy", "Sci-Fi"]`) to hard-filter. Empty = all genres pass through.

### Storage for User Preferences

Preferences are stored in a **JSON file** on the shared `/media/` filesystem (configurable via `preferences_file`). This follows the pattern used by `immich_fetcher` (`config_file`) and `photo_frame_viewer` (`state_dir`) — file-based persistence for data that can grow unboundedly.

**Not** stored in `input_text` helpers — the 255-character limit would overflow almost immediately with JSON arrays of media IDs. HA helpers are the wrong tool for growing collections.

```json
// {media_fs_root}/media-dashboard/preferences.json
{
  "hidden": ["tmdb-12345", "plex-67890"],
  "liked": ["tmdb-11111"],
  "hidden_at": {"tmdb-12345": "2026-03-29T10:00:00"},
  "liked_at": {"tmdb-11111": "2026-03-28T15:00:00"}
}
```

The app loads this file on startup and writes it back on each `dismiss`/`like`/`undo_dismiss` command. Timestamps enable future cleanup of stale preferences (e.g., items dismissed >90 days ago).

## Card Layout — Compact View

The compact card fits in the Wall Display right column, below the calendar. Horizontal carousel cycles through categories.

```
┌──────────────────────────────────────────────┐
│  MEDIA & MOVIES                     ◀  ▶  ●●│
│──────────────────────────────────────────────│
│                                              │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐     │
│  │ ░░░░░░░ │  │ ░░░░░░░ │  │ ░░░░░░░ │     │
│  │ ░poster░ │  │ ░poster░ │  │ ░poster░ │     │
│  │ ░░░░░░░ │  │ ░░░░░░░ │  │ ░░░░░░░ │     │
│  │ ░░░░░░░ │  │ ░░░░░░░ │  │ ░░░░░░░ │     │
│  ├─────────┤  ├─────────┤  ├─────────┤     │
│  │Title    │  │Title    │  │Title    │     │
│  │PG-13    │  │Added 2d │  │Opens 4/1│     │
│  └─────────┘  └─────────┘  └─────────┘     │
│                                              │
│  🎬 THEATERS │ 📺 PLEX │ 🍿 COMING SOON    │
└──────────────────────────────────────────────┘
```

### Category Tabs

| Tab | Icon | Content | Subtitle format |
|-----|------|---------|-----------------|
| **In Theaters** | Film | Now playing at configured theaters | "PG-13 · 2h 15m" |
| **New on Plex** | TV | Recently added movies & shows | "Added 2 days ago" |
| **Coming Soon** | Popcorn | Upcoming streaming + theatrical releases | "Opens Apr 18" / "Netflix Apr 2" |

**Note**: Merged "Coming to Theaters" and "Coming Soon to Streaming" into one "Coming Soon" tab, sorted by release date. The subtitle distinguishes theatrical vs streaming. This keeps the tab count to 3 for a cleaner compact card.

### Compact Card Behavior

- Shows 3 posters in a horizontal row (fits right-column width)
- Category tabs at bottom switch content
- Left/right arrows (or swipe) to scroll within a category
- Auto-rotate categories every ~15 seconds (pauses on user interaction)
- Tapping a poster opens the detail popup
- Each poster has a subtle dismiss (X) button in the corner for thumbs-down

## Detail Popup

Tapping a poster or the card header opens a larger popup card with all categories.

```
┌───────────────────────────────────────────────────┐
│  MEDIA & MOVIES                              ✕    │
│───────────────────────────────────────────────────│
│                                                   │
│  ┌──────────────────────────────────────────────┐ │
│  │ 🎬 IN THEATERS                               │ │
│  │                                              │ │
│  │  ┌──────┐  ┌──────┐  ┌──────┐  ┌──────┐ ►  │ │
│  │  │poster│  │poster│  │poster│  │poster│    │ │
│  │  │      │  │      │  │      │  │      │    │ │
│  │  ├──────┤  ├──────┤  ├──────┤  ├──────┤    │ │
│  │  │Title │  │Title │  │Title │  │Title │    │ │
│  │  │PG-13 │  │PG-13 │  │R     │  │PG   │    │ │
│  │  │2h15m │  │1h50m │  │2h30m │  │1h45m│    │ │
│  │  │👍 👎 │  │👍 👎 │  │👍 👎 │  │👍 👎│    │ │
│  │  └──────┘  └──────┘  └──────┘  └──────┘    │ │
│  └──────────────────────────────────────────────┘ │
│                                                   │
│  ┌──────────────────────────────────────────────┐ │
│  │ 📺 NEW ON PLEX                               │ │
│  │  (same poster row layout)                    │ │
│  └──────────────────────────────────────────────┘ │
│                                                   │
│  ┌──────────────────────────────────────────────┐ │
│  │ 🍿 COMING SOON                               │ │
│  │  (same poster row layout)                    │ │
│  └──────────────────────────────────────────────┘ │
│                                                   │
│  Last updated: 6:00 PM · Refresh ↻              │
└───────────────────────────────────────────────────┘
```

### Poster Tap → Movie Detail Inline Expand

Tapping a specific poster in the popup expands an inline detail section below that category:

```
┌──────────────────────────────────────────────┐
│  ◄ Back to list                              │
│                                              │
│  ┌───────────┐  Thunderbolts*                │
│  │           │  ★ 7.8  ·  PG-13  ·  2h 7m   │
│  │  poster   │  Action, Adventure            │
│  │  (large)  │                               │
│  │           │  In theaters now               │
│  │           │  👍  👎                        │
│  └───────────┘                               │
│                                              │
│  A team of antiheroes is recruited by        │
│  Valentina Allegra de Fontaine to go on a    │
│  dangerous mission...                        │
│                                              │
│  ─── SHOWTIMES — Sat, Mar 29 ───            │
│                                              │
│  AMC Tyngsboro 12                            │
│    1:30 PM  4:15 PM  7:00 PM  9:45 PM       │
│                                              │
│  Showcase Lowell                             │
│    1:00 PM  3:45 PM  6:30 PM  9:15 PM       │
│                                              │
│  AMC Methuen 20 (The Loop)                   │
│    2:00 PM  5:30 PM  8:15 PM  10:30 PM      │
│                                              │
│  Cinemark Salem XD                           │
│    1:15 PM  4:00 PM  7:30 PM                 │
└──────────────────────────────────────────────┘
```

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  media_dashboard_app (AppDaemon)                    │
│                                                     │
│  ┌───────────────┐ ┌──────────────┐ ┌────────────┐ │
│  │ tautulli_     │ │ tmdb_        │ │ showtime_  │ │
│  │ fetcher.py    │ │ fetcher.py   │ │ fetcher.py │ │
│  │               │ │              │ │            │ │
│  │ get_recently_ │ │ now_playing  │ │ SerpApi    │ │
│  │ added         │ │ upcoming     │ │ showtimes/ │ │
│  │ get_home_stats│ │ trending     │ │ theater    │ │
│  │ pms_image_    │ │ discover     │ │            │ │
│  │ proxy (imgs)  │ │ image CDN    │ │            │ │
│  └───────┬───────┘ └──────┬───────┘ └─────┬──────┘ │
│          │                │               │         │
│          └────────┬───────┘───────────────┘         │
│                   ▼                                  │
│         ┌─────────────────────┐                     │
│         │ Poster cache        │                     │
│         │ /media/media-       │                     │
│         │ dashboard/posters/  │                     │
│         └────────┬────────────┘                     │
│                  │ shell_command sync                │
│                  ▼                                   │
│         /config/www/media-dashboard/posters/         │
│                                                     │
│         ┌─────────────────────┐                     │
│         │ sensor.media_       │                     │
│         │ dashboard_status    │                     │
│         │ (set_state)         │                     │
│         └────────┬────────────┘                     │
└──────────────────┼──────────────────────────────────┘
                   │ HA WebSocket
                   ▼
┌─────────────────────────────────────────────────────┐
│  Lovelace Cards                                     │
│  - media-dashboard-card.js (compact view)           │
│  - media-dashboard-detail-card.js (popup)           │
│                                                     │
│  Reads: sensor attributes (metadata + local URLs)   │
│  Sends: commands via script.media_dashboard_relay    │
└─────────────────────────────────────────────────────┘
```

### AppDaemon App: `media_dashboard_app`

```
appdaemon/apps/media_dashboard_app/
├── __init__.py
├── media_dashboard_app.py      # Main app — orchestrates fetchers, publishes sensor
├── cards/
│   ├── media-dashboard-card.js
│   └── media-dashboard-detail-card.js
└── README.md

appdaemon/providers/media_providers/
├── __init__.py
├── tautulli_fetcher.py         # Plex recently added + popular, poster download
├── tmdb_fetcher.py             # TMDb now playing, upcoming, trending, poster download
├── serpapi_client.py           # HTTP client for SerpApi Google Showtimes
└── serpapi_fetcher.py          # SerpApi showtimes for configured theaters
```

**Fetcher pattern**: Each fetcher is a standalone module (not an AppDaemon app) under `providers/media_providers/`. The main app imports and calls them. This follows the S2 security rule (all external HTTP in `providers/`) and matches the `photo_providers/` pattern for Immich. Fetchers are reusable if other apps need media data in the future.

### Sensor Schema (sensor.media_dashboard_status)

To stay under HA's ~16KB WebSocket limit, we keep items lean — metadata only, poster URLs are short local paths, and we cap items per category.

```json
{
  "state": "ok",
  "attributes": {
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
      "plex_movies": {
        "label": "Plex Movies",
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
      "plex_shows": {
        "label": "Plex Shows",
        "items": [
          {
            "id": "plex-67890",
            "title": "Show Title",
            "year": 2026,
            "poster": "/local/media-dashboard/posters/plex-67890.jpg",
            "type": "show",
            "subtitle": "Added 1 day ago",
            "genres": "Drama, Thriller"
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
}
```

**Size budget**: ~10 items per category × 3 categories × ~200 bytes per item = ~6KB. Showtimes are read from the local cache (not stored in the main sensor) — see [Showtime Caching Contract](#showtime-caching-contract).

### Relay Commands

| Command | Payload | Description |
|---------|---------|-------------|
| `refresh` | `{"source": "all"\|"tautulli"\|"tmdb"\|"showtimes"}` | Force refresh from one or all sources |
| `get_detail` | `{"id": "tmdb-11111"}` | Fetch full detail + showtimes for a movie; publishes to a separate detail sensor |
| `dismiss` | `{"id": "tmdb-11111"}` | Hide an item (thumbs down) |
| `like` | `{"id": "tmdb-11111"}` | Boost an item (thumbs up) |
| `undo_dismiss` | `{"id": "tmdb-11111"}` | Un-hide a previously dismissed item |

### Detail Sensor (sensor.media_dashboard_detail)

When user taps a poster, the card sends `get_detail` via relay. The app reads the item's full metadata and cached showtimes from disk (no API call), then publishes to a second sensor:

```json
{
  "state": "tmdb-11111",
  "attributes": {
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
}
```

**Why a second sensor instead of one sensor?** The repo generally prefers one primary sensor plus relay commands. The detail sensor is justified here because:
1. **Frontend simplicity**: The detail card subscribes to `sensor.media_dashboard_detail` and re-renders on state change. Without it, the detail card would need to parse the main sensor's nested categories to find the selected item, and there's no clean way for the card to know *which* item the user selected without a dedicated state channel.
2. **Payload isolation**: Showtimes for 5 theaters + synopsis add ~1-2KB per movie. Embedding this for all ~30 items in the main sensor would push toward the 16KB limit. Serving it for one movie at a time keeps both sensors well under the limit.
3. **The `state` field acts as a selection signal**: The detail sensor's `state` is the selected item ID, giving the card a simple "is something selected?" check.

### App Configuration (apps-prod.yaml)

```yaml
media_dashboard_app:
  module: media_dashboard_app.media_dashboard_app
  class: MediaDashboardApp
  disable: true
  # Tautulli
  tautulli_url: !secret tautulli_url
  tautulli_api_key_env: TAUTULLI_API_KEY
  # TMDb
  tmdb_api_key_env: TMDB_API_KEY
  # SerpApi — Google Showtimes
  serpapi_api_key_env: SERPAPI_KEY
  location: "Westford, MA"
  theaters:
    - name: "AMC Tyngsboro 12"
    - name: "Showcase Cinema de Lux Lowell"
    - name: "AMC Methuen 20"
    - name: "Cinemark Rockingham Park and XD"
    - name: "AMC Burlington Cinema 10"
  # Refresh intervals (seconds)
  plex_refresh_interval: 7200       # 2 hours
  tmdb_refresh_interval: 43200      # 12 hours
  showtimes_refresh_interval: 86400 # 24 hours
  # Content filters
  max_items_per_category: 10
  popularity_threshold: 10.0        # TMDb popularity minimum
  vote_count_threshold: 50          # TMDb vote count minimum
  genre_filter: []                  # Empty = all genres
  # Filesystem (configurable; dev/prod may differ)
  media_fs_root_env: MEDIA_FS_ROOT  # Maps to /media in prod, varies in dev
  poster_media_subdir: media-dashboard/posters
  poster_www_subdir: media-dashboard/posters
  preferences_file_subdir: media-dashboard/preferences.json
  showtime_cache_subdir: media-dashboard/showtime-cache.json
  poster_width: 342                 # TMDb w342 size
  # Shell commands (must exist in configuration.yaml — NOT auto-provisioned)
  poster_sync_shell_command: media_dashboard_sync_posters
  # HA
  ha_url: !secret ha_url
  ha_token_env: HA_TOKEN
```

## Self-Provisioned Entities

| Entity | Type | Purpose |
|--------|------|---------|
| `sensor.media_dashboard_status` | sensor (set_state) | Main data sensor — category items with local poster URLs |
| `sensor.media_dashboard_detail` | sensor (set_state) | On-demand detail for selected movie (showtimes, synopsis) |
| `script.media_dashboard_relay` | script | Relay card commands to AppDaemon events |

**Not self-provisioned** (manual HA steps):
- `shell_command.media_dashboard_sync_posters` — must be added to `configuration.yaml` (see [Shell Command](#shell-command-manual-ha-setup))
- Lovelace card JS files — must be copied to `/config/www/` and registered as Lovelace resources

**Not stored in HA helpers**: User preferences (hidden/liked) are persisted in a JSON file on `/media/` — see [Storage for User Preferences](#storage-for-user-preferences).

## Filesystem Paths

AppDaemon and HA run in separate Kubernetes pods. AppDaemon cannot write to `/config/www/` directly — it writes to the shared `/media/` mount, then calls a `shell_command` service (which runs inside HA's pod) to copy files to `/config/www/`.

The actual mount root varies between dev and prod. Following the `detection_summary_app` pattern, all paths are derived from a configurable `media_fs_root` (resolved from `media_fs_root_env`, defaulting to `/media`):

| Logical path | AppDaemon sees | HA sees | Config key |
|---|---|---|---|
| Poster cache (working) | `{media_fs_root}/media-dashboard/posters/` | `/media/media-dashboard/posters/` | `poster_media_subdir` |
| Poster cache (served) | N/A (HA-only) | `/config/www/media-dashboard/posters/` | `poster_www_subdir` |
| Preferences file | `{media_fs_root}/media-dashboard/preferences.json` | `/media/media-dashboard/preferences.json` | `preferences_file_subdir` |
| Showtime cache | `{media_fs_root}/media-dashboard/showtime-cache.json` | `/media/media-dashboard/showtime-cache.json` | `showtime_cache_subdir` |

Cards reference posters as `/local/media-dashboard/posters/{source}-{id}.jpg` (HA serves `/config/www/` as `/local/`).

## Image Caching

### Flow

1. **Fetchers download posters** to `{media_fs_root}/{poster_media_subdir}/`
   - Plex content: via Tautulli `pms_image_proxy` (hides Plex token)
   - TMDb content: via TMDb image CDN (`image.tmdb.org/t/p/w342/...`)
   - Filename convention: `{source}-{id}.jpg` (e.g., `plex-54321.jpg`, `tmdb-11111.jpg`)

2. **App calls `shell_command/{poster_sync_shell_command}`** to sync `/media/{poster_media_subdir}/` → `/config/www/{poster_www_subdir}/`
   - The shell command is **manual** — must exist in `configuration.yaml` before the app runs
   - Atomic copy pattern (matches existing detection_summary/photo_frame patterns)
   - Removes stale posters no longer in the source directory

3. **Cards reference** `/local/{poster_www_subdir}/{source}-{id}.jpg`

4. **Cleanup**: On each refresh cycle, the app removes posters from the media dir that are no longer in any active category, then re-syncs.

### Shell Command (manual HA setup)

Must be added to `configuration.yaml` and HA restarted. This is **not** auto-provisioned — the provisioner only handles scripts and helpers. Follows the same pattern as `photo_frame_stage_gen`, `ds_refresh_detection_summary_viewer_www`, and `dashboard_notify_stage`.

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

## Showtime Caching Contract

Showtimes are **batch-fetched daily and cached on disk**, never fetched on-demand per user interaction. This keeps the caching boundary explicit and API usage predictable.

### Flow

1. **Daily batch fetch** (via `showtimes_refresh_interval`, default 24h):
   - SerpApi fetcher searches ``"showtimes near {location}"`` — one request covers all configured theaters
   - Results are filtered to configured theater names (case-insensitive substring match)
   - Cached to `{media_fs_root}/{showtime_cache_subdir}` as a JSON file keyed by lowercase film title

2. **`get_detail` relay command** (user taps a poster):
   - App reads the item's full metadata from the in-memory fetcher cache
   - App reads showtimes from the **disk cache** — no SerpApi call
   - Publishes combined result to `sensor.media_dashboard_detail`

3. **Staleness handling**:
   - If showtime cache is >24h old and refresh fails, show stale data with a "Showtimes from yesterday" note
   - If showtime cache is >48h old, omit showtimes entirely — show "Showtimes unavailable" in the detail view
   - `has_showtimes` field in the main sensor reflects whether current-day data exists

### Why not on-demand?
- SerpApi charges per search request — on-demand fetches for each poster tap would be costly
- One daily search covers all configured theaters (single API call per day)
- Cached showtimes don't change intra-day, so staleness is not a concern within the same day

## Failure Modes

On partial upstream failure, the app **retains last-known-good data** per category and marks freshness in `fetch_status`. This matches the pattern used by `school_lunch_app` (retains per-school data on failure) and `detection_summary_app` (retains usable state while marking freshness).

| Failure scenario | Behavior |
|---|---|
| Tautulli unreachable | Keep existing `plex_movies` and `plex_shows` items. Set `fetch_status.tautulli.status = "error"`. Log warning. |
| TMDb unreachable | Keep existing `in_theaters` and `coming_soon` items. Set `fetch_status.tmdb.status = "error"`. |
| SerpApi unreachable | Keep existing showtime cache on disk. Set `fetch_status.serpapi = "error"`. Detail view shows stale showtimes with note. |
| Tautulli returns empty | Clear `plex_movies` and `plex_shows` items (genuinely empty library is valid). Set status to `"ok"`. |
| TMDb returns empty | Clear items for affected category. Set status to `"ok"`. |
| SerpApi returns no configured theaters | Set `has_showtimes = false` for all in-theater movies. |
| All sources fail simultaneously | All categories retain last-known-good data. All `fetch_status` entries show `"error"`. |
| Stale data TTL | Items older than `stale_ttl` (configurable, default 7 days) are evicted even if refresh keeps failing. Prevents showing week-old "now playing" data. |

The card reads `fetch_status` to show a subtle warning indicator (e.g., dimmed refresh icon) when any source is in error state, without disrupting the content display.

## Testing Surface

Since fetchers live in `providers/media_providers/`, they must be testable independently of AppDaemon. The following behaviors must have unit test coverage:

### Provider tests (`tests/test_media_providers/`)

| Behavior | What to test |
|---|---|
| **Tautulli fetcher** | Parse `get_recently_added` response → normalized item list. Handle empty response. Handle malformed response. Poster URL construction from `rating_key`. |
| **TMDb fetcher** | Parse `now_playing`, `upcoming`, `trending` responses → normalized items. Popularity/vote_count filtering (items below threshold excluded). Genre filtering. Poster URL construction from `poster_path`. |
| **SerpApi fetcher** | Parse `showtimes_results` response → normalized showtime map. Filter to configured theaters. Handle empty results. Handle missing film titles. |
| **Poster download** | Download to correct path. Skip if file already exists and is recent. Cleanup of stale posters. |
| **Cross-source ID matching** | Plex `guids` field parsed to extract TMDb IDs. TMDb popularity lookup for Plex items. |

### App tests (`tests/test_media_dashboard_app/`)

| Behavior | What to test |
|---|---|
| **Ranking/filtering** | Items sorted by popularity within each category. Hidden items excluded. Liked items boosted to top. Genre whitelist applied. |
| **Preference persistence** | `dismiss` writes to JSON file. `like` writes to JSON file. `undo_dismiss` removes from hidden list. File survives app restart. |
| **Poster cache cleanup** | Posters not in active set are removed from media dir. Shell command called after cleanup. |
| **Partial-source failure** | Tautulli failure retains `plex_movies` + `plex_shows` items. TMDb failure retains `in_theaters` + `coming_soon`. `fetch_status` updated correctly. |
| **Stale data TTL** | Items older than `stale_ttl` evicted. Fresh items retained. |
| **Relay command handling** | `refresh` triggers correct fetcher(s). `get_detail` reads from cache and publishes detail sensor. `dismiss`/`like`/`undo_dismiss` update preferences and re-publish main sensor. |
| **Showtime cache** | Daily fetch writes cache file. `get_detail` reads from cache (no API call). Stale cache >24h shows warning. Stale cache >48h omits showtimes. |

### Integration tests (`tests/integration-tests/`) — env-gated

| Test | Gate |
|---|---|
| Live Tautulli fetch | `RUN_TAUTULLI_TESTS=1` |
| Live TMDb fetch | `RUN_TMDB_TESTS=1` |
| Live SerpApi fetch | `RUN_SERPAPI_TESTS=1` |

## Resolved Decisions

| Question | Decision | Rationale |
|----------|----------|-----------|
| Showtimes API | **SerpApi Google Showtimes** | Replaced MovieGlu (which required RapidAPI, was brittle). SerpApi uses Google's live data — covers all major US chains. One daily search covers all configured theaters. |
| Content filtering | **Layered: popularity gate + Plex history + thumbs up/down** | Automatic mainstream filter via TMDb popularity, user refinement via thumbs up/down persisted to JSON file on `/media/` |
| Poster hosting | **Local cache via /media → /config/www sync** | Matches existing repo patterns (photo_frame, detection_summary). Works offline. Tautulli proxies Plex images; TMDb CDN for non-Plex. |
| Sensor size | **Split: main sensor (lean) + detail sensor (on-demand)** | Main sensor stays under 16KB. Detail sensor provides clean selection signal + showtimes for frontend simplicity. Justified in detail sensor section. |
| Theater location | **Location string + theater names in config** | SerpApi takes a human-readable location string (e.g. "Westford, MA"). Theater names are filtered client-side by substring match — no cinema ID discovery needed. |
| Fetcher placement | **`providers/media_providers/`** | Follows S2 rule (all external HTTP in providers/). Matches `photo_providers/` pattern. |
| Preference storage | **JSON file on `/media/`** | `input_text` 255-char limit would overflow immediately. File-based persistence matches `immich_fetcher` (`config_file`) and `photo_frame_viewer` (`state_dir`) patterns. |
| Showtime caching | **Daily batch fetch, cached to disk, read from cache on `get_detail`** | Explicit caching boundary. 5 API calls/day for 5 theaters. No on-demand fetches — predictable API usage, fast detail view. |
| Failure handling | **Retain last-known-good per category, mark `fetch_status`** | Matches `school_lunch_app` and `detection_summary` patterns. Stale TTL (7d) prevents showing very old data. |
| Shell commands | **Manual `configuration.yaml`** | Provisioner only handles scripts + helpers. Shell commands are manual in this repo (same as photo_frame, detection_summary, dashboard_notify). |
| Filesystem paths | **Configurable via `media_fs_root_env` + subdirectory keys** | Dev/prod mount roots differ. Matches `detection_summary_app`'s `media_fs_root_env` pattern. |

## Non-Goals (v1)

- Trailer playback within the card
- Plex playback controls
- Ticket purchasing / deep links to Fandango
- TV episode-level tracking (show-level only)
- Sonarr/Radarr integration
- AI-powered recommendations (future enhancement — v1 uses popularity + manual thumbs)

## Dashboard Placement

- **Column**: Right column (column 3)
- **Position**: Below the Upcoming Events calendar
- **Width**: Same as calendar card
- **Height**: Compact view ~200-250px (3 posters + header + tabs)
