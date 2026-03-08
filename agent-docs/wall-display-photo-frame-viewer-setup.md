# Wall Display Photo Frame Viewer

AppDaemon app + Lovelace dashboard card for a photo slideshow on a
wall-mounted display.  Photos are sourced from Immich via an external fetcher
and displayed without ever showing a broken image, even when the fetcher
refreshes the batch.

## How it works

```
Immich fetcher                  AppDaemon                         Dashboard
 (writes to NFS)            (PhotoFrameViewerApp)            (markdown <img>)
       |                           |                               |
       |  /media/immich-photos/    |                               |
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
       |                           |   publish sensor URL -------> | displays image
       |                           |   cleanup old gen dir         |
```

### Generation swap

The external Immich fetcher periodically deletes and replaces files.  If the
dashboard pointed at those files directly, it would show broken images during
a refresh.

Instead, AppDaemon copies each batch into a versioned **generation directory**
(`/config/www/photo-frame/live/<gen_id>/`) via an HA `shell_command`.  The
dashboard URL always points at a gen directory that is guaranteed to exist.
When a new batch arrives:

1. A new gen directory is created atomically (copy to temp, then `mv`).
2. The `input_select` picker updates to reflect the new filenames.
3. The currently displayed image URL is **not changed** yet (pinned to old gen).
4. On the next slideshow advance (or manual nav), the URL moves to the new gen.
5. Only then is the old gen directory deleted.

This means a paused slideshow keeps its current image visible indefinitely,
even after multiple batch refreshes.

### Self-provisioning

`PhotoFrameViewerApp` provisions its own HA entities on startup via
`ha_provisioner`.  No manual helper creation is needed.

Provisioned entities:
- `input_select.wall_display_photo_frame_image` — image picker
- `script.wall_display_photo_frame_relay` — card-to-AppDaemon command relay

State (paused, interval, image URL, cache-bust) is published on a virtual
sensor `sensor.wall_display_photo_frame_status` as attributes.  Dashboard
cards read this sensor; they do not write to separate helpers.

---

## Setup

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

### 3. Fallback image

Ensure `/config/www/immich-album/no-image.jpg` (or the path you configure for
`fallback_image_path`) exists as a fallback when no photos are available.

### 4. No manual helpers required

The app provisions the `input_select` and relay script automatically on
startup.  Previously, several helpers had to be created manually — they have
been replaced by internal AppDaemon state and the virtual sensor.

If upgrading from an older version that had these helpers, they can be deleted
after verifying the new app is running correctly:
- `input_boolean.wall_display_photo_frame_paused`
- `input_number.wall_display_photo_frame_interval_seconds`
- `input_text.wall_display_photo_frame_cache_bust`
- `input_text.wall_display_photo_frame_image_local_url`

---

## Dashboard cards

Card YAML snippets (copy-paste into the Lovelace manual editor):

- **Viewer + controls**: `home-assistant/cards/global/photo-frame-viewer/wall-display-photo-frame-viewer.yaml`
- **Settings popup**: `home-assistant/cards/global/photo-frame-viewer/wall-display-photo-frame-settings.yaml`

The viewer card reads image URL and cache-bust from the virtual sensor:

```yaml
type: markdown
content: >-
  <img style="width: 100%; height: auto;" src="{{
  state_attr('sensor.wall_display_photo_frame_status', 'image_url')
  }}?cb={{
  state_attr('sensor.wall_display_photo_frame_status', 'cache_bust') }}" />
```

Pause/next/previous buttons call `script.wall_display_photo_frame_relay` via
`script.turn_on` (works for non-admin accounts — no `fire_event` required).

---

## AppDaemon config

In `appdaemon/apps/apps.yaml` (or `apps-dev.yaml` for local dev):

```yaml
photo_frame_viewer_wall_display:
  module: photo_frame_viewer.photo_frame_viewer_app
  class: PhotoFrameViewerApp
  ha_url: !secret ha_url
  ha_token: !secret token
  source_dir: /media/immich-photos
  ha_local_url_base: /local/photo-frame/live
  stage_shell_command: photo_frame_stage_gen
  cleanup_shell_command: photo_frame_cleanup_gen
  source_poll_interval_s: 30
  stage_settle_delay_s: 3
  fallback_image_path: /config/www/immich-album/no-image.jpg
  options_max: 100
  refresh_options_every_s: 60
  auto_cycle: true
  default_interval_s: 10
  state_dir: /media/photo-frame-viewer/wall_display
```

### Key config parameters

| Key | Default | Description |
|---|---|---|
| `ha_url` | — | Home Assistant URL (required for provisioning) |
| `ha_token` | — | Long-lived access token (required for provisioning) |
| `source_dir` | `/media/immich-photos` | NFS directory the Immich fetcher writes to |
| `ha_local_url_base` | `/local/photo-frame/live` | URL prefix mapping to `/config/www/photo-frame/live/` |
| `stage_shell_command` | `photo_frame_stage_gen` | HA shell_command name for staging a gen |
| `cleanup_shell_command` | `photo_frame_cleanup_gen` | HA shell_command name for deleting an old gen |
| `source_poll_interval_s` | `30` | How often to check for source changes (seconds) |
| `stage_settle_delay_s` | `3` | Delay after staging before marking the gen ready |
| `default_interval_s` | `10` | Default slideshow interval (overridden by user via relay) |
| `state_dir` | `/media/photo-frame-viewer/<prefix>` | Directory for persisting interval across restarts |
| `entity_prefix` | derived from instance name | Override entity ID prefix (see below) |

### Entity prefix

Entity IDs are derived from the app instance name in `apps.yaml`:

```
Instance:  photo_frame_viewer_wall_display
Prefix:    wall_display

Entities:
  sensor.wall_display_photo_frame_status
  input_select.wall_display_photo_frame_image
  script.wall_display_photo_frame_relay
```

To use a custom prefix, add `entity_prefix: my_prefix` to the app config.

---

## External service: Immich fetcher

The Immich fetcher (`hass-immich-addon` repo) runs as a separate Kubernetes
pod.  It periodically queries the Immich API and writes photos to
`/media/immich-photos/` via the shared `/media` NFS mount.

> **Tech debt**: An older NFS mount still exists between the Immich addon pod
> and HA's pod at `/config/www/immich-album/`.  See
> `agent-docs/image-view-roadmap.md` for the planned migration to `/media/`.
