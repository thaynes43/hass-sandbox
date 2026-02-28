# Image / Viewer Roadmap

This repo currently contains (or is moving toward) multiple "image viewers" for dashboards:

- **Wall Display photo frame**: show Immich-sourced photos with next/prev/pause + interval controls.
- **Detection Summary viewer**: browse generated security-camera runs and show the selected run image(s).

The long-term goal is to keep Home Assistant YAML thin (helpers + UI) and keep viewer logic in AppDaemon so it's testable, loggable, and reusable.

## Phase 0 (current MVP): Wall Display Photo Frame Viewer

**Inputs**
- `sensor.immich_album` is the source of truth for which photos are currently available (via `attributes.file_list`).

**Controls / state (helpers)**
- `input_select.wall_display_photo_frame_image` (dropdown selection)
- `input_boolean.wall_display_photo_frame_paused` (pause)
- `input_number.wall_display_photo_frame_interval_seconds` (cycle interval)
- `input_text.wall_display_photo_frame_cache_bust` (cache-bust for dashboard reload)

**Rendering**
- Dashboard displays a stable `/local/...` image path that AppDaemon updates via an HA `shell_command`.

### Tech debt: immich-album NFS mount on /config/www/

- The `hass-immich-addon` (repo: `hass-immich-addon`) currently writes photos directly to `/config/www/immich-album/` via a shared NFS mount between its pod and HA's pod.
- This predates AppDaemon integration and breaks the standard pattern where external services write to `/media/` and HA `shell_command`s copy display files into `/config/www/`.
- The photo frame viewer's generation-swap design works around this by treating `/config/www/immich-album/` as a read-only source and copying into `/config/www/photo-frame/live/<gen>/` via shell_commands.
- **Future migration**: update `hass-immich-addon` to write to `/media/immich-album/` instead. Then the photo frame stage shell_command would read from `/media/` (or AppDaemon could stage directly since it has `/media/` access). This would also let us remove the special NFS mount between the Immich pod and HA.

## Phase 1: Dashboard "configuration popup" for Immich fetch rules (out of scope for MVP)

Today, the Immich fetcher is configured outside Home Assistant (Kubernetes config) and periodically writes new photos into `/config/www/immich-album`.

Desired: allow a wall-display user to adjust what photos appear without editing cluster config.

### Possible approaches (pick later)
- **HA helpers as desired config** (preferred UI/UX):
  - Expose an `input_select` for "filter set" (e.g. `"Penelope"`, `"Tom"`, etc.)
  - Optional toggles/fields for: `num_photos`, `update_interval_minutes`, date ranges, people lists
  - AppDaemon (or a small service) translates helper state into a fetcher config update + refresh

- **AppDaemon service bridge**
  - AppDaemon exposes a service like `photo_frame_viewer/set_filter_set`
  - Implementation updates the running fetcher via a small HTTP endpoint or a config-reload mechanism

### Hard constraints
- The fetcher currently owns the Immich API integration; Home Assistant should not become the heavy worker.
- Avoid tight coupling between viewer UI and fetcher implementation details; keep a stable "contract" at the HA helper layer.

## Phase 2: Detection Summary refactor (decouple producer from viewer)

The `DetectionSummary` app currently contains both:
- **Producer** responsibilities (capture/select/score/generate/publish), and
- **Viewer** responsibilities (staging + refresh of dashboard-browsable images).

Target end state:
- `appdaemon/apps/detection_summary_app/` focuses on producing runs and publishing bundles/events.
- A separate viewer module/app (target name: `appdaemon/apps/detection_summary_viewer/`) handles:
  - run list -> `input_select` options
  - staging/caching strategy into `/media` and refresh into `/config/www/...`
  - any dashboard-specific conveniences (auto-reset selection, cache-bust, etc.)

Reference: repo intent and "HA YAML as glue / AppDaemon for brittle logic" direction in `README.md`.

## Phase 3: Shared "viewer" library (optional)

If the wall photo frame viewer and detection summary viewer converge, extract shared pieces into a reusable module (e.g. under `appdaemon/` and deployed via `deploy.py`):

- helper syncing patterns (`input_select` options + selection preservation)
- stable image publishing patterns (`shell_command` copy + cache-bust)
- scheduling patterns (pause + interval-controlled auto-advance)

Keep the app-specific parts thin: how to enumerate items, how to map a selection to an image(s), and what UI helpers are used.
