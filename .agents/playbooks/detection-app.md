# Detection App Setup: new entrance (summary + viewer + notification)

### When to use this

Use this playbook when adding a new detection summary + viewer + door notification app for a new entrance (door, window, entrypoint). Covers any new `{bundle_key}` alongside existing deployments like `garage` or `bulkhead`.

### Critical rule: prerequisites must be verified before executing the workflow

The four `local_file` camera entities MUST exist in HA before the agent executes any step. If they are missing, AppDaemon will fail to start because `hass_entities` references non-existent cameras. The **Agent gate** in the workflow section is mandatory — do not skip it.

---

## Prerequisites (user must complete before running agent)

These steps require SSH/exec into the HA container and manual HA UI work. An agent **cannot** complete them.

### 1. Create directories (SSH into HA container)

Replace `{bundle_key}` with your chosen key (e.g., `bulkhead`, `front_door`, `kitchen_slider`):

```bash
mkdir -p /media/detection-summary/{bundle_key}
mkdir -p /config/www/detection-summary/{bundle_key}/viewer
```

### 2. Copy placeholder images

`local_file` cameras require valid files to exist at their configured path at creation time. Copy from an existing deployment:

```bash
cp /media/detection-summary/garage/detection_summary_best.jpg \
   /media/detection-summary/{bundle_key}/detection_summary_best.jpg
cp /media/detection-summary/garage/detection_summary_generated.png \
   /media/detection-summary/{bundle_key}/detection_summary_generated.png

# Use any existing run_id from garage viewer
cp /config/www/detection-summary/garage/viewer/<any_run_id>_best.jpg \
   /config/www/detection-summary/{bundle_key}/viewer/placeholder_best.jpg
cp /config/www/detection-summary/garage/viewer/<any_run_id>_generated.png \
   /config/www/detection-summary/{bundle_key}/viewer/placeholder_generated.png
```

### 3. Create four `local_file` camera entities in HA UI

Go to **Settings > Devices & services > Add integration > Local File** and create all four:

| Camera entity ID | `file_path` |
|---|---|
| `camera.{bundle_key}_detection_summary_best` | `/media/detection-summary/{bundle_key}/detection_summary_best.jpg` |
| `camera.{bundle_key}_detection_summary_generated` | `/media/detection-summary/{bundle_key}/detection_summary_generated.png` |
| `camera.{bundle_key}_detection_summary_selected_best` | `/config/www/detection-summary/{bundle_key}/viewer/placeholder_best.jpg` |
| `camera.{bundle_key}_detection_summary_selected_generated` | `/config/www/detection-summary/{bundle_key}/viewer/placeholder_generated.png` |

### 4. Gather required entity IDs

Before running the agent, confirm these entities exist:

- **Door/contact sensor** — the entity that changes state when the door opens
- **Camera** — security camera for snapshots
- **Motion trigger** — motion sensor that fires the detection pipeline

---

## Workflow

### Agent gate: verify prerequisites before proceeding

Before executing any steps below, use the MCP server to verify all four `camera.{bundle_key}_detection_summary_*` entities exist plus the door sensor, camera, and motion trigger. If **any** check fails, **STOP** and tell the user which prerequisites are missing.

---

**Step 1 — Add `detection_summary` config to `apps-dev.yaml` (1 file edit)**

```yaml
detection_summary_{bundle_key}_dev:
  module: detection_summary_app.manager
  class: DetectionSummary
  ha_url: !secret ha_url
  ha_token_env: TOKEN
  bundle_key: {bundle_key}
  snapshot_ha_dir: /media/detection-summary/{bundle_key}
  media_fs_root: !secret media_fs_root
  hass_entities:
    camera_entity_id: camera.{camera_entity}
    trigger_entity_id: binary_sensor.{motion_entity}
    best_image_camera_entity_id: camera.{bundle_key}_detection_summary_best
    generated_image_camera_entity_id: camera.{bundle_key}_detection_summary_generated
  data_instructions: >
    You are analyzing ONE security camera snapshot from a {description} entrance.
    Focus ONLY on the people and any animals.
    The summary should be short and notification-friendly: 1 sentence, <= 140 characters.
    Include only: person/animal count, what they are doing, and where they are in
    frame (e.g. "near left", "center", "back").
    Also return male_count and female_count as integer counts of people by gender
    (0 if none).
    Also return animal_count as an integer count of visible animals/pets (0 if none).
    Return scores on a 0-10 scale (integers preferred) with enough spread to rank frames.
    Heavily favor frames where at least one person's FACE is clearly visible.
    face_score meaning: 0=no face visible, 10=clear unobstructed face.
  image_instructions: >
    Create a simple, clean illustration representing the scene at a {description} entrance.
  debug_preserve_run_dirs: true
  ai_provider_conf:
    provider: openai
    api_key_env: OPENAPI_TOKEN
    data_model: gpt-5.2
    data_image_detail: auto
    image_model: gpt-image-1.5
```

---

**Step 2 — Add `detection_summary_viewer` config to `apps-dev.yaml` (1 file edit)**

