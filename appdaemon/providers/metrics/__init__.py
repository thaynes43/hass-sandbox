"""Prometheus metrics for the Health Check framework.

Exposes a process-global :class:`HealthMetrics` (registry + exposition
server) that the controller feeds from published snapshots and forwarded
checker payloads. Import-safe when ``prometheus_client`` is absent.
"""

from .health_metrics import (
    HealthMetrics,
    STATUS_TO_INT,
    get_metrics,
    prometheus_available,
    start_metrics_server,
)

__all__ = [
    "HealthMetrics",
    "STATUS_TO_INT",
    "get_metrics",
    "prometheus_available",
    "start_metrics_server",
]
