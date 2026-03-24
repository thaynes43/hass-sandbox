# AppDaemon Apps

## Installed apps

| App | Description |
|-----|-------------|
| **door_notify** | Door open/close push notifications with optional AI image attachment |
| **detection_summary_app** | Motion-triggered detection pipeline: capture, AI scoring, publish |
| **detection_summary_viewer** | Dashboard viewer for detection summary bundles |
| **photo_frame_viewer** | Photo slideshow on Lovelace dashboards (wall displays) |
| **dashboard_notify** | AI-generated notification carousel for wall displays |
| **calendar_from_schedule_app** | Sync YAML maintenance schedules to HA local calendar |
| **immich_fetcher** | Periodic photo fetching from Immich photo library |
| **school_lunch_app** | Fetch daily school lunch menus and publish to HA sensor |
| **vestaboard_controller_app** | Vestaboard controller with frame queue, TTL/expiration, and board automations |
| **vestaboard_configuration_app** | Configuration bridge between Lovelace card and Vestaboard controller |

## Shared providers

| Provider | Description |
|----------|-------------|
| **ai_providers** | LLM and image generation adapters (OpenAI, Gemini, Ollama, ComfyUI) |
| **ha_provisioner** | Idempotent HA entity provisioning (scripts, helpers) |
| **photo_providers** | Photo source abstraction (Immich implementation) |
| **school_menu** | Async client for the School Nutrition and Fitness API |
| **vestaboard** | Vestaboard local API client and character encoding |

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

calendar_from_schedule_app (standalone — reads YAML, writes to HA calendar)

school_lunch_app (standalone — fetches school menus, publishes to HA sensor)

vestaboard_controller_app
  └─ controls physical Vestaboard via provider
       └─ vestaboard_configuration_app (forwards user commands)
```

!!! note "Per-app documentation"
    Detailed documentation for each app lives in the app's README:
    `appdaemon/apps/<app_name>/README.md`
