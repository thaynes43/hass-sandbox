# alertmanager Provider

Minimal async client for the Prometheus Alertmanager v2 API. Posts alert batches to `POST /api/v2/alerts` so AppDaemon apps can raise, refresh, and resolve alerts in the cluster's Alertmanager. Shared library — **not** an AppDaemon app.

## Package layout

```
alertmanager/
├── alertmanager_client.py — AlertmanagerClient (aiohttp-based, no AppDaemon dependency)
└── __init__.py            — Package exports (AlertmanagerClient, AlertmanagerError)
```

## API contract

### `AlertmanagerClient`

```python
client = AlertmanagerClient("http://alertmanager:9093", timeout_s=10)

await client.post_alerts([
    {
        "labels": {"alertname": "SpaUnhealthy", "severity": "critical", ...},
        "annotations": {"summary": "...", "description": "..."},
        "startsAt": "<RFC3339>",   # keep stable across re-posts
        "endsAt": "<RFC3339>",     # optional — a past time resolves the alert
    },
])
```

- Each alert dict follows the Alertmanager v2 schema (`labels`, `annotations`, optional `startsAt`/`endsAt`).
- All failures (connection error, timeout, non-2xx response) raise `AlertmanagerError`, so callers can treat Alertmanager downtime as non-fatal — log and retry on the next cycle.
- An empty batch is a no-op.

## Alertmanager semantics callers rely on

- **Identity is the label set.** Repeated posts of the same labels refresh one alert; changing any label creates a different alert.
- **Silence resolves.** If posts stop, the alert auto-resolves after Alertmanager's `resolve_timeout` (5m in this cluster) — so firing alerts must be re-posted more often than that.
- **`endsAt` resolves immediately.** A post carrying a past `endsAt` produces an immediate `[RESOLVED]`.

## Auth and URL configuration

The Alertmanager API is **unauthenticated** — the client sends no credentials. It is only reachable in-cluster, so the URL is plain config (not a secret), set on the health-check controller in `apps.yaml`:

```yaml
alertmanager_url: http://kube-prometheus-stack-alertmanager.observability.svc.cluster.local:9093
alertmanager_repost_interval_s: 120   # must stay below Alertmanager's resolve_timeout (5m)
```

## Limitations

- No authentication — intended for an in-cluster service URL.
- Opens a fresh `aiohttp.ClientSession` per post (fine at health-check volumes).

## Dependencies

- `aiohttp` — async HTTP client
- No AppDaemon or HA dependencies — fully testable in isolation

## Used by

- `health_check_controller` — via `health_checks/shared/alertmanager_bridge.py`, which decides when to raise/refresh/resolve one alert per unhealthy checker
