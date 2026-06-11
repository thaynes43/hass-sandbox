"""Unit tests for AlertmanagerBridge.

The bridge is pure decision logic — no HTTP and no real Home Assistant
access. We inject an AsyncMock client (``post_alerts``) and drive the
bridge via ``_run(bridge.sync(snapshot))``, where ``snapshot`` maps
checker_id → {"name", "status", "checks", "alerting", "repair_state"}.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Mock hassapi before importing the app package (convention — the bridge
# itself is hassapi-free, but health_checks modules may pull it in).
# ---------------------------------------------------------------------------
mock_hass = MagicMock()
mock_hass.Hass = type("_MockHass", (), {"__init__": lambda self, *a, **kw: None})
sys.modules["hassapi"] = mock_hass

_repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo_root / "apps"))
sys.path.insert(0, str(_repo_root))

from health_checks.shared.alertmanager_bridge import (
    AlertmanagerBridge,
    SOURCE_LABEL,
    default_alertname,
)

_MOD = "health_checks.shared.alertmanager_bridge"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bridge() -> tuple[AlertmanagerBridge, MagicMock, MagicMock]:
    """Return (bridge, client, log) with an AsyncMock post_alerts client."""
    client = MagicMock()
    client.post_alerts = AsyncMock()
    log = MagicMock()
    bridge = AlertmanagerBridge(client, log_fn=log)
    return bridge, client, log


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _checker(
    status: str = "critical",
    name: str = "Westford Spa",
    checks: List[Dict[str, Any]] | None = None,
    alerting: Dict[str, Any] | None = None,
    repair_state: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Build a single resolved-snapshot checker entry."""
    if checks is None:
        if status == "ok":
            checks = [{"name": "Gateway Ping", "status": "ok", "detail": "5ms"}]
        else:
            checks = [{"name": "Gateway Ping", "status": status, "detail": "timeout"}]
    return {
        "name": name,
        "status": status,
        "checks": checks,
        "alerting": alerting or {},
        "repair_state": repair_state or {},
    }


def _batches(client: MagicMock) -> List[List[Dict[str, Any]]]:
    """All alert batches posted so far (one list per post_alerts await)."""
    return [c.args[0] for c in client.post_alerts.await_args_list]


# ---------------------------------------------------------------------------
# Tests — Raising alerts
# ---------------------------------------------------------------------------

class TestRaise:
    def test_critical_posts_one_alert_with_labels_and_annotations(self):
        bridge, client, _log = _make_bridge()

        _run(bridge.sync({"spa": _checker("critical")}))

        client.post_alerts.assert_awaited_once()
        batch = _batches(client)[0]
        assert len(batch) == 1
        alert = batch[0]
        assert alert["labels"] == {
            "alertname": "WestfordSpaUnhealthy",
            "severity": "critical",
            "source": SOURCE_LABEL,
            "checker": "spa",
        }
        assert alert["labels"]["source"] == "appdaemon-health-check"
        assert alert["annotations"]["summary"] == "Westford Spa health check is critical"
        assert alert["annotations"]["description"] == "Gateway Ping: timeout"
        assert alert.get("startsAt")
        assert "endsAt" not in alert
        assert "spa" in bridge.active_alerts

    def test_warning_maps_to_warning_severity(self):
        bridge, client, _log = _make_bridge()

        _run(bridge.sync({"spa": _checker("warning")}))

        alert = _batches(client)[0][0]
        assert alert["labels"]["severity"] == "warning"

    def test_degraded_maps_to_warning_severity(self):
        bridge, client, _log = _make_bridge()

        _run(bridge.sync({"spa": _checker("degraded")}))

        alert = _batches(client)[0][0]
        assert alert["labels"]["severity"] == "warning"

    def test_ok_does_not_post(self):
        bridge, client, _log = _make_bridge()

        _run(bridge.sync({"spa": _checker("ok")}))

        client.post_alerts.assert_not_awaited()
        assert bridge.active_alerts == {}

    def test_unknown_does_not_post(self):
        bridge, client, _log = _make_bridge()

        _run(bridge.sync({"spa": _checker("unknown")}))

        client.post_alerts.assert_not_awaited()
        assert bridge.active_alerts == {}


# ---------------------------------------------------------------------------
# Tests — Resolving alerts
# ---------------------------------------------------------------------------

class TestResolve:
    def test_critical_to_ok_posts_resolve(self):
        bridge, client, _log = _make_bridge()
        _run(bridge.sync({"spa": _checker("critical")}))
        firing = _batches(client)[0][0]
        original_starts_at = firing["startsAt"]
        original_labels = dict(firing["labels"])
        client.post_alerts.reset_mock()

        _run(bridge.sync({"spa": _checker("ok")}))

        client.post_alerts.assert_awaited_once()
        batch = _batches(client)[0]
        assert len(batch) == 1
        resolved = batch[0]
        assert resolved["labels"] == original_labels
        assert resolved["startsAt"] == original_starts_at
        assert resolved.get("endsAt")
        assert "spa" not in bridge.active_alerts

    def test_checker_missing_from_snapshot_keeps_alert(self):
        bridge, client, _log = _make_bridge()
        _run(bridge.sync({"spa": _checker("critical")}))
        client.post_alerts.reset_mock()

        # spa vanished from the snapshot entirely — a dead checker must
        # stay visible, not silently resolve.
        _run(bridge.sync({"other": _checker("ok", name="Other")}))

        client.post_alerts.assert_not_awaited()
        assert "spa" in bridge.active_alerts


