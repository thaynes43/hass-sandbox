# Detection Summary Viewer

This folder contains the **`DetectionSummaryViewer`** AppDaemon app — a standalone companion to `detection_summary_app` that manages the dashboard viewer: run picker, selected run display, viewer cache staging, and notification action handling.

The key design decision: **the dashboard loads images from `/local/.../<run_id>_best.jpg`** (unique URLs per run), not from `camera_proxy`. That avoids sluggish dashboard caching and makes run switching feel instant.

---

## Architecture

`DetectionSummaryViewer` is **fully decoupled** from `detection_summary_app`. Communication between the two uses:

- **HA event `detection_summary/run_published`** (fired by `detection_summary_app` when a bundle is ready)
- **Shared filesystem** under `snapshot_ha_dir` (both apps read from `/media/detection-summary/<bundle_key>/`)
- **`detection_summary_store`** (shared in-process store; the viewer reads bundle data from here)

`detection_summary_app` can run standalone with no viewer, and the viewer can be restarted independently without affecting detection.

---

## How it works (high level)

For a given `bundle_key` (example: `garage`):

1. `detection_summary_app` fires `detection_summary/run_published` when a bundle is ready.
2. `DetectionSummaryViewer` listens for this event and calls `_sync_run_picker_periodic`.
3. AppDaemon stages the files for recent run_ids into `/media/.../viewer_stage/` with stable names:
   - `<run_id>_best.jpg`
   - `<run_id>_generated.png` (falls back to best if generated is missing)
4. AppDaemon calls a Home Assistant `shell_command` that **atomically** refreshes:
   - `/config/www/detection-summary/<bundle_key>/viewer/`
5. AppDaemon updates `input_select.{bundle_key}_detection_summary_run_id` options.
6. The Lovelace dashboard displays images directly from:
   - `/local/detection-summary/<bundle_key>/viewer/<run_id>_generated.png`
   - `/local/detection-summary/<bundle_key>/viewer/<run_id>_best.jpg`

Because the path includes `run_id`, the browser sees a new URL on every selection change (no cache-buster query string needed).

---

## Self-provisioned entities

On startup, `DetectionSummaryViewer` automatically creates (idempotent):

| Entity | Purpose |
|---|---|
| `input_select.{bundle_key}_detection_summary_run_id` | Run picker — lists recent `run_id`s newest-first |
| `input_text.{bundle_key}_detection_summary_selected` | Selected run's summary text (max 255 chars) |
| `script.{bundle_key}_detection_summary_relay` | Dashboard relay script for non-admin access |

No manual helper creation is needed.

---

## Required manual steps

### 1) `shell_command` (atomic refresh, wipe+fill)

Add this to `configuration.yaml`, then restart Home Assistant. This is the only manual step.

```yaml
shell_command:
  ds_refresh_detection_summary_viewer_www: >-
    /bin/sh -c 'set -e;
    base="/config/www/{{ snapshot_rel }}";
    dest="$base/{{ viewer_www_subdir }}";
    tmp="$base/.{{ viewer_www_subdir }}.tmp";
    old="$base/.{{ viewer_www_subdir }}.old";
    stage="/media/{{ snapshot_rel }}/{{ viewer_stage_subdir }}";

    mkdir -p "$base";
    rm -rf "$tmp" "$old";
    mkdir -p "$tmp";

    if [ -d "$stage" ] && [ -n "$(ls -A "$stage" 2>/dev/null)" ]; then
      cp -a "$stage"/. "$tmp"/;
      if [ -d "$dest" ]; then mv "$dest" "$old"; fi;
      mv "$tmp" "$dest";
      rm -rf "$old";
    else
      rm -rf "$tmp";
    fi'
```

**Notes:**
- `snapshot_rel` is the part after `/media/`. For garage it's `detection-summary/garage`; for nested layouts (e.g. doorbell) it's `detection-summary/doorbell/front-door`.
- `viewer_www_subdir` must be non-empty (default `viewer`). The atomic swap uses a temp dir (`.viewer.tmp`) then renames it to `viewer` in one operation.
- This keeps `/config/www/.../viewer/` bounded to only the run_ids AppDaemon chose.

### 2) `local_file` cameras for selected images (optional)

The viewer can repoint two `local_file` cameras to the staged selected files via `local_file/update_file_path`. These are optional — the dashboard can reference `/local/...` paths directly without them.

| Camera | Initial file path |
|---|---|
| `camera.{bundle_key}_detection_summary_selected_best` | `/config/www/detection-summary/{bundle_key}/viewer/placeholder_best.jpg` |
| `camera.{bundle_key}_detection_summary_selected_generated` | `/config/www/detection-summary/{bundle_key}/viewer/placeholder_generated.png` |

Add these to `hass_entities` in the app config if you want this feature. For nested layouts (e.g. doorbell/front-door), the path includes the full `snapshot_rel` segment: `/config/www/detection-summary/doorbell/front-door/viewer/placeholder_best.jpg`.

---

## App config format