```yaml
detection_viewer_{bundle_key}_dev:
  module: detection_summary_viewer.detection_summary_viewer_app
  class: DetectionSummaryViewer
  ha_url: !secret ha_url
  ha_token_env: TOKEN
  bundle_key: {bundle_key}
  snapshot_ha_dir: /media/detection-summary/{bundle_key}
  media_fs_root: !secret media_fs_root
  hass_entities:
    selected_best_image_camera_entity_id: camera.{bundle_key}_detection_summary_selected_best
    selected_generated_image_camera_entity_id: camera.{bundle_key}_detection_summary_selected_generated
  notification_action_prefix: "{BUNDLE_KEY_UPPER}_DS_VIEW"
```

Replace `{BUNDLE_KEY_UPPER}` with the bundle key in UPPER_SNAKE_CASE (e.g., `FRONT_DOOR`).

The viewer self-provisions on startup: `input_select.{bundle_key}_detection_summary_run_id`, `input_text.{bundle_key}_detection_summary_selected`, `input_text.{bundle_key}_detection_summary_timing`, `input_text.{bundle_key}_detection_summary_cooldown`, `script.{bundle_key}_detection_summary_relay`.

---

**Step 3 — Add `door_notify` config to `apps-dev.yaml` (1 file edit)**

| Door entity type | `door_open_state` | `door_closed_state` |
|---|---|---|
| `cover` (garage openers) | `"open"` (default) | `"closed"` (default) |
| `binary_sensor` (contact sensors) | `"on"` | `"off"` |

For `binary_sensor` contact sensors (must set state values explicitly):

```yaml
{bundle_key}_door_notify_dev:
  module: door_notify
  class: DoorNotify
  notify_services:
    - notify.mobile_app_toms_iphone_15_pro
    - notify.mobile_app_toms_iphone_air
    - notify.mobile_app_kellies_iphone_air
  doors:
    - binary_sensor.{door_sensor_entity}
  door_open_state: "on"
  door_closed_state: "off"
  ai_enabled: true
  ai_bundle_key: {bundle_key}
  ai_wait_timeout_s: 180
  ai_max_bundle_age_s: 900
  notification_url: "/detection-summary/{bundle_key}"
```

---

**Step 4 — Add dashboard view via MCP (MANDATORY — do not skip)**

Read `.agents/playbooks/ha-dashboard.md` before executing. Use `ha_config_get_dashboard` to get the full `config_hash` (never truncate), then `ha_config_set_dashboard` with `python_transform` to populate the view using the 5-card detection-summary pattern. See the dashboard playbook's "Detection-summary view card template" section for the full template.

---

**Step 5 — Verify `shell_command` exists (1 MCP call)**

The viewer app calls `shell_command.ds_refresh_detection_summary_viewer_www`. If not present, instruct the user to add it to `configuration.yaml` and restart HA. Definition in `appdaemon/apps/detection_summary_viewer/README.md`.

---

### Self-provisioning (helpers + relay script)

`DetectionSummaryViewer` self-provisions all required HA entities on startup using `HAProvisioner`. **No manual helper creation is needed.**

The relay script is required because non-admin tablet/phone accounts cannot call `input_select.select_*` directly — they call the relay script via `callService`, which fires an event that AppDaemon handles.

---

## Testing checklist

- [ ] AppDaemon starts without errors for all three new app instances
- [ ] Self-provisioned helpers created: run_id input_select, selected/timing/cooldown input_texts
- [ ] Relay script created: `script.{bundle_key}_detection_summary_relay`
- [ ] Trigger the motion sensor and verify detection pipeline runs
- [ ] After a run completes, run picker has the run_id as an option
- [ ] Select the run in the dashboard — summary text and both images update
- [ ] Open the door sensor and verify a push notification fires with AI summary
- [ ] Notification URL links to `/detection-summary/{bundle_key}`
- [ ] Run all unit tests: `wsl bash -c "cd /mnt/d/labspace/hass-sandbox && source .venv-wsl/bin/activate && cd appdaemon && python -m pytest tests/ -v --tb=short"`

---

## Prod promotion

Add production entries to `apps-prod.yaml` (omit `_dev` suffix, add `disable: true`). **Remove `debug_preserve_run_dirs: true`** — dev-only, prevents cleanup of old run directories.

Then deploy:

```bash
python appdaemon/deploy.py --dry-run
python appdaemon/deploy.py
```

---

## Common pitfalls

| Symptom | Cause | Fix |
|---------|-------|-----|
| `local_file` camera creation fails | Placeholder image files don't exist | Complete Prerequisites step 2 first |
| AppDaemon "entity not found" on startup | Camera entities not created before AppDaemon start | Complete Prerequisites step 3, then restart AppDaemon |
| Images don't update after run selection | `ds_refresh_detection_summary_viewer_www` shell command missing | Add it per README, restart HA |
| `binary_sensor` door never triggers notify | Missing `door_open_state: "on"` using default `"open"` | Set `door_open_state: "on"` and `door_closed_state: "off"` for contact sensors |
| Dashboard view empty after MCP update | `config_hash` truncated or stale | Use the **full** `config_hash` (all 16 hex chars). Re-fetch if stale. |
| Run picker not populating | Viewer app not running | Check AppDaemon logs; ensure both detection_summary and detection_viewer instances are running |
