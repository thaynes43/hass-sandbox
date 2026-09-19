# Zigbee OTA Orchestrator

## What it does

Sequentially installs pending Zigbee2MQTT OTA firmware updates — one device at
a time — until every device is on the latest firmware. It covers the **whole
Zigbee2MQTT fleet**, battery-powered sensors included; `exclude_globs` is the
opt-out. Built for the 2026-08 Hue fleet refresh (~90 bulbs at 45–90 min each),
and widened in 2026-09 to every Z2M device.

## How it works

- **Queue** — re-derived every `scan_interval_s` from Home Assistant `update.*`
  entities (state `on` = firmware pending) matched against `include_globs` /
  `exclude_globs`. Because the queue is derived state, restarts are harmless:
  the app picks up wherever the fleet actually is (only in-memory retry counters
  reset).
- **Only Zigbee2MQTT devices, ever** — the glob is just the first filter. On
  every tick the app asks Home Assistant which `update.*` entities belong to
  Z2M (`integration_entities('mqtt')` narrowed to entities whose device
  identifiers carry `zigbee2mqtt_`) and manages nothing else, so `update.*`
  never reaches an Immich, HACS, ESPHome or Z-Wave update entity. If that
  lookup fails — or comes back empty when devices were known a moment ago,
  which is what an mqtt config entry still starting up renders — the app keeps
  the last good list, starts nothing on that tick and says so in `last_event`.
- **The bridge document is a fallback, not a dependency** — the retained
  `zigbee2mqtt/bridge/devices` document is accepted when it arrives, but
  nothing waits for it: AppDaemon's MQTT plugin subscribes once at plugin
  start, so the retained copy lands seconds before this app registers its
  listener and is never replayed on an app restart.
- **Nothing to go on means nothing runs** — with no device list from either
  source, `identity_source` reads `none`, nothing is queued, and the app logs
  a warning every tick, because that is what a template that has stopped
  matching looks like. If the bridge document *did* arrive, it carries the
  fleet on its own and there is no warning — `identity_source` on the status
  sensor is then the only sign that the Home Assistant lookup has gone quiet,
  so it is the attribute to check first when something looks wrong.
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
- **"No image currently available"** — Z2M's answer when a device advertises an
  update the OTA index has no file for (usually a pulled release). Nothing is
  transferred, so it counts as neither a completed update nor a failure: the
  device lands in `skipped_no_image` with no retry attempt and no backoff. It
  is picked up again when a different version is offered, when the update stops
  being offered and later returns, or after `park_recheck_s` — upstream often
  republishes a pulled release under the *same* version number.
- **"Device 'X' does not exist"** — Z2M's answer when the name it was sent
  matches no device, which means the Home Assistant friendly name has been
  renamed away from the Zigbee2MQTT device name. The availability and progress
  topics for that name can never match either, so the device is parked in
  `unknown_to_z2m` rather than retried forever. Renaming it back recovers on
  the next tick.
- **`unavailable` is not `off`** — Z2M marks a device's `update.*` entity
  unavailable whenever the device is out of touch (switched off at the wall, a
  Home Assistant restart). Only an explicit `off` means the update went away;
  `unavailable` and `unknown` leave the queue entry, its backoff and any
  no-image park exactly as they were.
- **Offline devices** (bulbs without power) — skipped while Home Assistant
  reports the update entity `unavailable` or the retained
  `zigbee2mqtt/<device>/availability` topic says `offline`. Home Assistant is
  the feed that is always there; MQTT is the faster one. A failed attempt
  classified as offline-type (`timeout` / `didn't respond`) backs off
  exponentially (`retry_base_s` doubling to `retry_max_s`), but the moment the
  device publishes `online` again the retry is fast-tracked to
  `online_retry_grace_s`. Other failures use the same backoff without the
  fast-track.
- **Safety valves** — an attempt that has not transferred a single byte after
  `progress_stall_s` is abandoned and the device goes back in the queue as an
  offline-type failure. That matters most for battery devices: Z2M counts a
  sleeping end-device as online for 25 hours, and without this it would hold
  the fleet's one slot for the full `update_timeout_s`. An attempt that *is*
  transferring gets more patience — it only raises a `stalled` flag after
  `progress_stall_s` without movement, and the per-attempt absolute timeout
  (`update_timeout_s`) is what stops a lost response freezing the queue
  forever (a late success is still recorded). Turning on
  `input_boolean.zigbee_ota_pause` (create it in HA if needed) stops new
  updates while letting the in-flight one finish.

