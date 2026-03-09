# AppDaemon: Implement a new AI provider

### When to use this

Use this playbook when adding a new AI provider (e.g. Gemini, Ollama) to `appdaemon/providers/ai_providers/`. The refactored structure uses capability-specific interfaces and provider packages.

### Critical rule: Honor the capability split

**The refactor split the old `DataProvider` into two distinct capabilities:**

1. **Multimodal text** — image + text → structured JSON (e.g. frame scoring, vision analysis)
2. **Simple text** — text only → structured JSON (e.g. narrative synthesis)
3. **Image generation** — image edit/generation (separate from text providers)

Do **not** reintroduce a single `DataProvider` that handles both image-to-JSON and text-to-JSON. Use separate providers and config paths.

### Critical rule: Model capability validation must run before live calls

**Unsupported model/capability combinations must fail fast with a clear error.** Validate in `provider_settings.py` and in each provider constructor so misconfigurations fail before the first HTTP request.

### Critical rule: Secrets use `_env` keys

Config passes env var **names** (e.g. `api_key_env: GEMINI_API_KEY`). Providers resolve via `providers.secrets.resolve_secret()`. Never hardcode API keys in code, logs, or tests.

### Critical rule: Logging must be safe and explicit

Log provider name, capability mode, model, endpoint/base URL, timeout, truncated request/response previews (e.g. first 400 chars). Never log full tokens or API keys.

---

### Workflow: Add a new AI provider

**Step 1 — Create provider package (1 dir)**

Create a folder under `appdaemon/providers/ai_providers/<provider>/` with an `__init__.py`.

**Step 2 — Add provider-local settings (1 file)**

Add a provider-local settings map or validation in `provider_settings.py` (or a `_settings.py` inside the provider package). Register supported models and capability compatibility.

**Step 3 — Add capability-specific provider classes (3 files)**

Create exactly these files matching the convention:

- `<provider>_image_generation_provider.py` — implements `ImageGenerationProvider`
- `<provider>_multimodal_text_provider.py` — implements `MultimodalTextProvider` (method: `generate_from_image`)
- `<provider>_simple_text_provider.py` — implements `SimpleTextProvider` (method: `generate_from_text`)

**Step 4 — Register in registry**

In `registry.py`:

- Add the new provider to the enum(s): `ImageProviderName`, `MultimodalProviderName`, `SimpleTextProviderName`
- Add a branch in `build_image_provider`, `build_multimodal_text_provider`, `build_simple_text_provider`
- Add config parsing for the new provider's keys

**Step 5 — Add tests**

- Unit tests for each capability class (mock HTTP)
- Registry/config parsing tests
- Provider-settings validation tests (unsupported model fails before request)
- Run full pytest:
  ```bash
  wsl bash -c "cd /mnt/d/labspace/hass-sandbox && source .venv-wsl/bin/activate && cd appdaemon && python -m pytest tests/ -v --tb=short"
  ```

---

### Known-good example: OpenAI package layout

```
appdaemon/providers/ai_providers/
├── image_generation_provider.py   # interface
├── multimodal_text_provider.py    # interface
├── simple_text_provider.py        # interface
├── provider_settings.py           # validation
├── registry.py                    # builders + config
├── types.py                       # re-exports for compat
├── openai/
│   ├── __init__.py
│   ├── openai_image_generation_provider.py
│   ├── openai_multimodal_text_provider.py
│   ├── openai_simple_text_provider.py
│   └── _chat_helpers.py
└── ollama/
    ├── __init__.py
    ├── ollama_image_generation_provider.py
    ├── ollama_multimodal_text_provider.py
    └── ollama_simple_text_provider.py
```

### Known-good example: Provider class pattern

```python
from ..multimodal_text_provider import (
    ExternalDataGenError,
    MultimodalProviderName,
    MultimodalTextProvider,
)
from ..provider_settings import validate_multimodal_model

class OpenAIMultimodalTextProvider(MultimodalTextProvider):
    name = MultimodalProviderName.OPENAI

    def __init__(self, config: OpenAIMultimodalConfig):
        ok, err = validate_multimodal_model("openai", config.model)
        if not ok and err:
            raise ValueError(err)
        self._config = config

    def generate_from_image(self, *, input_image_path, instructions, expected_keys=None):
        ...
```

---

### Common pitfalls

| Symptom | Cause | Fix |
|---------|-------|-----|
| Vague API error for vision request | Model does not support vision | Add validation in `provider_settings.validate_multimodal_model` and call it in the provider constructor |
| `ModuleNotFoundError: providers` | AppDaemon only has `apps/` on `sys.path` | Add AppDaemon root via `import_paths` in `appdaemon.yaml` or `sys.path.append` in app |
| Secret in logs | Logging full response or token | Truncate previews (e.g. 400 chars), never log `api_key` or tokens |
| Single provider for image+text | Reintroducing unified DataProvider | Use separate multimodal and simple-text providers and config paths |

---

### After creating (don't forget)

1. Run full pytest.
2. Verify `manager.py` and `narrative.py` use the correct capability providers (multimodal for scoring, simple-text for narrative).
3. Do **not** manually deploy to production — deployment is automatic on merge to `main` via Docker image build.
