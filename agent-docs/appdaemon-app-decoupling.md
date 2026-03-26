# AppDaemon App Decoupling Pattern

## Overview

This document describes how to build AppDaemon apps that communicate via HA events instead of direct Python references (`get_app()`). This pattern enables:

- **Split deployment**: A controller runs in production Kubernetes while new satellite apps are developed and tested on a laptop
- **Independent lifecycles**: Apps can restart, crash, or be added/removed without affecting each other
- **No `dependencies:` config needed**: Apps don't reference each other in `apps.yaml`

## Pattern Summary

```
┌─────────────────┐    HA Events     ┌──────────────┐
│  Satellite App   │ ──────────────▶  │  Controller   │
│  (e.g. checker)  │  register        │               │
│                  │  report_status   │  Provisions:  │
│                  │                  │  - helpers    │
│                  │                  │  - relay      │
│                  │                  │  - sensor     │
│                  │ ◀────────────── │               │
│                  │  controller_ready│               │
│                  │  recheck         │               │
└─────────────────┘                  └──────────────┘
```

## How It Works

### 1. Controller Startup

The controller app:
1. Provisions HA entities (helpers, relay script) via `ha_provisioner`
2. Registers event listener for `<app>_command` events
3. Fires `<app>_controller_ready` event to signal availability

### 2. Satellite Registration

Satellite apps register with the controller by firing an event:

```python
self.fire_event(
    "<app>_command",
    command="register",
    payload=json.dumps({
        "id": "my_satellite",
        "name": "My Satellite",
        "metadata": { ... },
    }),
)
```

The controller stores a **proxy** (metadata only — no live Python reference):

```python
def _handle_register(self, payload):
    sat_id = payload["id"]
    self._satellites[sat_id] = {
        "name": payload["name"],
        "metadata": payload["metadata"],
    }
```

### 3. Late-Join / Restart Tolerance

Satellites listen for `<app>_controller_ready` and re-register:

```python
self.listen_event(self._on_controller_ready, "<app>_controller_ready")

def _on_controller_ready(self, event_name, data, kwargs):
    self._register()  # re-fire the registration event
```

Additionally, satellites use `run_in(..., 10)` to delay initial registration by 10 seconds, giving the controller time to start.

### 4. Satellite → Controller Communication

All satellite-to-controller communication goes through HA events:

```python
self.fire_event(
    "<app>_command",
    command="report_status",
    payload=json.dumps({
        "satellite_id": self._id,
        "results": [ ... ],
    }),
)
```

### 5. Controller → Satellite Communication

The controller broadcasts commands to all satellites via events:

```python
# Controller broadcasts
self.fire_event("<app>_recheck", {})

# Satellite listens
self.listen_event(self._on_recheck, "<app>_recheck")
```

## Key Rules

### Never Use `get_app()`

Using `get_app()` creates a direct Python reference between apps, which:
- Requires both apps to run in the same process
- Breaks when one app restarts or is not yet loaded
- Prevents split dev/prod deployment

### JSON-Stringify Complex Payloads

HA events can mangle complex data types (arrays, nested objects). Always JSON-stringify payloads:

```python
# Sending
self.fire_event("my_event", command="foo", payload=json.dumps(data))

# Receiving
raw = data.get("payload", "{}")
payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
```

### Event Naming Convention

- `<app>_command` — satellite → controller commands
- `<app>_controller_ready` — controller startup signal
- `<app>_recheck` or `<app>_<action>` — controller → satellite broadcasts

### Config-Driven Satellites

Design satellite apps to be generic and config-driven so one class can be instantiated multiple times:

```yaml
# Same class, different config
zigbee_checker:
  module: health_checks.network_protocol_checker.network_protocol_checker
  class: NetworkProtocolChecker
  checker_id: zigbee
  entity_id: binary_sensor.zigbee2mqtt_bridge_connection_state
  ...

zwave_checker:
  module: health_checks.network_protocol_checker.network_protocol_checker
  class: NetworkProtocolChecker
  checker_id: zwave
  entity_id: sensor.800_series_long_range_gpio_module_status
  ...
```

## Examples

- **Health Checks**: `appdaemon/apps/health_checks/` — controller + generic network protocol checker
- **Vestaboard**: `appdaemon/apps/vestaboard/` (PR #21) — controller + 7 automation apps

## Testing

Mock `fire_event` and `listen_event` in tests. Verify:
- Registration events include correct metadata
- Controller handles registration and status reporting correctly
- Satellites re-register on controller_ready
- Force-recheck broadcasts trigger satellite check execution