## Self-provisioned entities

| Entity | Purpose |
| --- | --- |
| `sensor.zigbee_ota_orchestrator` | State = devices remaining. Attributes: `in_flight` (device, progress %, remaining s, stalled), `pending` (only what could start right now), `cooldown` (per-device attempts / `retry_at` / last error), `offline`, `completed_this_run`, `skipped_no_image`, `unknown_to_z2m`, `cleared_without_update`, `failed_attempts_this_run`, `busy_until`, `z2m_devices_known`, `identity_source`, `paused`, `last_event`. |

The lists are capped at 25 entries with a `*_count` beside them, and every
schedule is an absolute time (`retry_at`, `busy_until`, `started_at`) rather
than a countdown. Home Assistant writes a recorder row every time an attribute
changes, so on a 160-device fleet an uncapped list — or a countdown, which
changes by definition on every tick — would write a multi-kB row every 2
minutes.

A device is only listed in `completed_this_run` when its installed version
actually moved (or Z2M reported the update ok). An update Z2M withdrew without
installing anything shows up under `cleared_without_update` instead.

Booleans and numbers are published as strings (`"true"`, `"0"`, `"42"`).
AppDaemon strips values equal to `None`/`False` from the state it POSTs to Home
Assistant, and in Python `0 == False`, so a bare `0` would post no state at all
and Home Assistant would reject the whole update with a 400. Every number is
stringified, not just the zeros, so an attribute never changes type between
ticks.

## Associated card

None — the status sensor is designed to be readable from Developer Tools or a
simple entities card.

## Dependencies

- AppDaemon **HASS plugin** (reads `update.*` entities, renders the Z2M device
  template, writes the status sensor) and **MQTT plugin** (namespace `mqtt`,
  already subscribed to `zigbee2mqtt/#`). No HTTP providers, no secrets.
- Zigbee2MQTT ≥ 2.x with availability enabled (for the offline gate) and HA
  discovery (for the `update.*` entities).

## Configuration reference

| Key | Default | Meaning |
| --- | --- | --- |
| `include_globs` | `["update.*"]` | fnmatch globs an `update.*` entity must match to be managed. Prod uses the default — every Zigbee2MQTT device. Non-Z2M entities are filtered out regardless. |
| `exclude_globs` | `[]` | Globs to exclude after include matching — the opt-out for a device that should be left alone. Prod uses `[]`. |
| `scan_interval_s` | `120` | Queue refresh / decision tick interval. |
| `retry_base_s` | `900` | First retry backoff after a failed attempt. |
| `retry_max_s` | `21600` | Backoff cap. |
| `online_retry_grace_s` | `60` | Retry delay once an offline-failed device comes back online. |
| `progress_stall_s` | `2700` | No progress movement for this long → `stalled: true` on the sensor. An attempt that never started transferring at all is abandoned at this point instead. |
| `update_timeout_s` | `14400` | Absolute per-attempt cap; after it the attempt is marked failed and the queue moves on. |
| `busy_backoff_s` | `300` | Wait after Z2M reports another OTA is already running. |
| `park_recheck_s` | `86400` | How long a device Z2M cannot install (no image, or a name it doesn't know) stays parked before being tried again. |
| `mqtt_namespace` | `mqtt` | AppDaemon MQTT plugin namespace. |
| `base_topic` | `zigbee2mqtt` | Z2M base topic. |
| `status_sensor` | `sensor.zigbee_ota_orchestrator` | Status sensor entity id. |
| `pause_entity` | `input_boolean.zigbee_ota_pause` | Optional kill switch entity. |

## Manual setup

Optional: create `input_boolean.zigbee_ota_pause` in HA to get a pause switch.
Nothing else — no secrets, no shell commands, no helpers required.

## Upstream/downstream dependencies

- **Upstream**: Zigbee2MQTT bridge topics (request/response/devices), per-device
  availability + state topics, HA `update.*` entities from Z2M discovery, and
  the HA device registry (which entities are Z2M).
- **Downstream**: `sensor.zigbee_ota_orchestrator` consumers (dashboards,
  monitoring). No other app depends on this one.