# ---------------------------------------------------------------------------
# Tests — Severity escalation (label identity change)
# ---------------------------------------------------------------------------

class TestEscalation:
    def test_warning_to_critical_resolves_old_and_raises_new_in_one_batch(self):
        bridge, client, _log = _make_bridge()

        # Note: _resolved_copy evaluates the .get() default eagerly, so the
        # second sync consumes 3 timestamps (discarded default, endsAt,
        # fresh startsAt) — provide enough values for all calls.
        with patch(
            f"{_MOD}._utcnow_iso", side_effect=["T1", "T2", "T3", "T4", "T5"]
        ):
            _run(bridge.sync({"spa": _checker("warning")}))
            client.post_alerts.reset_mock()
            _run(bridge.sync({"spa": _checker("critical")}))

        client.post_alerts.assert_awaited_once()
        batch = _batches(client)[0]
        assert len(batch) == 2

        resolved, fresh = batch
        # Resolve of the OLD label set, with the old startsAt and endsAt set.
        assert resolved["labels"]["severity"] == "warning"
        assert resolved["startsAt"] == "T1"
        assert resolved.get("endsAt")
        # Fresh alert with the new labels and a NEW startsAt, still firing.
        assert fresh["labels"]["severity"] == "critical"
        assert fresh.get("startsAt")
        assert fresh["startsAt"] != resolved["startsAt"]
        assert "endsAt" not in fresh

        assert bridge.active_alerts["spa"]["labels"]["severity"] == "critical"


# ---------------------------------------------------------------------------
# Tests — Per-checker alerting config
# ---------------------------------------------------------------------------

class TestAlertingConfig:
    def test_disabled_never_fires(self):
        bridge, client, _log = _make_bridge()

        _run(bridge.sync({"spa": _checker("critical", alerting={"enabled": False})}))

        client.post_alerts.assert_not_awaited()
        assert bridge.active_alerts == {}

    def test_disabling_resolves_already_active_alert(self):
        bridge, client, _log = _make_bridge()
        _run(bridge.sync({"spa": _checker("critical")}))
        client.post_alerts.reset_mock()

        # Checker still critical, but alerting got disabled → desired is
        # None, so the active alert is resolved.
        _run(bridge.sync({"spa": _checker("critical", alerting={"enabled": False})}))

        client.post_alerts.assert_awaited_once()
        batch = _batches(client)[0]
        assert len(batch) == 1
        assert batch[0].get("endsAt")
        assert batch[0]["labels"]["alertname"] == "WestfordSpaUnhealthy"
        assert "spa" not in bridge.active_alerts

    def test_alertname_override_used(self):
        bridge, client, _log = _make_bridge()

        _run(
            bridge.sync(
                {
                    "protect": _checker(
                        "critical",
                        name="UniFi Protect",
                        alerting={"alertname": "ProtectEventStreamFrozen"},
                    )
                }
            )
        )

        alert = _batches(client)[0][0]
        assert alert["labels"]["alertname"] == "ProtectEventStreamFrozen"
        assert alert["labels"]["checker"] == "protect"


# ---------------------------------------------------------------------------
# Tests — default_alertname()
# ---------------------------------------------------------------------------

class TestDefaultAlertname:
    def test_camel_cases_name(self):
        assert default_alertname("Westford Spa", "spa") == "WestfordSpaUnhealthy"

    def test_empty_name_falls_back_to_checker_id(self):
        assert default_alertname("", "spa") == "SpaUnhealthy"

    def test_weird_chars_stripped(self):
        assert (
            default_alertname("fan/attic (2.4GHz)!", "fan")
            == "FanAttic24GHzUnhealthy"
        )

    def test_both_empty_falls_back_to_unknown(self):
        assert default_alertname("", "") == "UnknownCheckerUnhealthy"


# ---------------------------------------------------------------------------
# Tests — Description building
# ---------------------------------------------------------------------------

