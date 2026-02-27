# Wall Display Photo Frame Viewer (setup notes)

This repo contains the AppDaemon app + Lovelace card snippet for a wall-display photo slideshow.
Some prerequisites live in Home Assistant configuration and must be applied there.

## 1) Home Assistant `shell_command` (required)

Add a **new** shell command alongside the existing `update_photo_frame` (do not modify the existing one):

```yaml
shell_command:
  # existing:
  update_photo_frame: cp "{{ image_path }}" /config/www/photo-frame/photo_frame_image.jpg

  # new (wall display):
  update_photo_frame_wall_display: >-
    /bin/sh -c 'set -e;
    mkdir -p /config/www/photo-frame-wall-display;
    cp "{{ image_path }}" /config/www/photo-frame-wall-display/photo.jpg'
```

Restart Home Assistant after updating `configuration.yaml`.

## 2) Helpers (required)

Create the following helpers in Home Assistant (UI) using these entity ids:

- `input_select.wall_display_photo_frame_image`
- `input_boolean.wall_display_photo_frame_paused`
- `input_number.wall_display_photo_frame_interval_seconds`
- `input_text.wall_display_photo_frame_cache_bust`

Reference YAML (if you prefer YAML-mode): `home-assistant/helpers/wall_display_photo_frame_viewer.yaml`.

## 3) Dashboard card snippet

Copy/paste the snippet from:

- `home-assistant/cards/global/photo-frame-viewer/wall-display-photo-frame-viewer.yaml`

into your `/wall-display` dashboard.

