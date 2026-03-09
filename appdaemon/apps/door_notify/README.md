# Door Notify

Push notifications when configured door entities open or close. Consolidates rapid open-then-closed transitions into a single "was open for N minutes" notification to avoid duplicate alerts. Optionally attaches AI detection summaries from `detection_summary_app`.

## How it works

1. Listens for state changes on configured door entities (covers or binary sensors).
2. On first transition (e.g. closed → open), schedules a delayed notification.
3. If the opposite transition occurs within the consolidation delay (default 60 s), cancels the pending notification and sends a single consolidated message instead.
4. If AI is enabled, waits briefly for a detection summary bundle from `detection_summary_app` and attaches the generated image + narrative to the notification.

## Entity types supported

| Entity type | Open state | Closed state | Intermediate states |
|-------------|-----------|--------------|---------------------|
| `cover.*` (default) | `open` | `closed` | `opening` → closed, `closing` → open |
| `binary_sensor.*` | `on` | `off` | None |

Configure via `door_open_state` and `door_closed_state` in the app config.

## Dependencies

- `detection_summary_store.STORE` — shared in-process bundle store (for AI attachment, optional)

No providers are imported directly. This app does not use `ha_provisioner` — it's a notification-only app with no self-provisioned entities.

## Config (apps.yaml)

### Required

```yaml
garage_door_notify:
  module: door_notify.door_notify
  class: DoorNotify
  doors:
    - cover.ratgdov25i_4a0325_door
    - cover.ratgdov25i_dbfa50_door
  notify_services:
    - notify.mobile_app_toms_iphone_15_pro
```

### Optional (with defaults)

| Key | Default | Description |
|-----|---------|-------------|
| `door_open_state` | `open` | State value that means "door is open" |
| `door_closed_state` | `closed` | State value that means "door is closed" |
| `consolidation_delay` | `60` | Seconds to wait for second transition before sending |
| `intermediate_state_map` | Auto (covers) | Map intermediate states to display names |
| `notification_url` | `/detection-summary/garage` | Deep-link URL in push notification |

### AI attachment (optional)

| Key | Default | Description |
|-----|---------|-------------|
| `ai_enabled` | `false` | Enable AI detection summary attachment |
| `ai_bundle_key` | `garage` | Bundle key matching `detection_summary_app` config |
| `ai_wait_timeout_s` | `30` | Max wait for detection summary bundle |
| `ai_max_bundle_age_s` | `120` | Max age of eligible bundles |
| `ai_window_pad_s` | `5` | Time padding for window boundaries |
| `ai_use_detection_summary_events` | `true` | Use event-driven coordination (preferred) |
| `ai_run_started_lookback_s` | `900` | How far back to look for run_started events |

## AI attachment flow

When `ai_enabled: true`, the app coordinates with `detection_summary_app` via:

1. **Event-driven** (preferred): Listens for `detection_summary/run_started` events and waits for the specific `run_id` to complete.
2. **Window-based** (fallback): Searches the bundle store for bundles created within the door event time window.

The generated illustration is attached as an image in the push notification.

## Upstream dependencies

- `detection_summary_app` — provides AI detection bundles (optional, only when `ai_enabled: true`)

## Manual setup

- Mobile app notify services must be configured in Home Assistant.
- No HA entities need to be created manually.
