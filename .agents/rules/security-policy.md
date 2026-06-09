# Security policy (AppDaemon)

> **Applies to:** `appdaemon/**` — always read before working in scope.

This rule is **always applied** when working in `appdaemon/`. It defines mandatory security practices. Violations must be fixed before production deploy.

## S1 — No credentials in app code

Files under `appdaemon/apps/` must **never** contain hardcoded API keys, tokens, passwords, or secrets. App configs pass only env var **names** (e.g., `api_key_env: OPENAI_API_KEY`). Apps forward these names to provider constructors. Apps must never store resolved secret values as module-level constants, log them, or include them in string literals.

## S2 — All external API calls in providers

Code that makes HTTP requests to external services (OpenAI, Immich, HA REST API, etc.) must live in `appdaemon/providers/` subdirectories, **never** in `appdaemon/apps/`. Apps call provider classes; providers make the HTTP calls.

## S3 — Never expose secrets to the frontend

Credentials, tokens, API keys, and internal service URLs must **never** appear in:

- `fire_event` payloads
- `set_state` attributes (sensor state published to HA)
- Custom card JavaScript files (under `apps/`)
- Any data sent over WebSocket to the browser

## S4 — Secrets YAML handling

`secrets.yaml` is `.gitignored` and must never be committed. `.env` is also `.gitignored`. Production secrets come from Kubernetes ExternalSecret injected as environment variables. The only acceptable `!secret` reference is in `appdaemon.yaml` for the HASS plugin.

## S5 — Test credentials

Test files must use obvious placeholder values (e.g., `"test-key"`, `"tok-123"`). Integration tests that load real secrets from environment variables must be clearly marked and never run in CI without secret masking.

## S6 — Logging hygiene

Never log full tokens or API keys. If credential presence must be logged for debugging, log only a masked version (e.g., `"token: ****{last4}"`).

## S7 — Env-var secret resolution for API keys

All API keys and tokens in app configs must use the `_env` key pattern (e.g., `api_key_env: OPENAPI_TOKEN`, `ha_token_env: TOKEN`, `immich_api_key_env: IMMICH_API_KEY`). Providers resolve these at runtime via `providers.secrets.resolve_secret()`. This prevents the AppDaemon admin UI from displaying them. URLs (e.g., `ha_url`, `immich_url`) may use `!secret` from `secrets.yaml` — intercluster endpoints are acceptable there.

### Known exclusions

- `appdaemon/appdaemon.yaml` lines 15–16 (`!secret ha_url`, `!secret token`) — HASS plugin config required by AppDaemon, not app code. These are NOT displayed in the per-app config UI.

### Enforcement

Before deploying to production, run the security audit playbook (`.agents/playbooks/security-audit.md`). See `.agents/playbooks/appdaemon-deploy.md` for the deploy workflow.
