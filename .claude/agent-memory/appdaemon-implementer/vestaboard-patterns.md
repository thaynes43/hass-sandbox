---
name: vestaboard-patterns
description: How the Vestaboard controller and its automations talk purely over HA events (no get_app), and the provider client/encoding API plus its test double shape
metadata:
  type: reference
---

# Vestaboard: event-based decoupling + provider

## Event-based communication (confirmed)

- Automations never use `get_app()` to reach the controller — all comms go through `fire_event`.
- The mixin fires `vestaboard_controller_command` with `command=register_automation` and a JSON payload on startup.
- The controller creates a `RemoteAutomationProxy` (metadata only) — no live Python reference to the automation app. It lives in `vestaboard_controller_app.py`, before the main class.
- The controller fires per-automation events back: `vestaboard_automation_config_{id}`, `vestaboard_automation_enabled_{id}`, `vestaboard_automation_generate_{id}`.
- **Grid data (`characters`, `preview_frame`) must be JSON-stringified in event payloads** or HA strips the zeros.
- `_handle_generate_by_type` / `_ai_art` / `_ai_art_preview` fire generate events; results return asynchronously via the `push_automation_frame` or `push_ai_art_preview_result` command.
- `apps-dev.yaml` / `apps-prod.yaml`: **no** `dependencies:` or `controller_app:` on automation entries — that is what lets automations run in a different AppDaemon instance.
- The controller fires `vestaboard_controller_ready` at the end of `_async_startup()` so automations re-register after a restart.

## Provider (`appdaemon/providers/vestaboard/`)

- `vestaboard_client.py`: `VestaboardClient(ip, api_key, session=None)` — async context manager, POST/GET to `http://{ip}:7000/local-api/message`, header `X-Vestaboard-Local-Api-Key`.
- `character_encoding.py`: `CHAR_TO_CODE` (A-Z = 1-26, 1-9 = 27-35, 0 = 36, punctuation), `COLOR_CODES` (red 63 … black 70), `blank_grid()`, `encode_char()`, `encode_text()`, `decode_grid()`, `text_to_grid(justify, align)`.
- Test pattern: inject a `MagicMock` session with `__aenter__`/`__aexit__` as `AsyncMock`; mock `.post`/`.get` to return a mock response with `.status` and `.json = AsyncMock()`.
