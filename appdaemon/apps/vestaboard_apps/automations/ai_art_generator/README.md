# AI Art Generator

Vestaboard automation that uses an LLM to generate pixel art for a given subject and display it on the board. Validates the 6×22 grid output and retries once on validation failure.

## How it works

1. On `initialize()`, registers with the controller by firing a `vestaboard_controller_command` event with `command="register_automation"` — no direct `get_app()` call is needed.
2. Listens for the `vestaboard_controller_ready` event so it automatically re-registers if the controller restarts.
3. If `enabled` is true in YAML args, schedules a random interval timer between `frequency_min_minutes` and `frequency_max_minutes`. When the timer fires:
   - If `art_prompt_bundles_path` is configured and the file contains valid bundles, randomly picks one. If the bundle has `entities`, resolves their HA state values and includes them as context data in the subject.
   - Otherwise, generates art for the default subject `"abstract art"`.
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
  → run_in(random delay) → _pick_art_bundle() → generate_frame(subject=...)
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
- `vestaboard_apps._shared.template_resolver` — `resolve_entities()` for art bundle entity resolution

## Self-provisioned entities

None. The controller provisions all shared entities.

## Config reference

### YAML config keys

| Key | Required | Default | Description |
|-----|----------|---------|-------------|
| `module` | Yes | — | `vestaboard_apps.automations.ai_art_generator.ai_art_generator_app` |
| `class` | Yes | — | `AiArtGeneratorApp` |
| `ai_provider_conf` | Yes | — | AI provider capability bundle config. Must include a `simple_text` bundle name |
| `art_prompt_bundles_path` | No | — | Absolute path to a YAML file containing art prompt bundles. The file is re-read on every fire (no caching). If omitted or the file is missing, falls back to `"abstract art"` as the default subject |

### UI-editable config (stored in controller's `automation_config_path`)

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | bool | `false` | Whether the automation fires on its random interval |
| `ttl_minutes` | int | `10` | How long to display the frame before yielding |
| `should_expire` | bool | `true` | If `true`, frame is dropped after TTL rather than added to fallback |
| `frequency_min_minutes` | int | `120` | Minimum minutes between random fires |
| `frequency_max_minutes` | int | `480` | Maximum minutes between random fires |

### Art prompt bundles file

The bundles file is a YAML list. Each entry is a "bundle" that provides a subject for pixel art generation. On each random fire, the app picks one bundle at random.

**File format:**

```yaml
# Simple subject-only bundles:
- subject: "a sunset over the ocean"
- subject: "a cat sleeping on a keyboard"

# Bundle with HA entity context for data-driven art:
- subject: "weather-inspired pixel art"
  entities:
    - entity_id: "weather.forecast_home"
      description: "current weather condition and temperature"
```

**Hot-reload behavior:** The file is read fresh on every fire. You can edit it at any time — changes take effect on the next fire without restarting AppDaemon.

**Error handling:**
- If the file doesn't exist, a WARNING is logged once and the app falls back to `"abstract art"`.
- If the file can't be parsed, a WARNING is logged and the app falls back.
- Individual malformed entries (missing `subject` key, not a dict) are skipped with a WARNING; valid entries in the same file still work.

**How entity context works:** When a bundle has `entities`, the app resolves their current HA state values and appends them as context data to the subject string. The LLM then uses this context to create more relevant art (e.g., sunny weather → warm colors, rainy → cool colors).

**Paths:**
- Dev: `/mnt/cephfs-hdd/misc/hass-media/vestaboard/art-prompt-bundles.yaml`
- Prod: `/media/vestaboard/art-prompt-bundles.yaml`

A seed file with examples is provided at `vestaboard_apps/seed-bundles/art-prompt-bundles.yaml`.

### YAML example

```yaml
art_generated_by_ai:
  module: vestaboard_apps.automations.ai_art_generator.ai_art_generator_app
  class: AiArtGeneratorApp
  disable: true
  ai_provider_conf:
    simple_text: openai-pixel-art
  art_prompt_bundles_path: /media/vestaboard/art-prompt-bundles.yaml
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
- The art prompt bundles file must be placed at the configured `art_prompt_bundles_path`. If absent, the app gracefully falls back to generating abstract art.

## Upstream/downstream dependencies

- **Upstream**: `vestaboard_controller` — must be running and listening for events before this app starts. Registration happens via HA events; no AppDaemon `dependencies:` entry is needed. The app also listens for `vestaboard_controller_ready` and re-registers automatically if the controller restarts.
- **Downstream**: None.
