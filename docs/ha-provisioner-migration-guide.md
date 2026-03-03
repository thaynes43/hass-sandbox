# HAProvisioner Migration Guide

Step-by-step guide for migrating an AppDaemon app from manually-created Home
Assistant helpers to self-provisioning via `ha_provisioner`.  The
`PhotoFrameViewerApp` migration is used as the worked example throughout.

---

## Overview

**Before**: Helpers (`input_boolean`, `input_number`, `input_text`) are created
manually in the HA UI.  The app reads/writes them via `get_state` /
`call_service`.  Dashboard cards call helper services directly, which requires
admin privileges on non-admin accounts.

**After**: The app provisions its own entities on startup using
`HAProvisioner`.  State lives internally in Python and is published on a
**virtual sensor** via `self.set_state()`.  A **relay script** (also
provisioned) lets dashboard cards fire commands without admin access.

### Benefits

- No manual setup steps for end-users or new deployments.
- State is typed Python, not HA entity round-trips.
- Non-admin wall-display accounts can interact with the card.
- Provisioning is idempotent — safe to restart the app repeatedly.

---

## Prerequisites

1. **`ha_provisioner` library** is available in the repo at
   `appdaemon/ha_provisioner/provisioner.py`.
2. The AppDaemon app's `apps.yaml` entry needs `ha_url` and `ha_token` secrets
   so the provisioner can call the REST API.
3. The app module must add the AppDaemon root to `sys.path` so the shared
   library is importable.

---

## Step 1 — Add `sys.path` fix

AppDaemon only adds `appdaemon/apps/` to `sys.path`.  Shared libraries live
one level up, at `appdaemon/`.  Add this near the top of the app module,
**before** `import hassapi`:

```python
import sys
from pathlib import Path

# AppDaemon only adds `appdaemon/apps` to sys.path.
# Our shared libraries live at `appdaemon/`, so add that.
sys.path.append(str(Path(__file__).resolve().parents[2]))

import hassapi as hass
```

---

## Step 2 — Add credentials to `apps.yaml`

```yaml
my_app_name:
  module: my_app.my_app
  class: MyApp
  ha_url: !secret ha_url
  ha_token: !secret token
  # ... other config
```

Add matching secrets to `appdaemon/secrets.yaml` (local dev) and the
Kubernetes `ExternalSecret` (production).

---

## Step 3 — Implement `_provision_entities`

Create an async method that calls `HAProvisioner` once per startup:

```python
async def _provision_entities(self) -> None:
    ha_url = self.args.get("ha_url")
    ha_token = self.args.get("ha_token")
    if not ha_url or not ha_token:
        self.log("ha_url / ha_token not configured — skipping provisioning",
                 level="WARNING")
        return

    from ha_provisioner import HAProvisioner
    prov = HAProvisioner(ha_url=ha_url, ha_token=ha_token)

    # Provision relay script
    try:
        created = await prov.ensure_script(
            "my_app_relay",
            {
                "alias": "My App Relay",
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
                "sequence": [
                    {
                        "event": "my_app_command",
                        "event_data": {
                            "command": "{{ command }}",
                            "payload": "{{ payload | default('{}') }}",
                        },
                    }
                ],
            },
        )
        level = "INFO" if created else "DEBUG"
        msg = "created" if created else "already exists"
        self.log(f"Relay script {msg}: script.my_app_relay", level=level)
    except Exception as exc:
        self.log(f"Failed to provision relay script: {exc}", level="ERROR")

    # Provision input_select helper (the only helper that needs HA state)
    try:
        created = await prov.ensure_helper("input_select", "My App Image")
        level = "INFO" if created else "DEBUG"
        msg = "created" if created else "already exists"
        self.log(f"input_select {msg}", level=level)
    except Exception as exc:
        self.log(f"Failed to provision input_select: {exc}", level="ERROR")
```

`ensure_script` and `ensure_helper` are **idempotent** — calling them when
the entity already exists is a no-op (returns `False`).

---

## Step 4 — Wire up async startup

AppDaemon's `initialize()` is synchronous.  Use `run_in` + `create_task` to
schedule async work:

```python
def initialize(self) -> None:
    # ... sync init ...
    self.run_in(self._on_startup, 0)

def _on_startup(self, kwargs) -> None:
    self.create_task(self._async_startup())

async def _async_startup(self) -> None:
    await self._provision_entities()
    self._publish_sensor_state()   # publish initial sensor state after provision
```

---

## Step 5 — Replace helper state with internal Python state

Remove the helper entity IDs from `args` and from `get_state` calls.
Initialise the values as instance attributes in `initialize()`:

```python
# Old (helpers)
paused = self.get_state("input_boolean.my_app_paused") == "on"
interval = float(self.get_state("input_number.my_app_interval") or 10)

# New (internal state)
self._paused: bool = False
self._interval: float = self._load_interval(float(cfg.get("default_interval_s", 10)))
```

