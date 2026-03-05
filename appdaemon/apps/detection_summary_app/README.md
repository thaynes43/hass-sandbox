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

## What gets published (bundle outputs)

Each run publishes a bundle that includes:

- **Best-frame summary**: `best.summary` (what is visible in the chosen best frame)
- **Run narrative summary**: `run_narrative.run_summary` (what happened across the whole run)
  - Convenience copy: `summary.run_text`
- **Images**
  - Best frame: `runs/<run_id>/best.jpg` and stable mirror `detection_summary_best.jpg` (if configured)
  - Generated illustration: `runs/<run_id>/generated.png` and stable mirror `detection_summary_generated.png`

## Events (contract for consumers)

`DetectionSummary` fires:

- `detection_summary/run_started` with `{bundle_key, run_id, started_ts, trigger_entity_id, camera_entity_id}`
- `detection_summary/run_capture_done` with `{bundle_key, run_id, captured_count, ended_ts, timed_out}`
- `detection_summary/run_published` with `{bundle_key, run_id, created_at_epoch, summary, generated_image_url}`

Consumers (e.g. `GarageDoorNotify`) should listen for these events and wait for the matching `run_published` to attach the generated image + summary.

## Config reference (apps.yaml)

This app is designed to keep `apps.yaml` minimal: **wire it to HA entities + directories** and rely on code defaults.

### Required (per deployment)

- `bundle_key`
- `snapshot_ha_dir` (must be under `/media/...`)
- `media_fs_root` (local filesystem path that maps to HA `/media`)
- `data_instructions`
- `image_instructions`
- `hass_entities` (nested HA entity IDs; at minimum `camera_entity_id` and `trigger_entity_id`)
- `ai_provider_conf` (at minimum, `provider` + `api_key`)

### `hass_entities` (nested)

All Home Assistant entity IDs live under this object so the main app config stays readable.

Required:

- `hass_entities.camera_entity_id`
- `hass_entities.trigger_entity_id`

Optional (enable features/UI integration):

- `hass_entities.best_image_camera_entity_id`
- `hass_entities.generated_image_camera_entity_id`
- `hass_entities.summary_text_entity_id`
- `hass_entities.run_picker_entity_id`
- `hass_entities.selected_summary_text_entity_id`
- `hass_entities.selected_best_image_camera_entity_id`
- `hass_entities.selected_generated_image_camera_entity_id`

### `ai_provider_conf` (nested)

Used for **both** vision scoring and image generation.

- `provider` (default: `openai`)
- `api_key` (**required** for OpenAI)
- `base_url` (default: `https://api.openai.com`)
- `data_model` (default: `gpt-5.2`)
- `data_timeout_s` (default: `60`)
- `data_max_output_tokens` (default: `300`)
- `data_image_detail` (default: `low`)
- `image_model` (default: `gpt-image-1.5`)
- `image_timeout_s` (default: `90`)
- `image_size` (default: `1024x1024`)
- `image_quality` (default: `medium`)
- `image_output_format` (default: `png`)

### Defaults (overridable app args)

These are defaults in code. You can override any of these by adding the key at the top-level of the app config in `apps.yaml`.

#### Capture / cooldown

| Key | Default | Description |
| --- | --- | --- |
| `trigger_to` | `on` | State value that starts a run |
| `task_name` | `detection summary` | Used in log messages |
| `snapshot_interval_s` | `2.5` | Seconds between frame captures while motion is on |
| `off_grace_s` | `15` | Seconds motion must be OFF before capture ends |
| `capture_max_s` | `300` | Hard cap on total capture duration |
| `cooldown_s` | `150` | Base post-finalize cooldown (seconds). See cooldown/backoff section below. |
| `cooldown_backoff_max_s` | `1800` | Hard cap on the effective cooldown (seconds) |
| `cooldown_backoff_window_n` | `3` | Backoff window multiplier. See cooldown/backoff section below. |

#### Selection / scoring

| Key | Default |
| --- | --- |
| `analyze_max_snapshots` | `10` |
| `no_people_threshold` | `1.0` |
| `external_data_parallelism` | `4` |
| `best_min_person_score` | `2` |

#### Image generation (image-to-image “edit”)

| Key | Default |
| --- | --- |
| `external_image_gen_enabled` | `true` |
| `external_image_gen_wait_for_best_s` | `5` |
| `external_generated_filename` | `generated.png` |

#### Bundle filenames / layout

| Key | Default |
| --- | --- |
| `bundle_best_filename` | `best.jpg` |
| `published_best_filename` | `detection_summary_best.jpg` |
| `published_generated_filename` | `detection_summary_generated.png` |
| `selected_best_filename` | `detection_summary_selected_best.jpg` |
| `selected_generated_filename` | `detection_summary_selected_generated.png` |
| `bundle_runs_subdir` | `runs` |
| `captured_subdir` | `captured` |
| `write_bundle_json` | `true` |

