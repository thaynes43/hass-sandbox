# Dashboard Notify

AI-generated notification carousel for wall displays. Shows scheduled notifications with AI-generated images, detection summary events, and an idle placeholder — all in an auto-advancing carousel.

## How It Works

1. **Scheduled notifications** — YAML config defines notifications with time windows, text, and prompt hints. When a schedule is active, the app generates an AI image and adds it to the carousel.
2. **Detection summary hook** — Listens for `detection_summary/run_published` events and adds detection images as notifications.
3. **Placeholder** — When no notifications are active, generates a calming AI placeholder image.
4. **Carousel** — Auto-advances through active notifications. Card supports swipe, pause, dismiss.

## Architecture

```
dashboard_notify_app.py      # Main AppDaemon app (lifecycle, scheduling, generation)
notification_manager.py      # Notification pool (add/remove/prune/priority)
prompt_builder.py            # Prompt construction with style variants
dashboard-notify-card.js     # Custom Lovelace carousel card
```

State is published to `sensor.dashboard_notify_status`. The card reads from this sensor and sends commands via `script.dashboard_notify_relay`.

## Self-Provisioned Entities

- `script.dashboard_notify_relay` — Card-to-AppDaemon relay script

## Manual Setup Required

### 1. Shell Command (`configuration.yaml`)

```yaml
shell_command:
  dashboard_notify_stage: >-
    /bin/sh -c 'set -e;
    dest="/config/www/dashboard-notify";
    src="/media/dashboard-notify/staged";
    mkdir -p "$dest";
    for f in "$dest"/*.png; do
      [ -f "$f" ] || continue;
      bn=$(basename "$f");
      [ -f "$src/$bn" ] || rm -f "$f";
    done;
    [ -n "$(ls -A "$src" 2>/dev/null)" ] && cp -f "$src"/* "$dest/"'
```

The shell command syncs `/config/www/` to match `/media/<app>/staged/` — it removes `.png` files from www that are no longer in staged, then copies current staged files. This keeps `/config/www/` lean (only actively displayed images + the card JS).

### 2. Lovelace Resource

```yaml
url: /local/dashboard-notify/dashboard-notify-card.js?v=1
type: module
```

### 3. Media Directory

Ensure `/media/dashboard-notify/` exists (the app creates subdirectories automatically).

### 4. Card YAML

```yaml
type: custom:dashboard-notify-card
status_entity: sensor.dashboard_notify_status
relay_script: dashboard_notify_relay
```

## Notification Classes

| Class | Priority | Use Case |
|-------|----------|----------|
| `UrgentImage` | 100 | Time-sensitive alerts |
| `BasicTextImage` | 50 | Standard scheduled notifications |
| `FunPictureImage` | 25 | Playful/humorous reminders |
| `PreexistingImage` | 10 | Detection summary events |

## Configuration

See `apps-prod.yaml` for full config. Key settings:

- `media_fs_root` — Local filesystem root mapping to HA's `/media` (default: `/media`; override in dev to e.g. `/mnt/cephfs-hdd/misc/hass-media`)
- `carousel_interval_s` — Auto-advance interval (default: 10s)
- `default_ttl_s` — Default notification TTL (default: 1hr)
- `no_notification_refresh_s` — Placeholder refresh interval (default: 1hr)
- `notifications` — List of scheduled notification configs
- `detection_summary_hook` — Detection event integration settings