class TestDescription:
    def _description_for(self, checker: Dict[str, Any]) -> str:
        bridge, client, _log = _make_bridge()
        _run(bridge.sync({"spa": checker}))
        return _batches(client)[0][0]["annotations"]["description"]

    def test_joins_non_ok_checks(self):
        checks = [
            {"name": "Gateway Ping", "status": "critical", "detail": "timeout"},
            {"name": "Overall Connection", "status": "warning", "detail": "flapping"},
            {"name": "Staleness", "status": "ok", "detail": "fresh"},
            {"name": "Dependency", "status": "unknown", "detail": "upstream down"},
        ]
        desc = self._description_for(_checker("critical", checks=checks))
        assert desc == "Gateway Ping: timeout; Overall Connection: flapping"

    def test_check_without_detail_uses_status(self):
        checks = [{"name": "Gateway Ping", "status": "critical", "detail": ""}]
        desc = self._description_for(_checker("critical", checks=checks))
        assert desc == "Gateway Ping: critical"

    def test_fallback_when_no_failing_checks(self):
        checks = [{"name": "Gateway Ping", "status": "ok", "detail": "5ms"}]
        desc = self._description_for(_checker("critical", checks=checks))
        assert desc == "Westford Spa reported critical"

    def test_repair_in_progress_appended(self):
        desc = self._description_for(
            _checker("critical", repair_state={"status": "in_progress"})
        )
        assert desc == "Gateway Ping: timeout; auto-repair in progress"

    def test_repair_failed_with_detail_appended(self):
        desc = self._description_for(
            _checker(
                "critical",
                repair_state={"status": "failed", "detail": "Did not recover after 300s"},
            )
        )
        assert desc == "Gateway Ping: timeout; auto-repair FAILED: Did not recover after 300s"

    def test_repair_failed_without_detail(self):
        desc = self._description_for(
            _checker("critical", repair_state={"status": "failed", "detail": ""})
        )
        assert desc == "Gateway Ping: timeout; auto-repair FAILED"

    def test_truncated_to_600_chars_with_ellipsis(self):
        checks = [
            {"name": "Gateway Ping", "status": "critical", "detail": "x" * 700},
        ]
        desc = self._description_for(_checker("critical", checks=checks))
        assert len(desc) <= 600
        assert desc.endswith("…")
        assert desc.startswith("Gateway Ping: xxx")


# ---------------------------------------------------------------------------
# Tests — Same-identity refresh
# ---------------------------------------------------------------------------

class TestAnnotationRefresh:
    def test_annotations_refresh_without_new_post(self):
        bridge, client, _log = _make_bridge()
        checks_v1 = [{"name": "Gateway Ping", "status": "critical", "detail": "timeout 1s"}]
        _run(bridge.sync({"spa": _checker("critical", checks=checks_v1)}))
        original_starts_at = bridge.active_alerts["spa"]["startsAt"]
        client.post_alerts.reset_mock()

        checks_v2 = [{"name": "Gateway Ping", "status": "critical", "detail": "timeout 9s"}]
        _run(bridge.sync({"spa": _checker("critical", checks=checks_v2)}))

        # Same labels → no post; the re-post timer keeps it alive.
        client.post_alerts.assert_not_awaited()
        active = bridge.active_alerts["spa"]
        assert active["annotations"]["description"] == "Gateway Ping: timeout 9s"
        assert active["startsAt"] == original_starts_at


# ---------------------------------------------------------------------------
# Tests — repost_active()
# ---------------------------------------------------------------------------

class TestRepostActive:
    def test_reposts_all_firing_alerts_without_ends_at(self):
        bridge, client, _log = _make_bridge()
        _run(bridge.sync({
            "spa": _checker("critical"),
            "protect": _checker("warning", name="UniFi Protect"),
        }))
        client.post_alerts.reset_mock()

        _run(bridge.repost_active())

        client.post_alerts.assert_awaited_once()
        batch = _batches(client)[0]
        assert len(batch) == 2
        checkers = {a["labels"]["checker"] for a in batch}
        assert checkers == {"spa", "protect"}
        for alert in batch:
            assert "endsAt" not in alert
            assert alert.get("startsAt")
            # Posted entries are copies, not the live active dicts.
            assert alert is not bridge.active_alerts[alert["labels"]["checker"]]

    def test_no_post_when_nothing_active(self):
        bridge, client, _log = _make_bridge()

        _run(bridge.repost_active())

        client.post_alerts.assert_not_awaited()


# ---------------------------------------------------------------------------
# Tests — Client failure tolerance
# ---------------------------------------------------------------------------

class TestClientFailure:
    def test_sync_does_not_raise_and_alert_stays_active(self):
        bridge, client, log = _make_bridge()
        client.post_alerts = AsyncMock(side_effect=Exception("alertmanager down"))

        # Must not raise.
        _run(bridge.sync({"spa": _checker("critical")}))

        # Unsent raise stays active so the next repost retries it.
        assert "spa" in bridge.active_alerts
        warning_calls = [
            c for c in log.call_args_list if c.kwargs.get("level") == "WARNING"
        ]
        assert len(warning_calls) >= 1

    def test_repost_does_not_raise_and_alert_stays_active(self):
        bridge, client, _log = _make_bridge()
        _run(bridge.sync({"spa": _checker("critical")}))
        client.post_alerts = AsyncMock(side_effect=Exception("alertmanager down"))

        # Must not raise.
        _run(bridge.repost_active())

        assert "spa" in bridge.active_alerts
