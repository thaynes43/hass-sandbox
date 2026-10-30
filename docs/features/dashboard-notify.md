# Dashboard Notify

An AI-generated notification carousel for wall displays. Scheduled notifications, motion detection events, and an idle placeholder rotate through an auto-advancing carousel — each backed by a freshly generated image.

<!-- TODO: Add screenshot of wall display showing the notification carousel -->

## Overview

Wall-mounted displays show a live carousel of notifications, each paired with an AI-generated image. Three content sources feed the carousel:

1. **Scheduled notifications** — YAML config defines time-windowed notifications with custom text and a scene hint. When a schedule is active, the app generates an image via the configured AI provider and adds it to the pool.
2. **Detection summary hook** — Listens for `detection_summary/run_published` events from `detection_summary_app` and injects the detection's generated image as a carousel entry.
3. **Idle placeholder** — When no notifications are active, a calming AI-generated image fills the display. It refreshes on a configurable interval.

The card supports swipe gestures, manual pause, and per-notification dismiss.

## Architecture

```
detection_summary_app
  └─ fires detection_summary/run_published
       └─ dashboard_notify_app (AppDaemon)
            │  generates images → /media/dashboard-notify/
            │  stages to /config/www/dashboard-notify/ via shell_command
            ▼
       sensor.dashboard_notify_status
            │  carousel state + image URLs
            ▼
       custom:dashboard-notify-card (Lovelace)
```

## Image generation

Images are generated using the configured AI provider. Production renders them locally on ComfyUI with Qwen-Image-2.1 text-to-image, the same model that draws the detection summaries, so each image takes about 35 seconds and costs nothing. Each image is built from:

- The scene hint (or, with no hint, the notification's theme as pictures only; the card prints the message text below the image)
- A randomly selected **style profile** (cartoon, watercolor, pixel art, etc.)
- A randomly selected **environment variant** (space, underwater, enchanted forest, etc.)

Both layers are applied as secondary appearance/setting modifiers — subject content is always preserved.

A failed render is retried, and after a restart the last idle image already on disk is shown until a new one is ready, so a restart or a provider outage does not blank the card.

## Dashboard card

The `custom:dashboard-notify-card` element provides the carousel UI. It reads from `sensor.dashboard_notify_status` and sends commands (dismiss, pause, navigate) via `script.dashboard_notify_relay`.

```yaml
type: custom:dashboard-notify-card
status_entity: sensor.dashboard_notify_status
relay_script: dashboard_notify_relay
```

## Configuration

See `appdaemon/apps/dashboard_notify/README.md` for the full configuration reference, including scheduled notification definitions, detection hook settings, and AI provider configuration.
