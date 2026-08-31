# Architecture Overview

## System diagram

```
┌─────────────┐     callService      ┌──────────────────┐
│  Lovelace   │ ──────────────────▶  │  Home Assistant   │
│  Cards (JS) │                      │                   │
│             │ ◀── sensor state ──  │  helpers, scripts │
└─────────────┘                      │  automations      │
                                     └────────┬──────────┘
                                              │
                                     HASS plugin API
                                     (listen_state, call_service,
                                      fire_event, get_state)
                                              │
                                     ┌────────▼──────────┐
                                     │    AppDaemon       │
                                     │                    │
                                     │  apps/             │
                                     │  ├─ door_notify    │
                                     │  ├─ detection_*    │
                                     │  ├─ photo_frame_*  │
                                     │  ├─ dashboard_notify│
                                     │  ├─ calendar_from_*│
                                     │  ├─ immich_fetcher │
                                     │  ├─ health_checks  │
                                     │  ├─ media_dashboard│
                                     │  ├─ countdown_app  │
                                     │  └─ school_lunch_* │
                                     │                    │
                                     │  providers/        │
                                     │  ├─ ai_providers   │
                                     │  ├─ ha_provisioner │
                                     │  ├─ photo_providers│
                                     │  ├─ media_providers│
                                     │  ├─ school_menu    │
                                     │  └─ alertmanager   │
                                     └───────────────────-┘
                                              │
                                     External APIs
                                     (OpenAI, Gemini, Ollama,
                                      ComfyUI, Immich,
                                      School Nutrition and Fitness,
                                      Tautulli, TMDb, SerpApi,
                                      Alertmanager)
```

## Data flow paths

| Path | Direction | Mechanism |
|------|-----------|-----------|
| **Commands** (card → app) | Card → HA → AppDaemon | `callService("script", "relay")` → HA event → `listen_event()` |
| **State** (app → card) | AppDaemon → HA → Card | `set_state()` on sensor → card reads attributes |
| **External APIs** | AppDaemon → Internet | Provider adapters in `providers/` make HTTP calls |
| **Provisioning** | AppDaemon → HA REST API | `ha_provisioner` creates helpers/scripts on startup |

## Key concepts

### Self-provisioning

Apps create their own HA entities (helpers, scripts) on startup via `ha_provisioner`. No manual entity setup is needed.

### Relay script pattern

Lovelace cards communicate with AppDaemon through relay scripts:

1. Card calls `hass.callService("script", "<app>_relay", { command, payload })`
2. HA script fires an `<app>_command` event
3. AppDaemon listens for the event and processes it

This pattern works for non-admin users (unlike `fire_event` which requires admin).

### Health monitoring

The [health check system](../features/health-checks.md) uses the event bus as a decoupling layer between checker apps and a central controller. Checker apps register themselves and report status via HA events; the controller aggregates everything into a single sensor that custom Lovelace cards read. This pattern allows new checkers to be added — often config-only — without modifying the controller. Repair-capable checkers handle their own recovery logic (e.g., smart switch power cycling, config-entry reloads) while the controller only routes commands. The controller also mirrors checker status into the cluster's Alertmanager via the `alertmanager` provider — critical findings page the phone, and recovery resolves the alert once it holds, so a flapping condition pages [once per incident](../features/health-checks.md#one-page-per-incident) rather than once per swing.

### Container separation

HA and AppDaemon run in separate Kubernetes pods. They share a `/media` NFS mount for file exchange. AppDaemon cannot access `/config/www/` directly — it uses HA `shell_command` services for file operations in that directory.
