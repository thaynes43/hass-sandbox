# Detection Summary App (motion-ended burst + adaptive selection)

This package implements a generic “detection summary” producer:

- Capture snapshots **while motion is ON**
- Stop when motion has been **OFF for a grace period** (default 15s)
- Select up to **N frames** to score (faces-first) and generate an illustration
- Publish a bundle to the in-process store and fire AppDaemon events so consumer apps can reliably attach the result to notifications.

## Layout

- `manager.py`: AppDaemon app class. Orchestrates the pipeline, cooldown backoff, fires events.
- `capture.py`: Motion-ended capture loop logic (off-grace + capture cap).
- `selection.py`: Adaptive selection algorithm with caching (seed + ternary-ish peak + cutoff after no-people).
- `bundle.py`: Bundle dict assembly, stable generated mirroring, optional trace artifacts.

## Key Home Assistant concepts

- **Snapshots** are written by Home Assistant via `camera.snapshot` into `/media/...`.
- For push notifications, we prefer a **`local_file` camera** that points at a stable path:
  - `/media/detection-summary/<bundle_key>/detection_summary_generated.png`
  - (example entity: `camera.garage_detection_summary_generated`)
  - Notifications attach `/api/camera_proxy/<camera_entity_id>`

## Events (contract for consumers)

`DetectionSummary` fires:

- `detection_summary/run_started` with `{bundle_key, run_id, started_ts, trigger_entity_id, camera_entity_id}`
- `detection_summary/run_capture_done` with `{bundle_key, run_id, captured_count, ended_ts, timed_out}`
- `detection_summary/run_published` with `{bundle_key, run_id, created_at_epoch, summary, generated_image_url}`

Consumers (e.g. `GarageDoorNotify`) should listen for these events and wait for the matching `run_published` to attach the generated image + summary.

## Config reference (apps.yaml)

### Capture
- `trigger_entity_id`: motion binary sensor
- `trigger_to`: usually `on`
- `snapshot_interval_s`: seconds between snapshots while motion is on
- `off_grace_s`: motion must be off continuously this long to stop capture (default: 15)
- `capture_max_s`: maximum run duration while motion is on (default: 300)
- `cooldown_s`: base cooldown between runs
- `cooldown_backoff_max_s`: cap for exponential cooldown backoff (default: 1800)

### Selection/scoring
- `analyze_max_snapshots`: max frames to score per run (budget)
- `no_people_threshold`: person_score <= this is treated as “no people” for cutoff
- `external_data_parallelism`: max concurrent vision calls

### Trace/debug artifacts (optional)
When enabled, write:
- `runs/<run_id>/trace/selected/` (frames sent to the LLM)
- `runs/<run_id>/trace/best/` (best frame)
- `runs/<run_id>/trace/meta.json` (selection probes and scores)

Settings:
- `trace_enabled` (default false)
- `trace_copy_selected_frames` (default true)
- `trace_copy_best_frame` (default true)
- `trace_max_copies` (default 50)