For values you want to survive restarts, persist them to a JSON file:

```python
def _load_interval(self, default: float) -> float:
    try:
        data = json.loads(Path(self._state_file).read_text())
        val = float(data.get("interval_seconds", default))
        if val > 0:
            return max(1.0, val)
    except Exception:
        pass
    return max(1.0, default)

def _save_interval(self) -> None:
    os.makedirs(self._state_dir, exist_ok=True)
    Path(self._state_file).write_text(
        json.dumps({"interval_seconds": self._interval})
    )
```

---

## Step 6 — Add a virtual sensor for read-only state

Publish all read-only state as attributes on a virtual sensor.  This is the
only place the dashboard card reads data:

```python
def _publish_sensor_state(self) -> None:
    self.set_state(
        "sensor.my_app_status",
        state="paused" if self._paused else "playing",
        attributes={
            "paused": "true" if self._paused else "false",
            "interval_seconds": self._interval,
            "image_url": self._last_published_local_url or "",
            "cache_bust": self._cache_bust,
        },
    )
```

Call `_publish_sensor_state()` any time internal state changes (after pause
toggle, interval update, URL change, etc.).

### On startup recovery

If you need to recover state from a previous run on AppDaemon restart, read
the virtual sensor's attributes back in `initialize()`:

```python
def _recover_state(self) -> None:
    state = self.get_state("sensor.my_app_status", attribute="all")
    if isinstance(state, dict):
        attrs = state.get("attributes", {})
        url = attrs.get("image_url", "")
        if url:
            self._last_published_local_url = url
```

---

## Step 7 — Add a relay command handler

Register a listener for the event the relay script fires:

```python
# In initialize()
self.listen_event(self._on_command, "my_app_command")

def _on_command(self, event_name: str, data: dict, kwargs) -> None:
    cmd = data.get("command")
    raw = data.get("payload", "{}")
    try:
        payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (json.JSONDecodeError, TypeError):
        self.log(f"Invalid command payload: {raw!r}", level="WARNING")
        return

    if cmd == "toggle_pause":
        self._handle_toggle_pause()
    elif cmd == "set_interval":
        self._handle_set_interval(payload)
    else:
        self.log(f"Unknown command: {cmd!r}", level="WARNING")
```

---

## Step 8 — Update the Lovelace card

### Reading state

Replace direct helper state lookups with sensor attribute reads:

```js
// Old
const paused = hass.states['input_boolean.my_app_paused']?.state === 'on';

// New
const paused = hass.states['sensor.my_app_status']?.attributes?.paused === 'true';
```

### Sending commands

Replace `hass.callService('input_boolean', 'toggle', ...)` with a call to the
relay script:

```js
async _callRelay(command, data = {}) {
    await this.hass.callService('script', 'turn_on', {
        entity_id: 'script.my_app_relay',
        variables: {
            command,
            payload: JSON.stringify(data),
        },
    });
}

// Usage
this._callRelay('toggle_pause');
this._callRelay('set_interval', { seconds: 30 });
```

`callService` works for all authenticated users (no admin required).

---

## Step 9 — Update dashboard YAML cards

**Image URL** (markdown card):

```yaml
# Old
content: >-
  <img src="{{ states('input_text.my_app_image_url') }}?cb={{
  states('input_text.my_app_cache_bust') }}" />

# New
content: >-
  <img src="{{ state_attr('sensor.my_app_status', 'image_url') }}?cb={{
  state_attr('sensor.my_app_status', 'cache_bust') }}" />
```

**Pause button action**:

```yaml
# Old
tap_action:
  action: call-service
  service: input_boolean.toggle
  target:
    entity_id: input_boolean.my_app_paused

# New
tap_action:
  action: call-service
  service: script.turn_on
  target:
    entity_id: script.my_app_relay
  data:
    variables:
      command: toggle_pause
      payload: "{}"
```

**Pause highlight CSS** (Bubble Card):

```yaml
# Old
"{{ 'border: 3px solid orange' if hass.states['input_boolean.my_app_paused']?.state == 'on' }}"

# New
"{{ 'border: 3px solid orange' if state_attr('sensor.my_app_status', 'paused') == 'true' }}"
```

---

## Step 10 — Update `apps.yaml`

Remove old helper entity ID keys and add `ha_url`, `ha_token`, and the new
state keys:

```yaml
my_app_name:
  module: my_app.my_app
  class: MyApp
  ha_url: !secret ha_url
  ha_token: !secret token
  source_dir: /media/my-photos
  ha_local_url_base: /local/my-app/live
  default_interval_s: 10
  state_dir: /media/my-app-state
  # Removed: paused_entity_id, interval_entity_id, cache_bust_entity_id, image_url_entity_id
```

---

## Step 11 — Write unit tests

Three test files are the minimum:

