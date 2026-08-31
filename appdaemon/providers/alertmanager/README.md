# alertmanager Provider

Minimal async client for the Prometheus Alertmanager v2 API. Posts alert batches to `POST /api/v2/alerts` — on every replica of an HA pair — so AppDaemon apps can raise, refresh, and resolve alerts in the cluster's Alertmanager. Shared library — **not** an AppDaemon app.

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

### Replica fan-out

Alertmanager HA pairs gossip silences and the notification log — but **not** the alert set. Prometheus therefore sends every alert to every replica, and a direct poster has to do the same: posting to a single endpoint leaves each replica with an intermittent, partial view (whichever POSTs happened to land on it), their notification logs never line up, and both replicas page for the same incident. Observed 2026-08-31, when one flapping fan paged twice per transition.

So `post_alerts` fans out:

1. **Resolve** the base URL's host to all of its addresses (`getaddrinfo`, de-duplicated and sorted), rebuilding one per-replica URL per address.
2. **POST concurrently** to `/api/v2/alerts` on every one of them, in a single shared `aiohttp` session.
3. **Succeed on ≥ 1 acceptance.** `AlertmanagerError` is raised only when *no* replica accepted the batch — one replica being down degrades to the old single-endpoint behaviour rather than losing the page.
4. **Log partial failures** at WARNING (with the ok/total count); a fully successful post logs at DEBUG.

The **single-address contract is unchanged**: a host that resolves to one address, a literal IP, or a host that cannot be resolved at all (DNS trouble must never block an alert) takes exactly the original code path, and raises exactly the original error — an HTTP-status `AlertmanagerError` re-raises as-is, anything else is wrapped with the original as `__cause__`.

## Alertmanager semantics callers rely on

- **Identity is the label set.** Repeated posts of the same labels refresh one alert; changing any label creates a different alert.
- **Silence resolves.** If posts stop, the alert auto-resolves after Alertmanager's `resolve_timeout` (5m in this cluster) — so firing alerts must be re-posted more often than that.
- **`endsAt` resolves immediately.** A post carrying a past `endsAt` produces an immediate `[RESOLVED]`.

## Auth and URL configuration

The Alertmanager API is **unauthenticated** — the client sends no credentials. It is only reachable in-cluster, so the URL is plain config (not a secret), set on the health-check controller in `apps.yaml`:

```yaml
alertmanager_url: http://alertmanager-operated.observability.svc.cluster.local:9093
alertmanager_repost_interval_s: 120   # must stay below Alertmanager's resolve_timeout (5m)
```

Point it at the **headless** service (`alertmanager-operated`, created by the Prometheus operator) — that is what makes the fan-out above do anything. A ClusterIP service (`kube-prometheus-stack-alertmanager`) resolves to a single VIP, so the client sees one address, posts once, and the load balancer picks a replica: the old behaviour, and the bug.

## Limitations

- No authentication — intended for an in-cluster service URL.
- Opens a fresh `aiohttp.ClientSession` per post (fine at health-check volumes).

## Dependencies

- `aiohttp` — async HTTP client
- No AppDaemon or HA dependencies — fully testable in isolation

## Used by

- `health_check_controller` — via `health_checks/shared/alertmanager_bridge.py`, which decides when to raise/refresh/resolve one alert per unhealthy checker
