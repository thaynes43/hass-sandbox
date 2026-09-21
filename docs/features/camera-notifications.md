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

### Stylised images

Between scoring and publishing, the best frames of the event are turned into a
single stylised illustration — the picture that actually lands on your phone.
That render runs on a local ComfyUI box.

Which ComfyUI *workflow* does the rendering is named in the AppDaemon config,
so every camera's look is pinned to a specific, versioned graph rather than to
whatever the box happened to have loaded. Each workflow in the registry records
when its model was released and what to expect from it — how the output looks,
and roughly how long a render takes.

The default is Qwen-Image-2.1, which produces clean restyles that keep the
scene, people and vehicles where they are, in roughly a minute per image. It is
given up to three of the run's frames rather than one: a person can be half out
of shot in the best frame and plain in the next, and the extra frames let the
model get them right — on this model they cost about five seconds. The older
Qwen-Image-Edit-2509 workflows are still registered and render a little faster
from one frame, at the cost of harsher colour; on that larger model extra
frames are expensive, so they are normally given just the best one.

How many frames a workflow takes is something the app asks it before it builds
the prompt, so the notes it writes about the scene ("Image 1 … Image 2 …")
describe exactly the frames the model is looking at — the best one first. A
run that turns up a fourth good frame simply drops it rather than telling the
model about a picture it was never sent.

Changing a camera's look, or rolling one back, is a config change in a normal
release; a single-frame Qwen-Image-2.1 entry and the original pre-2026-09 graph
are both kept unchanged for exactly that purpose. Which workflow produced any
given image is recorded in that run's bundle.

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
  │    (up to 3 frames; workflow named in the app's config)
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
