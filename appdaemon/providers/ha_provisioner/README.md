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

### `AssistExposureClient(ha_url, ha_token_env)`

Read and change Home Assistant's **Assist exposure list** — the only security boundary in front of the LLM voice agents. Same construction pattern as `HaAdminClient`, and likewise requires an **admin** token: all three WebSocket commands below are decorated `@websocket_api.require_admin`.

- `await list_exposed_entities(assistant="conversation") -> list[str]` — `homeassistant/expose_entity/list`. HA answers `{"exposed_entities": {entity_id: {assistant: True}}}` and only includes assistants whose `should_expose` is truthy, so an entity absent from the map (or whose map lacks the assistant) is simply not exposed. Returns a sorted list.
- `await list_entity_platforms(entity_ids) -> dict[str, str]` — `config/entity_registry/get_entries` for exactly those ids (chunks of 200), reduced to `{entity_id: platform}` (the supplying integration, lowercased). Never `config/entity_registry/list`: the whole registry is a 9.5 MB frame on the live instance, over the WebSocket client's 4 MB limit. Ids are normalised first (`strip().lower()`, de-duplicated) and **the result is keyed by the normalised id**, so look results up with the same normalisation. Ids that are not well-formed are skipped with a WARNING (HA validates the list all-or-nothing); entities without a registry entry are absent from the result. Read `device_class` from entity state, not from here.
- `await set_exposure(entity_ids, should_expose, assistant="conversation") -> ExposureChange` — `homeassistant/expose_entity`, the only bulk primitive HA offers. Sends one command for the whole list. Ids are normalised and de-duplicated like the read path; malformed ids are left out with a WARNING, because HA validates the list all-or-nothing and would otherwise reject the whole batch. An empty `sent` is a no-op with no round trip.

  It returns an **`ExposureChange`**, not a count: `sent` is the normalised ids HA accepted, `skipped` is the ids left out. Callers must partition their own work by `sent` — a caller that reads "no exception" as "everything applied" will report a skipped entity as handled while it is still exposed, which on a security boundary is a false all-clear. There is deliberately no `__len__` or `__bool__` on it: collapsing the result back to one number is the habit that caused the bug.

Every call raises `RuntimeError` when HA answers `success: false`, so a failed un-expose can never be mistaken for a successful one. Used by the `assist_exposure_guard` app.

### `await local_file_status(ha_url, url_path, timeout_s=5.0, session=None) -> int`

Unauthenticated `HEAD` probe against a HA `/local/...` static URL, returning the HTTP status HA answered. Never raises: a connection error, a timeout, or a URL that cannot be built all return the module constant `STATUS_UNREACHABLE` (`-1`), which is deliberately negative so it can never collide with a real status.

It deliberately sends **no** `Authorization` header: `/local/...` maps to `/config/www` and is served unauthenticated, so attaching the long-lived token would leak it for no benefit (security policy S3/S6). Redirects are not followed, so a 302 to the HA login page reads as 302 rather than as a served file. `build_local_url(ha_url, url_path)` is exported alongside it for callers that need the joined URL.

**Prefer this over `local_file_exists` whenever the caller reports failure to an operator.** The distinction is operational, not cosmetic: `404` means the file is not there (usually transient and self-healing), while a redirect, a 401/403, or `STATUS_UNREACHABLE` means `ha_url` or HA itself is wrong and *nothing will fix itself*. Collapsing them makes a permanent misconfiguration look like a transient miss.

### `await local_file_exists(ha_url, url_path, timeout_s=5.0, session=None) -> bool`

Thin wrapper over `local_file_status` — `True` **only** on HTTP 200. A 404, redirect, connection error or timeout all return `False`, and it never raises.

Used by `photo_frame_viewer` (via `local_file_status`) to confirm HA is really serving a staged generation before publishing it — HA kills `shell_command`s at 60s, so the staging command's return value cannot be trusted.

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
| `exposure_client.py` | `AssistExposureClient` + `ExposureChange` — read/write the Assist exposure list, and per-id registry platform lookups |
| `ha_rest_client.py` | `HaRestClient` — low-level async HTTP + WebSocket wrapper |
| `local_file_check.py` | `local_file_status()` / `local_file_exists()` / `build_local_url()` — unauthenticated `/local/...` probe |
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

`local_file_status` / `local_file_exists` users:
- `photo_frame_viewer` — staged-generation verification (uses `local_file_status` so its give-up WARNING can tell a 404 apart from a bad `ha_url`)

## Detailed playbook

For full kwargs reference, common pitfalls, and integration walkthrough, see `.agents/playbooks/ha-provisioner.md`.
