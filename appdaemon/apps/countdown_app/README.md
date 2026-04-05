# Countdown App

AppDaemon app that manages multiple countdowns with AI-generated background images. Each countdown has a configurable target datetime, image prompt, and text overlay styling. Drives a wall-display card that rotates through active countdowns with cross-fade transitions.

## How It Works

1. On startup, provisions a relay script (`script.countdown_relay`), creates the media directory, and loads persisted countdown state from a JSON file.
2. Publishes all countdown data to `sensor.countdown_status` — the main state is the active countdown's text (e.g., "15D 5H 23M"), with the full countdown list in attributes.
3. When a user creates/edits a countdown in the config card, the card sends commands via the relay script to the app, which updates state and republishes the sensor.
4. Image generation is triggered on demand by the user. The app calls the configured AI image provider (OpenAI by default) with the user's prompt, saves the result to `/media/countdown-app/`, and calls a shell command to sync to `/config/www/countdown-app/`.
5. A periodic timer (default 60s) refreshes countdown text values so the display stays current. A second timer (default 15s) rotates the active countdown index.
6. Countdowns show "NOW" for 24 hours after the target datetime, then automatically hide from rotation (but remain in the config card for editing or deletion).

## Architecture

```
┌───────────────────────────────────────────────────┐
│  countdown_app (AppDaemon)                        │
│                                                   │
│  ┌───────────────────────────────────────┐        │
│  │ AI Image Provider (OpenAI)            │        │
│  │ providers/ai_providers/               │        │
│  │ - text-to-image generation            │        │
│  └──────────────┬────────────────────────┘        │
│                  │ generated images                │
│                  ▼                                 │
│  ┌───────────────────────────────────────┐        │
│  │ /media/countdown-app/                 │        │
│  │ - countdowns.json (state)             │        │
│  │ - <uuid>.png (generated images)       │        │
│  └──────────────┬────────────────────────┘        │
│                  │ shell_command sync              │
│                  ▼                                 │
│  /config/www/countdown-app/ (served by HA)        │
│                                                   │
│  ┌───────────────────────────────────────┐        │
│  │ sensor.countdown_status               │        │
│  │ state: "15D 5H 23M" | "NOW" | "idle" │        │
│  │ attributes:                           │        │
│  │   active_countdown: {...}             │        │
│  │   countdowns: [{...}, ...]            │        │
│  │   visible_count: N                    │        │
│  │   active_index: N                     │        │
│  └──────────────┬────────────────────────┘        │
└─────────────────┼─────────────────────────────────┘
                  │ HA WebSocket
                  ▼
┌───────────────────────────────────────────────────┐
│  Lovelace Cards                                   │
│  - countdown-card.js (wall display card)          │
│  - countdown-config-card.js (popup editor)        │
│                                                   │
│  Reads: sensor.countdown_status                   │
│  Sends: commands via script.countdown_relay       │
└───────────────────────────────────────────────────┘
```

## File Layout

```
appdaemon/apps/countdown_app/
├── __init__.py
├── countdown_app.py           # Main AppDaemon app
├── cards/
│   ├── countdown-card.js      # Wall display card (16:9, cross-fade rotation)
│   └── countdown-config-card.js  # Config popup (CRUD, image gen, text styling)
└── README.md
```

## Self-Provisioned Entities

| Entity | Type | Purpose |
|--------|------|---------|
| `sensor.countdown_status` | Virtual sensor | Countdown data for dashboard cards |
| `script.countdown_relay` | Script | Card → AppDaemon command relay |

## Associated Cards

| Card | File | Purpose |
|------|------|---------|
| `countdown-card` | `cards/countdown-card.js` | 16:9 display card with image backgrounds, auto-rotate, swipe nav |
| `countdown-config-card` | `cards/countdown-config-card.js` | Popup for managing countdowns, generating images, styling text |

## Config Reference

| Key | Default | Description |
|-----|---------|-------------|
| `ha_url` | (required) | Home Assistant base URL |
| `ha_token_env` | (required) | Env var name for HA long-lived access token |
| `ai_provider_conf.image` | `openai-default` | Image generation provider bundle |
| `media_fs_root_env` | `MEDIA_FS_ROOT` | Env var for media filesystem root |
| `media_fs_root` | `/media` | Direct media root path (dev override) |
| `media_subdir` | `countdown-app` | Subdirectory under media root for images |
| `www_subdir` | `countdown-app` | Subdirectory for `/local/` URL construction |
| `image_sync_shell_command` | `countdown_sync_images` | Shell command to sync images to www |
| `rotation_interval_s` | `15` | Seconds between auto-rotation ticks |
| `countdown_refresh_s` | `60` | Seconds between countdown text refreshes |

## Manual Setup Required

### Shell Command

Add to your HA `configuration.yaml` (or `packages/shell_commands.yaml`):

```yaml
shell_command:
  countdown_sync_images: >-
    /bin/sh -c 'set -e;
    src="/media/countdown-app";
    dest="/config/www/countdown-app";
    mkdir -p "$dest";
    for f in "$dest"/*.png; do
      [ -f "$f" ] || continue;
      bn=$(basename "$f");
      [ -f "$src/$bn" ] || rm -f "$f";
    done;
    [ -n "$(ls -A "$src" 2>/dev/null)" ] && cp -f "$src"/*.png "$dest/"'
```

### Lovelace Resources

Register both card JS files as Lovelace resources:

| URL | Type |
|-----|------|
| `/local/countdown/countdown-card.js?v=1` | `module` |
| `/local/countdown/countdown-config-card.js?v=1` | `module` |

### Dashboard Cards

Display card (in a wall-display view section):
```yaml
type: custom:countdown-card
status_entity: sensor.countdown_status
navigation_path: "#countdown-popup"
```

Config card (in a popup view):
```yaml
type: custom:countdown-config-card
status_entity: sensor.countdown_status
relay_script: countdown_relay
```

## Relay Commands

| Command | Payload | Description |
|---------|---------|-------------|
| `save_countdown` | `{id?, title, subtitle, target_datetime, image_prompt, text_style}` | Create or update a countdown |
| `delete_countdown` | `{id}` | Delete a countdown and its image |
| `generate_image` | `{id, prompt?}` | Generate AI background image |
| `update_style` | `{id, text_style: {font_size, color, position_y, text_shadow}}` | Update text overlay style |
| `set_active` | `{index}` | Set the active countdown rotation index |

## Dependencies

- `providers/ai_providers/` — Image generation (OpenAI adapter)
- `providers/ha_provisioner/` — Self-provisioning relay script
- `providers/secrets.py` — Runtime secret resolution
