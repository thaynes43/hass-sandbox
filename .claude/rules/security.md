# Security rules (always apply in appdaemon/)

Full policy: `.cursor/rules/security-policy.mdc`

## Mandatory rules

**S1 — No credentials in app code**: Files under `appdaemon/apps/` must never contain hardcoded API keys, tokens, passwords. App configs pass only env var names (e.g. `api_key_env: OPENAI_API_KEY`). Providers resolve via `providers.secrets.resolve_secret()`.

**S2 — All external HTTP in providers**: Code making HTTP requests to external services lives in `appdaemon/providers/` only, never in `appdaemon/apps/`.

**S3 — Never expose secrets to frontend**: No credentials in `fire_event` payloads, `set_state` attributes, card JS, or WebSocket data.

**S4 — secrets.yaml / .env gitignored**: Never commit these. Production uses Kubernetes ExternalSecret.

**S5 — Test placeholders only**: Tests use obvious fake values (`"test-key"`, `"tok-123"`). Integration tests requiring real secrets must be env-gated.

**S6 — Safe logging**: Never log full tokens/API keys. Mask: `****{last4}` if needed.

**S7 — `_env` key pattern**: All API keys/tokens in app configs use `_env` suffix (e.g. `api_key_env`, `ha_token_env`). URLs may use `!secret` from `secrets.yaml`.

## Before deploying to production

Run the security audit: `.agents/playbooks/security-audit.md`
