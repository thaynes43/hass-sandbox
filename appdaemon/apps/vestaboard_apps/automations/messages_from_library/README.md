# Messages From Library

Vestaboard automation that randomly selects a saved message from the frame library and displays it on the board. Falls back to a curated list of built-in messages when the library has no qualifying frames.

## How it works

1. On `initialize()`, reads `frame_library_path` from YAML args and registers with the controller.
2. If `enabled` is true in YAML args, schedules a random interval timer between `frequency_min_minutes` and `frequency_max_minutes`.
3. When the timer fires, calls `generate_frame()`:
   - Loads the `FrameLibrary` from disk (lazy — loaded once on first use).
   - Filters for frames with `category="message"` and `rating >= min_stars`.
   - Picks one at random and returns its stored `characters` grid.
   - If no qualifying library frames exist, picks from the built-in fallback message list and renders it with a randomly colored border.
4. The frame is pushed to the controller with the configured TTL and `should_expire` value, then the next random interval is scheduled.
5. The automation can also be triggered on-demand via the controller's `generate_random_message` command.

## Architecture

```
MessagesFromLibraryApp
  → VestaboardAutomation.register_with_controller()
  → run_in(random delay) → generate_frame() → push_frame()
  → VestaboardControllerApp.push_automation_frame()

On-demand:
  vestaboard_controller_command: generate_random_message
  → VestaboardControllerApp._handle_generate_by_type()
  → MessagesFromLibraryApp.generate_frame()
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
| `dependencies` | Yes | — | Must include `vestaboard_controller` |
| `frame_library_path` | Yes | — | Filesystem path to the frame library JSON (must match the path used by `vestaboard_configuration`) |
| `controller_app` | No | `vestaboard_controller` | AppDaemon app key of the controller instance |

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
  dependencies:
    - vestaboard_controller
  frame_library_path: /media/vestaboard/frame-library.json
```

## Manual setup required

- The `frame_library_path` directory must exist and be writable. The file is created by `vestaboard_configuration` on first run.
- Rate this app's star value applies to library frames saved via the configuration card.

## Upstream/downstream dependencies

- **Upstream**: `vestaboard_controller` — must be running and registered before this app starts.
- **Downstream**: None.
