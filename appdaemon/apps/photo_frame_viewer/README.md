# Photo Frame Viewer

Displays rotating photos from a source directory on a Lovelace dashboard card with slideshow controls: pause/resume, interval adjustment, and manual navigation.

## How it works

1. On startup, provisions a relay script, an image picker (`input_select`), and a status sensor.
2. Polls a source directory (typically `immich_fetcher` output) for image files.
3. Stages images for serving via HA shell commands that atomically swap content into `/config/www/photo-frame/live/`.
4. Cycles through images on a configurable interval, publishing the current image URL via the status sensor.
5. Dashboard card reads the sensor for the current image URL with cache-busting.

## Dependencies

- `photo_frame_viewer.gen_helpers` — URL generation, fingerprinting, label building
- `providers.ha_provisioner.HAProvisioner` — HA entity provisioning
- `providers.secrets.resolve_secret()` — credential resolution

## Upstream dependencies

- `immich_fetcher` — provides the source photos in `source_dir`

## Self-provisioned entities

| Entity | Type | Purpose |
|--------|------|---------|
| `input_select.{prefix}_photo_frame_image` | Input Select | Image picker dropdown |
| `sensor.{prefix}_photo_frame_status` | Virtual sensor | State (paused/playing), image URL, interval |
| `script.{prefix}_photo_frame_relay` | Script | Card-to-AppDaemon relay |

Where `{prefix}` defaults to `wall_display` (configurable via `entity_prefix`).

## Associated card

`photo-frame-viewer-card.js` — Lovelace card for pause, interval slider, next/previous navigation.

## Config (apps.yaml)

### Required

```yaml
photo_frame_viewer_wall_display:
  module: photo_frame_viewer.photo_frame_viewer_app
  class: PhotoFrameViewerApp
  ha_url_env: HA_URL
  ha_token_env: TOKEN
  stage_shell_command: photo_frame_stage_gen
  cleanup_shell_command: photo_frame_cleanup_gen
```

### Optional (with defaults)

| Key | Default | Description |
|-----|---------|-------------|
| `source_dir` | `/media/immich-photos` | Directory to scan for photos |
| `ha_source_dir` | (same as source_dir) | HA-side path if filesystem differs |
| `ha_local_url_base` | `/local/photo-frame/live` | Base URL for served images |
| `source_poll_interval_s` | `30` | Poll disk for source changes |
| `stage_settle_delay_s` | `3` | Wait after shell command before reading staged files |
| `fallback_image_path` | `/config/www/immich-album/no-image.jpg` | Fallback when source is empty |
| `options_max` | `100` | Max options in picker |
| `auto_cycle` | `true` | Auto-advance images |
| `reset_timer_on_manual_nav` | `true` | Restart timer on manual selection |
| `default_interval_s` | `10` | Slideshow interval in seconds |
| `entity_prefix` | `wall_display` | Prefix for all provisioned entity IDs |
| `state_dir` | `/media/photo-frame-viewer/{prefix}` | Persisted state directory |

## Manual setup required

These cannot be auto-provisioned and must be configured manually:

### Shell commands (`configuration.yaml`)

```yaml
shell_command:
  photo_frame_stage_gen: >-
    /bin/sh -c '...'   # Stages images into /config/www/photo-frame/live/
  photo_frame_cleanup_gen: >-
    /bin/sh -c '...'   # Cleans old generations
```

### Directory structure

`/config/www/photo-frame/live/` must exist inside the HA container (created by shell commands on first run).

### Lovelace resource

Register the card JS file as a Lovelace resource with cache-busting `?v=N` query param.
