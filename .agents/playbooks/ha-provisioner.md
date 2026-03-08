# HAProvisioner: integrate self-provisioning into an AppDaemon app

### When to use this

Use this playbook when an AppDaemon app needs to **create HA entities on startup** (helpers like `input_text`, `input_select`, `input_boolean`, `input_number`, or relay scripts). The `ha_provisioner` library at `appdaemon/providers/ha_provisioner/` handles this idempotently — safe to call on every restart.

Also reference this when an agent encounters provisioner errors or needs to understand what the provisioner can and cannot create.

### Critical rule: helpers use WebSocket, not the REST Config Entry Flow

The provisioner creates **scripts** via the HA REST API (`POST /api/config/script/config/{id}`) and creates **helpers** via the HA **WebSocket** API (`{helper_type}/create` command). The old Config Entry Flow REST API (`POST /api/config/config_entries/flow`) does **not** support helper types (`input_text`, `input_select`, etc.) in modern HA versions (confirmed broken on HA 2026.2.x). Never use the Config Entry Flow for helpers.

---

### What the provisioner CAN create

| Entity type | Method | API |
|---|---|---|
| Scripts (`script.*`) | `ensure_script(script_id, config)` | REST `POST /api/config/script/config/{id}` |
| Helpers (`input_text`, `input_select`, `input_boolean`, `input_number`, `input_button`, `input_datetime`, `counter`, `timer`) | `ensure_helper(helper_type, name, **kwargs)` | WebSocket `{helper_type}/create` |

Both methods are **idempotent** — they check if the entity exists first (`GET /api/states/{entity_id}`) and skip creation if found.

### What the provisioner CANNOT create

These require manual user setup. Document them as **prerequisites** in the app's README or playbook.

| Entity type | Why | User action |
|---|---|---|
| `local_file` cameras | Config Entry Flow integration, complex setup | Create in HA UI: Settings > Devices & services > Add Integration > Local File |
| Shell commands | Defined in `configuration.yaml`, no REST/WS API | User edits `configuration.yaml` and restarts HA |
| Lovelace dashboard resources | Custom card JS registration | Use MCP `ha_config_set_dashboard_resource` or manual UI |
| Directories (`/media/...`, `/config/www/...`) | Filesystem operations inside HA container | User SSHs/execs into container and runs `mkdir -p` |
| Placeholder images for `local_file` cameras | Files must exist before camera creation | User copies existing images to the new paths |

---

### Workflow: add provisioner to a new app

**Step 1 — Add `sys.path` fix (if not already present)**

```python
import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parents[2]))

import hassapi as hass
```

For apps in `appdaemon/apps/<package>/module.py`, `.parents[2]` resolves to `appdaemon/`.

**Step 2 — Add `ha_url` and `ha_token_env` to app config (2 files)**

In `apps-dev.yaml`:

```yaml
my_app_dev:
  module: my_app.my_module
  class: MyApp
  ha_url: !secret ha_url
  ha_token_env: TOKEN
```

In `apps-prod.yaml`:

```yaml
my_app:
  module: my_app.my_module
  class: MyApp
  disable: true
  ha_url: !secret ha_url
  ha_token_env: TOKEN
```

`ha_token_env` is the **name** of an environment variable — the provisioner resolves it at runtime via `providers.secrets.resolve_secret()`.

**Step 3 — Implement `_provision_entities` (async method)**

```python
async def _provision_entities(self) -> None:
    ha_url = self.args.get("ha_url")
    ha_token_env = self.args.get("ha_token_env")
    if not ha_url or not ha_token_env:
        self.log("ha_url / ha_token_env not configured — skipping provisioning",
                 level="WARNING")
        return

    from providers.ha_provisioner import HAProvisioner
    prov = HAProvisioner(ha_url=ha_url, ha_token_env=ha_token_env)

    # --- Helpers ---
    for helper_type, name, extra_kwargs in [
        ("input_select", "My App Picker", {"options": ["loading"]}),
        ("input_text", "My App Status Text", {}),
    ]:
        try:
            created = await prov.ensure_helper(helper_type, name, **extra_kwargs)
            slug = prov._helper_slug(helper_type, name)
            entity_id = f"{helper_type}.{slug}"
            msg = "created" if created else "already exists"
            self.log(f"Helper {entity_id} {msg}", level="INFO" if created else "DEBUG")
        except Exception as exc:
            self.log(f"Failed to provision {helper_type} '{name}': {exc!r}", level="ERROR")

    # --- Relay script ---
    try:
        created = await prov.ensure_script("my_app_relay", {
            "alias": "My App Relay",
            "description": "Relays dashboard commands to AppDaemon",
            "mode": "queued",
            "max": 10,
            "fields": {
                "command": {"name": "Command", "required": True, "selector": {"text": {}}},
                "payload": {"name": "Payload", "required": False, "selector": {"text": {}}},
            },
            "sequence": [{
                "event": "my_app_command",
                "event_data": {
                    "command": "{{ command }}",
                    "payload": "{{ payload | default('{}') }}",
                },
            }],
        })
        msg = "created" if created else "already exists"
        self.log(f"Relay script.my_app_relay {msg}", level="INFO" if created else "DEBUG")
    except Exception as exc:
        self.log(f"Failed to provision relay script: {exc!r}", level="ERROR")
```

