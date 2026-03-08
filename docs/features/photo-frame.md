# Immich Photo Frame

A wall-mounted photo slideshow powered by self-hosted Immich photos, displayed on a custom Lovelace card.

<!-- TODO: Add screenshot of wall display showing photo frame -->

## Overview

Wall-mounted displays throughout the house cycle through personal photos sourced from a self-hosted [Immich](https://immich.app/) photo library. The system is designed for reliability — no broken images, even during photo refreshes.

Two AppDaemon apps work together:

### Immich Fetcher

The `immich_fetcher` app periodically queries the Immich API and downloads a batch of photos to the shared `/media/` NFS mount. It supports filtering by album, date range, and other criteria.

### Photo Frame Viewer

The `photo_frame_viewer` app manages the slideshow lifecycle:

1. **Poll** — watches the fetcher's output directory for new photos
2. **Stage** — atomically copies photos to a versioned generation directory via HA `shell_command`
3. **Display** — publishes the current image URL on an HA sensor that the dashboard card reads
4. **Swap** — on the next slide advance, switches to the new generation and cleans up the old one

This generation-swap pattern means a paused slideshow keeps its current image visible indefinitely, even after multiple batch refreshes from Immich.

## Architecture

```
Immich Server
  │
  ▼
immich_fetcher (AppDaemon)
  │  writes to /media/immich-photos/
  ▼
photo_frame_viewer (AppDaemon)
  │  stages to /config/www/photo-frame/live/<gen>/
  │  via HA shell_command
  ▼
sensor.wall_display_photo_frame_status
  │  image_url + cache_bust attributes
  ▼
Custom Lovelace card (wall display dashboard)
```

## Dashboard card

The viewer card uses a relay script for pause/next/previous controls — works on non-admin wall display accounts. Settings are managed through a companion settings popup card.

| Card | Path |
|------|------|
| Viewer + controls | `home-assistant/cards/global/photo-frame-viewer/wall-display-photo-frame-viewer.yaml` |
| Settings popup | `home-assistant/cards/global/photo-frame-viewer/wall-display-photo-frame-settings.yaml` |

## Configuration

<!-- TODO: Add config walkthrough -->

See the app READMEs for full configuration:

- `appdaemon/apps/immich_fetcher/README.md`
- `appdaemon/apps/photo_frame_viewer/README.md`
