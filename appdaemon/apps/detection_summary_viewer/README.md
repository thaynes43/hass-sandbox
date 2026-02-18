# Detection Summary Viewer (Home Assistant dashboard)

This folder documents the **Detection Summary Viewer** setup: a fast, mobile-friendly Lovelace page for browsing historical detection runs produced by `detection_summary_app`.

The key design decision: **the dashboard loads images from `/local/.../<run_id>_best.jpg`** (unique URLs per run), not from `camera_proxy`. That avoids sluggish dashboard caching and makes run switching feel instant.

---

## How it works (high level)

For a given `bundle_key` (example: `garage`):

1. AppDaemon maintains an `input_select` of recent `run_id`s (newest first).
2. AppDaemon stages the files for those run_ids into `/media/.../viewer_stage/` with stable names:
   - `<run_id>_best.jpg`
   - `<run_id>_generated.png` (falls back to best if generated is missing)
3. AppDaemon calls a Home Assistant `shell_command` that **atomically** refreshes:
   - `/config/www/detection-summary/<bundle_key>/viewer/`
4. The Lovelace dashboard displays images directly from:
   - `/local/detection-summary/<bundle_key>/viewer/<run_id>_generated.png`
   - `/local/detection-summary/<bundle_key>/viewer/<run_id>_best.jpg`

Because the path includes `run_id`, the browser sees a new URL on every selection change (no cache-buster query string needed).

---

## Required Home Assistant pieces

### 1) Helpers

Create these helpers (UI: **Settings → Devices & services → Helpers**):

- **Run picker** (`input_select`):
  - Example entity_id: `input_select.garage_detection_summary_run_id`
  - Options will be managed by AppDaemon.
- **Selected summary** (`input_text`, max 255):
  - Example entity_id: `input_text.garage_detection_summary_selected`

### 2) `shell_command` (atomic refresh, wipe+fill)

Add this to `configuration.yaml`, then restart Home Assistant.

This implementation:
- copies staged files into a temporary directory
- swaps the directory into place with `mv` (no “half-copied” window)
- does **not** replace the viewer folder with an empty one

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

**Notes**
- `snapshot_rel` is the part after `/media/`. For garage it’s `detection-summary/garage`.
- This keeps `/config/www/.../viewer/` bounded to only the run_ids AppDaemon chose.

---

## AppDaemon configuration (per deployment)

In `appdaemon/apps/apps.yaml` under your `DetectionSummary` instance:

### Required entities (`hass_entities`)

| Purpose | Suggested entity_id |
|---|---|
| Trigger | `binary_sensor.g5_dome_motion` |
| Camera | `camera.garage_g5_dome_medium_resolution_channel` |
| Run picker | `input_select.garage_detection_summary_run_id` |
| Selected summary text | `input_text.garage_detection_summary_selected` |

### Optional entities

You can optionally create `local_file` cameras for debugging / entity-detail viewing:

| Purpose | Example entity_id |
|---|---|
| Selected best (optional) | `camera.garage_detection_summary_selected_best` |
| Selected generated (optional) | `camera.garage_detection_summary_selected_generated` |

If you do not create them, leave these out of `hass_entities`; the dashboard still works because it reads `/local/...` directly.

### Viewer cache args (defaults)

These defaults are used unless overridden:

- `viewer_enabled`: `true`
- `viewer_stage_subdir`: `viewer_stage`
- `viewer_www_subdir`: `viewer`
- `viewer_refresh_shell_command`: `ds_refresh_detection_summary_viewer_www`

---

## Dashboard (Lovelace) example

Create (or update) a storage-mode dashboard and add a view like this.
Example is for bundle_key `garage`.

The two image cards are the important part: **they load `/local/.../<run_id>_...`**.

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

## Cleanup / legacy notes

If you previously created an automation that overwrote stable files like:
- `/config/www/detection-summary/garage/selected_best.jpg`
- `/config/www/detection-summary/garage/selected_generated.png`

Turn it off or delete it; the viewer uses per-run files under `.../viewer/` now.

---

## BIG TODO: refactor viewer out of `detection_summary_app`

Today, `detection_summary_app` both:
- generates bundles (core value), and
- manages dashboard viewer staging + picker behavior.

**TODO (next phase):** move the viewer logic into a standalone AppDaemon “addon app” living in this folder, so the detection summary code stays lean and reusable.

Suggested shape:

- New app (example): `detection_summary_viewer.app.DetectionSummaryViewer`
- Inputs:
  - `snapshot_ha_dir` + `media_fs_root` mapping
  - run picker entity
  - selected summary entity
  - viewer stage / viewer dir names
  - refresh shell_command name
  - optionally: listen for an event like `detection_summary/bundle_published` with `{bundle_key, run_id}`
- Responsibilities:
  - maintain run picker options from disk (or store)
  - stage renamed images into `/media/.../viewer_stage/`
  - call HA shell_command to atomically refresh `/config/www/.../viewer/`
  - update selected summary text when run changes

When that’s done, `detection_summary_app` can focus on generating bundles (runs, summaries, best images, generated images) and become reusable beyond the garage door use case.

---

## TODO: notification tap deep-link should auto-select the run

Today (iOS especially), tapping the notification body can only open the dashboard URL; it cannot fire an event to select a `run_id`.
Action buttons can fire events, but on iPhone they require a long-press UX which is not ideal for V1.

**TODO (v2):** support `?run_id=<uuid>` (or similar) in the dashboard URL and add a tiny frontend helper (custom resource)
that, on page load, parses the query param and calls the appropriate HA service to select that run (for example:
`input_select.select_option` on the run picker).

This should live with the viewer frontend/docs (or the standalone viewer app), not inside the bundle-generating app.

---

## TODO: phone-friendly image sizes (performance)

Right now the viewer serves the staged images at whatever resolution/encoding they were produced at.
On mobile, this can be slower than necessary.

**TODO:** during staging into `/media/.../viewer_stage/`, generate smaller derivatives (for example: max width 1080, crop/cover to
match desired aspect ratio, and compress) before the HA refresh copies them into `/config/www`.

---

## TODO: move Vestaboard message generation into AppDaemon (richer + real home data)

Today we generate Vestaboard messages directly in Home Assistant via `ai_task.generate_data`, which works but lacks
context (and can produce generic output).

**TODO:** move message generation into an AppDaemon app so we can:

- Pull real, current home state (examples):
  - actual thermostat setpoint and HVAC mode
  - outside temperature / weather condition
  - number of lights on
  - alarm / lock status
  - time of day
- Let the model request which data it wants using structured output, e.g.:
  - `required_facts`: list of fact keys (thermostat_setpoint, outside_temp, etc.)
  - `template`: 22×6 message with placeholders like `{{ thermostat_setpoint }}` filled by AppDaemon
- Validate and normalize the final 22×6 text before sending to the Vestaboard (retry/fallback on violations)

Net result: **random messages that are coherent and include real information from the house**.

