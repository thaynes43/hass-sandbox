# Logging Standards for AppDaemon Apps

> **Applies to:** `appdaemon/**/*.py`

Consistent logging is critical for debugging live systems where you cannot attach a debugger. Every AppDaemon app must follow these standards.

## Log Levels

| Level | Use for | Examples |
|-------|---------|---------|
| `ERROR` | Failures that break expected behavior — the app cannot complete its task | Provider API returned 500, board write failed, file I/O error, uncaught exception in a handler |
| `WARNING` | Unexpected conditions the app can recover from — something is wrong but not fatal | Unknown command received, entity not found, validation failure with fallback, config key missing (using default) |
| `INFO` | Significant business events a human operator would want to see in normal operation | App initialized, config persisted, automation activated/deactivated, frame pushed to board, API call made, user action received, board write with frame content |
| `DEBUG` | Verbose detail useful only when actively investigating a specific issue | Tick no-ops, cache hits, intermediate calculation values, full entity state dumps |

### Key principle

A healthy running system should produce a readable `INFO`-level log that tells the story of what happened and why, without drowning in noise. `DEBUG` is for deep investigation only. An operator reading only `INFO` logs should be able to reconstruct the full sequence of events, user actions, and board state changes.

## Required Logging Points

### 1. App Initialization

Every app must log what it was initialized/seeded with at `INFO` level during `initialize()`:

- Connection parameters (IPs, entity IDs — never secrets/tokens)
- Key config values that affect behavior (intervals, thresholds, enabled states, paths)
- What was loaded from persistent storage (count of items, store path)
- Registration outcomes (registered with controller, listeners set up, timers scheduled)

```python
def initialize(self):
    # ... setup ...
    self.log(
        f"MyApp initialized — entity={entity_id!r} interval={interval_s}s "
        f"enabled={enabled}",
        level="INFO",
    )
```

### 2. Command/Event Reception

Every command or event received must be logged at `INFO` with the **sender** and a **payload summary**:

```python
# Good — identifies sender and summarizes payload
self.log(
    f"Command received: {command!r} from={automation_id!r} "
    f"payload_keys={list(payload.keys())}",
    level="INFO",
)

# Good — for small payloads, show them directly
self.log(
    f"Command received: 'update_next_fire_time' from='messages_from_library_dev' "
    f"next_fire_time=1710648000.0",
    level="INFO",
)
```

**Payload summary rules:**
- **Small payloads** (< 5 keys, all scalar values): log the full payload inline
- **Medium payloads** (5-10 keys): log key names and key scalar values, skip large nested data
- **Large payloads** (arrays, grids, nested structures): log a summary — item count, first N items, or key fields only
- **Never dump** raw 6x22 grids, full JSON blobs, or multi-KB strings at INFO level

```python
# Small payload — show it all
self.log(f"Config update: {new_config}", level="INFO")

# Large array — summarize
self.log(f"Received {len(items)} frames, first={items[0].get('name')!r}", level="INFO")

# Grid data — show dimensions, not content
self.log(f"Frame received: 6x22 grid from={source!r} ttl_s={ttl_s}", level="INFO")
```

### 3. User Actions from Frontend

Any command received from a card/relay that will change backend state or trigger work must be logged at `INFO`:

- What command was received and from which automation/source
- What config values were changed (before persisting)
- Confirmation that persistence succeeded
- What side effects occurred (timers rescheduled, automation enabled/disabled)

```python
self.log(f"Updating config for {automation_id!r}: {new_config}", level="INFO")
self._config_store.update(automation_id, new_config)
self.log(f"Config persisted for {automation_id!r}", level="INFO")
```

For enable/disable actions, log the state transition and persistence:

```python
self.log(f"Persisted enabled={enabled} for {automation_id!r}", level="INFO")
self.log(f"Automation {automation_id!r} {'activated' if enabled else 'deactivated'}", level="INFO")
```

### 4. Board Writes

**Every board write must be logged at `INFO` with the frame content** so operators can see what was actually sent to the physical display:

```python
from providers.vestaboard.character_encoding import decode_grid

self.log(
    f"Board write — source={source!r}\n{decode_grid(characters)}",
    level="INFO",
)
```

