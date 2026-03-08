# Detection Summary App

Generic motion-triggered detection pipeline: capture → score → publish. Supports built-in profiles for people, animals, packages, and vehicles.

- Capture snapshots **while motion is ON**
- Stop when motion has been **OFF for a grace period** (default 15 s)
- Adaptively select up to **N frames** to score via a multimodal LLM
- Gate publishing on a configurable **detection profile** (people, animals, packages, vehicles, …)
- Generate an AI illustration of the scene and publish a bundle that consumers attach to push notifications

## Layout

```
detection_summary_app/
├── manager.py          — AppDaemon app class; orchestrates the full pipeline
├── profiles.py         — DetectionProfile dataclasses + built-in profiles + loaders
├── capture.py          — Motion-ended capture loop state machine
├── selection.py        — Adaptive frame selection (ternary search + cutoff heuristic)
├── publish_gate.py     — Profile-driven publish/skip decision
├── population.py       — Multi-frame signal consensus (mode/max/median)
├── bundle.py           — Bundle dict assembly, stable image mirroring, trace artifacts
├── narrative.py        — Run-level narrative synthesis (second LLM step)
├── retention.py        — Run directory lifecycle (age-based pruning)
└── prompting/
    ├── schema_specs.py         — ScoreFieldSpec + ScoreSchemaSpec; schema_from_profile()
    ├── score_prompt_builder.py — Scoring instructions for the multimodal LLM
    ├── score_normalizer.py     — Raw LLM output → ScoreResult (populates extra_signals)
    ├── image_prompt_builder.py — Image-generation prompt; profile-aware guardrails
    ├── narrative_prompt_builder.py — Run narrative instructions
    └── style_variants.py       — Style/environment variant stubs (future)
```

## Detection profiles

A **`DetectionProfile`** defines what signals to extract from frames, what thresholds gate publishing, and how multi-frame signals are aggregated for image generation.

### Built-in profiles

| Name | Categories (required for publish) | Extra score fields |
|------|-----------------------------------|--------------------|
| `default` | people (male_count + female_count), animals (animal_count) | — |
| `packages` | people, animals, packages (package_count) | `package_count` |
| `vehicles` | people, animals, vehicles (vehicle_count) | `vehicle_count`, `vehicle_type` |
| `animals` | animals (animal_count) | People are context only (not required for publish) |

### Configuration

Set `detection_profile` in the app's YAML config:

```yaml
# Named built-in profile
detection_profile: packages

# Inline custom profile
detection_profile:
  name: delivery_vehicles
  categories:
    - name: vehicles
      required_for_publish: true
      count_signals: [vehicle_count]
      min_count_for_publish: 1
    - name: people
      required_for_publish: true
      count_signals: [male_count, female_count]
  extra_score_fields:
    - key: vehicle_count
      type_hint: int
      default: 0
      prompt_guidance: "integer count of vehicles visible (0 if none)"
    - key: vehicle_type
      type_hint: str
      default: ""
      prompt_guidance: "vehicle type: car, truck, van, delivery, motorcycle, none"
  consensus_strategy: mode   # "mode" | "max" | "median"
```

Omitting `detection_profile` uses the `default` profile (current behavior, fully backward-compatible).

### How profiles affect the pipeline

| Stage | Effect |
|-------|--------|
| **Scoring prompt** | Extra score fields added to the LLM schema; extra categories get detection guidance |
| **Score normalization** | Non-standard fields (e.g. `package_count`) land in `ScoreResult.extra_signals` |
| **Frame selection** | `_pick_key` sums all category count signals; profile-aware frames rank higher |
| **Publish gate** | Publishes if ANY `required_for_publish` category meets `min_count_for_publish` in any frame |
| **Population consensus** | Aggregates all count signals per strategy; generates `consensus_X` + `max_X` keys |
| **Image prompt** | "Most likely N (max M)" language per signal; category-specific hallucination guardrails |
| **Bundle** | `extra_signals` included in candidate data and summary scores; trace meta includes extras |

### Consensus strategies

Used for multi-frame signal aggregation when building image generation prompts:

- **`mode`** (default) — most frequent count across scored frames; ties broken conservatively (lower)
- **`max`** — highest count seen in any frame (pre-profiles behavior)
- **`median`** — median count rounded down

## Pipeline

```
Trigger (motion on)
  └─ Capture loop
       ├─ snapshot every N seconds while motion is on
       └─ stop after M seconds off (grace)
  └─ Adaptive selection (budget = analyze_max_snapshots)
       ├─ Ternary search for peak-quality frame
       ├─ Cutoff heuristic (stop scoring after subjects leave)
       └─ Fill budget around peak
  └─ Multimodal LLM scoring (parallelized)
       └─ ScoreResult per frame (standard fields + extra_signals)
  └─ Publish gate (profile-driven)
       └─ None → skip (reset cooldown, no image gen)
  └─ Run narrative (text LLM, optional)
  └─ Population consensus (profile-aware)
  └─ Image generation (image-to-image edit)
  └─ Bundle assembly + publish
  └─ Cooldown / backoff
```

