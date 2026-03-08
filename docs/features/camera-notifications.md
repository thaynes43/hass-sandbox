# GenAI Camera Notifications

AI-powered motion detection summaries and door open/close alerts with camera snapshots.

<!-- TODO: Add screenshot of a detection summary push notification -->

## Overview

When a camera detects motion or a door opens, the system captures a snapshot, runs it through an LLM for analysis, and delivers a rich push notification to your phone — all within seconds.

Two AppDaemon apps work together to make this happen:

### Detection Summary

The `detection_summary_app` is the backbone of the camera notification pipeline:

1. **Trigger** — a camera's motion sensor fires
2. **Capture** — HA takes a snapshot from the camera
3. **Score** — the snapshot is sent to a multimodal LLM (Gemini, OpenAI, or Ollama) which describes what it sees
4. **Publish** — the summary bundle (image + AI description + metadata) is written to the shared filesystem and an HA event is fired

The `detection_summary_viewer` provides a Lovelace dashboard card for browsing historical detection bundles — useful for reviewing what happened while you were away.

### Door Notifications

The `door_notify` app listens for door open/close events (both `binary_sensor` and `cover` entities) and sends push notifications. It optionally attaches the most recent AI detection summary from a nearby camera, giving you context like *"Person walking up driveway"* alongside the *"Garage door opened"* alert.

Key features:

- **Consolidation window** — rapid open/close events are batched into a single notification
- **AI attachment** — when a detection summary is available for the door's camera, the AI description and snapshot are included in the notification
- **Multiple doors** — each door gets its own app instance with independent config

## Architecture

```
Camera motion sensor
  │
  ▼
detection_summary_app
  ├─ captures snapshot via HA
  ├─ sends to multimodal LLM
  ├─ writes bundle to /media/
  └─ fires HA event
        │
        ├──▶ detection_summary_viewer (dashboard card)
        │
        └──▶ door_notify (attaches to push notification)
                │
                ▼
           Mobile push notification
           (image + AI description)
```

## Home Assistant YAML

The HA side of camera notifications lives in:

- `home-assistant/cards/detection-summary/` — Lovelace cards for garage and front door detection viewers
- `home-assistant/automations/open-close-door/` — door state change automations
- `home-assistant/automations/garage/` — garage-specific automations

## Configuration

<!-- TODO: Add config examples and per-entrance setup details -->

See the app READMEs for full configuration:

- `appdaemon/apps/detection_summary_app/README.md`
- `appdaemon/apps/detection_summary_viewer/README.md`
- `appdaemon/apps/door_notify/README.md`
