# metrics Provider

Prometheus metrics for the health-check framework. The health-check controller feeds one process-wide `HealthMetrics` instance, and a small HTTP server exposes it at `/metrics` for the cluster's `ServiceMonitor` to scrape. This is a shared library, **not** an AppDaemon app. The metric catalogue (names, labels, meaning) and the checker-side opt-in protocol are documented in [`apps/health_checks/README.md` → Prometheus Metrics](../../apps/health_checks/README.md#prometheus-metrics). This README covers the interface.

## Package layout

```
metrics/
├── health_metrics.py — HealthMetrics, STATUS_TO_INT, get_metrics, start_metrics_server, prometheus_available
└── __init__.py       — package exports (the same five names)
```

## API contract

```python
from providers.metrics import get_metrics, prometheus_available, start_metrics_server

if prometheus_available():
    start_metrics_server(9100)          # once per process; later calls are no-ops
metrics = get_metrics()                 # process-global singleton (survives app hot-reloads)

metrics.update_snapshot(resolved, muted_ids, firing_by_severity, pending_by_severity)
metrics.record_repair_event(checker_id, result="success", duration_s=17.2, device="Pink Room")
metrics.record_custom(checker_id, name="humidity_percent", value=64.0, labels={"sensor": "jar1"})
metrics.remove_checker(checker_id)
```

- **`update_snapshot(checkers, muted_ids=None, firing_by_severity=None, pending_by_severity=None)`** sets the base gauges from the controller's resolved snapshot (`checker_id → {name, status, checks[], last_check, supports_repair, repair_state}`). It is called on every `_publish_status()`. A check that disappears from a checker's report has its series removed.
- **`record_repair_event(checker_id, result, duration_s=None, device="")`** counts a repair completion (`result` is `success` or `failed`; anything else is ignored) and observes the recovery duration.
- **`record_custom(checker_id, name, value, metric_type="gauge", labels=None)`** sets or observes a checker-supplied metric, `appdaemon_health_custom_<name>`, which is created on first use. `metric_type` is `gauge`, `counter` or `histogram`.
- **`remove_checker(checker_id)`** drops every per-checker series (status, per-check status and time-in-state, counts, freshness, repair and mute flags) for a checker that no longer exists. The registration watchdog calls it when a checker's app leaves the instance. Removing a checker it never saw is a no-op.
- **`render()`** returns the exposition text, for tests and debugging.
- **`STATUS_TO_INT`** is the severity encoding: `ok=0`, `warning=1`, `degraded=2`, `critical=3`, `unknown=-1`.

## Failure behaviour

- **Missing dependency.** Without `prometheus-client`, `prometheus_available()` is False and every `HealthMetrics` method is a no-op, so the controller and its tests still import and run.
- **Never breaks the caller.** Every public method catches its own exceptions and logs them; a metrics fault can never abort a controller status publish or a watchdog tick.
- **Isolated registry.** Each instance owns its own `CollectorRegistry` (not the global default), so tests can build fresh instances without "Duplicated timeseries" errors, and the server exposes exactly this registry.
- **One server per process.** `start_metrics_server` binds once, even across AppDaemon hot-reloads. A bind failure on an already-bound port counts as running.

## Configuration

Set on the health-check controller in `apps.yaml`:

```yaml
metrics_enabled: true   # default true; false = no server, no-op metrics
metrics_port: 9100      # exposition port scraped by the ServiceMonitor
```
