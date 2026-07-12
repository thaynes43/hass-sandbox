"""Unit tests for the HealthMetrics Prometheus exporter.

Each test builds its own HealthMetrics (isolated CollectorRegistry), so no
global state or exposition server is involved.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo_root))

from providers.metrics.health_metrics import (  # noqa: E402
    STATUS_TO_INT,
    HealthMetrics,
)


def _val(m: HealthMetrics, name: str, labels: dict):
    return m.registry.get_sample_value(name, labels)


def _snapshot(status="critical", check_status="critical"):
    return {
        "fans": {
            "name": "Ceiling Fans",
            "status": status,
            "checks": [
                {"name": "Pink Ping", "status": check_status,
                 "last_changed": "2026-07-12T10:00:00"},
                {"name": "Pink State", "status": "ok",
                 "last_changed": "2026-07-12T09:00:00"},
            ],
            "last_check": "2026-07-12T11:00:00",
            "supports_repair": True,
            "repair_state": {"auto_repair_enabled": True},
        }
    }


class TestSnapshot:
    def test_checker_and_check_status_severity(self):
        m = HealthMetrics()
        m.update_snapshot(_snapshot())
        assert _val(m, "appdaemon_health_checker_status", {"checker_id": "fans"}) == 3
        assert _val(m, "appdaemon_health_check_status",
                    {"checker_id": "fans", "check": "Pink Ping"}) == 3
        assert _val(m, "appdaemon_health_check_status",
                    {"checker_id": "fans", "check": "Pink State"}) == 0

    def test_check_summary_counts(self):
        m = HealthMetrics()
        m.update_snapshot(_snapshot())
        assert _val(m, "appdaemon_health_checks", {"checker_id": "fans", "kind": "total"}) == 2
        assert _val(m, "appdaemon_health_checks", {"checker_id": "fans", "kind": "ok"}) == 1
        assert _val(m, "appdaemon_health_checks", {"checker_id": "fans", "kind": "non_ok"}) == 1

    def test_unknown_status_is_negative_sentinel(self):
        m = HealthMetrics()
        m.update_snapshot(_snapshot(status="unknown", check_status="unknown"))
        assert _val(m, "appdaemon_health_checker_status", {"checker_id": "fans"}) == -1
        assert STATUS_TO_INT["unknown"] == -1

    def test_repair_and_mute_gauges(self):
        m = HealthMetrics()
        m.update_snapshot(_snapshot(), muted_ids={"fans"})
        assert _val(m, "appdaemon_health_checker_supports_repair", {"checker_id": "fans"}) == 1
        assert _val(m, "appdaemon_health_checker_auto_repair_enabled", {"checker_id": "fans"}) == 1
        assert _val(m, "appdaemon_health_checker_muted", {"checker_id": "fans"}) == 1

    def test_status_clears_on_recovery(self):
        m = HealthMetrics()
        m.update_snapshot(_snapshot(status="critical", check_status="critical"))
        m.update_snapshot(_snapshot(status="ok", check_status="ok"))
        assert _val(m, "appdaemon_health_checker_status", {"checker_id": "fans"}) == 0
        assert _val(m, "appdaemon_health_check_status",
                    {"checker_id": "fans", "check": "Pink Ping"}) == 0

    def test_vanished_check_is_removed(self):
        m = HealthMetrics()
        m.update_snapshot(_snapshot())
        # Next report drops "Pink Ping"
        snap = _snapshot()
        snap["fans"]["checks"] = [
            {"name": "Pink State", "status": "ok", "last_changed": "2026-07-12T09:00:00"}
        ]
        m.update_snapshot(snap)
        assert _val(m, "appdaemon_health_check_status",
                    {"checker_id": "fans", "check": "Pink Ping"}) is None

    def test_alert_severity_gauges(self):
        m = HealthMetrics()
        m.update_snapshot(
            _snapshot(),
            firing_by_severity={"critical": 2, "warning": 1},
            pending_by_severity={"critical": 1},
        )
        assert _val(m, "appdaemon_health_alerts_firing", {"severity": "critical"}) == 2
        assert _val(m, "appdaemon_health_alerts_firing", {"severity": "warning"}) == 1
        assert _val(m, "appdaemon_health_alerts_pending", {"severity": "critical"}) == 1

    def test_controller_up(self):
        m = HealthMetrics()
        assert _val(m, "appdaemon_health_controller_up", {}) == 1


class TestRepairEvents:
    def test_success_increments_counter_and_histogram(self):
        m = HealthMetrics()
        m.record_repair_event("fans", "success", 18.0, "Pink Room")
        assert _val(m, "appdaemon_health_repairs_total",
                    {"checker_id": "fans", "device": "Pink Room", "result": "success"}) == 1
        assert _val(m, "appdaemon_health_repair_recovery_duration_seconds_count",
                    {"checker_id": "fans", "result": "success"}) == 1
        # 18s falls in the le=20 bucket
        assert _val(m, "appdaemon_health_repair_recovery_duration_seconds_bucket",
                    {"checker_id": "fans", "result": "success", "le": "20.0"}) == 1

    def test_failed_result_counts_without_requiring_duration(self):
        m = HealthMetrics()
        m.record_repair_event("fans", "failed", None, "Blue Room")
        assert _val(m, "appdaemon_health_repairs_total",
                    {"checker_id": "fans", "device": "Blue Room", "result": "failed"}) == 1

    def test_invalid_result_ignored(self):
        m = HealthMetrics()
        m.record_repair_event("fans", "bogus", 5.0, "X")
        assert _val(m, "appdaemon_health_repairs_total",
                    {"checker_id": "fans", "device": "X", "result": "bogus"}) is None


class TestCustomMetrics:
    def test_gauge_created_and_set(self):
        m = HealthMetrics()
        m.record_custom("cigars", "humidity_percent", 64.0, "gauge", {"sensor": "jar1"})
        assert _val(m, "appdaemon_health_custom_humidity_percent",
                    {"checker_id": "cigars", "sensor": "jar1"}) == 64.0

    def test_counter_increments(self):
        m = HealthMetrics()
        m.record_custom("imagegen", "queue_flushes", 1, "counter")
        m.record_custom("imagegen", "queue_flushes", 1, "counter")
        assert _val(m, "appdaemon_health_custom_queue_flushes_total",
                    {"checker_id": "imagegen"}) == 2

    def test_histogram_observes(self):
        m = HealthMetrics()
        m.record_custom("net", "latency_seconds", 0.2, "histogram")
        assert _val(m, "appdaemon_health_custom_latency_seconds_count",
                    {"checker_id": "net"}) == 1

    def test_bad_type_ignored(self):
        m = HealthMetrics()
        m.record_custom("x", "foo", 1.0, "summary")  # unsupported type
        # no metric registered
        assert not any("custom_foo" in n for n in _all_names(m))

    def test_non_numeric_value_ignored(self):
        m = HealthMetrics()
        m.record_custom("x", "foo", "not-a-number", "gauge")
        assert _val(m, "appdaemon_health_custom_foo", {"checker_id": "x"}) is None


def _all_names(m: HealthMetrics):
    return [
        line.split("{")[0].split(" ")[0]
        for line in m.render().splitlines()
        if line and not line.startswith("#")
    ]