| File | What it tests |
|---|---|
| `test_my_app_provisioning.py` | `_provision_entities`: relay script created, input_select created, missing credentials skipped, errors caught |
| `test_my_app_relay_commands.py` | `_on_command` routing: each command dispatches correctly, unknown commands log a warning, invalid JSON payloads handled |
| Updated existing tests | Replace helper mock expectations with virtual sensor mocks; set `app._paused = True` after `initialize()` instead of injecting via `fake_get_state` |

Key mocking pattern for provisioning tests:

```python
with patch("ha_provisioner.HAProvisioner", return_value=mock_prov):
    asyncio.get_event_loop().run_until_complete(app._provision_entities())

mock_prov.ensure_script.assert_called_once()
```

---

## Step 12 — Delete replaced helpers (post-verification)

After confirming the new app is running correctly in production, delete the
now-unused helpers via the MCP server:

```python
ha_config_remove_helper("input_boolean", "my_app_paused")
ha_config_remove_helper("input_number", "my_app_interval")
ha_config_remove_helper("input_text", "my_app_cache_bust")
ha_config_remove_helper("input_text", "my_app_image_url")
# Keep input_select — it's now provisioned by the app
```

**Do not delete helpers until the app is confirmed working.**

> **Note**: The virtual sensor `sensor.<prefix>_photo_frame_status` is created
> by AppDaemon on first startup via `set_state()` — it does not need to be
> manually provisioned and cannot be created via the MCP server.

---

## Step 13 — Verify entity prefix derivation

Apps that follow the `_derive_entity_prefix` pattern generate all entity IDs
from a single prefix, derived in this priority order:

1. **Explicit override** — `entity_prefix: my_custom_prefix` key in `apps.yaml`
2. **Instance name suffix** — the part after the marker `photo_frame_viewer_`
   in the app's key in `apps.yaml`
3. **Fallback** — `wall_display`

Example (`PhotoFrameViewerApp`):

```
apps.yaml key:   photo_frame_viewer_wall_display
                                   ^^^^^^^^^^^^ suffix extracted here

Derived prefix:  wall_display

Derived entity IDs:
  sensor.wall_display_photo_frame_status       ← virtual sensor (AppDaemon set_state)
  input_select.wall_display_photo_frame_image  ← provisioned by ensure_helper
  script.wall_display_photo_frame_relay        ← provisioned by ensure_script
  (event)  wall_display_photo_frame_command    ← fired by relay script, heard by app
```

To run two independent instances, use distinct keys:

```yaml
photo_frame_viewer_living_room:   # prefix → living_room
  ...
photo_frame_viewer_bedroom:       # prefix → bedroom
  ...
```

Or override explicitly:

```yaml
photo_frame_viewer_my_instance:
  entity_prefix: kitchen_display
```

---

## Step 14 — Cache-bust the Lovelace resource

After deploying an updated card JS file to `/config/www/`, browsers will not
pick up the new version until the resource URL's cache-busting query parameter
is bumped.  Do this via the MCP server using `ha_config_set_dashboard_resource`
(see `custom-card-guidelines.mdc` §5 — Cache busting):

```python
# Find the resource ID first (if not already known)
ha_config_list_dashboard_resources()

# Bump the version query parameter on the existing resource
ha_config_set_dashboard_resource(
    url="/local/photo-frame/photo-frame-viewer-card.js?v=4",
    resource_type="module",
    resource_id="<resource_id_from_list>",
)
```

The resource ID is stable across version bumps — only the `?v=N` suffix
changes.  Increment `N` by 1 each time.

> After bumping, users must hard-refresh (`Ctrl+Shift+R`) or clear the browser
> cache to load the new card version.

---

## Checklist

- [ ] Step 1 — `sys.path` fix added before `import hassapi`
- [ ] Step 2 — `ha_url` / `ha_token` added to `apps.yaml`
- [ ] Step 3 — `_provision_entities` implemented with `ensure_script` + `ensure_helper`
- [ ] Step 4 — `run_in` → `create_task` async startup wired up
- [ ] Step 5 — Internal state fields replace helper `get_state` calls
- [ ] Step 6 — `_publish_sensor_state` publishes all state as sensor attributes
- [ ] Step 7 — `listen_event` registered for relay command event; `_on_command` routes all commands to handlers
- [ ] Step 8 — Lovelace card reads from sensor attributes; sends commands via `callService('script', 'turn_on', ...)`
- [ ] Step 9 — Dashboard YAML cards updated (markdown `img`, button actions, CSS)
- [ ] Step 10 — `apps.yaml` updated (removed old helper keys, added new keys)
- [ ] Step 11 — Unit tests written for provisioning and relay commands; existing tests updated
- [ ] Step 12 — App verified working in production; old helpers deleted via MCP
- [ ] Step 13 — Entity prefix derivation verified (correct instance names / overrides)
- [ ] Step 14 — Card JS deployed to `/config/www/` and Lovelace resource cache-busted via MCP
