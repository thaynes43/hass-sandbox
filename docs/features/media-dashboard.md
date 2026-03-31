# Media Dashboard

A wall-display card that aggregates media content from four sources into a single glanceable view with poster art, external ratings, local theater showtimes, and a preference system.

## Overview

The media dashboard shows what's worth watching right now across four categories:

- **In Theaters** -- movies currently playing at local cinemas, ranked by quality and popularity, with showtimes for nearby theaters
- **Plex Movies** -- recently added movies on the home Plex server, ranked by runtime and rating
- **Plex Shows** -- recently added TV shows on the home Plex server, quality scored using a 50-minute episode runtime normalization instead of the 120-minute movie baseline
- **Coming Soon** -- upcoming theatrical and streaming releases

Users can like or dismiss items. Liked items float to the top; dismissed items move to a collapsible "Hidden" section with a restore button. All preferences persist across restarts.

## Cards

### Compact Card

The compact card sits on the wall display alongside other dashboard cards. It shows 3 posters at a time with category tabs at the bottom. Arrows paginate by 3 posters. The card auto-rotates categories every 15 seconds when not being interacted with.

### Detail Popup

Tapping the compact card opens the detail popup with all four categories as horizontally scrollable poster rows. Each row has scroll arrows for quick navigation. Tapping a poster loads its full detail panel with:

- Ratings row with Kometa-style icons: TMDb, IMDb, Rotten Tomatoes critics (tomato), and RT audience (popcorn) -- fresh/rotten icon variants based on 60% threshold
- MPAA certification, runtime, and genre names
- Director, theatrical release date, and worldwide box office revenue
- Movie tagline in italics
- Synopsis
- Like/unlike toggle and dismiss button
- Collapsible 7-day showtime schedule grouped by day, with theater summaries

Dismissed items appear in a "Hidden (N)" toggle below each category's poster row. Expanding it shows dimmed posters with a restore button.

## Data Sources

| Source | What it provides | Refresh cadence |
|--------|-----------------|-----------------|
| **Tautulli** | Recently added Plex movies and shows, poster images via `pms_image_proxy` | Every 2 hours |
| **TMDb** | Now-playing and upcoming movies, popularity/vote metadata, poster CDN, director/certification/revenue via detail endpoint | Every 12 hours |
| **SerpApi** | Theater showtimes via Google search for configured local cinemas | Once daily |
| **MDbList** | External ratings aggregator -- IMDb, Rotten Tomatoes (critics + audience), Metacritic; enriches items during each refresh | On each refresh |

## Architecture

The system uses the standard AppDaemon relay script pattern for card-to-app communication:

```
Lovelace Cards (JS)
  │ callService("script", "media_dashboard_relay", { command, payload })
  ▼
Home Assistant (relay script fires event)
  │ media_dashboard_command event
  ▼
media_dashboard_app (AppDaemon)
  │ Orchestrates fetchers, publishes sensors
  ▼
sensor.media_dashboard_status  ──▶  Cards read attributes
sensor.media_dashboard_detail  ──▶  Detail panel reads on demand
```

Poster images are downloaded to `/media/` and synced to `/config/www/` via a shell command, so HA can serve them as `/local/` URLs.

## Ranking

All items are scored using a unified heuristic combining three factors:

| Factor | Weight | Measures |
|--------|--------|----------|
| Quality | 45% | TMDb score (0-10) or runtime as a proxy for Plex items (TV shows use 50min episode normalization; movies use 120min) |
| Recency | 25% | How recently released or added (newer = higher) |
| Prominence | 30% | TMDb popularity (log-scale), vote count, genre presence |

Liked items are boosted to the top of their category after scoring.

## Related

- App README: `appdaemon/apps/media_dashboard_app/README.md`
- Card files: `appdaemon/apps/media_dashboard_app/cards/`
- Providers: `appdaemon/providers/media_providers/`
