# Image / Viewer Roadmap

This repo currently contains (or is moving toward) multiple "image viewers" for dashboards:

- **Wall Display photo frame**: show Immich-sourced photos with next/prev/pause + interval controls.
- **Detection Summary viewer**: browse generated security-camera runs and show the selected run image(s).

The long-term goal is to keep Home Assistant YAML thin (helpers + UI) and keep viewer logic in AppDaemon so it's testable, loggable, and reusable.

## Phase 0 (current): Wall Display Photo Frame Viewer

**Inputs**
- AppDaemon scans `source_dir` (typically `/media/immich-photos/`) directly
  via `os.listdir()` — no sensor dependency.

**Controls / state (self-provisioned)**
- `input_select.wall_display_photo_frame_image` (image picker — provisioned by AppDaemon on startup)
- `script.wall_display_photo_frame_relay` (card→AppDaemon command relay — provisioned by AppDaemon)
- Pause state, interval, and image URL live in AppDaemon-internal Python fields.
- All read-only state is published as attributes on `sensor.wall_display_photo_frame_status`.

Previously, `input_boolean`, `input_number`, and two `input_text` helpers were
created manually and read/written by AppDaemon.  These have been replaced by
internal state + the virtual sensor.  The relay script pattern ensures dashboard
cards work on non-admin wall-display accounts (`callService` only — no
`fire_event`).

**Rendering**
- Dashboard reads `image_url` and `cache_bust` attributes from the virtual sensor.
- AppDaemon calls `shell_command/photo_frame_stage_gen` to copy batches into
  versioned gen directories under `/config/www/photo-frame/live/`.

### Tech debt: immich-album NFS mount on /config/www/

- The `hass-immich-addon` (repo: `hass-immich-addon`) currently writes photos to `/config/www/immich-album/` via a dedicated NFS mount shared between its pod and HA's pod.
- This predates the `/media` convention where external services write to the shared `/media` NFS mount.
- The photo frame viewer now reads from `/media/immich-photos/` (AppDaemon has direct `/media` access), so the production `source_dir` no longer requires the old `/config/www/immich-album` mount.
- **Remaining migration**: update `hass-immich-addon` to write to `/media/immich-photos/` instead, and remove the `/config/www/immich-album` NFS mount from both the Immich addon pod and HA's pod.  The `fallback_image_path` can also migrate to `/media/...` once the dedicated mount is retired.

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

The `photo_providers` shared library (`appdaemon/photo_providers/`) owns the Immich API logic and defines a `PhotoProvider` interface. Future providers (Google Photos, Apple Photos) can be plugged in without changing the fetcher app logic.

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

If the wall photo frame viewer and detection summary viewer converge, extract shared pieces into a reusable module (e.g. under `appdaemon/providers/`):

- helper syncing patterns (`input_select` options + selection preservation)
- stable image publishing patterns (`shell_command` copy + cache-bust)
- scheduling patterns (pause + interval-controlled auto-advance)

Keep the app-specific parts thin: how to enumerate items, how to map a selection to an image(s), and what UI helpers are used.
