# AI Message Generator

Vestaboard automation that uses an LLM to generate witty, personality-driven bordered messages and display them on the board. Falls back to a curated list of built-in messages when the AI provider is unavailable or fails.

## How it works

1. On `initialize()`, registers with the controller by firing a `vestaboard_controller_command` event with `command="register_automation"` — no direct `get_app()` call is needed.
2. Listens for the `vestaboard_controller_ready` event so it automatically re-registers if the controller restarts.
3. If `enabled` is true in YAML args, schedules a random interval timer between `frequency_min_minutes` and `frequency_max_minutes`.
4. When the timer fires, calls `generate_frame()`:
   - If `prompt_data_bundles` is configured, randomly selects a bundle, resolves all entity values via `get_state()`, and builds a data-aware prompt for the LLM.
   - Otherwise, calls `build_simple_text_provider()` from the AI provider registry using `ai_provider_conf.simple_text`.
   - Sends a structured prompt with an AI personality: a clever AI consciousness "trapped inside a flip messageboard." Themes rotate through home status, motivation, smart home humor, weather vibe, family chaos, tech humor, and secret AI thoughts.
   - The prompt instructs the LLM to return a JSON `{"message": "..."}` where the message is a 6-line × 22-character string with a colored tile border on rows 1 and 6.
   - If the returned message is already a properly formatted 6×22 pre-formatted grid, it is decoded directly (supports emoji color tile characters).
   - Otherwise, the raw message text is rendered through `text_to_grid` with a randomly colored border applied.
5. If the AI call fails, falls back to a random message from the built-in fallback list, rendered with a random border color.
6. The frame is pushed to the controller by firing a `vestaboard_controller_command` event with `command="push_automation_frame"`, then the next random interval is scheduled.
7. On-demand generation via the controller's `generate_ai_message` command fires a `vb_auto_generate (with automation_id in data)` event back to this app.

## Architecture

```
AiMessageGeneratorApp
  → fire_event("vestaboard_controller_command", command="register_automation")
  → run_in(random delay) → generate_frame()
  → fire_event("vestaboard_controller_command", command="update_next_fire_time")
  → fire_event("vestaboard_controller_command", command="push_automation_frame")
  → VestaboardControllerApp handles push → FrameQueue → VestaboardClient

On-demand:
  vestaboard_controller_command: generate_ai_message
  → VestaboardControllerApp._handle_generate_by_type()
  → fires vb_auto_generate (with automation_id in data)
  → AiMessageGeneratorApp._on_generate_event() → generate_frame() → push_frame()
```

## Dependencies

- `providers.ai_providers.registry` — builds `SimpleTextProvider` from capability bundle config
- `providers.vestaboard.character_encoding` — character/color code constants, `text_to_grid`
- `vestaboard_apps._shared.base.VestaboardAutomation` — controller registration and frame push API

## Self-provisioned entities

None. The controller provisions all shared entities.

## Config reference

### YAML config keys

| Key | Required | Default | Description |
|-----|----------|---------|-------------|
| `module` | Yes | — | `vestaboard_apps.automations.ai_message_generator.ai_message_generator_app` |
| `class` | Yes | — | `AiMessageGeneratorApp` |
| `ai_provider_conf` | No | — | AI provider capability bundle config. Must include a `simple_text` bundle name. If omitted, the app always uses the fallback message list |
| `prompt_data_bundles` | No | `[]` | List of topic bundles with HA entity references. Each fire randomly picks a bundle, resolves entity values, and feeds them to the LLM for data-driven messages. See below |

### UI-editable config (stored in controller's `automation_config_path`)

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | bool | `false` | Whether the automation fires on its random interval |
| `ttl_minutes` | int | `5` | How long to display the frame before yielding |
| `should_expire` | bool | `true` | If `true`, frame is dropped after TTL rather than added to fallback |
| `frequency_min_minutes` | int | `60` | Minimum minutes between random fires |
| `frequency_max_minutes` | int | `240` | Maximum minutes between random fires |

### Prompt data bundles

When `prompt_data_bundles` is configured, each fire randomly selects a bundle instead of generating a fully random message. The bundle provides:
- `description` — what the LLM should write about
- `entities` — list of `{entity_id, description}` objects whose current HA state values are resolved and fed to the LLM

The LLM receives the actual live data and writes a message incorporating it. This makes AI messages informative about the smart home rather than purely random.

If no bundles are configured, the app falls back to its existing random personality-driven behavior.

### YAML example (basic)

```yaml
message_generated_by_ai:
  module: vestaboard_apps.automations.ai_message_generator.ai_message_generator_app
  class: AiMessageGeneratorApp
  disable: true
  ai_provider_conf:
    simple_text: openai-default
```

### YAML example (with prompt data bundles)

```yaml
message_generated_by_ai:
  module: vestaboard_apps.automations.ai_message_generator.ai_message_generator_app
  class: AiMessageGeneratorApp
  disable: true
  ai_provider_conf:
    simple_text: openai-default
  prompt_data_bundles:
    - description: "Report on home security cameras"
      entities:
        - entity_id: "binary_sensor.front_door_motion"
          description: "front door camera motion status"
        - entity_id: "binary_sensor.garage_motion"
          description: "garage camera motion status"
    - description: "Report on home energy and UPS"
      entities:
        - entity_id: "sensor.apc_2700w_load"
          description: "UPS load percentage"
        - entity_id: "sensor.apc_2700w_battery"
          description: "UPS battery level"
    - description: "Report on flood/leak sensors"
      entities:
        - entity_id: "binary_sensor.water_leak_sensor"
          description: "water leak detection status"
```

## Manual setup required

- An AI provider must be configured in `providers/ai_providers/model_settings/` with a bundle name matching `ai_provider_conf.simple_text` (e.g. `openai-default`).
- The corresponding API key env var (e.g. `OPENAI_API_KEY`) must be available in the runtime environment.
- Without `ai_provider_conf`, the app still works using the built-in fallback message list.

## Upstream/downstream dependencies

- **Upstream**: `vestaboard_controller` — must be running and listening for events before this app starts. Registration happens via HA events; no AppDaemon `dependencies:` entry is needed. The app also listens for `vestaboard_controller_ready` and re-registers automatically if the controller restarts.
- **Downstream**: None.
