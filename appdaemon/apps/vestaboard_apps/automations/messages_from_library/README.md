# Messages From Library

Vestaboard automation that randomly selects a saved message from the frame library and displays it on the board. Falls back to a curated list of built-in messages when the library has no qualifying frames.

## How it works

1. On `initialize()`, reads `frame_library_path` from YAML args and registers with the controller by firing a `vestaboard_controller_command` event with `command="register_automation"` — no direct `get_app()` call is needed.
2. Listens for the `vestaboard_controller_ready` event so it automatically re-registers if the controller restarts.
3. The controller fires back a `vb_auto_config` event containing the persisted config (including `enabled`, frequencies, etc.). Only when that event arrives and `enabled` is `true` does the app schedule a random interval timer between `frequency_min_minutes` and `frequency_max_minutes`. The interval is never started at `initialize()` — the comment in that method reads "Do NOT start interval here — wait for config event from controller".
4. When the timer fires, calls `generate_frame()`:
   - Loads the `FrameLibrary` from disk (lazy — loaded once on first use).
   - Filters for frames with `category="message"` and `rating >= min_stars`.
   - Picks one at random and returns its stored `characters` grid.
   - If no qualifying library frames exist, picks from the built-in fallback message list and renders it with a randomly colored border.
5. The frame is pushed to the controller by firing a `vestaboard_controller_command` event with `command="push_automation_frame"`, then the next random interval is scheduled.
6. The automation can also be triggered on-demand via the controller's `generate_random_message` command, which fires a `vb_auto_generate (with automation_id in data)` event back to this app.

## Architecture

```
MessagesFromLibraryApp
  → fire_event("vestaboard_controller_command", command="register_automation")
  → run_in(random delay) → generate_frame()
  → fire_event("vestaboard_controller_command", command="update_next_fire_time")
  → fire_event("vestaboard_controller_command", command="push_automation_frame")
  → VestaboardControllerApp handles push → FrameQueue → VestaboardClient

On-demand:
  vestaboard_controller_command: generate_random_message
  → VestaboardControllerApp._handle_generate_by_type()
  → fires vb_auto_generate (with automation_id in data)
  → MessagesFromLibraryApp._on_generate_event() → generate_frame() → push_frame()
```

## Dependencies

- `providers.vestaboard.character_encoding` — character and color code constants, `text_to_grid`
- `vestaboard_apps._shared.base.VestaboardAutomation` — controller registration and frame push API
- `vestaboard_apps.vestaboard_configuration.frame_library.FrameLibrary` — frame library CRUD

## Self-provisioned entities

None. The controller provisions all shared entities.

## Config reference

### YAML config keys

| Key | Required | Default | Description |
|-----|----------|---------|-------------|
| `module` | Yes | — | `vestaboard_apps.automations.messages_from_library.messages_from_library_app` |
| `class` | Yes | — | `MessagesFromLibraryApp` |
| `frame_library_path` | No | — | Filesystem path to the frame library JSON (must match the path used by `vestaboard_configuration`). If omitted, the app falls back to built-in curated messages. |

### UI-editable config (stored in controller's `automation_config_path`)

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | bool | `false` | Whether the automation fires on its random interval |
| `ttl_minutes` | int | `5` | How long to display the frame before yielding |
| `should_expire` | bool | `true` | If `true`, frame is dropped after TTL rather than added to fallback |
| `frequency_min_minutes` | int | `30` | Minimum minutes between random fires |
| `frequency_max_minutes` | int | `120` | Maximum minutes between random fires |
| `min_stars` | int | `3` | Minimum library frame rating (0–5) required for selection |

### YAML example

```yaml
messages_from_library:
  module: vestaboard_apps.automations.messages_from_library.messages_from_library_app
  class: MessagesFromLibraryApp
  disable: true
  frame_library_path: /media/vestaboard/frame-library.json
```

## Manual setup required

- The `frame_library_path` directory must exist and be writable. The file is created by `vestaboard_configuration` on first run.
- Rate this app's star value applies to library frames saved via the configuration card.

## Upstream/downstream dependencies

- **Upstream**: `vestaboard_controller` — must be running and listening for events before this app starts. Registration happens via HA events; no AppDaemon `dependencies:` entry is needed. The app also listens for `vestaboard_controller_ready` and re-registers automatically if the controller restarts.
- **Downstream**: None.
