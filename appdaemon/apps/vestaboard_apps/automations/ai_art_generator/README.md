# AI Art Generator

Vestaboard automation that uses an LLM to generate pixel art for a given subject and display it on the board. Validates the 6×22 grid output and retries once on validation failure.

## How it works

1. On `initialize()`, registers with the controller by firing a `vestaboard_controller_command` event with `command="register_automation"` — no direct `get_app()` call is needed.
2. Listens for the `vestaboard_controller_ready` event so it automatically re-registers if the controller restarts.
3. If `enabled` is true in YAML args, schedules a random interval timer between `frequency_min_minutes` and `frequency_max_minutes`. When the timer fires, it generates art for the subject `"abstract art"`.
4. `generate_frame(subject=...)` is the public entry point:
   - Builds a structured prompt instructing the LLM to output a JSON `{"grid": [[...], ...]}` with exactly 6 rows × 22 columns using valid Vestaboard codes (0–60, 63–70).
   - Calls `build_simple_text_provider()` from the AI provider registry using `ai_provider_conf.simple_text`.
   - Parses and validates the returned grid. Invalid codes are codes outside `0–60` and `63–70`.
   - Retries once if the first attempt produces an invalid grid.
   - Returns a blank grid if both attempts fail.
5. The frame is pushed to the controller by firing a `vestaboard_controller_command` event with `command="push_automation_frame"`.
6. On-demand generation via the controller's `generate_ai_art` command fires a `vb_auto_generate (with automation_id in data)` event with a user-specified subject.
7. Preview mode via the controller's `generate_ai_art_preview` command fires a `vb_auto_generate (with automation_id in data)` event with `preview_only=True`. The automation generates the art and fires back a `vestaboard_controller_command` event with `command="push_ai_art_preview_result"` — the result is stored in the controller status without being pushed to the board, so the card can show it for review before saving or pushing.

## Architecture

```
AiArtGeneratorApp
  → fire_event("vestaboard_controller_command", command="register_automation")
  → run_in(random delay) → generate_frame(subject="abstract art")
  → fire_event("vestaboard_controller_command", command="update_next_fire_time")
  → fire_event("vestaboard_controller_command", command="push_automation_frame")
  → VestaboardControllerApp handles push → FrameQueue → VestaboardClient

On-demand:
  vestaboard_controller_command: generate_ai_art { subject }
  → VestaboardControllerApp._handle_generate_ai_art()
  → fires vb_auto_generate (with automation_id in data) (preview_only=False)
  → AiArtGeneratorApp._on_generate_event() → generate_frame(subject=subject) → push_frame()

Preview mode:
  vestaboard_controller_command: generate_ai_art_preview { subject }
  → VestaboardControllerApp._handle_generate_ai_art_preview()
  → fires vb_auto_generate (with automation_id in data) (preview_only=True)
  → AiArtGeneratorApp._on_generate_event() → generate_frame(subject=subject)
  → fire_event("vestaboard_controller_command", command="push_ai_art_preview_result")
  → stored in sensor.vestaboard_controller_status.ai_art_preview
```

## Dependencies

- `providers.ai_providers.registry` — builds `SimpleTextProvider` from capability bundle config
- `providers.vestaboard.character_encoding` — `blank_grid`
- `vestaboard_apps._shared.base.VestaboardAutomation` — controller registration and frame push API

## Self-provisioned entities

None. The controller provisions all shared entities.

## Config reference

### YAML config keys

| Key | Required | Default | Description |
|-----|----------|---------|-------------|
| `module` | Yes | — | `vestaboard_apps.automations.ai_art_generator.ai_art_generator_app` |
| `class` | Yes | — | `AiArtGeneratorApp` |
| `ai_provider_conf` | Yes | — | AI provider capability bundle config. Must include a `simple_text` bundle name |

### UI-editable config (stored in controller's `automation_config_path`)

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | bool | `false` | Whether the automation fires on its random interval |
| `ttl_minutes` | int | `10` | How long to display the frame before yielding |
| `should_expire` | bool | `true` | If `true`, frame is dropped after TTL rather than added to fallback |
| `frequency_min_minutes` | int | `120` | Minimum minutes between random fires |
| `frequency_max_minutes` | int | `480` | Maximum minutes between random fires |

### YAML example

```yaml
art_generated_by_ai:
  module: vestaboard_apps.automations.ai_art_generator.ai_art_generator_app
  class: AiArtGeneratorApp
  disable: true
  ai_provider_conf:
    simple_text: openai-pixel-art
```

## Valid Vestaboard codes

| Code | Meaning |
|------|---------|
| `0` | Blank (black background) |
| `1–26` | Letters A–Z |
| `27–36` | Digits 1–9, 0 |
| `37–60` | Punctuation and special characters |
| `63` | Red tile |
| `64` | Orange tile |
| `65` | Yellow tile |
| `66` | Green tile |
| `67` | Blue tile |
| `68` | Violet tile |
| `69` | White tile |
| `70` | Black tile |

Codes `61–62` are not valid and will cause validation failure.

## Manual setup required

- An AI provider must be configured in `providers/ai_providers/model_settings/` with a bundle name matching `ai_provider_conf.simple_text` (e.g. `openai-pixel-art`).
- The corresponding API key env var (e.g. `OPENAI_API_KEY`) must be available in the runtime environment.

## Upstream/downstream dependencies

- **Upstream**: `vestaboard_controller` — must be running and listening for events before this app starts. Registration happens via HA events; no AppDaemon `dependencies:` entry is needed. The app also listens for `vestaboard_controller_ready` and re-registers automatically if the controller restarts.
- **Downstream**: None.
