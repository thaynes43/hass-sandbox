# Laundry Notify

Push notifications when the washer and dryer finish, combined into one when that is the useful thing to know, plus a reminder when wet clothes have been left in the washer.

## How it works

The app watches the LG ThinQ status sensors (`sensor.washer_current_status`, `sensor.dryer_current_status`). A machine **finishes** when its status goes from an active state (`running`, `rinsing`, `spinning`, `cooling`, `pause`, …) to `end`. The dryer's `wrinkle_care` tumble counts as `end`. `unavailable`/`unknown` are ignored, so a cloud blip mid-cycle does not hide the finish and a reconnect does not fake one.

| What happens | Notification |
|---|---|
| Washer finishes, dryer idle | **Washer complete** |
| Washer finishes while the dryer runs | nothing yet: a wet load can't move until the dryer is free. One **Washer & dryer complete** when the dryer finishes |
| …and the dryer is cancelled instead of finishing | **Washer complete** at that moment |
| …and the washer is emptied before the dryer finishes | the dryer then sends a plain **Dryer complete** |
| Dryer finishes while the washer runs | **Dryer complete** right away (`dryer_first_policy: notify_now`, Tom's pick on 2026-11-27), so it can be emptied. With `wait_for_washer` it is held for one combined push |
| Dryer finishes, washer idle | **Dryer complete** |

### Wet-clothes reminder

Every notification that tells someone the washer is done arms a one-shot timer (`washer_reminder_minutes`, default 120). If the washer is still in `end` when it fires, the app sends **Washer still needs switching** with how long the clothes have sat. The washer has no door sensor, so "opened" is the washer leaving `end`. In practice it powers off when the door is opened, and starting a new cycle counts too. Either cancels the reminder.

### Restarts

AppDaemon re-initialises the app on its own restart and every time Home Assistant restarts. On start the app reads the current states and sends nothing. It also rebuilds a hold that was in progress: a washer in `end` while the dryer runs is still waiting for the combined push.

The reminder episode (when the washer finished, when the reminder is due, whether it was sent) is saved in `laundry_notify_state.json` in the AppDaemon config dir (`state_file` overrides). A Home Assistant restart resets `last_changed`, so without that file a reminder could be sent twice and report the wrong wait. With the file:

- a reminder that was already sent is not sent again;
- a pending one keeps its original due time;
- one that fell due during the downtime is sent on start.

The file lives in the pod's emptyDir, so a pod restart loses it. The app then falls back to `last_changed` and skips a reminder whose time has passed. If AppDaemon starts while a machine is `unavailable` and it comes back as `end`, that finish is not reported, because the app never saw it running. A washer that comes back sitting in `end` still gets its saved reminder.

## Structure

- `coordinator.py`: pure state machine (`LaundryCoordinator`), no AppDaemon import; returns `Action`s.
- `laundry_notify.py`: AppDaemon app; listeners, timer, notify calls, message text.

## Dependencies

None: no providers, no `ha_provisioner`, no other apps. It creates no HA entities.

## Config (apps.yaml)

```yaml
laundry_notify:
  module: laundry_notify.laundry_notify
  class: LaundryNotify
  washer_status_entity: sensor.washer_current_status
  dryer_status_entity: sensor.dryer_current_status
  notify_services:
    - notify.mobile_app_toms_iphone_15_pro
  dryer_first_policy: notify_now
  washer_reminder_minutes: 120
```

| Key | Default | Description |
|---|---|---|
| `washer_status_entity` | `sensor.washer_current_status` | Washer status sensor |
| `dryer_status_entity` | `sensor.dryer_current_status` | Dryer status sensor |
| `notify_services` | Tom's two phones + Kellie's | `notify.mobile_app_*` services |
| `dryer_first_policy` | `notify_now` | `notify_now` or `wait_for_washer` |
| `washer_reminder_minutes` | `120` | Minutes after the washer push before the reminder |
| `notification_url` | `""` | Optional Companion App deep link |
| `state_file` | `<config_dir>/laundry_notify_state.json` | Reminder episode saved across restarts |

Pushes use tag `laundry_washer` (washer, combined and reminder: each replaces the last) or `laundry_dryer`, grouped under `laundry`.

## Upstream / downstream

- Upstream: LG ThinQ integration (washer, dryer devices).
- Downstream: none.

## Manual setup

None beyond the mobile app notify services, which already exist.
