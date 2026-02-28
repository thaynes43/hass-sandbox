# Wall Display Photo Frame Viewer

AppDaemon app + Lovelace dashboard card for a photo slideshow on a wall-mounted display. Photos are sourced from Immich via an external fetcher and displayed without ever showing a broken image, even when the fetcher refreshes the batch.

## How it works

```
Immich fetcher                  AppDaemon                         Dashboard
 (writes to NFS)            (PhotoFrameViewerApp)            (markdown <img>)
       |                           |                               |
       |  /config/www/             |                               |
       |  immich-album/            |                               |
       |  ├── IMG_001.jpg   poll   |                               |
       |  └── IMG_002.jpg -------> | fingerprint changed?          |
       |                           |   yes -> call shell_command   |
       |                           |          photo_frame_stage_gen|
       |                           |          (copy to gen dir)    |
       |                           |                               |
       |                           |  /config/www/photo-frame/     |
       |                           |  live/2/                      |
       |                           |  ├── IMG_001.jpg              |
       |                           |  └── IMG_002.jpg              |
       |                           |                               |
       |                           | update input_select options   |
       |                           | (picker shows new filenames)  |
       |                           |                               |
       |                           | on next advance (tick/nav):   |
       |                           |   set input_text URL -------> | displays image
       |                           |   cleanup old gen dir         |
```

### Generation swap

The external Immich fetcher periodically deletes and replaces files in `/config/www/immich-album/`. If the dashboard pointed at those files directly, it would show broken images during a refresh.

Instead, AppDaemon copies each batch into a versioned **generation directory** (`/config/www/photo-frame/live/<gen_id>/`) via an HA `shell_command`. The dashboard URL always points at a gen directory that is guaranteed to exist. When a new batch arrives:

1. A new gen directory is created atomically (copy to temp, then `mv`).
2. The `input_select` picker updates to reflect the new filenames.
3. The currently displayed image URL is **not changed** yet (pinned to old gen).
4. On the next slideshow advance (or manual nav), the URL moves to the new gen.
5. Only then is the old gen directory deleted.

This means a paused slideshow keeps its current image visible indefinitely, even after multiple batch refreshes.

## Prerequisites

### 1. Filesystem directory

Create the live root on the HA host (one-time):

```bash
mkdir -p /config/www/photo-frame/live
```

### 2. Shell commands

Add to HA `configuration.yaml` under `shell_command:`:

```yaml
shell_command:
  photo_frame_stage_gen: >-
    /bin/sh -c 'set -e;
    live_root="/config/www/photo-frame/live";
    src="{{ source_dir }}";
    gen="{{ gen_id }}";
    dest="$live_root/$gen";
    tmp="$live_root/.staging-$gen";
    lock="$live_root/.stage.lock";

    [ -n "$gen" ] || { echo "gen_id empty"; exit 2; };
    [ -d "$live_root" ] || mkdir -p "$live_root";

    exec 9>"$lock";
    if ! flock -n 9; then
      echo "photo_frame_stage_gen: lock busy"; exit 3;
    fi

    find "$live_root" -maxdepth 1 -type d -name ".staging-*" -mmin +60 -exec rm -rf {} \; 2>/dev/null || true;

    rm -rf "$tmp" "$dest";
    mkdir -p "$tmp";

    if [ -d "$src" ] && [ -n "$(ls -A "$src" 2>/dev/null)" ]; then
      cp -a "$src"/. "$tmp"/;
      mv "$tmp" "$dest";
    else
      rm -rf "$tmp";
      exit 1;
    fi'

  photo_frame_cleanup_gen: >-
    /bin/sh -c 'set -e;
    target="/config/www/photo-frame/live/{{ gen_id }}";
    if [ -d "$target" ]; then rm -rf "$target"; fi'
```

Restart Home Assistant to register the new shell commands.

### 3. Helpers (entity IDs)

Create these via the HA UI (or they may already exist):

| Entity ID | Type | Purpose |
|---|---|---|
| `input_select.wall_display_photo_frame_image` | input_select | Image picker (populated by AppDaemon) |
| `input_boolean.wall_display_photo_frame_paused` | input_boolean | Pause/resume slideshow |
| `input_number.wall_display_photo_frame_interval_seconds` | input_number | Seconds between slides |
| `input_text.wall_display_photo_frame_cache_bust` | input_text | Cache-bust token (managed by AppDaemon) |
| `input_text.wall_display_photo_frame_image_local_url` | input_text | Currently displayed image URL (managed by AppDaemon) |

### 4. Fallback image

Ensure `/config/www/immich-album/no-image.jpg` exists as a fallback when no photos are available.

## Dashboard cards

Card YAML snippets (copy-paste into the Lovelace manual editor):

- **Viewer + controls**: `home-assistant/cards/global/photo-frame-viewer/wall-display-photo-frame-viewer.yaml`
- **Settings popup**: `home-assistant/cards/global/photo-frame-viewer/wall-display-photo-frame-settings.yaml`

The viewer card displays:

```yaml
type: markdown
content: >-
  <img style="width: 100%; height: auto;" src="{{
  states('input_text.wall_display_photo_frame_image_local_url') }}?cb={{
  states('input_text.wall_display_photo_frame_cache_bust') }}" />
```

The URL is path-agnostic -- it works regardless of which gen directory is active.

## AppDaemon config

In `appdaemon/apps/apps.yaml` (or `apps-dev.yaml` for local dev):

```yaml
photo_frame_viewer_wall_display:
  module: photo_frame_viewer.photo_frame_viewer_app
  class: PhotoFrameViewerApp
  source_sensor_entity_id: sensor.immich_album
  source_dir: /config/www/immich-album
  ha_local_url_base: /local/photo-frame/live
  stage_shell_command: photo_frame_stage_gen
  cleanup_shell_command: photo_frame_cleanup_gen
  source_poll_interval_s: 30
  stage_settle_delay_s: 3
  picker_entity_id: input_select.wall_display_photo_frame_image
  paused_entity_id: input_boolean.wall_display_photo_frame_paused
  interval_entity_id: input_number.wall_display_photo_frame_interval_seconds
  cache_bust_entity_id: input_text.wall_display_photo_frame_cache_bust
  image_local_url_entity_id: input_text.wall_display_photo_frame_image_local_url
  fallback_image_path: /config/www/immich-album/no-image.jpg
  options_max: 50
  refresh_options_every_s: 60
  auto_cycle: true
```

### Key config parameters

- `source_dir`: The NFS directory the Immich fetcher writes to (read-only from AppDaemon's perspective).
- `ha_local_url_base`: URL prefix for gen directories (maps to `/config/www/photo-frame/live/`).
- `stage_shell_command` / `cleanup_shell_command`: Names of the HA shell commands.
- `source_poll_interval_s`: How often to check for source changes (seconds).
- `stage_settle_delay_s`: Delay after calling the stage shell_command before marking the gen ready.

## External service: Immich fetcher

The Immich fetcher (`hass-immich-addon` repo) runs as a separate Kubernetes pod. It periodically queries the Immich API and writes photos to `/config/www/immich-album/` via a shared NFS mount between the addon pod and HA's pod.

The `sensor.immich_album` entity (from the HA `folder_watcher` or `folder` integration) exposes `attributes.file_list` with the current file paths. AppDaemon polls this attribute to detect changes.

> **Tech debt**: This NFS mount on `/config/www/` predates the `/media` convention. See `docs/image-view-roadmap.md` for the planned migration to `/media/immich-album/`.
