# AppDaemon architecture decisions

> **Applies to:** `appdaemon/**`

This ruleset captures major architectural decisions for AppDaemon apps in this project. It supplements `appdaemon-coding-guidelines.md` (coding conventions) and the deploy playbook (`.agents/playbooks/appdaemon-deploy.md`).

## 0) System overview: how it all fits together

```
┌──────────────────────────────────────────────────────────────────────┐
│  Browser / Wall Display / Mobile                                     │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  Custom Lovelace Card (.js)                                    │  │
│  │  - Reads entity state via hass.states[entity_id]               │  │
│  │  - Sends commands via hass.callService("script", "<app>_relay")│  │
│  │  - Never calls fire_event (requires admin)                     │  │
│  └──────────────┬─────────────────────────────┬───────────────────┘  │
└─────────────────┼─────────────────────────────┼──────────────────────┘
                  │ callService                 │ read state
                  ▼                             ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Home Assistant (HA)                                                 │
│                                                                      │
│  ┌──────────────┐  ┌────────────┐  ┌──────────────────────────────┐ │
│  │ Relay Script  │  │  Helpers   │  │  Cameras / Sensors / Devices │ │
│  │ script.<app>  │  │ input_*.*  │  │  camera.*, binary_sensor.*   │ │
│  │ _relay        │  │            │  │  local_file cameras          │ │
│  │               │  │ Provisioned│  │                              │ │
│  │ Provisioned   │  │ by app on  │  │  Some provisioned, some     │ │
│  │ by app on     │  │ startup    │  │  manual (local_file, shell)  │ │
│  │ startup       │  │            │  │                              │ │
│  └───────┬───────┘  └────────────┘  └──────────────────────────────┘ │
│          │ fires event                                               │
│          ▼                                                           │
│  ┌─────────────────┐                                                 │
│  │ <app>_command    │                                                 │
│  │ event bus        │                                                 │
│  └───────┬─────────┘                                                 │
└──────────┼───────────────────────────────────────────────────────────┘
           │ listen_event
           ▼
┌──────────────────────────────────────────────────────────────────────┐
│  AppDaemon (Kubernetes pod or local dev)                              │
│                                                                      │
│  ┌──────────────────────────────┐  ┌──────────────────────────────┐ │
│  │  App (appdaemon/apps/<pkg>/) │  │  Providers (appdaemon/       │ │
│  │                              │  │  providers/)                  │ │
│  │  - Extends hass.Hass         │  │                              │ │
│  │  - listen_event / state      │  │  ai_providers/  — LLM calls  │ │
│  │  - set_state (virtual sensor)│  │  ha_provisioner/ — REST/WS   │ │
│  │  - call_service              │  │  photo_providers/ — Immich    │ │
│  │  - Calls providers for AI,   │  │  secrets.py — env var lookup  │ │
│  │    provisioning, photos      │  │                              │ │
│  └──────────────────────────────┘  └──────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────┘
```

### Data flow summary

1. **Card → HA → AppDaemon (commands):** Card calls `hass.callService("script", "<app>_relay", {command, payload})`. The relay script fires an `<app>_command` event. AppDaemon's `listen_event` picks it up and routes to the handler.

2. **AppDaemon → HA → Card (state):** AppDaemon writes state via `self.set_state()` (virtual sensors), `self.call_service("input_text/set_value", ...)`, or `self.call_service("input_select/select_option", ...)`. Cards read these entities reactively via `set hass(hass)`.

3. **AppDaemon → External APIs (AI, photos):** Apps call provider libraries under `appdaemon/providers/` which make HTTP requests to OpenAI, Gemini, Ollama, Immich, etc. All external HTTP lives in providers, never in app code (security rule S2).

4. **AppDaemon → HA (provisioning on startup):** Apps call `ha_provisioner` to create relay scripts and helpers via HA's REST and WebSocket APIs. This requires an elevated long-lived access token, passed as an env var name (`ha_token_env`).

## 1) Folder structure: apps vs shared libraries

- `appdaemon/apps/` — **AppDaemon app modules only** (things with `module:`/`class:` in `apps-prod.yaml` or `apps-dev.yaml`).
- `appdaemon/providers/` — **Shared libraries** used by multiple apps (consolidated under one parent).
  - `appdaemon/providers/ai_providers/` — LLM/provider plumbing.
  - `appdaemon/providers/ha_provisioner/` — HA entity provisioning (scripts, helpers).
  - `appdaemon/providers/photo_providers/` — Photo source provider plumbing (Immich; extensible for Google Photos, Apple Photos).
  - `appdaemon/providers/secrets.py` — Env-var secret resolution (`resolve_secret()`).