## Events

| Event | Payload |
|-------|---------|
| `detection_summary/run_started` | `bundle_key`, `run_id`, `started_ts`, `trigger_entity_id`, `camera_entity_id` |
| `detection_summary/run_capture_done` | `bundle_key`, `run_id`, `captured_count`, `ended_ts`, `timed_out` |
| `detection_summary/run_published` | `bundle_key`, `run_id`, `created_at_epoch`, `summary`, `generated_image_url` |

Consumers (e.g. `DoorNotify`) listen for `detection_summary/run_published` matching `bundle_key` + `run_id` before attaching images to notifications.

## Key Home Assistant concepts

- **Snapshots** are written by HA via `camera.snapshot` into `/media/...`
- Push notifications use a **`local_file` camera** pointing at a stable path:
  - `/media/detection-summary/<bundle_key>/detection_summary_generated.png`
  - Notifications attach `/api/camera_proxy/<camera_entity_id>`
- The app **self-provisions** `input_text.<bundle_key>_detection_summary` on startup; no manual helper creation needed
- `local_file` cameras and shell commands must be added to `configuration.yaml` manually

## Config reference (apps.yaml)

### Required

```yaml
bundle_key: garage                              # Unique identifier
snapshot_ha_dir: /media/detection-summary/zone # HA /media snapshot path
media_fs_root: !secret media_fs_root            # Local filesystem mapping of HA /media
data_instructions: |                            # Scoring instructions for multimodal LLM
  ...
hass_entities:
  camera_entity_id: camera.garage_g5_dome_medium_resolution_channel
  trigger_entity_id: binary_sensor.g5_dome_motion
ai_provider_conf:                               # Named capability bundle references
  simple_text: openai-default
  multimodal: openai-default
  image: openai-default
```

### `hass_entities` (nested)

| Key | Required | Description |
|-----|----------|-------------|
| `camera_entity_id` | Yes | Source camera for snapshots |
| `trigger_entity_id` | Yes | Motion sensor entity |
| `best_image_camera_entity_id` | No | `local_file` camera for stable best-frame path |
| `generated_image_camera_entity_id` | No | `local_file` camera for stable generated image |
| `summary_text_entity_id` | No | Auto-derived as `input_text.<bundle_key>_detection_summary` |
| `run_picker_entity_id` | No | `input_select` for viewer dashboard |
| `selected_summary_text_entity_id` | No | Viewer: text for selected run |
| `selected_best_image_camera_entity_id` | No | Viewer: best-frame camera for selected run |
| `selected_generated_image_camera_entity_id` | No | Viewer: generated-image camera for selected run |

### `ai_provider_conf` (nested)

References named capability bundles from `providers/ai_providers/model_settings/`:

```yaml
ai_provider_conf:
  simple_text: openai-default   # Text-only LLM (run narrative)
  multimodal: openai-default    # Vision scoring
  image: openai-default         # Image generation (edit)
```

### Defaults (overridable)

#### Capture / cooldown

| Key | Default | Description |
|-----|---------|-------------|
| `trigger_to` | `on` | State value that starts a run |
| `task_name` | `detection summary` | Used in log messages |
| `snapshot_interval_s` | `2.5` | Seconds between frame captures while motion is on |
| `off_grace_s` | `15` | Seconds motion must be OFF before capture ends |
| `capture_max_s` | `300` | Hard cap on total capture duration |
| `cooldown_s` | `150` | Base post-finalize cooldown (seconds) |
| `cooldown_backoff_max_s` | `1800` | Hard cap on effective cooldown |
| `cooldown_backoff_window_n` | `3` | Backoff window multiplier |

#### Selection / scoring

| Key | Default | Description |
|-----|---------|-------------|
| `analyze_max_snapshots` | `10` | Frame scoring budget |
| `no_people_threshold` | `1.0` | person_score below this = "no subjects" for cutoff heuristic |
| `external_data_parallelism` | `4` | Concurrent LLM scoring threads |
| `best_min_person_score` | `2` | Minimum person_score to publish (legacy gate, alongside profile) |
| `best_min_animal_count` | `1` | Minimum animal count to publish (legacy gate, alongside profile) |
| `detection_profile` | `default` | Profile name or inline dict. See Detection profiles section. |

#### Image generation

| Key | Default |
|-----|---------|
| `external_image_gen_enabled` | `true` |
| `external_image_gen_wait_for_best_s` | `5` |
| `image_instructions` | `""` |
| `external_generated_filename` | `generated.png` |

#### Bundle filenames / layout

| Key | Default |
|-----|---------|
| `bundle_best_filename` | `best.jpg` |
| `published_best_filename` | `detection_summary_best.jpg` |
| `published_generated_filename` | `detection_summary_generated.png` |
| `selected_best_filename` | `detection_summary_selected_best.jpg` |
| `selected_generated_filename` | `detection_summary_selected_generated.png` |
| `bundle_runs_subdir` | `runs` |
| `write_bundle_json` | `true` |

