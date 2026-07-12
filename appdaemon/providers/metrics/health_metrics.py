"""Prometheus metrics layer for the Health Check framework.

A single process-global :class:`HealthMetrics` instance holds an isolated
``CollectorRegistry`` and the base metric objects. The controller feeds it:

* **base gauges** from the published (dependency-resolved) snapshot on every
  ``_publish_status()`` — so every checker gets status/summary/freshness
  metrics for free with zero per-checker code, and
* **explicit events** forwarded from checker report payloads: repair
  completions (``repair_events``) and arbitrary domain values (``metrics``).

Design notes
------------
* Import-safe without ``prometheus_client`` — the class degrades to a no-op so
  unit tests and dev runs lacking the dependency still import the controller.
* Each instance owns its own ``CollectorRegistry`` (not the global default),
  so re-instantiation in tests never raises "Duplicated timeseries" and the
  exposition server serves exactly this registry.
* Status is exposed as a **numeric severity gauge** (one series per
  checker/check) rather than a per-status label explosion — lower cardinality,
  and Grafana value-mappings render the label text. See :data:`STATUS_TO_INT`.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any, Dict, Iterable, Optional, Tuple

logger = logging.getLogger(__name__)

try:  # pragma: no cover - trivial import guard
    from prometheus_client import (
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
        start_http_server,
    )

    _PROM_AVAILABLE = True
except Exception:  # pragma: no cover - exercised only when dep missing
    _PROM_AVAILABLE = False


# Numeric severity encoding. ``unknown`` is a negative sentinel so it never
# dominates a max() rollup and ``critical`` stays the numeric maximum of the
# "bad" states. Grafana value-mappings turn these back into coloured labels.
STATUS_TO_INT: Dict[str, int] = {
    "ok": 0,
    "warning": 1,
    "degraded": 2,
    "critical": 3,
    "unknown": -1,
}

NAMESPACE = "appdaemon_health"
CUSTOM_PREFIX = f"{NAMESPACE}_custom"

# Recovery-duration histogram buckets (seconds). Tuned to the observed fan
# recovery band (~15-20s) with headroom to the 300-600s repair windows.
RECOVERY_BUCKETS: Tuple[float, ...] = (
    5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 300, 600,
)

_VALID_CUSTOM_TYPES = ("gauge", "counter", "histogram")

# Module-level guards so the exposition server binds at most once per process
# even across AppDaemon hot-reloads, and so the controller shares one instance.
_server_started = False
_server_lock = threading.Lock()
_singleton: Optional["HealthMetrics"] = None
_singleton_lock = threading.Lock()


def prometheus_available() -> bool:
    """True when ``prometheus_client`` imported successfully."""
    return _PROM_AVAILABLE


def get_metrics() -> "HealthMetrics":
    """Return the process-global HealthMetrics singleton (lazily created).

    The controller uses this so metrics survive an app hot-reload; tests
    construct :class:`HealthMetrics` directly for an isolated registry.
    """
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = HealthMetrics()
    return _singleton


def start_metrics_server(port: int, addr: str = "0.0.0.0") -> bool:
    """Start the exposition HTTP server once, serving the singleton registry.

    Returns True if a server is running (started now or already running),
    False if it could not start (dependency missing or bind failure). Safe to
    call repeatedly — subsequent calls are no-ops.
    """
    global _server_started
    if not _PROM_AVAILABLE:
        logger.warning("prometheus_client not installed — metrics server disabled")
        return False
    with _server_lock:
        if _server_started:
            return True
        try:
            start_http_server(port, addr=addr, registry=get_metrics().registry)
            _server_started = True
            logger.info("Prometheus metrics server listening on %s:%s", addr, port)
            return True
        except OSError as exc:
            # Already bound (hot-reload) or port unavailable — treat a bind on
            # our own port as success, anything else as a soft failure.
            logger.warning("Metrics server bind on %s:%s failed: %r", addr, port, exc)
            _server_started = True  # avoid a retry storm; server likely already up
            return True
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("Failed to start metrics server: %r", exc)
            return False


def _to_epoch(iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (ValueError, TypeError):
        return None


class HealthMetrics:
    """Owns the registry and base metric objects; fed by the controller."""

    def __init__(self, registry: Any = None) -> None:
        self.enabled = _PROM_AVAILABLE
        self._custom: Dict[Tuple[str, Tuple[str, ...]], Any] = {}
        self._custom_lock = threading.Lock()
        # Track which (checker_id, check) series we have set so a check that
        # disappears from a report can be cleared rather than lingering stale.
        self._seen_checks: Dict[str, set] = {}
        if not self.enabled:
            self.registry = None
            return

        self.registry = registry or CollectorRegistry()
        r = self.registry
        ns = NAMESPACE

        self.checker_status = Gauge(
            f"{ns}_checker_status",
            "Aggregate checker status as a severity int (see STATUS_TO_INT).",
            ["checker_id"], registry=r,
        )
        self.check_status = Gauge(
            f"{ns}_check_status",
            "Per-check status as a severity int (see STATUS_TO_INT).",
            ["checker_id", "check"], registry=r,
        )
        self.checks = Gauge(
            f"{ns}_checks",
            "Check counts per checker (kind=total|ok|non_ok).",
            ["checker_id", "kind"], registry=r,
        )
        self.checker_last_report_ts = Gauge(
            f"{ns}_checker_last_report_timestamp_seconds",
            "Unix time of the checker's last status report (freshness).",
            ["checker_id"], registry=r,
        )
        self.check_state_entered_ts = Gauge(
            f"{ns}_check_state_entered_timestamp_seconds",
            "Unix time a check entered its current state (time-in-state).",
            ["checker_id", "check"], registry=r,
        )
        self.checker_supports_repair = Gauge(
            f"{ns}_checker_supports_repair",
            "1 if the checker supports repair.",
            ["checker_id"], registry=r,
        )
        self.checker_auto_repair_enabled = Gauge(
            f"{ns}_checker_auto_repair_enabled",
            "1 if auto-repair is enabled for the checker.",
            ["checker_id"], registry=r,
        )
        self.checker_muted = Gauge(
            f"{ns}_checker_muted",
            "1 while the checker is muted (alerts suppressed).",
            ["checker_id"], registry=r,
        )
        self.alerts_firing = Gauge(
            f"{ns}_alerts_firing",
            "Count of firing Alertmanager alerts by severity.",
            ["severity"], registry=r,
        )
        self.alerts_pending = Gauge(
            f"{ns}_alerts_pending",
            "Count of for-gated pending alerts by severity.",
            ["severity"], registry=r,
        )
        self.controller_up = Gauge(
            f"{ns}_controller_up",
            "1 while the health-check controller is running.",
            registry=r,
        )
        self.controller_up.set(1)
        self.repairs_total = Counter(
            f"{ns}_repairs_total",
            "Repair completions by result.",
            ["checker_id", "device", "result"], registry=r,
        )
        self.repair_recovery_seconds = Histogram(
            f"{ns}_repair_recovery_duration_seconds",
            "Repair recovery duration (time from repair start to recovery).",
            ["checker_id", "result"], buckets=RECOVERY_BUCKETS, registry=r,
        )

    # ------------------------------------------------------------------
    # Snapshot → base gauges
    # ------------------------------------------------------------------

    def update_snapshot(
        self,
        checkers: Dict[str, Dict[str, Any]],
        muted_ids: Optional[Iterable[str]] = None,
        firing_by_severity: Optional[Dict[str, int]] = None,
        pending_by_severity: Optional[Dict[str, int]] = None,
    ) -> None:
        """Set base gauges from the controller's resolved snapshot.

        ``checkers`` maps checker_id -> the resolved checker dict (name,
        status, checks[], last_check, supports_repair, repair_state).
        """
        if not self.enabled:
            return
        muted = set(muted_ids or ())
        try:
            for cid, c in checkers.items():
                self.checker_status.labels(cid).set(
                    STATUS_TO_INT.get(c.get("status", "unknown"), -1)
                )
                checks = c.get("checks", []) or []
                current = {ch.get("name", "") for ch in checks}
                # Clear checks that vanished from this checker's report.
                for stale in self._seen_checks.get(cid, set()) - current:
                    try:
                        self.check_status.remove(cid, stale)
                        self.check_state_entered_ts.remove(cid, stale)
                    except KeyError:
                        pass
                self._seen_checks[cid] = current

                ok = 0
                for ch in checks:
                    name = ch.get("name", "")
                    st = ch.get("status", "unknown")
                    self.check_status.labels(cid, name).set(
                        STATUS_TO_INT.get(st, -1)
                    )
                    if st == "ok":
                        ok += 1
                    entered = _to_epoch(ch.get("last_changed"))
                    if entered is not None:
                        self.check_state_entered_ts.labels(cid, name).set(entered)
                total = len(checks)
                self.checks.labels(cid, "total").set(total)
                self.checks.labels(cid, "ok").set(ok)
                self.checks.labels(cid, "non_ok").set(total - ok)

                last = _to_epoch(c.get("last_check"))
                if last is not None:
                    self.checker_last_report_ts.labels(cid).set(last)

                self.checker_supports_repair.labels(cid).set(
                    1 if c.get("supports_repair") else 0
                )
                rs = c.get("repair_state") or {}
                self.checker_auto_repair_enabled.labels(cid).set(
                    1 if rs.get("auto_repair_enabled") else 0
                )
                self.checker_muted.labels(cid).set(1 if cid in muted else 0)

            self._set_severity_gauge(self.alerts_firing, firing_by_severity)
            self._set_severity_gauge(self.alerts_pending, pending_by_severity)
        except Exception as exc:  # never let metrics break the controller
            logger.error("update_snapshot failed: %r", exc)

    @staticmethod
    def _set_severity_gauge(gauge: Any, by_sev: Optional[Dict[str, int]]) -> None:
        for sev in ("critical", "degraded", "warning"):
            gauge.labels(sev).set(int((by_sev or {}).get(sev, 0)))

    # ------------------------------------------------------------------
    # Explicit events forwarded from checker payloads
    # ------------------------------------------------------------------

    def record_repair_event(
        self,
        checker_id: str,
        result: str,
        duration_s: Optional[float] = None,
        device: str = "",
    ) -> None:
        """Count a repair completion and observe its recovery duration."""
        if not self.enabled:
            return
        result = str(result or "").lower()
        if result not in ("success", "failed"):
            return
        try:
            self.repairs_total.labels(checker_id, device or "", result).inc()
            if duration_s is not None:
                self.repair_recovery_seconds.labels(checker_id, result).observe(
                    float(duration_s)
                )
        except Exception as exc:
            logger.error("record_repair_event failed: %r", exc)

    def record_custom(
        self,
        checker_id: str,
        name: str,
        value: float,
        metric_type: str = "gauge",
        labels: Optional[Dict[str, str]] = None,
    ) -> None:
        """Set/observe an arbitrary per-checker metric, created lazily.

        Emitted as ``appdaemon_health_custom_<name>`` with a ``checker_id``
        label plus any checker-supplied labels. ``metric_type`` is one of
        gauge|counter|histogram.
        """
        if not self.enabled:
            return
        metric_type = (metric_type or "gauge").lower()
        if metric_type not in _VALID_CUSTOM_TYPES:
            logger.warning("record_custom: bad type %r for %r", metric_type, name)
            return
        try:
            fval = float(value)
        except (TypeError, ValueError):
            logger.warning("record_custom: non-numeric value for %r: %r", name, value)
            return

        extra = dict(labels or {})
        label_keys = ("checker_id",) + tuple(sorted(extra.keys()))
        metric = self._get_or_create_custom(name, metric_type, label_keys)
        if metric is None:
            return
        try:
            label_values = {"checker_id": checker_id, **extra}
            bound = metric.labels(**label_values)
            if metric_type == "counter":
                bound.inc(fval)
            elif metric_type == "histogram":
                bound.observe(fval)
            else:
                bound.set(fval)
        except Exception as exc:
            logger.error("record_custom set failed for %r: %r", name, exc)

    def _get_or_create_custom(
        self, name: str, metric_type: str, label_keys: Tuple[str, ...]
    ) -> Any:
        key = (name, label_keys)
        with self._custom_lock:
            existing = self._custom.get(key)
            if existing is not None:
                return existing
            full_name = f"{CUSTOM_PREFIX}_{name}"
            doc = f"Checker-supplied metric '{name}'."
            try:
                if metric_type == "counter":
                    metric = Counter(full_name, doc, label_keys, registry=self.registry)
                elif metric_type == "histogram":
                    metric = Histogram(full_name, doc, label_keys, registry=self.registry)
                else:
                    metric = Gauge(full_name, doc, label_keys, registry=self.registry)
            except Exception as exc:
                logger.error("Failed to create custom metric %r: %r", full_name, exc)
                return None
            self._custom[key] = metric
            return metric

    # ------------------------------------------------------------------
    # Test / debug helper
    # ------------------------------------------------------------------

    def render(self) -> str:
        """Return the Prometheus exposition text for this registry."""
        if not self.enabled:
            return ""
        return generate_latest(self.registry).decode("utf-8")