- The entire `providers/` tree is deployed into the Docker image and copied to `/conf/apps/providers/` at runtime, importable under prod's `/conf/apps` sys.path.

Do **not** put shared libraries inside `appdaemon/apps/`. Only actual AppDaemon app modules belong there.

### Dev import path

In production, the Docker entrypoint copies shared libraries into `apps/providers/` where they're on AppDaemon's `sys.path`. In dev, they live at `appdaemon/providers/` (sibling of `apps/`), so they're **not** automatically importable. Any app module that imports a shared library must add the AppDaemon root to `sys.path` at module level:

```python
import sys
from pathlib import Path

# AppDaemon only adds `appdaemon/apps` to sys.path. Our shared libraries
# live at `appdaemon/<lib>`, so add the AppDaemon root directory.
sys.path.append(str(Path(__file__).resolve().parents[2]))
```

The `.parents[2]` assumes the standard `appdaemon/apps/<app_name>/module.py` layout. Apps import providers with `from providers.ai_providers...`, `from providers.ha_provisioner...`, etc.

## 2) Self-provisioning: apps create their own HA entities

Apps **must not** assume that helpers, scripts, or other HA entities already exist. Instead, apps provision them on startup using the `ha_provisioner` shared library.

### Why provisioning needs elevated access

The `ha_provisioner` uses HA's REST API (for scripts) and WebSocket API (for helpers) — both require a **long-lived access token** with admin privileges. This token is:

- Stored as an environment variable (e.g. `TOKEN` in Kubernetes via ExternalSecret, or in `.env` for dev)
- Referenced in app config by env var **name** only: `ha_token_env: TOKEN`
- Resolved at runtime by `providers.secrets.resolve_secret()` — never hardcoded, never in YAML values
- Used **server-side only** by AppDaemon — never exposed to the frontend

This is the only part of the system that requires admin-level HA access. The Lovelace cards themselves work with non-admin accounts (see §4).

### What this means in practice

- **Never** instruct users to manually create helpers (`input_button`, `input_text`, `input_boolean`, etc.).
- **Never** instruct users to manually create HA scripts for app communication.
- On startup (in `_async_startup` or equivalent), call `ha_provisioner` to `ensure_script` / `ensure_helper` for everything the app needs.
- If an entity already exists, the provisioner skips creation (idempotent).

### What to provision

| Need | Provision with |
|------|---------------|
| Card-to-AppDaemon communication | Relay script (see §3) |
| User-facing state (pause toggle, interval slider) | Helper (`input_boolean`, `input_number`, etc.) |
| App status / read-only state | `set_state()` virtual sensor (no provisioning needed) |

### What is NOT auto-provisioned (manual steps)

These remain manual for now and should be documented per-app:

- **Shell commands** — defined in HA `configuration.yaml`, no REST API to create them.
- **Lovelace resources** — custom card JS registration (could be automated later via MCP).
- **`local_file` cameras** — Config Entry integration, complex to automate.
- **Directories** — `/media/...` or `/config/www/...` paths inside the HA container.
- **Placeholder images** — files that must exist before `local_file` camera creation.

For full provisioner API details and kwargs, see `.agents/playbooks/ha-provisioner.md`.

### App config requirements

Apps using `ha_provisioner` need `ha_url` and `ha_token_env` in their `apps-prod.yaml` / `apps-dev.yaml` args:

```yaml
my_app:
  module: my_app.my_app_module
  class: MyApp
  ha_url: !secret ha_url
  ha_token_env: TOKEN
```

- `ha_url` uses `!secret ha_url` — resolved from `secrets.yaml` (the HA base URL, not highly sensitive).
- `ha_token_env` is the **name** of an environment variable — the provisioner resolves it at runtime via `providers.secrets.resolve_secret()`.

Production injects env vars via Kubernetes ExternalSecret. Dev uses `.env` (gitignored) loaded by `python-dotenv`.

## 3) Relay script pattern: card-to-AppDaemon communication

All Lovelace card-to-AppDaemon communication **must** use a relay script. This is a single HA script per app that the card calls via `callService` and that fires a namespaced event for AppDaemon to handle.

### Why not `fire_event`?

`fire_event` (both WebSocket and REST) requires **admin privileges** in HA. Non-admin device accounts (tablets, phones, wall displays) cannot use it. `callService` on a script entity uses the `call_service` WebSocket command, which works for any authenticated user.

### How it works

1. AppDaemon provisions `script.<app>_relay` on startup via `ha_provisioner`.
2. The Lovelace card calls `hass.callService("script", "<app>_relay", { command, payload })`.
3. The script fires `<app>_command` event with the command and payload.
4. AppDaemon listens for `<app>_command` and routes to the appropriate handler.

### Relay script definition (template)

Each app's relay script follows this pattern:

```python
await provisioner.ensure_script("<app>_relay", {
    "alias": "<App Name> Relay",
    "description": "Relays dashboard commands to AppDaemon",
    "mode": "queued",
    "max": 10,
    "fields": {
        "command": {
            "name": "Command",
            "description": "Command name",
            "required": True,
            "selector": {"text": {}},
        },
        "payload": {
            "name": "Payload",
            "description": "JSON-encoded command data",
            "required": False,
            "selector": {"text": {}},
        },
    },
    "sequence": [{
        "event": "<app>_command",
        "event_data": {
            "command": "{{ command }}",
            "payload": "{{ payload | default('{}') }}",
        },
    }],
})
```

### Card-side pattern (JavaScript)

```javascript
_callRelay(command, data) {
  if (!this._hass) return;
  this._hass.callService("script", "<app>_relay", {
    command,
    payload: JSON.stringify(data || {}),
  }).catch((err) => {
    console.warn("<app>-card: relay failed", command, err);
  });
}
```

### AppDaemon-side pattern (Python)

```python
# In initialize():
self.listen_event(self._on_command, "<app>_command")

# Unified command router:
def _on_command(self, event_name, data, kwargs):
    cmd = data.get("command")
    raw = data.get("payload", "{}")
    try:
        payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (json.JSONDecodeError, TypeError):
        self.log(f"Invalid command payload: {raw}", level="WARNING")
        return

    if cmd == "do_something":
        self._handle_do_something(payload)
    else:
        self.log(f"Unknown command: {cmd}", level="WARNING")
```

## 4) Non-admin frontend rule

All Lovelace card interactions **must** work without admin access:

- **`hass.callService()`** is the only safe frontend-to-backend channel.
- **Never** use `fire_event`, `callApi("POST", "events/...")`, or `connection.sendMessagePromise({ type: "fire_event" })` from card code.
- Desktop admin accounts are used for HA configuration; tablets/phones/wall displays use non-admin device-specific accounts.

## 5) Async startup pattern

AppDaemon's `initialize()` is synchronous. Apps that need async provisioning use `run_in` + `create_task`:

```python
def initialize(self) -> None:
    self.run_in(self._async_startup_wrapper, 0)

def _async_startup_wrapper(self, kwargs) -> None:
    self.create_task(self._async_startup())

async def _async_startup(self) -> None:
    await self._provision_entities()
    # Register listeners AFTER provisioning so entities exist
    self.listen_state(self._on_state_change, "input_select.my_picker")
    self.listen_event(self._on_command, "my_app_command")
```

## 6) File serving: `/media/` storage → `/config/www/` via shell commands

AppDaemon apps that generate or manage files (images, JS assets) must follow a two-directory pattern dictated by HA's architecture:

### Why two directories?

- **`/media/`** — Large, unbounded storage. Backed by CephFS (or equivalent network storage). **Not backed up** by HA. AppDaemon and HA pods both mount this. Use for generated images, archives, and any files that grow over time.
- **`/config/www/`** — Served by HA as `/local/...` URLs. **Backed up** by HA snapshots. Must stay small. Only contains files actively needed by the frontend.

### The staging pattern

```
AppDaemon writes to:     /media/<app>/staged/<file>.png
                              ↓
Shell command copies to:  /config/www/<app>/<file>.png
                              ↓
Card loads from:          /local/<app>/<file>.png
```

