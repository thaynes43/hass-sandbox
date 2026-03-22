# Vestaboard Controller

Drives the physical Vestaboard flip-tile display. Manages a LIFO priority queue of frames, dispatches them to the board on a periodic tick, and exposes an event-based automation registration API so each automation app independently publishes frames.

## How it works

1. On startup, provisions `script.vestaboard_controller_relay` in HA via `HAProvisioner`.
2. Reads the current frame from the physical board so the queue has a starting state.
3. Loads the persistent `AutomationConfigStore` from `automation_config_path` to restore previously saved UI settings.
4. Registers a periodic tick (default 15 s) that advances the `FrameQueue` — promoting pending frames when TTLs expire and publishing the updated status sensor.
5. Fires `vestaboard_controller_ready` event so automation apps can (re-)register after a controller restart.
6. Automation apps register by firing a `vestaboard_controller_command` event with `command="register_automation"`. The controller creates a `RemoteAutomationProxy` and fires persisted config back.
7. When an automation generates a frame it fires a `vestaboard_controller_command` event with `command="push_automation_frame"`. The controller pushes it into the LIFO queue and may immediately display it.
8. Commands from the Lovelace card arrive via `script.vestaboard_controller_relay` → `vestaboard_controller_command` event → `_on_command()`.

### Frame queue concepts

| Concept | Meaning |
|---------|---------|
| **LIFO** | Most recently pushed pending frame is shown first |
| **TTL (`ttl_s`)** | Seconds to hold the board before yielding to pending. `None` = no protection; any new frame can replace it |
| **Max age (`max_age_s`)** | Hard expiry since creation time. Frame is dropped from queue without being shown if this passes |
| **Override TTL** | Frame immediately pre-empts whatever is on the board regardless of active TTL |
| **Should expire** | If `True`, frame is dropped when **displaced** by another frame (not moved to fallback stack). TTL expiry alone does not drop it — something must actively push it off. |
| **Fallback stack** | Previously displayed frames. Shown when pending queue is empty |
| **Same-source dedup** | A newer push from the same source replaces the older pending frame |

### Queue lifecycle in detail

The controller manages four frame zones. Frames move between them based on TTL, push events, and tick evaluations.

```
                                    ┌─────────────────────────────┐
   Automation fires                 │         CURRENT             │
   ─────────────────┐               │  (physically on the board)  │
                    │               │  TTL countdown ticking       │
                    ▼               └──────────┬──────────────────┘
              ┌──────────┐                     │
              │  push()  │─── display now ─────┘  (if nothing displayed, TTL expired,
              └──────────┘                         same-source, or override_ttl)
                    │
                    │ active TTL blocks
                    ▼
              ┌──────────────┐     tick() TTL expired     ┌─────────────────┐
              │   PENDING    │ ──────────────────────────► │     CURRENT     │
              │  (LIFO stack)│     promote top frame       └─────────────────┘
              └──────────────┘
                                                          old CURRENT moves to:
                                                          ┌─────────────────┐
   UPCOMING                                               │    FALLBACK     │  (if should_expire=False)
   (not in queue yet —                                    │  (re-show stack)│
    just a timer countdown                                └─────────────────┘
    until automation fires)                                       or
                                                          ┌─────────────────┐
                                                          │    DROPPED      │  (if should_expire=True)
                                                          └─────────────────┘
```

#### CURRENT (displayed frame)

The frame physically written to the board. Has a TTL that counts down from when this source **first claimed the board** (not from each same-source update).

- **Same-source updates** replace the displayed grid but do NOT reset the TTL anchor. Example: `calendar_clock` fires every minute, each update replaces the grid content but the TTL keeps counting from the first display.
- **TTL=None** means no protection — any push from a different source immediately replaces it. But during `tick()`, a None-TTL frame holds the board (prevents fallback cycling).
- When TTL expires, the tick promotes the next frame from pending (or fallback if pending is empty).

#### PENDING (queued frames)

Frames that have been **generated but can't display yet** because the current frame's TTL is active.

**Selection order**: LIFO (last in, first out). The most recently pushed frame is promoted first. Internally, pending is a list where `append()` adds to the end and promotion pops from the end.

**Same-source dedup**: Only one pending frame per source. If `calendar_clock` pushes a frame every minute while another automation holds the board, each new clock frame replaces the previous pending clock frame. This prevents queue buildup — at most 1 pending frame per automation.

**Max age pruning**: On every push and tick, `_prune_expired()` removes any pending or fallback frame whose `max_age_s` has elapsed since creation. This prevents stale calendar frames from being promoted after their events have passed.

