# HA Provisioner

Idempotent provisioning library for Home Assistant scripts and helpers. Apps call this on startup to ensure all required HA entities exist without manual user setup.

## API

### `HAProvisioner(ha_url, ha_token_env)`

- `ha_url` or `ha_url_env` — HA base URL value, either inline or resolved from an environment variable
- `ha_token_env` — env var **name** containing a long-lived access token (resolved at runtime via `providers.secrets.resolve_secret()`)

### `await ensure_script(script_id, config) -> bool`

Creates a script via the HA REST API (`POST /api/config/script/config/{id}`). Returns `True` if created, `False` if it already exists.

### `await ensure_helper(helper_type, name, **kwargs) -> bool`

Creates a helper via the HA WebSocket API (`{helper_type}/create` command). Returns `True` if created, `False` if it already exists.

Supported helper types: `input_text`, `input_select`, `input_boolean`, `input_number`, `input_button`, `input_datetime`, `counter`, `timer`.

### `HaAdminClient(ha_url, ha_token_env)`

Admin-level HA REST operations beyond provisioning. Mirrors `HAProvisioner`'s construction pattern (`ha_url` + `ha_token_env`, token resolved at runtime via `providers.secrets.resolve_secret()`) and likewise requires a long-lived **admin** access token.

- `await list_config_entries(domain=None) -> list[dict]` — config entries via `GET /api/config/config_entries/entry`, optionally filtered by integration domain. Each entry includes `entry_id`, `domain`, `title`, `state` (e.g. `loaded`, `not_loaded`) and `source` (e.g. `user`, `ignore`) — callers filtering for the live entry should match `state == "loaded"`.
- `await reload_config_entry(entry_id)` — `POST /api/config/config_entries/entry/{entry_id}/reload`; returns HA's response payload.
- `await render_template(template) -> str` — server-side Jinja2 rendering via `POST /api/template`. Always returns a string (re-serialises if a proxy/HA version hands back parsed JSON). Useful for registry-backed lookups unavailable through plain state reads, e.g. `integration_entities('unifiprotect')`.

#### Why REST instead of `call_service` for reload?

AppDaemon cancels in-flight service calls after ~60 seconds, and a config-entry reload (e.g. UniFi Protect re-establishing its websocket) can exceed that. Going through the REST API keeps the timeout under our control and returns HA's actual response instead of a cancelled future.

## Why WebSocket for helpers?

The REST Config Entry Flow API (`POST /api/config/config_entries/flow`) does **not** support helper types in modern HA versions (confirmed broken on HA 2026.2.x). Helpers must be created via the WebSocket `{helper_type}/create` command.

## Entity ID derivation

The helper name is slugified to produce the entity ID:

```
Name: "Garage Detection Summary Run Id"
Slug: garage_detection_summary_run_id
Entity ID: input_select.garage_detection_summary_run_id
```

Rules: lowercase, non-alphanumeric chars become underscores, consecutive underscores collapsed.

## Files

| File | Purpose |
|------|---------|
| `provisioner.py` | `HAProvisioner` — high-level idempotent ensure API |
| `ha_admin_client.py` | `HaAdminClient` — config-entry inspection/reload + server-side template rendering |
| `ha_rest_client.py` | `HaRestClient` — low-level async HTTP + WebSocket wrapper |
| `__init__.py` | Package exports |

## Dependencies

- `aiohttp` — HTTP and WebSocket client
- `providers.secrets` — env var resolution

## Used by

All apps that self-provision HA entities:
- `detection_summary_app`
- `detection_summary_viewer`
- `immich_fetcher`
- `photo_frame_viewer`
- `health_checks` (controller + repair-capable checkers provision helpers/scripts)

`HaAdminClient` users:
- `health_checks/checker_apps/protect_health_checker` — `render_template` for `integration_entities` sensor discovery; `list_config_entries` + `reload_config_entry` for the websocket-freeze auto-heal

## Detailed playbook

For full kwargs reference, common pitfalls, and integration walkthrough, see `.agents/playbooks/ha-provisioner.md`.