The `decode_grid()` function converts the 6x22 integer grid to a human-readable 6-line string representation. This is the canonical way to log frame content — it's compact (6 lines) and immediately tells you what the board shows.

Board write failures must be logged at `ERROR`:
```python
self.log(f"Board write failed: {exc!r}", level="ERROR")
```

### 5. Provider / API Calls

Every outbound call through a provider must be logged at `INFO` with:

- **Before the call**: What is being requested (provider type, purpose, key parameters — not full payloads)
- **After the call**: Success/failure status and a brief summary of the response

Never dump full request/response bodies at `INFO`. Summarize: item counts, status codes, key fields. Use `DEBUG` for full payloads when needed for investigation.

```python
# Good — summarizes intent and outcome
self.log(f"Generating AI art for subject={subject!r}", level="INFO")
# ... call provider ...
self.log(
    f"AI art response — model={model!r} usage={usage} valid={ok}",
    level="INFO",
)

# Bad — dumps entire payload
self.log(f"AI response: {json.dumps(result)}", level="INFO")  # NO
```

### 6. Home Assistant State Modifications

Log at `INFO` whenever the app modifies HA state, whether through:

**AppDaemon's built-in API** (`set_state`, `call_service`, `fire_event`):
```python
self.log(f"Publishing status to {self.SENSOR_ENTITY}", level="DEBUG")
# Use DEBUG for frequent status publishes (every tick), INFO for significant ones
```

**HAProvisioner** (`ensure_script`, `ensure_helper`):
```python
# Log provisioning outcomes
self.log(f"Relay script.{script_id} {'created' if created else 'already exists'}", level="INFO")
```

The distinction: routine state updates (sensor republish on tick) are `DEBUG`. One-time or user-triggered state changes (provisioning, config-driven updates) are `INFO`.

### 7. Timer and Listener Lifecycle

Log at `INFO` when registering or cancelling timers/listeners that affect user-visible behavior:

```python
self.log(f"Weather scheduled at {time_str}", level="INFO")
self.log(f"Random interval scheduled: next fire in {delay:.1f} min", level="INFO")
```

Both user-configured schedules (daily times) and random intervals should be `INFO` — operators need to see when automations will fire next.

### 8. Event-Based Communication (Vestaboard)

For event-based inter-app communication (automations ↔ controller via HA events):

**Registration events**: Log at `INFO` with automation type and key metadata:
```python
self.log(
    f"Automation registered: {auto_id!r} (type={automation_type!r})",
    level="INFO",
)
```

**Config/enable events sent to automations**: Log the target and payload:
```python
self.log(
    f"Config event fired to {automation_id!r}: keys={list(config.keys())}",
    level="INFO",
)
```

**Generate events**: Log the target, kwargs, and whether it's preview-only:
```python
self.log(
    f"Generate event fired to {auto_id!r} preview_only={preview_only} kwargs={kwargs}",
    level="INFO",
)
```

## What NOT to Log

- **Secrets, tokens, API keys** — never at any level. Mask if needed: `****{last4}`
- **Full JSON payloads** from APIs at `INFO` — summarize instead
- **Routine no-op ticks** at `INFO` — these happen every 15 seconds and create noise
- **Redundant messages** — don't log the same fact twice at the same level in the same code path

## Formatting Conventions

- Use f-strings with `!r` for values that could be empty, None, or contain spaces: `f"entity={entity_id!r}"`
- Use `key=value` pairs separated by spaces for structured data: `f"ttl_s={ttl_s} max_age_s={max_age_s}"`
- Prefix log messages with context when the app handles multiple concerns: `f"[FrameQueue] push → ..."`
- Keep messages on one line when practical — multi-line logs are harder to grep
- Use `level="INFO"` explicitly (don't rely on defaults) for clarity in code review
- Always include the **sender/source** when logging received commands or events

## Applying to Existing Code

When modifying an app, check that the logging points above are covered. Don't add logging to code you're not changing — but if a handler you're editing is missing user-action logging or persistence confirmation, add it as part of your change.