#### UPCOMING (not in the queue)

Automations that have a **scheduled future fire time** but haven't generated a frame yet. These are just timers — no frame exists in the queue. The UI shows the countdown until the automation fires.

Sources of upcoming timers:
- **Random interval** (`_schedule_random_interval`): Messages From Library, Art From Library, AI Art Generator, AI Message Generator
- **Cooldown timer** (`_start_cooldown`): Calendar Summary automations after a non-urgent push
- **NOT shown**: `calendar_clock` (uses `run_every`, not random interval), `weather_schedule` (uses `run_daily`)

When an upcoming timer fires, the automation generates a frame and calls `push_frame()`. That frame either displays immediately (if current TTL expired) or enters pending.

#### FALLBACK (displaced frame stack)

Frames that were **displaced from the board** by another frame before they were done. Only frames with `should_expire=False` move here. Frames do NOT spontaneously leave CURRENT and enter fallback — something must actively push them off.

**`should_expire` controls what happens when a frame is displaced**:
- `should_expire=True`: Frame is **dropped** when displaced. Gone forever, never re-shown. Use this for time-sensitive content (calendar events, library messages, weather).
- `should_expire=False`: Frame **moves to fallback** when displaced. Can be re-shown when pending is empty. Use this for content worth re-displaying.

**Critical: TTL expiry alone does NOT move a frame to fallback**. If a frame's TTL expires and nothing is pending, the frame stays on the board indefinitely. Fallback only happens when another frame needs the board. The sequence is:

1. Frame A is on the board (CURRENT), `should_expire=False`
2. Frame A's TTL expires
3. **If nothing is pending**: Frame A stays on the board — holds until a new push arrives
4. **If Frame B is pending**: Frame A is displaced → moved to fallback. Frame B promoted to CURRENT.
5. Later, Frame B's TTL expires and nothing else is pending → Frame A re-promoted from fallback

**Example — temporary notification over clock**:
```
Clock on board (should_expire=False, TTL=None)
  → Notification force-pushes (override_ttl=True, should_expire=True, TTL=60s)
  → Clock moves to FALLBACK (should_expire=False)
  → Notification holds board for 60s
  → Notification TTL expires, clock re-promoted from fallback
```

If the clock had `should_expire=True`, it would be DROPPED instead of moving to fallback — a fresh clock frame from the next 1-minute fire would take over later.

If that same notification had `should_expire=False`, the notification would hold the board **past its TTL** until the next clock push displaced it, at which point the notification moves to fallback.

**Selection order**: Most recently displaced first. `_next_non_expired()` iterates `reversed(self._fallback)`, so the frame that was most recently moved to fallback is re-promoted first.

**When fallback is consulted**: Only when pending is empty AND the tick needs to promote. Pending always takes priority over fallback.

**Why duplicates from the same source can appear**: An automation with `should_expire=False` pushes a frame that eventually gets displaced to fallback. Later, the automation fires again with new content. That new frame eventually gets displaced to fallback too. Now fallback has two entries from the same source — different frames with different content (e.g., two different AI art pieces).

**Remaining TTL on displaced frames**: If a frame is displaced mid-TTL (e.g., a force-push overrides it with 15 minutes of TTL remaining), the remaining TTL is preserved in `remaining_ttl_s`. When re-promoted from fallback, the frame gets only the remaining TTL, not the full original. This prevents re-promoted frames from holding the board for another full TTL cycle.

### Automation lifecycle through the queue

Here's how each automation type flows through the system:

#### calendar_clock (update_interval=1m, TTL=None, should_expire=False)

```
Timer fires every 1m (configurable via update_interval_minutes)
  → generate_frame() renders current date/time
  → push_frame(ttl_s=None, should_expire=False)
  → If clock already on board: same-source update (grid replaced, no TTL to reset)
  → If another source on board: queued in pending (dedup replaces older pending clock)
  → When displaced by another frame: moves to FALLBACK (should_expire=False)
  → TTL=None + nothing pending: stays on board indefinitely (no one to give it to)
  → TTL=None + pending available: pending frame replaces it on next push
```

**Key behavior**: Clock is the "default" frame — it fills gaps between other automations. Its 1-minute updates keep the time fresh while it holds the board. With `TTL=None`, it has no protection — any push from another source immediately replaces it. Because `should_expire=False`, when displaced it moves to fallback and can be re-promoted when the displacing frame's TTL expires.

