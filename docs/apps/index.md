# AppDaemon Apps

## Installed apps

| App | Description |
|-----|-------------|
| **door_notify** | Door open/close push notifications with optional AI image attachment |
| **detection_summary_app** | Motion-triggered detection pipeline: capture, AI scoring, publish |
| **detection_summary_viewer** | Dashboard viewer for detection summary bundles |
| **photo_frame_viewer** | Photo slideshow on Lovelace dashboards (wall displays) |
| **dashboard_notify** | AI-generated notification carousel for wall displays |
| **immich_fetcher** | Periodic photo fetching from Immich photo library |

## Shared providers

| Provider | Description |
|----------|-------------|
| **ai_providers** | LLM and image generation adapters (OpenAI, Gemini, Ollama, ComfyUI) |
| **ha_provisioner** | Idempotent HA entity provisioning (scripts, helpers) |
| **photo_providers** | Photo source abstraction (Immich implementation) |

## App dependency graph

```
immich_fetcher
  └─ writes photos to disk
       └─ photo_frame_viewer (reads from same directory)

detection_summary_app
  └─ fires detection_summary/run_published event
  └─ writes bundles to shared filesystem
       └─ detection_summary_viewer (listens for events, reads filesystem)
       └─ door_notify (optional: attaches AI summary to notifications)
       └─ dashboard_notify (listens for events, copies generated images)
```

!!! note "Per-app documentation"
    Detailed documentation for each app lives in the app's README:
    `appdaemon/apps/<app_name>/README.md`