#### Selected-run dashboard integration

| Key | Default |
| --- | --- |
| `run_picker_max_options` | `25` |
| `selected_auto_reset_s` | `900` |

#### Run narrative (second LLM step)

| Key | Default |
| --- | --- |
| `run_narrative_enabled` | `true` |
| `run_narrative_max_chars` | `220` |
| `run_narrative_instructions` | `null` |

#### Trace/debug artifacts

When enabled, write:
- `runs/<run_id>/trace/selected/` (frames sent to the LLM)
- `runs/<run_id>/trace/best/` (best frame)
- `runs/<run_id>/trace/meta.json` (selection probes and scores)

| Key | Default |
| --- | --- |
| `trace_enabled` | `false` |
| `trace_copy_selected_frames` | `true` |
| `trace_copy_best_frame` | `true` |
| `trace_max_copies` | `50` |

## Cooldown and backoff

The cooldown system prevents rapid back-to-back image generation when motion is flapping (e.g. sensor briefly turns off and on again while someone is still in the scene).

### How it works

After a pipeline completes (bundle published **or** skipped), triggers are suppressed for `_effective_cooldown_s` seconds. The effective cooldown starts from the moment `_finalize` returns — not from run start — so it is a true post-pipeline gate regardless of how long capture + LLM + image generation took.

`_effective_cooldown_s` starts at `cooldown_s` and is updated each time a bundle is produced:

- **First image ever**: resets to `cooldown_s` (no history to compare against).
- **Next image within the backoff window**: `_effective_cooldown_s` doubles, capped at `cooldown_backoff_max_s`.
- **Next image outside the backoff window**: resets to `cooldown_s`.
- **Skipped run** (no people/animals detected): always resets to `cooldown_s`. Does not affect the backoff chain.

The **backoff window** is `cooldown_backoff_window_n × previous_effective_cooldown_s`. The delta is measured between finalize timestamps of successive bundles.

### The three config keys

| Key | Role |
| --- | --- |
| `cooldown_s` | Base cooldown after each finalized pipeline. Also the reset value when the window is exceeded. |
| `cooldown_backoff_window_n` | Multiplier defining how long "rapid" means. If the next bundle arrives within `n × prev_cooldown`, the cooldown doubles. |
| `cooldown_backoff_max_s` | Hard cap. The effective cooldown will never exceed this regardless of how many doublings occur. |

### Example: groceries unloading (150s base, n=3)

| Event | Time (after prev finalize) | Effective cooldown set |
| --- | --- | --- |
| First trip detected, image generated | T₀ | 150s (first image) |
| Second trip trigger | T₀ + 60s | **SUPPRESSED** — 90s remaining |
| Second trip trigger | T₀ + 160s | allowed — delta 160s ≤ window 450s → **backed off to 300s** |
| Third trip trigger | T₁ + 200s | **SUPPRESSED** — 100s remaining |
| Later distinct event | T₁ + 500s | allowed — delta 500s > window 900s → **reset to 150s** |

Backoff progression from 150s base: **150 → 300 → 600 → 1200 → 1800s** (cap).

### Suppression logging

Every suppressed trigger is logged at `WARNING` level:

```
DetectionSummary[bulkhead]: trigger SUPPRESSED by cooldown
  entity=binary_sensor.basement_motion
  elapsed=16.6s remaining=133.4s
  effective_cooldown=150s (base=150s)
```

When the cooldown is backed off after a bundle, it is logged at `WARNING`:

```
DetectionSummary[bulkhead]: cooldown backed_off delta=160.0s <= window=450.0s -> 300s
```

When it resets (delta exceeded the window), it is logged at `INFO`:

```
DetectionSummary[bulkhead]: cooldown reset delta=500.0s > window=450.0s -> 150s
```

---

## Future work (TODO)

### 1) Dynamic, high-variety image style prompts

Goal: Keep **contents** consistent with the best frame, but vary **style/theme** every run.

- Add a “prompt-writer” step that produces an image-generator prompt.
- Requirements:
  - Maximize variety without hard-coded examples (no anchoring on specific styles).
  - Enforce constraints: preserve subject count/positions/actions from the chosen frame.
  - Store both prompts in the bundle:
    - the prompt-writer prompt + output
    - the final image-edit prompt passed to the image provider

### 2) Bundle viewer debug tool

Goal: A local UI/tool to load a bundle directory and show:

- images + their per-frame scores/facts side-by-side
- selection trace (probes/cutoff/peak exploration)
- prompts and model settings used
- optional “what-if” re-run with modified prompts/settings

