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

### Stylised images, switchable from Home Assistant

Between scoring and publishing, the best frame is turned into a stylised
illustration — the picture that actually lands on your phone. That render runs
on a local ComfyUI box, and which ComfyUI *workflow* it uses is a Home Assistant
setting, not a code change.

Three helpers control it, and the app creates all three itself:

| Helper | What it does |
|---|---|
| `input_select.comfyui_active_workflow` | The workflow every camera uses |
| `input_select.comfyui_trial_workflow` | The workflow a camera uses while it is trialling |
| `input_boolean.<camera>_detection_summary_trial_workflow` | Puts that one camera on the trial workflow |

So trying out a new look is: pick it in **Trial**, turn on the trial toggle for
one camera, and walk past that camera. Every other camera keeps rendering
exactly as before. If the result is better, set **Active** to the same workflow
and turn the toggle back off. If it is worse, change **Active** back — the
`qwen2509-original` entry is the original look, kept byte-for-byte for exactly
that reason.

Nothing restarts and nothing redeploys; the next detection uses the new setting.
The workflows differ mostly in how many reference frames they use and how long
they take — a single frame renders in about a minute, three frames in about
four. Which workflow produced any given image is recorded in that run's bundle.

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
  ├─ renders a stylised image on ComfyUI
  │    (workflow picked from the HA selects above)
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