1. **AppDaemon generates files** into `/media/<app>/generated/` (archive) and copies them to `/media/<app>/staged/` (ready for serving).
2. **AppDaemon manages staged/** — only actively displayed files remain. When content expires or is dismissed, the app removes stale files from staged/.
3. **AppDaemon calls** `self.call_service("shell_command/<app>_stage")` to trigger the sync.
4. **The shell command** (defined in HA's `configuration.yaml` or `packages/`) syncs `/config/www/<app>/` to match `/media/<app>/staged/` — copies new files and removes files no longer in staged.
5. **The Lovelace card** references `/local/<app>/<filename>` with a cache-bust query param (`?t=<epoch>`).

### Shell command rules

**Must use `/bin/sh -c '...'` wrapper.** HA's `shell_command` runs via `asyncio.create_subprocess_shell` with busybox `sh`. Bare commands with `&&`, glob `*`, or redirects (`2>/dev/null`) can fail silently or behave unexpectedly. Always wrap in `/bin/sh -c '...'` — this is what all working shell commands in this project use.

**Must clean up `/config/www/`.** The shell command should remove files from `/config/www/<app>/` that are no longer in `/media/<app>/staged/`. The app is responsible for keeping staged/ clean (only active assets). The shell command syncs www to match. This prevents `/config/www/` from growing unbounded — it's backed up by HA snapshots and must stay small.

**Never suppress errors with `2>/dev/null; true`.** HA logs shell command errors even with exit code 0 override, but `2>/dev/null` hides the actual error message, making debugging impossible.

### Shell command template

```yaml
shell_command:
  my_app_stage: >-
    /bin/sh -c 'set -e;
    dest="/config/www/my-app";
    src="/media/my-app/staged";
    mkdir -p "$dest";
    for f in "$dest"/*.png; do
      [ -f "$f" ] || continue;
      bn=$(basename "$f");
      [ -f "$src/$bn" ] || rm -f "$f";
    done;
    [ -n "$(ls -A "$src" 2>/dev/null)" ] && cp -f "$src"/* "$dest/"'
```

How it works:
1. `mkdir -p` creates the www directory if needed (as HA process user — currently root in our setup)
2. Loop removes `.png` files from www that are no longer in staged (cleanup)
3. Copies all staged files to www (only if staged is non-empty)

**App-side staged dir management:** The app must keep `/media/<app>/staged/` clean. When notifications expire or are dismissed, remove their staged files. Only actively displayed images + the current placeholder should remain in staged. See `dashboard_notify._sync_staged_dir()` for reference.

### File ownership

Never create files in `/config/www/` manually via `kubectl exec` — they'll be owned by root. Let the shell command create the directory and files on first run so ownership is consistent. If you must create files manually, ensure they're owned by the same user the HA process runs as (`ps aux | grep homeassistant` in the pod to check).

### Path mapping across environments

| Context | `/media/` path | `/config/www/` path |
|---------|---------------|---------------------|
| HA pod | `/media/<app>/` | `/config/www/<app>/` |
| AppDaemon pod | `/media/<app>/` | N/A (uses shell_command) |
| Local dev (WSL) | `/mnt/cephfs-hdd/misc/hass-media/<app>/` | N/A (uses shell_command) |

In local dev, AppDaemon writes to the cephfs mount directly. The shell command still runs on the HA pod where `/media/` is mounted at the standard path.

### Dev config: `media_fs_root` override

Apps use a single `media_fs_root` config (default: `/media`) rather than per-app `media_dir` paths. The app computes its own subdirectory (e.g. `<media_fs_root>/dashboard-notify`). This approach has two benefits:

1. **One config point** controls all media paths — the app's own files and cross-app references (e.g. dashboard_notify reading detection-summary images).
2. **No path duplication** — the app name subdirectory is hardcoded in the app, not repeated in YAML.

In production, both the AppDaemon pod and HA pod mount the same CephFS volume at `/media`, so the default works. In local dev, `/media/` on the dev machine is a different (root-owned) mount, so override to the cephfs mount point:

```yaml
# apps-prod.yaml — omit media_fs_root (default /media works)
my_app:
  www_subdir: my-app

# apps-dev.yaml — override to local cephfs mount
my_app_dev:
  media_fs_root: /mnt/cephfs-hdd/misc/hass-media
  www_subdir: my-app
```

**Why not just `media_dir`?** Some apps need to access files from _other_ apps' media directories (e.g. `dashboard_notify` copies detection-summary generated images). With `media_fs_root`, the app can construct paths to any `/media/` subdirectory using the same root. A per-app `media_dir` would require a second config key for each cross-app reference.

### Existing apps using this pattern

| App | Media dir | Shell command | www subdir |
|-----|-----------|--------------|------------|
| `photo_frame_viewer` | `/media/immich-photos` | `photo_frame_stage_gen` | `/config/www/photo-frame/live/<gen>/` |
| `detection_summary_viewer` | `/media/detection-summary/<bundle>` | `ds_refresh_detection_summary_viewer_www` | `/config/www/detection-summary/<bundle>/viewer/` |
| `dashboard_notify` | `/media/dashboard-notify` | `dashboard_notify_stage` | `/config/www/dashboard-notify/` |

## 7) New app checklist

When creating a new AppDaemon app with a Lovelace card:

1. Define the relay script config (event name, commands).
2. Define any user-facing helpers the app needs (pause toggles, sliders, etc.).
3. Call `ha_provisioner` on startup to ensure all entities exist.
4. Card uses `callService("script", "<app>_relay", ...)` for all actions.
5. AppDaemon listens for `<app>_command` event, routes commands.
6. Add `ha_url: !secret ha_url` and `ha_token_env: TOKEN` to `apps-dev.yaml` and `apps-prod.yaml`.
7. If the app generates files for the frontend, follow the `/media/` → `/config/www/` staging pattern (§6). Define a shell command, use `media_fs_root` config, and document the shell command in the README.
8. Document any manual steps (shell commands, Lovelace resources, `local_file` cameras, directory creation) in the app's README.