#### Run narrative

| Key | Default |
|-----|---------|
| `run_narrative_enabled` | `true` |
| `run_narrative_max_chars` | `220` |
| `run_narrative_instructions` | `null` (uses built-in template) |

#### Trace / debug

| Key | Default | Description |
|-----|---------|-------------|
| `trace_enabled` | `false` | Write trace artifacts |
| `trace_copy_selected_frames` | `true` | Copy scored frames to `trace/selected/` |
| `trace_copy_best_frame` | `true` | Copy best frame to `trace/best/` |
| `trace_max_copies` | `50` | Cap on trace file copies |
| `debug_preserve_run_dirs` | `false` | Skip retention pruning (dev/debug) |

## Cooldown and backoff

After a pipeline completes (published **or** skipped), triggers are suppressed for `_effective_cooldown_s` seconds measured from when `_finalize` returns (not run start).

- **First image ever** → reset to `cooldown_s`
- **Next image within backoff window** (`n × prev_cooldown`) → double cooldown, capped at `cooldown_backoff_max_s`
- **Next image outside window** → reset to `cooldown_s`
- **Skipped run** (no subjects detected) → always resets to `cooldown_s`; does not affect backoff chain

Backoff progression from 150 s base: **150 → 300 → 600 → 1200 → 1800 s** (cap).

Suppression and backoff are logged at `WARNING` level. Resets are logged at `INFO`.

---

## Future work (TODO)

### 1) Packages-only detection: front door doorbell (dev app)

Add `detection_summary_front_door_packages_dev` to `apps-dev.yaml` using the `packages` built-in profile.

**Camera**: G4 Doorbell Pro PoE
**Trigger**: `binary_sensor.g4_doorbell_pro_poe_motion` (or a package-specific detection event)
**Profile**: `detection_profile: packages`

The `packages` profile gates publishing on `package_count >= 1` in any scored frame. People and animals also satisfy the gate via their own categories. A delivery person carrying a package will publish. However, to allow a **package-only** publish (no person present), the legacy person gate must be disabled:

```yaml
best_min_person_score: 0   # Disable legacy gate so profile alone controls publishing
```

Starter config:

```yaml
detection_summary_front_door_packages_dev:
  module: detection_summary_app.manager
  class: DetectionSummary
  ha_url: !secret ha_url
  ha_token_env: TOKEN
  bundle_key: front_door_packages
  detection_profile: packages
  best_min_person_score: 0
  snapshot_ha_dir: /media/detection-summary/front-door-packages
  media_fs_root: !secret media_fs_root
  hass_entities:
    camera_entity_id: camera.g4_doorbell_pro_poe_high_resolution_channel
    trigger_entity_id: binary_sensor.g4_doorbell_pro_poe_motion
  data_instructions: |
    You are analyzing ONE security camera snapshot from a front door doorbell camera.
    Focus on people, packages/parcels/boxes, and delivery activity.
    Count packages carefully — a box, bag, or parcel on the porch counts even if no
    person is present. Cardboard boxes, shipping bags, and parcels all count as packages.
  image_instructions: |
    Create a simple, clean illustration of the front door scene.
    If packages are visible, make them prominent in the illustration.
  ai_provider_conf:
    simple_text: openai-default
    multimodal: openai-default
    image: openai-default
```

**Manual HA steps after first run**:
- Add `local_file` camera entries for `detection_summary_best.jpg` / `detection_summary_generated.png` in `configuration.yaml`

**Open questions**:
- Confirm trigger entity — doorbell has both motion and object-detection events; motion is simpler to start
- Consider the `g4_doorbell_pro_poe_package_detected` binary sensor as the trigger instead of motion for lower false-positive rate

---

### 2) Animals-only detection: back yard camera (dev app)

Implemented as `detection_summary_back_deck_pets_dev` in `apps-dev.yaml` using the built-in `animals` profile (`detection_profile: animals`). The `animals` built-in profile gates publishing on `animal_count >= 1` only; people in the frame are context but do not trigger publishing on their own.

---

### 3) Dynamic, high-variety image style prompts

Goal: Keep **contents** consistent with the best frame, but vary **style/theme** every run.

- Add a "prompt-writer" step that produces an image-generator prompt
- Requirements:
  - Maximize variety without hard-coded examples (no anchoring on specific styles)
  - Enforce constraints: preserve subject count/positions/actions from the chosen frame
  - Store both prompts in the bundle:
    - the prompt-writer prompt + output
    - the final image-edit prompt passed to the image provider

### 4) Bundle viewer debug tool

Goal: A local UI/tool to load a bundle directory and show:

- Images + their per-frame scores/facts side-by-side
- Selection trace (probes/cutoff/peak exploration)
- Prompts and model settings used
- Optional "what-if" re-run with modified prompts/settings
