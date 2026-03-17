# Art From Library

Vestaboard automation that randomly selects a pixel art frame from the bundled `art_library.json` file and displays it on the board.

## How it works

1. On `initialize()`, loads `art_library.json` from the same package directory into memory.
2. Registers with the controller by firing a `vestaboard_controller_command` event with `command="register_automation"` — no direct `get_app()` call is needed.
3. Listens for the `vestaboard_controller_ready` event so it automatically re-registers if the controller restarts.
4. If `enabled` is true in YAML args, schedules a random interval timer between `frequency_min_minutes` and `frequency_max_minutes`.
5. When the timer fires, calls `generate_frame()`:
   - Picks a random entry from the in-memory art library.
   - Validates that the frame has exactly 6 rows × 22 columns. Returns a blank grid if validation fails.
   - Returns the frame's `characters` grid.
6. The frame is pushed to the controller by firing a `vestaboard_controller_command` event with `command="push_automation_frame"`, then the next random interval is scheduled.
7. The automation can also be triggered on-demand via the controller's `generate_random_art` command, which fires a `vb_auto_generate (with automation_id in data)` event back to this app.

## Architecture

```
ArtFromLibraryApp
  → _load_library() reads art_library.json at startup
  → fire_event("vestaboard_controller_command", command="register_automation")
  → run_in(random delay) → generate_frame()
  → fire_event("vestaboard_controller_command", command="update_next_fire_time")
  → fire_event("vestaboard_controller_command", command="push_automation_frame")
  → VestaboardControllerApp handles push → FrameQueue → VestaboardClient

On-demand:
  vestaboard_controller_command: generate_random_art
  → VestaboardControllerApp._handle_generate_by_type()
  → fires vb_auto_generate (with automation_id in data)
  → ArtFromLibraryApp._on_generate_event() → generate_frame() → push_frame()
```

## Dependencies

- `providers.vestaboard.character_encoding` — color code constants, `blank_grid`
- `vestaboard_apps._shared.base.VestaboardAutomation` — controller registration and frame push API
- `art_library.json` — bundled pixel art frames (in the same package directory)

## Self-provisioned entities

None. The controller provisions all shared entities.

## Config reference

### YAML config keys

| Key | Required | Default | Description |
|-----|----------|---------|-------------|
| `module` | Yes | — | `vestaboard_apps.automations.art_from_library.art_from_library_app` |
| `class` | Yes | — | `ArtFromLibraryApp` |

### UI-editable config (stored in controller's `automation_config_path`)

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | bool | `false` | Whether the automation fires on its random interval |
| `ttl_minutes` | int | `10` | How long to display the frame before yielding |
| `should_expire` | bool | `true` | If `true`, frame is dropped after TTL rather than added to fallback |
| `frequency_min_minutes` | int | `60` | Minimum minutes between random fires |
| `frequency_max_minutes` | int | `240` | Maximum minutes between random fires |
| `min_stars` | int | `2` | Minimum star rating filter (currently applied at config schema level; art_library.json entries include a `rating` field) |

### YAML example

```yaml
art_from_library:
  module: vestaboard_apps.automations.art_from_library.art_from_library_app
  class: ArtFromLibraryApp
  disable: true
```

## Manual setup required

None. The `art_library.json` file is bundled with the app and loaded at startup.

## Upstream/downstream dependencies

- **Upstream**: `vestaboard_controller` — must be running and listening for events before this app starts. Registration happens via HA events; no AppDaemon `dependencies:` entry is needed. The app also listens for `vestaboard_controller_ready` and re-registers automatically if the controller restarts.
- **Downstream**: None.
