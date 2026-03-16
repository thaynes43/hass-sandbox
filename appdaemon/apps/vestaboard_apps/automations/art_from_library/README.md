# Art From Library

Vestaboard automation that randomly selects a pixel art frame from the bundled `art_library.json` file and displays it on the board.

## How it works

1. On `initialize()`, loads `art_library.json` from the same package directory into memory.
2. Registers with the controller via `VestaboardAutomation.register_with_controller()`.
3. If `enabled` is true in YAML args, schedules a random interval timer between `frequency_min_minutes` and `frequency_max_minutes`.
4. When the timer fires, calls `generate_frame()`:
   - Picks a random entry from the in-memory art library.
   - Validates that the frame has exactly 6 rows × 22 columns. Returns a blank grid if validation fails.
   - Returns the frame's `characters` grid.
5. The frame is pushed to the controller with the configured TTL and `should_expire` value, then the next random interval is scheduled.
6. The automation can also be triggered on-demand via the controller's `generate_random_art` command.

## Architecture

```
ArtFromLibraryApp
  → _load_library() reads art_library.json at startup
  → VestaboardAutomation.register_with_controller()
  → run_in(random delay) → generate_frame() → push_frame()
  → VestaboardControllerApp.push_automation_frame()

On-demand:
  vestaboard_controller_command: generate_random_art
  → VestaboardControllerApp._handle_generate_by_type()
  → ArtFromLibraryApp.generate_frame()
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
| `dependencies` | Yes | — | Must include `vestaboard_controller` |
| `controller_app` | No | `vestaboard_controller` | AppDaemon app key of the controller instance |

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
  dependencies:
    - vestaboard_controller
```

## Manual setup required

None. The `art_library.json` file is bundled with the app and loaded at startup.

## Upstream/downstream dependencies

- **Upstream**: `vestaboard_controller` — must be running and registered before this app starts.
- **Downstream**: None.
