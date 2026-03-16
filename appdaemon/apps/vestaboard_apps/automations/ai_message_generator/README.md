# AI Message Generator

Vestaboard automation that uses an LLM to generate witty, personality-driven bordered messages and display them on the board. Falls back to a curated list of built-in messages when the AI provider is unavailable or fails.

## How it works

1. On `initialize()`, registers with the controller via `VestaboardAutomation.register_with_controller()`.
2. If `enabled` is true in YAML args, schedules a random interval timer between `frequency_min_minutes` and `frequency_max_minutes`.
3. When the timer fires, calls `generate_frame()`:
   - Calls `build_simple_text_provider()` from the AI provider registry using `ai_provider_conf.simple_text`.
   - Sends a structured prompt with an AI personality: a clever AI consciousness "trapped inside a flip messageboard." Themes rotate through home status, motivation, smart home humor, weather vibe, family chaos, tech humor, and secret AI thoughts.
   - The prompt instructs the LLM to return a JSON `{"message": "..."}` where the message is a 6-line × 22-character string with a colored tile border on rows 1 and 6.
   - If the returned message is already a properly formatted 6×22 pre-formatted grid, it is decoded directly (supports emoji color tile characters).
   - Otherwise, the raw message text is rendered through `text_to_grid` with a randomly colored border applied.
4. If the AI call fails, falls back to a random message from the built-in fallback list, rendered with a random border color.
5. The frame is pushed to the controller with the configured TTL and `should_expire` value, then the next random interval is scheduled.
6. The automation can also be triggered on-demand via the controller's `generate_ai_message` command (forwarded by `vestaboard_configuration`).

## Architecture

```
AiMessageGeneratorApp
  → VestaboardAutomation.register_with_controller()
  → run_in(random delay) → generate_frame() → push_frame()
  → VestaboardControllerApp.push_automation_frame()

On-demand:
  vestaboard_controller_command: generate_ai_message
  → VestaboardControllerApp._handle_generate_by_type()
  → AiMessageGeneratorApp.generate_frame()
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
| `dependencies` | Yes | — | Must include `vestaboard_controller` |
| `ai_provider_conf` | No | — | AI provider capability bundle config. Must include a `simple_text` bundle name. If omitted, the app always uses the fallback message list |
| `controller_app` | No | `vestaboard_controller` | AppDaemon app key of the controller instance |

### UI-editable config (stored in controller's `automation_config_path`)

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | bool | `false` | Whether the automation fires on its random interval |
| `ttl_minutes` | int | `5` | How long to display the frame before yielding |
| `should_expire` | bool | `true` | If `true`, frame is dropped after TTL rather than added to fallback |
| `frequency_min_minutes` | int | `60` | Minimum minutes between random fires |
| `frequency_max_minutes` | int | `240` | Maximum minutes between random fires |

### YAML example

```yaml
message_generated_by_ai:
  module: vestaboard_apps.automations.ai_message_generator.ai_message_generator_app
  class: AiMessageGeneratorApp
  disable: true
  dependencies:
    - vestaboard_controller
  ai_provider_conf:
    simple_text: openai-default
```

## Manual setup required

- An AI provider must be configured in `providers/ai_providers/model_settings/` with a bundle name matching `ai_provider_conf.simple_text` (e.g. `openai-default`).
- The corresponding API key env var (e.g. `OPENAI_API_KEY`) must be available in the runtime environment.
- Without `ai_provider_conf`, the app still works using the built-in fallback message list.

## Upstream/downstream dependencies

- **Upstream**: `vestaboard_controller` — must be running and registered before this app starts.
- **Downstream**: None.