```yaml
detection_viewer_garage_dev:
  module: detection_summary_viewer.detection_summary_viewer_app
  class: DetectionSummaryViewer
  ha_url_env: HA_URL
  ha_token_env: TOKEN
  bundle_key: garage
  snapshot_ha_dir: /media/detection-summary/garage
  media_fs_root_env: MEDIA_FS_ROOT
  hass_entities:
    # Optional: local_file cameras for selected run images
    selected_best_image_camera_entity_id: camera.garage_detection_summary_selected_best
    selected_generated_image_camera_entity_id: camera.garage_detection_summary_selected_generated
  # notification_action_prefix: "GARAGE_DS_VIEW"  # enables iOS action button run selection
```

### Required args

| Key | Description |
|---|---|
| `bundle_key` | Identifies the detection summary bundle (e.g. `garage`, `bulkhead`) |
| `snapshot_ha_dir` | Base HA path (e.g. `/media/detection-summary/garage`) |
| `ha_url` or `ha_url_env` | HA base URL for provisioner |
| `ha_token_env` | Env var name holding the HA long-lived access token |

### Optional args (with defaults)

| Key | Default | Description |
|---|---|---|
| `media_fs_root` or `media_fs_root_env` | `/media` | Local filesystem path that maps to `/media` in HA |
| `bundle_runs_subdir` | `runs` | Subdirectory under `snapshot_ha_dir` containing per-run directories |
| `viewer_enabled` | `true` | Enable viewer cache staging |
| `viewer_stage_subdir` | `viewer_stage` | Staging directory name under `snapshot_ha_dir` |
| `viewer_www_subdir` | `viewer` | Viewer www directory name (under `/config/www/.../`). Must be non-empty for the atomic swap. |
| `viewer_refresh_shell_command` | `ds_refresh_detection_summary_viewer_www` | Shell command name |
| `run_picker_max_options` | `25` | Maximum run_ids to keep in the active directory and show in the picker; older runs are archived to `runs/archive/YYYY-MM/` |
| `selected_auto_reset_s` | `900` | Seconds of inactivity before picker auto-resets to latest (0 = disabled) |
| `notification_action_prefix` | `None` | Prefix for iOS notification action buttons (e.g. `GARAGE_DS_VIEW`) |

---

## Dashboard (Lovelace) example

For bundle_key `garage`. The two image cards load `/local/.../<run_id>_...` directly.

```yaml
views:
  - title: Summary
    path: summary
    type: sections
    max_columns: 1
    sections:
      - type: grid
        cards:
          - type: heading
            heading: Garage detection summary
            heading_style: title
            icon: mdi:garage

          - type: heading
            heading: Selected summary
            heading_style: subtitle
            icon: mdi:text
          - type: markdown
            content: |
              {{ states('input_text.garage_detection_summary_selected') }}

          - type: heading
            heading: Selected images
            heading_style: subtitle
            icon: mdi:image-multiple

          - type: markdown
            content: >
              <img src="/local/detection-summary/garage/viewer/{{ states('input_select.garage_detection_summary_run_id') }}_generated.png"
              style="width:100%;border-radius:12px;object-fit:cover;" />

          - type: markdown
            content: >
              <img src="/local/detection-summary/garage/viewer/{{ states('input_select.garage_detection_summary_run_id') }}_best.jpg"
              style="width:100%;border-radius:12px;object-fit:cover;" />

          - type: heading
            heading: Run
            heading_style: subtitle
            icon: mdi:timeline-text-outline
          - type: entities
            entities:
              - entity: input_select.garage_detection_summary_run_id
                name: Run

          - type: grid
            columns: 3
            square: false
            cards:
              - type: button
                name: Back
                icon: mdi:chevron-left
                tap_action:
                  action: call-service
                  service: input_select.select_previous
                  target:
                    entity_id: input_select.garage_detection_summary_run_id
                  data:
                    cycle: false
              - type: button
                name: Latest
                icon: mdi:star-four-points
                tap_action:
                  action: call-service
                  service: input_select.select_first
                  target:
                    entity_id: input_select.garage_detection_summary_run_id
              - type: button
                name: Next
                icon: mdi:chevron-right
                tap_action:
                  action: call-service
                  service: input_select.select_next
                  target:
                    entity_id: input_select.garage_detection_summary_run_id
                  data:
                    cycle: false
```

---

## Files in this package

| File | Purpose |
|---|---|
| `detection_summary_viewer_app.py` | Main `DetectionSummaryViewer` AppDaemon app |
| `viewer_cache.py` | `ViewerCache` helper — stages files to `/media` and calls HA shell_command to refresh `/config/www` |
| `__init__.py` | Package docstring |
| `README.md` | This file |

---

## TODO: phone-friendly image sizes (performance)

Right now the viewer serves the staged images at whatever resolution/encoding they were produced at.
On mobile, this can be slower than necessary.

**TODO:** during staging into `/media/.../viewer_stage/`, generate smaller derivatives (for example: max width 1080, crop/cover to
match desired aspect ratio, and compress) before the HA refresh copies them into `/config/www`.
