# AppDaemon documentation standards

> **Applies to:** `appdaemon/**`

Every app and provider package must have a README.md. This rule defines what goes in each README and maintains a map of all documentation in the project.

## README requirements by location

### App READMEs (`appdaemon/apps/<app_name>/README.md`)

Every app directory must have a README.md covering:

1. **What the app does** — 1-2 sentence summary
2. **How it works** — high-level flow (numbered steps)
3. **Dependencies** — providers and other shared libraries used
4. **Self-provisioned entities** — table of HA entities created on startup (entity ID pattern, type, purpose)
5. **Associated card** — JS card filename if the app has a Lovelace card
6. **Config reference** — required and optional config keys with defaults (from apps.yaml)
7. **Manual setup required** — anything the provisioner cannot create (shell commands, `local_file` cameras, directories, Lovelace resources)
8. **Upstream/downstream dependencies** — other apps this app depends on or that depend on it

All apps should live in a package directory (e.g. `appdaemon/apps/door_notify/door_notify.py`) rather than as standalone files in `apps/`. This keeps the layout consistent and ensures every app has a place for its README.

### Provider READMEs

**Interface-level README** (`appdaemon/providers/<provider_group>/README.md`):
- What the provider group does
- The protocol/interface contracts
- Table of implementations with capability summary
- Links to per-implementation READMEs

**Per-implementation README** (`appdaemon/providers/<provider_group>/<impl>/README.md`):
- What this specific implementation does
- Supported capabilities
- Current limitations
- Default models/settings
- Dependencies

### Documentation site (`docs/`)

The `docs/` directory is a **mkdocs-material** static site (built with `mkdocs serve` / `mkdocs build`). This is the human-facing documentation published to GitHub Pages. Content here should be polished and reader-friendly.

- `mkdocs.yml` at the repo root defines site structure and navigation
- Add new pages to `docs/` and update the `nav:` section in `mkdocs.yml`
- Build locally: `mkdocs serve` (from repo root with venv active)
- Deploy: `mkdocs gh-deploy`

### Agent-facing reference docs (`agent-docs/`)

Internal reference material for agents and developers — roadmaps, setup guides, button mappings. Not published to the doc site.

## Documentation map

Update this map when adding new apps, providers, or docs. Agents creating new apps or providers must add the README to this map.

### Apps

| App | README | Description |
|-----|--------|-------------|
| `detection_summary_app` | `appdaemon/apps/detection_summary_app/README.md` | Motion-triggered detection pipeline: capture, score, publish |
| `detection_summary_viewer` | `appdaemon/apps/detection_summary_viewer/README.md` | Dashboard viewer for detection summary bundles |
| `immich_fetcher` | `appdaemon/apps/immich_fetcher/README.md` | Periodic photo fetching from Immich |
| `photo_frame_viewer` | `appdaemon/apps/photo_frame_viewer/README.md` | Photo slideshow on Lovelace dashboards |
| `door_notify` | `appdaemon/apps/door_notify/README.md` | Door open/close push notifications with optional AI attachment |
| `calendar_from_schedule_app` | `appdaemon/apps/calendar_from_schedule_app/README.md` | Sync YAML maintenance schedules to HA local calendar |
| `dashboard_notify` | `appdaemon/apps/dashboard_notify/README.md` | AI-generated notification carousel for wall displays |
| `school_lunch_app` | `appdaemon/apps/school_lunch_app/README.md` | Fetch daily school lunch menus and publish to HA sensor |
| `vestaboard_controller` | `appdaemon/apps/vestaboard_apps/vestaboard_controller/README.md` | Vestaboard board controller with FIFO frame queue and dynamic automation registration |
| `vestaboard_configuration` | `appdaemon/apps/vestaboard_apps/vestaboard_configuration/README.md` | Vestaboard configuration bridge: frame library CRUD, card ↔ controller communication |
| `calendar_clock` | `appdaemon/apps/vestaboard_apps/automations/calendar_clock/README.md` | Calendar grid + clock display, updated every 60 seconds |
| `calendar_summary` | `appdaemon/apps/vestaboard_apps/automations/calendar_summary/README.md` | Upcoming HA calendar events with countdown (multi-instance) |
| `messages_from_library` | `appdaemon/apps/vestaboard_apps/automations/messages_from_library/README.md` | Random messages from frame library with curated fallback |
| `art_from_library` | `appdaemon/apps/vestaboard_apps/automations/art_from_library/README.md` | Random pixel art from bundled art_library.json |
| `ai_art_generator` | `appdaemon/apps/vestaboard_apps/automations/ai_art_generator/README.md` | LLM-generated pixel art with grid validation and retry |
| `ai_message_generator` | `appdaemon/apps/vestaboard_apps/automations/ai_message_generator/README.md` | LLM-generated witty messages with curated fallback |
| `weather_schedule` | `appdaemon/apps/vestaboard_apps/automations/weather_schedule/README.md` | Weather summary displayed on a daily schedule |
| `health_checks` | `appdaemon/apps/health_checks/README.md` | System health monitoring: controller + network protocol checkers + dashboard cards |
| `media_dashboard_app` | `appdaemon/apps/media_dashboard_app/README.md` | Media dashboard: Plex new arrivals, in-theaters, coming-soon with showtimes and thumbs up/down |
| `countdown_app` | `appdaemon/apps/countdown_app/README.md` | Multiple countdowns with AI-generated backgrounds, auto-rotation, text styling |

