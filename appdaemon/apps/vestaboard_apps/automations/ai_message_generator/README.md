# AI Message Generator

Vestaboard automation that uses an LLM to generate witty, personality-driven bordered messages and display them on the board. Falls back to a curated list of built-in messages when the AI provider is unavailable or fails.

## How it works

1. On `initialize()`, registers with the controller by firing a `vestaboard_controller_command` event with `command="register_automation"` — no direct `get_app()` call is needed.
2. Listens for the `vestaboard_controller_ready` event so it automatically re-registers if the controller restarts.
3. If `enabled` is true in YAML args, schedules a random interval timer between `frequency_min_minutes` and `frequency_max_minutes`.
4. When the timer fires, calls `generate_frame()`:
   - If `prompt_data_bundles_path` is configured, reads the external YAML file, randomly selects a bundle, resolves all entity values via `get_state()`, and builds a data-aware prompt for the LLM.
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
- `vestaboard_apps._shared.template_resolver` — `resolve_entities()` for bundle entity resolution

## Self-provisioned entities

None. The controller provisions all shared entities.

## Config reference

### YAML config keys

| Key | Required | Default | Description |
|-----|----------|---------|-------------|
| `module` | Yes | — | `vestaboard_apps.automations.ai_message_generator.ai_message_generator_app` |
| `class` | Yes | — | `AiMessageGeneratorApp` |
| `ai_provider_conf` | No | — | AI provider capability bundle config. Must include a `simple_text` bundle name. If omitted, the app always uses the fallback message list |
| `prompt_data_bundles_path` | No | — | Absolute path to a YAML file containing prompt data bundles. The file is re-read on every fire (no caching). If omitted or the file is missing, falls back to random personality-driven messages |

### UI-editable config (stored in controller's `automation_config_path`)

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | bool | `false` | Whether the automation fires on its random interval |
| `ttl_minutes` | int | `5` | How long to display the frame before yielding |
| `should_expire` | bool | `true` | If `true`, frame is dropped after TTL rather than added to fallback |
| `frequency_min_minutes` | int | `60` | Minimum minutes between random fires |
| `frequency_max_minutes` | int | `240` | Maximum minutes between random fires |

### Prompt data bundles file

The bundles file is a YAML list. Each entry is a "bundle" that tells the LLM what to write about. On each fire, the app picks one bundle at random, resolves entity values from HA, and feeds them to the AI.

**File format:**

```yaml
# Each entry must have a 'description' key. 'entities' is optional.
- description: "What the AI should write about"
  entities:
    - entity_id: "sensor.xxx"
      description: "Human-readable label for the value"

# Topic-only bundles (no HA data) are also valid:
- description: "Write something motivational about the weekend"
```

**Hot-reload behavior:** The file is read fresh on every fire. You can edit it at any time — changes take effect on the next fire without restarting AppDaemon.

**Error handling:**
- If the file doesn't exist, a WARNING is logged once and the app falls back to random personality-driven messages.
- If the file can't be parsed, a WARNING is logged and the app falls back.
- Individual malformed entries (missing `description` key, not a dict) are skipped with a WARNING; valid entries in the same file still work.

**Best practices for adding bundles:**
- Use valid HA entity_ids that exist in your instance. Invalid entity_ids resolve to `N/A`.
- Keep descriptions concise — they become part of the LLM prompt and affect message quality.
- Test entity_ids in HA Developer Tools → States to verify they return useful values.
- A bundle with no `entities` list is valid — the LLM receives just the topic description.

**Paths:**
- Dev: `/mnt/cephfs-hdd/misc/hass-media/vestaboard/prompt-data-bundles.yaml`
- Prod: `/media/vestaboard/prompt-data-bundles.yaml`

A seed file with examples is provided at `vestaboard_apps/seed-bundles/prompt-data-bundles.yaml`.

### YAML example (basic)

```yaml
message_generated_by_ai:
  module: vestaboard_apps.automations.ai_message_generator.ai_message_generator_app
  class: AiMessageGeneratorApp
  disable: true
  ai_provider_conf:
    simple_text: openai-default
  prompt_data_bundles_path: /media/vestaboard/prompt-data-bundles.yaml
```

## Manual setup required

- An AI provider must be configured in `providers/ai_providers/model_settings/` with a bundle name matching `ai_provider_conf.simple_text` (e.g. `openai-default`).
- The corresponding API key env var (e.g. `OPENAI_API_KEY`) must be available in the runtime environment.
- Without `ai_provider_conf`, the app still works using the built-in fallback message list.
- The prompt data bundles file must be placed at the configured `prompt_data_bundles_path`. If absent, the app gracefully falls back to random messages.

## Upstream/downstream dependencies

- **Upstream**: `vestaboard_controller` — must be running and listening for events before this app starts. Registration happens via HA events; no AppDaemon `dependencies:` entry is needed. The app also listens for `vestaboard_controller_ready` and re-registers automatically if the controller restarts.
- **Downstream**: None.
