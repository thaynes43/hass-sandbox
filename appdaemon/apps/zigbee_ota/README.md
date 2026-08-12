# Zigbee OTA Orchestrator

## What it does

Sequentially installs pending Zigbee2MQTT OTA firmware updates — one device at
a time — until every matching device is on the latest firmware. Built for the
2026-08 Hue fleet refresh (~90 bulbs at 45–90 min each), but generic to any
Zigbee2MQTT device exposing a Home Assistant `update.*` entity.

## How it works

- **Queue** — re-derived every `scan_interval_s` from Home Assistant `update.*`
  entities (state `on` = firmware pending) matched against `include_globs` /
  `exclude_globs`, and validated against the retained
  `zigbee2mqtt/bridge/devices` document so only real Z2M devices are touched.
  Because the queue is derived state, restarts are harmless: the app picks up
  wherever the fleet actually is (only in-memory retry counters reset).
- **One at a time** — an update starts by publishing
  `{"id": <friendly_name>, "transaction": ...}` to
  `zigbee2mqtt/bridge/request/device/ota_update/update`. Nothing else starts
  until the matching `.../response/device/ota_update/update` arrives (Z2M sends
  it on completion or failure). Progress (`progress`/`remaining`) is read from
  the device state topic's `update` object.
- **Externally started updates are adopted** — if an update is already
  `in_progress` (started from the Z2M frontend or HA), the app waits for it
  instead of dueling; a Z2M "already in progress" error just requeues without
  burning a retry attempt.
- **Offline devices** (bulbs without power) — skipped while their retained
  `zigbee2mqtt/<device>/availability` topic says `offline`. A failed attempt
  classified as offline-type (`timeout` / `didn't respond`) backs off
  exponentially (`retry_base_s` doubling to `retry_max_s`), but the moment the
  device publishes `online` again the retry is fast-tracked to
  `online_retry_grace_s`. Other failures use the same backoff without the
  fast-track.
- **Safety valves** — a per-attempt absolute timeout (`update_timeout_s`)
  prevents a lost response from freezing the queue forever (a late success is
  still recorded); a `stalled` flag is raised after `progress_stall_s` without
  progress movement; turning on `input_boolean.zigbee_ota_pause` (create it in
  HA if needed) stops new updates while letting the in-flight one finish.

## Self-provisioned entities

| Entity | Purpose |
| --- | --- |
| `sensor.zigbee_ota_orchestrator` | State = devices remaining. Attributes: `in_flight` (device, progress %, remaining s, stalled), `pending`, `cooldown` (per-device attempts/retry-in/last error), `offline`, `completed_this_run`, `failed_attempts_this_run`, `paused`, `last_event`. |

## Associated card

None — the status sensor is designed to be readable from Developer Tools or a
simple entities card.

## Dependencies

- AppDaemon **HASS plugin** (reads `update.*` entities, writes the status
  sensor) and **MQTT plugin** (namespace `mqtt`, already subscribed to
  `zigbee2mqtt/#`). No HTTP providers, no secrets.
- Zigbee2MQTT ≥ 2.x with availability enabled (for the offline gate) and HA
  discovery (for the `update.*` entities).

## Configuration reference

| Key | Default | Meaning |
| --- | --- | --- |
| `include_globs` | `["update.*"]` | fnmatch globs an `update.*` entity must match to be managed. Prod uses `["update.*hue*"]`. |
| `exclude_globs` | `[]` | Globs to exclude after include matching. |
| `scan_interval_s` | `120` | Queue refresh / decision tick interval. |
| `retry_base_s` | `900` | First retry backoff after a failed attempt. |
| `retry_max_s` | `21600` | Backoff cap. |
| `online_retry_grace_s` | `60` | Retry delay once an offline-failed device comes back online. |
| `progress_stall_s` | `2700` | No progress movement for this long → `stalled: true` on the sensor. |
| `update_timeout_s` | `14400` | Absolute per-attempt cap; after it the attempt is marked failed and the queue moves on. |
| `busy_backoff_s` | `300` | Wait after Z2M reports another OTA is already running. |
| `mqtt_namespace` | `mqtt` | AppDaemon MQTT plugin namespace. |
| `base_topic` | `zigbee2mqtt` | Z2M base topic. |
| `status_sensor` | `sensor.zigbee_ota_orchestrator` | Status sensor entity id. |
| `pause_entity` | `input_boolean.zigbee_ota_pause` | Optional kill switch entity. |

## Manual setup

Optional: create `input_boolean.zigbee_ota_pause` in HA to get a pause switch.
Nothing else — no secrets, no shell commands, no helpers required.

## Upstream/downstream dependencies

- **Upstream**: Zigbee2MQTT bridge topics (request/response/devices), per-device
  availability + state topics, HA `update.*` entities from Z2M discovery.
- **Downstream**: `sensor.zigbee_ota_orchestrator` consumers (dashboards,
  monitoring). No other app depends on this one.