### Providers

| Provider | README | Description |
|----------|--------|-------------|
| `ai_providers` (interface) | `appdaemon/providers/ai_providers/README.md` | LLM + image generation provider layer |
| `ai_providers/openai` | `appdaemon/providers/ai_providers/openai/README.md` | OpenAI adapter (text, multimodal, image) |
| `ai_providers/gemini` | `appdaemon/providers/ai_providers/gemini/README.md` | Google Gemini adapter (text, multimodal, image) |
| `ai_providers/ollama` | `appdaemon/providers/ai_providers/ollama/README.md` | Ollama local adapter (text, multimodal) |
| `ai_providers/comfyui` | `appdaemon/providers/ai_providers/comfyui/README.md` | ComfyUI local adapter (image) |
| `ha_provisioner` | `appdaemon/providers/ha_provisioner/README.md` | Idempotent HA entity provisioning (scripts, helpers) |
| `photo_providers` | `appdaemon/providers/photo_providers/README.md` | Photo source abstraction (Immich implementation) |
| `school_menu` | `appdaemon/providers/school_menu/README.md` | Async client for the School Nutrition and Fitness API |
| `media_providers` | `appdaemon/providers/media_providers/README.md` | HTTP clients and fetchers for Tautulli, TMDb, and MovieGlu |
| `vestaboard` | `appdaemon/providers/vestaboard/README.md` | Vestaboard local API client and character encoding |

### Repository docs (`agent-docs/`)

| Document | Description |
|----------|-------------|
| `agent-docs/appdaemon-setup.md` | AppDaemon infrastructure setup guide |
| `agent-docs/appdaemon-testing.md` | Testing strategy and conventions |
| `agent-docs/wall-display-photo-frame-viewer-setup.md` | Wall display photo frame end-to-end setup |
| `agent-docs/button-mappings.md` | Switch button mapping reference (must stay in sync with automations) |
| `agent-docs/roadmap.md` | General project roadmap |
| `agent-docs/image-view-roadmap.md` | Image viewing feature roadmap |
| `agent-docs/appdaemon-app-decoupling.md` | Event-based app decoupling pattern for split dev/prod deployment |

### Root

| Document | Description |
|----------|-------------|
| `appdaemon/README.md` | AppDaemon root: deploy process, dev vs prod config |

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

vestaboard_controller (vestaboard_apps/vestaboard_controller)
  └─ controls physical Vestaboard via provider
  └─ automation apps register via HA events (vestaboard_controller_command)
  |    — no AppDaemon dependencies: entries needed
  |    — automations can run in a different AppDaemon instance
       ├─ calendar_clock            fires register_automation + push_automation_frame
       ├─ calendar_summary          fires register_automation + push_automation_frame (one instance per calendar entity)
       ├─ messages_from_library     fires register_automation + push_automation_frame + update_next_fire_time
       ├─ art_from_library          fires register_automation + push_automation_frame + update_next_fire_time
       ├─ ai_art_generator          fires register_automation + push_automation_frame + push_ai_art_preview_result + update_next_fire_time
       ├─ ai_message_generator      fires register_automation + push_automation_frame + update_next_fire_time
       └─ weather_schedule          fires register_automation + push_automation_frame
  └─ fires vestaboard_controller_ready on startup (automations re-register automatically)
  └─ vestaboard_configuration (reads status sensor, forwards card commands via vestaboard_controller_command event)

media_dashboard_app (standalone — fetches from Tautulli, TMDb, SerpApi; publishes sensors for compact and detail Lovelace cards)

health_check_controller (listens for health_check_command events from all checkers)
  ├─ cloud_checker/cloud (root dependency)
  │    └─ depended on by: cielo, lock_batteries
  ├─ mqtt_broker_checker/mqtt_broker (root dependency)
  │    └─ depended on by: zigbee, basement_lights, downstairs_lights, upstairs_lights, exterior_lights
  ├─ network_protocol_checker/zigbee (depends on: mqtt_broker)
  │    └─ depended on by: basement_lights, downstairs_lights, upstairs_lights, exterior_lights, zigbee_batteries
  ├─ network_protocol_checker/zwave (root dependency)
  │    └─ depended on by: zwave_batteries
  ├─ mqtt_device_checker (4 instances: basement/downstairs/upstairs/exterior_lights, depends on: zigbee + mqtt_broker)
  ├─ battery_checker (6 instances: zwave/shade/lock/airthings/protect/zigbee_batteries)
  ├─ ups_checker/ups
  ├─ device_checker/vestaboard
  ├─ repairable_device_checker/printer
  ├─ device_group_checker/cielo (depends on: cloud)
  ├─ fan_health_checker/fans
  ├─ spa_health_checker/spa (depends on: cloud)
  └─ temp_humidity_checker/cigar_humidity (per-sensor deps: zwave, zigbee)

countdown_app
  └─ depends on: ai_providers (image generation), ha_provisioner (relay script provisioning)
```

## When creating a new app or provider

1. Create the README.md following the requirements above.
2. Add an entry to the documentation map in this file.
3. If the new app depends on or is depended on by other apps, update the dependency graph.
4. If the app requires cross-cutting setup documentation, add a guide to `agent-docs/`.