#### messages_from_library (random freq 30-120m, TTL=5m, should_expire=True)

```
Random timer fires (30-120m, configurable)
  → generate_frame() picks random library entry
  → push_frame(ttl_s=300, should_expire=True)
  → If board free: displayed immediately
  → If board busy: queued in pending
  → When displaced: DROPPED (should_expire=True)
  → Reschedule random timer for next fire
```

#### art_from_library (random freq 60-240m, TTL=10m, should_expire=True)

```
Random timer fires (60-240m, configurable)
  → generate_frame() picks random library entry
  → push_frame(ttl_s=600, should_expire=True)
  → If board free: displayed immediately
  → If board busy: queued in pending
  → When displaced: DROPPED (should_expire=True)
  → Reschedule random timer for next fire
```

**Key behavior**: One-shot content. When displaced, it's gone — never re-shown. This keeps the board fresh with new library picks each time.

#### ai_art_generator (random freq 120-480m, TTL=10m, should_expire=True)

```
Random timer fires (120-480m, configurable)
  → generate_frame() calls LLM to create pixel art
  → push_frame(ttl_s=600, should_expire=True)
  → If board free: displayed immediately
  → If board busy: queued in pending
  → When displaced: DROPPED (should_expire=True)
  → Reschedule random timer for next fire
```

#### ai_message_generator (random freq 60-240m, TTL=5m, should_expire=True)

```
Random timer fires (60-240m, configurable)
  → generate_frame() calls LLM to generate message
  → push_frame(ttl_s=300, should_expire=True)
  → If board free: displayed immediately
  → If board busy: queued in pending
  → When displaced: DROPPED (should_expire=True)
  → Reschedule random timer for next fire
```

**Key behavior**: AI-generated content behaves the same as library content by default — when displaced, it's dropped and not re-shown. All values are UI-configurable; setting `should_expire=False` for these automations would cause them to persist in fallback for re-display instead.

#### calendar_summary_* (cooldown-based, TTL=60m, should_expire=True)

```
Interval timer fires OR calendar state changes
  → _run_cycle() fetches events from HA calendar REST API
  → Filters: only future events (seconds_until >= 0)
  → Partitions: urgent (within reminder threshold) vs upcoming
  → If urgent: push_frame(override_ttl=True, should_expire=True)
      → Force-pushes to the board, displacing whatever was there
      → Displaced frame: dropped if should_expire=True, fallback if False
  → If upcoming + not in cooldown: push_frame(should_expire=True), start cooldown
  → If in cooldown: skip
  → Rotation: if multiple events, rotate through them with display_time_s intervals
  → Countdown updates: same-source pushes to update the countdown text
  → max_age_s: computed from latest event boundary + TTL (stale queued frames auto-drop)
  → When displaced: DROPPED (should_expire=True)
```

**Key behavior**: Calendar frames are time-sensitive and disposable. The cooldown (default 30-120m) prevents the calendar from dominating the board. When displaced, calendar frames are dropped — they don't accumulate in fallback. `max_age_s` prevents stale queued calendar frames from being promoted after events have elapsed.

#### weather_schedule (daily times, TTL=60m, should_expire=True, force_push=False)

```
Daily timer fires at configured times (default 07:30, 15:00)
  → generate_frame() reads weather entity + daily forecast
  → push_frame(ttl_s=3600, override_ttl=force_push, should_expire=True)
  → If force_push=True: override_ttl → immediately takes the board
  → If force_push=False: queued normally, respects active TTL
  → Re-fetches every 15m during TTL window (same-source updates with decreasing remaining TTL)
  → When TTL expires + nothing pending: stays on board (no one to give it to)
  → When displaced: DROPPED (should_expire=True)
```

**Key behavior**: Weather has a longer TTL (60m) and periodic re-fetch keeps it current. By default `force_push=False`, so weather respects the active frame's TTL. With `should_expire=True`, when displaced weather frames are dropped — not moved to fallback. All values are UI-configurable; setting `force_push=True` would make weather always take the board at its scheduled time.

### Sleep window

During the configured sleep window (default 01:00-07:00), board writes are suppressed. The queue continues to tick and manage state, but `_write_to_board()` returns early. On wake, the controller reconciles by writing the currently displayed frame (if any) back to the physical board.

## Architecture

### Card → Controller

```
Lovelace card
  → hass.callService("script", "vestaboard_controller_relay", {command, payload})
  → vestaboard_controller_command event
  → VestaboardControllerApp._on_command()
```

### Automation → Controller (event-based)