**Step 4 — Wire async startup in `initialize()`**

AppDaemon's `initialize()` is synchronous. Use `run_in` + `create_task`:

```python
def initialize(self) -> None:
    self.run_in(self._async_startup_wrapper, 0)

def _async_startup_wrapper(self, kwargs) -> None:
    self.create_task(self._async_startup())

async def _async_startup(self) -> None:
    await self._provision_entities()
    # Register listeners AFTER provisioning so entities exist
    self.listen_state(self._on_picker_change, self.picker_entity_id)
    self.listen_event(self._on_command, "my_app_command")
```

**Step 5 — Mock provisioner in unit tests**

```python
from unittest.mock import AsyncMock, MagicMock, patch

mock_prov = MagicMock()
mock_prov.ensure_script = AsyncMock(return_value=False)
mock_prov.ensure_helper = AsyncMock(return_value=False)

with patch("providers.ha_provisioner.HAProvisioner", return_value=mock_prov):
    app.create_task = MagicMock()
    app.initialize()
```

---

### `ensure_helper` kwargs by helper type

| Helper type | Required kwargs | Optional kwargs |
|---|---|---|
| `input_text` | — | `min` (int), `max` (int, **default 100 — set to 255 for summaries**), `mode` ("text"/"password"), `initial` (str) |
| `input_select` | `options` (list[str]) | `initial` (str, must be in options) |
| `input_boolean` | — | `initial` (bool) |
| `input_number` | — | `min` (float), `max` (float), `step` (float), `mode` ("box"/"slider"), `unit_of_measurement` (str) |
| `input_button` | — | `icon` (str) |
| `input_datetime` | — | `has_date` (bool), `has_time` (bool), `initial` (str) |
| `counter` | — | `initial` (int), `minimum` (int), `maximum` (int), `step` (int), `restore` (bool) |
| `timer` | — | `duration` (str, "HH:MM:SS"), `restore` (bool) |

All types accept `icon` (str, e.g. `"mdi:text"`) as an optional kwarg.

---

### Entity ID derivation

```
Name: "Garage Detection Summary Run Id"
Slug: garage_detection_summary_run_id
Entity ID: input_select.garage_detection_summary_run_id
```

Rules: lowercase, non-alphanumeric chars become underscores, consecutive underscores collapsed. If the entity ID you want differs, adjust the `name` parameter.

---

### Common pitfalls

| Symptom | Cause | Fix |
|---------|-------|-----|
| `ValueError: Required secret env var 'TOKEN' is not set` | `python-dotenv` not installed or `.env` file not found | Run `pip install -r appdaemon/requirements.txt`. Verify `.env` exists |
| `ClientResponseError 404: Invalid handler specified` | Using the old Config Entry Flow REST API for helpers | Use WebSocket `{helper_type}/create` — already fixed in current `provisioner.py` |
| `ModuleNotFoundError: No module named 'providers'` | Missing `sys.path` fix | Add `sys.path.append(str(Path(__file__).resolve().parents[2]))` before imports |
| Provisioner runs but helper entity ID doesn't match | Name doesn't slug to the expected entity ID | Check `HAProvisioner._helper_slug(helper_type, name)` output |
| `input_text/set_value` silently fails or truncates | Helper created with default `max: 100` but value exceeds 100 chars | Always pass `max=255` when creating `input_text` helpers for summaries |
| App starts without errors but helpers missing | `ha_url` or `ha_token_env` not in config | Check app config in `apps-dev.yaml` |

### After integrating (don't forget)

- **Both config files**: Add `ha_url` and `ha_token_env` to both `apps-dev.yaml` and `apps-prod.yaml`.
- **Tests**: Mock `HAProvisioner` in unit tests. Verify `ensure_script` and `ensure_helper` are called with expected args.
- **README**: Document any manual prerequisites the provisioner cannot handle.
- **Old helper cleanup**: If converting from manually-created helpers, delete the old ones via MCP after confirming the app provisions correctly (see `ha-helpers.md` for deletion workflow).