```
Automation app (e.g. CalendarClockApp)
  → fire_event("vestaboard_controller_command", command="register_automation", payload=...)
  → VestaboardControllerApp._handle_register_automation_event()
  → creates RemoteAutomationProxy
  → fires vb_auto_config (with automation_id in data) back to automation

Automation app generates a frame
  → fire_event("vestaboard_controller_command", command="push_automation_frame", payload=...)
  → VestaboardControllerApp._handle_push_automation_frame_event()
  → FrameQueue.push()
  → VestaboardClient.write_frame()  [if frame is immediately displayed]
```

### Controller → Automation (event-based)

```
VestaboardControllerApp
  → fires vb_auto_config (with automation_id in data)    (config updates)
  → fires vb_auto_enabled (with automation_id in data)   (enable/disable)
  → fires vb_auto_generate (with automation_id in data)  (on-demand generate requests)
  → fires vestaboard_controller_ready                     (startup/restart announcement)
```

### Periodic tick

```
run_every(tick_interval_s)
  → FrameQueue.tick()
  → VestaboardClient.write_frame()  [if a frame is promoted]
  → VestaboardControllerApp._publish_status()
  → sensor.vestaboard_controller_status
```

## RemoteAutomationProxy

When an automation registers, the controller creates a `RemoteAutomationProxy` object to store its metadata. This proxy holds the same interface fields the controller uses when communicating back (config schema, preview frame, display name, etc.) without requiring a direct Python object reference to the automation app. This design allows automation apps and the controller to run in **different AppDaemon instances** — useful for cross-instance dev testing and isolated deployments.

## Dependencies

- `providers.vestaboard.vestaboard_client` — Vestaboard local API client
- `providers.vestaboard.character_encoding` — character code utilities
- `providers.ha_provisioner` — HA entity provisioning
- `providers.secrets` — env var secret resolution
- `vestaboard_apps._shared.frame_queue` — LIFO frame queue logic
- `vestaboard_apps._shared.config_store` — persistent automation config store

## Self-provisioned entities

| Entity | Type | Purpose |
|--------|------|---------|
| `script.vestaboard_controller_relay` | Script | Relay for card/automation commands; fires `vestaboard_controller_command` event |
| `sensor.vestaboard_controller_status` | Sensor (via `set_state`) | Publishes queue state, automation list, displayed frame, and AI art preview |

## Supported commands (via relay script or direct event)

| Command | Payload fields | Description |
|---------|---------------|-------------|
| `push_frame` | `characters`, `ttl_s`/`ttl_minutes`, `max_age_s`, `override_ttl`, `should_expire`, `template`, `refresh_interval_minutes` | Push a pre-built frame (optionally with a template for live HA data) |
| `register_automation` | `automation_id`, `automation_type`, `display_name`, `display_description`, `default_ttl_s`, `default_max_age_s`, `default_should_expire`, `DEFAULT_UI_CONFIG`, `config_schema`, `preview_frame` | Register an automation app (fired by automation apps on startup) |
| `deregister_automation` | `automation_id` | Deregister an automation and purge its frames (fired by automation apps on terminate) |
| `push_automation_frame` | `automation_id`, `source_label`, `characters`, `ttl_s`, `max_age_s`, `override_ttl`, `should_expire`, `template`, `refresh_interval_minutes` | Push a frame generated by an automation (optionally with a template) |
| `push_ai_art_preview_result` | `characters`, `subject` | Store an AI art preview result without pushing to board (fired by ai_art_generator) |
| `update_next_fire_time` | `automation_id`, `next_fire_time` | Automation notifies controller of its next scheduled fire time (for display in the status sensor) |
| `activate_automation` | `automation_id` | Enable an automation |
| `deactivate_automation` | `automation_id` | Disable an automation and purge its frames |
| `clear_board` | — | Clear all frames and blank the board |
| `set_automation_config` | `automation_id`, `config` (dict) | Update persisted config for an automation |
| `generate_random_message` | `override_ttl` | On-demand frame from `messages_from_library` automation |
| `generate_random_art` | `override_ttl` | On-demand frame from `art_from_library` automation |
| `generate_ai_art` | `subject`, `override_ttl` | Generate and push AI pixel art |
| `generate_ai_art_preview` | `subject` | Generate AI art and store as preview without pushing to board |
| `clear_ai_art_preview` | — | Clear the AI art preview from status |
| `generate_ai_message` | `override_ttl` | On-demand AI-generated message |
| `preview_automation` | `automation_id` | Fire a generate event to any registered automation by ID for instant preview |

## Events fired by the controller

| Event | Data | Description |
|-------|------|-------------|
| `vestaboard_controller_ready` | — | Fired on startup; automations listen for this to re-register after a controller restart |
| `vb_auto_config (with automation_id in data)` | `config` (dict) | Config update pushed to a specific automation |
| `vb_auto_enabled (with automation_id in data)` | `enabled` (bool) | Enable/disable signal pushed to a specific automation |
| `vb_auto_generate (with automation_id in data)` | `generate_kwargs` (dict), `preview_only` (bool) | On-demand generate request to a specific automation |

## Template resolution

Frames can contain `{entity_id}` placeholders (e.g. `"UPS LOAD: {sensor.apc_load}W"`) that are resolved to live Home Assistant entity state at display time. Template resolution is handled by the controller using the shared `template_resolver` utility.

### How it works

1. When `push_frame` or `push_automation_frame` receives a payload with a `template` field, the controller calls `resolve_template()` to substitute all `{entity_id}` placeholders with current HA entity state values.
2. The resolved text is encoded to a 6×22 character grid via `text_to_grid()`, replacing the original `characters`.
3. If the payload also includes `refresh_interval_minutes`, the controller re-resolves the template on each tick (default 15s) once the interval has elapsed. If the resolved grid changes, the board is updated; if unchanged, the write is skipped.
4. Unavailable or unknown entities are substituted with `"N/A"`.
5. Overflow protection: if resolved text exceeds the 132-character grid capacity (6×22), entity values are proportionally truncated.

### Template refresh on tick

The tick loop checks the currently displayed frame for `template` + `refresh_interval_minutes`. When the interval elapses:
- Re-resolves all `{entity_id}` placeholders
- Compares the new grid to what's currently displayed
- Writes to the board only if the grid changed
- Logs at INFO when refreshing, DEBUG when unchanged

## Grid data encoding

All 6×22 character grids are JSON-stringified before being placed in event payloads to prevent Home Assistant from stripping leading/trailing zero cells. The controller and automation mixin both handle the JSON-string round-trip transparently.

## Config reference

| Key | Required | Default | Description |
|-----|----------|---------|-------------|
| `vestaboard_ip_env` | Yes | — | Env var name holding the Vestaboard local IP address |
| `vestaboard_api_key_env` | Yes | — | Env var name holding the Vestaboard local API key |
| `ha_url_env` | Yes | — | Env var name holding the HA base URL |
| `ha_token_env` | Yes | — | Env var name holding the HA long-lived access token |
| `tick_interval_s` | No | `15` | Seconds between queue tick evaluations |
| `automation_config_path` | No | `""` | Filesystem path for persistent automation config YAML |
| `frame_library_path` | No | `""` | Filesystem path for the frame library JSON (passed to automation apps that need it) |
| `sleep_window.enabled` | No | `true` | Whether to suppress board writes during the sleep window |
| `sleep_window.start` | No | `"01:00:00"` | Sleep window start time (HH:MM:SS) |
| `sleep_window.end` | No | `"07:00:00"` | Sleep window end time (HH:MM:SS) |

### YAML example

```yaml
vestaboard_controller:
  module: vestaboard_apps.vestaboard_controller.vestaboard_controller_app
  class: VestaboardControllerApp
  disable: true
  ha_url_env: HA_URL
  ha_token_env: TOKEN
  vestaboard_ip_env: VESTABOARD_IP
  vestaboard_api_key_env: VESTABOARD_API_KEY
  tick_interval_s: 15
  frame_library_path: /media/vestaboard/frame-library.json
  automation_config_path: /media/vestaboard/automation-config.yaml
  sleep_window:
    enabled: true
    start: "01:00:00"
    end: "06:45:00"
```

## Manual setup required

- Ensure the media directory (e.g. `/media/vestaboard/`) exists and is writable by the AppDaemon container.
- The Vestaboard device must be on the local network and have the local API enabled with a known IP and API key.
- Add the Lovelace resource for the configuration card JS after first deploy.

## Upstream/downstream dependencies

- **Upstream**: None — this is the root of the Vestaboard system.
- **Downstream**: All automation apps (`calendar_clock`, `messages_from_library`, `art_from_library`, `ai_art_generator`, `ai_message_generator`, `calendar_summary`, `weather_schedule`) register with this app via HA events at startup. No `dependencies:` YAML entry is needed.
- `vestaboard_configuration` reads `sensor.vestaboard_controller_status` and forwards commands to this app's event.
