"""Unit tests for AlertmanagerBridge.

The bridge is pure decision logic — no HTTP and no real Home Assistant
access. We inject an AsyncMock client (``post_alerts``) and drive the
bridge via ``_run(bridge.sync(snapshot))``, where ``snapshot`` maps
checker_id → {"name", "status", "checks", "alerting", "repair_state"}.
"""

from __future__ import annotations

import asyncio
import datetime
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


def _make_gated_bridge(default_for_seconds, for_overrides=None, repair_hold_cap_s=1800):
    """Bridge with a for-duration gate and a manually-advanceable clock.

    Returns (bridge, client, log, advance) where ``advance(seconds)`` moves
    the injected clock forward so the for-duration gate can be driven
    deterministically.  ``repair_hold_cap_s`` (default 1800, matching the
    bridge default) controls how long an active auto-repair may withhold a
    due critical promotion — pass 0 to disable the hold entirely.
    """
    client = MagicMock()
    client.post_alerts = AsyncMock()
    log = MagicMock()
    clock = {"now": datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)}

    def now_fn():
        return clock["now"]

    bridge = AlertmanagerBridge(
        client,
        log_fn=log,
        default_for_seconds=default_for_seconds,
        for_overrides=for_overrides,
        now_fn=now_fn,
        repair_hold_cap_s=repair_hold_cap_s,
    )

    def advance(seconds):
        clock["now"] = clock["now"] + datetime.timedelta(seconds=seconds)

    return bridge, client, log, advance


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


# ---------------------------------------------------------------------------
# Tests — for-duration gate (flap suppression)
# ---------------------------------------------------------------------------

class TestForSecondsResolution:
    def test_default_by_severity(self):
        bridge, *_ = _make_gated_bridge({"critical": 300, "warning": 600})
        assert bridge._for_seconds("spa", "critical") == 300
        assert bridge._for_seconds("spa", "warning") == 600

    def test_unconfigured_severity_is_zero(self):
        bridge, *_ = _make_gated_bridge({"critical": 300})
        assert bridge._for_seconds("spa", "warning") == 0

    def test_bare_bridge_is_zero(self):
        bridge, _client, _log = _make_bridge()
        assert bridge._for_seconds("spa", "critical") == 0

    def test_override_wins_over_default(self):
        bridge, *_ = _make_gated_bridge(
            {"critical": 300, "warning": 600},
            for_overrides={"ups": {"critical": 0, "warning": 0}},
        )
        assert bridge._for_seconds("ups", "critical") == 0
        assert bridge._for_seconds("ups", "warning") == 0
        # A checker without an override still uses the default.
        assert bridge._for_seconds("spa", "critical") == 300

    def test_partial_override_falls_back_to_default(self):
        bridge, *_ = _make_gated_bridge(
            {"critical": 300, "warning": 600},
            for_overrides={"ups": {"critical": 0}},
        )
        assert bridge._for_seconds("ups", "critical") == 0
        # warning not overridden → default
        assert bridge._for_seconds("ups", "warning") == 600

    def test_non_numeric_config_coerces_to_zero(self):
        bridge, *_ = _make_gated_bridge({"critical": "nope"})
        assert bridge._for_seconds("spa", "critical") == 0


class TestForDurationGate:
    def test_blip_recovering_within_for_never_posts(self):
        bridge, client, _log, advance = _make_gated_bridge({"critical": 300})

        _run(bridge.sync({"spa": _checker("critical")}))
        # Withheld — pending, nothing posted yet.
        client.post_alerts.assert_not_awaited()
        assert "spa" in bridge.pending_alerts
        assert "spa" not in bridge.active_alerts

        advance(60)
        _run(bridge.sync({"spa": _checker("ok")}))

        # Recovered before the for-threshold → suppressed entirely: no raise,
        # and crucially no [RESOLVED] post either.
        client.post_alerts.assert_not_awaited()
        assert bridge.pending_alerts == {}
        assert bridge.active_alerts == {}

    def test_persisting_past_for_promotes_and_posts_once(self):
        bridge, client, _log, advance = _make_gated_bridge({"critical": 300})

        _run(bridge.sync({"spa": _checker("critical")}))
        client.post_alerts.assert_not_awaited()

        advance(300)
        _run(bridge.sync({"spa": _checker("critical")}))

        client.post_alerts.assert_awaited_once()
        batch = _batches(client)[0]
        assert len(batch) == 1
        alert = batch[0]
        assert alert["labels"]["severity"] == "critical"
        assert alert["labels"]["checker"] == "spa"
        assert alert.get("startsAt")
        assert "endsAt" not in alert
        assert "spa" in bridge.active_alerts
        assert "spa" not in bridge.pending_alerts

    def test_stays_pending_below_threshold_then_promotes(self):
        bridge, client, _log, advance = _make_gated_bridge({"critical": 300})

        _run(bridge.sync({"spa": _checker("critical")}))      # t=0   pending
        advance(200)
        _run(bridge.sync({"spa": _checker("critical")}))      # t=200 still pending
        client.post_alerts.assert_not_awaited()
        assert "spa" in bridge.pending_alerts

        advance(100)                                          # t=300
        _run(bridge.sync({"spa": _checker("critical")}))      # promote
        client.post_alerts.assert_awaited_once()
        assert "spa" in bridge.active_alerts

    def test_for_zero_posts_immediately(self):
        bridge, client, _log, _advance = _make_gated_bridge({"critical": 0})

        _run(bridge.sync({"spa": _checker("critical")}))

        client.post_alerts.assert_awaited_once()
        assert "spa" in bridge.active_alerts
        assert bridge.pending_alerts == {}

    def test_per_checker_override_pages_now_while_others_wait(self):
        bridge, client, _log, _advance = _make_gated_bridge(
            {"critical": 300},
            for_overrides={"ups": {"critical": 0}},
        )

        _run(
            bridge.sync(
                {
                    "ups": _checker("critical", name="UPS"),
                    "spa": _checker("critical"),
                }
            )
        )

        # UPS pages immediately; spa is withheld pending.
        client.post_alerts.assert_awaited_once()
        batch = _batches(client)[0]
        assert len(batch) == 1
        assert batch[0]["labels"]["checker"] == "ups"
        assert "ups" in bridge.active_alerts
        assert "spa" in bridge.pending_alerts
        assert "spa" not in bridge.active_alerts

    def test_warning_uses_its_own_longer_for(self):
        bridge, client, _log, advance = _make_gated_bridge(
            {"critical": 300, "warning": 600}
        )

        _run(bridge.sync({"spa": _checker("warning")}))       # t=0   pending
        advance(300)
        _run(bridge.sync({"spa": _checker("warning")}))       # t=300 < 600 → pending
        client.post_alerts.assert_not_awaited()

        advance(300)                                          # t=600
        _run(bridge.sync({"spa": _checker("warning")}))       # promote
        client.post_alerts.assert_awaited_once()
        assert bridge.active_alerts["spa"]["labels"]["severity"] == "warning"

    def test_escalation_while_pending_uses_new_severity_for(self):
        # Pending as warning (for=600); escalates to critical (for=300) after
        # 300s → promotes immediately under the shorter critical threshold.
        bridge, client, _log, advance = _make_gated_bridge(
            {"critical": 300, "warning": 600}
        )

        _run(bridge.sync({"spa": _checker("warning")}))       # t=0 pending(warning)
        advance(300)
        _run(bridge.sync({"spa": _checker("critical")}))      # t=300, crit for=300

        client.post_alerts.assert_awaited_once()
        alert = _batches(client)[0][0]
        assert alert["labels"]["severity"] == "critical"
        assert "spa" in bridge.active_alerts
        assert "spa" not in bridge.pending_alerts

    def test_repost_ignores_pending_alerts(self):
        bridge, client, _log, _advance = _make_gated_bridge({"critical": 300})
        _run(bridge.sync({"spa": _checker("critical")}))      # pending only
        client.post_alerts.reset_mock()

        _run(bridge.repost_active())

        # Nothing is active yet, so the keep-alive re-post does nothing.
        client.post_alerts.assert_not_awaited()
        assert "spa" in bridge.pending_alerts

    def test_disabled_alerting_drops_pending_without_post(self):
        bridge, client, _log, _advance = _make_gated_bridge({"critical": 300})
        _run(bridge.sync({"spa": _checker("critical")}))      # pending
        assert "spa" in bridge.pending_alerts

        _run(bridge.sync({"spa": _checker("critical", alerting={"enabled": False})}))

        # desired becomes None → pending dropped, nothing posted.
        client.post_alerts.assert_not_awaited()
        assert bridge.pending_alerts == {}
        assert bridge.active_alerts == {}


# ---------------------------------------------------------------------------
# Tests — Escalation for-duration gate (warning firing → critical)
# ---------------------------------------------------------------------------

def _drive_warning_firing(bridge, client, advance, checker_id="spa"):
    """Drive ``checker_id`` to a firing warning alert.

    Assumes a warning for-gate of 600s (the value used by every escalation
    test below): pending on the first sync, promoted to firing after 600s.
    """
    _run(bridge.sync({checker_id: _checker("warning")}))   # t=0   pending
    advance(600)
    _run(bridge.sync({checker_id: _checker("warning")}))   # t=600 promote


class TestEscalationGate:
    def test_firing_warning_annotations_stay_fresh_while_escalation_pending(self):
        """During a pending escalation the still-firing warning's annotations
        refresh each sync, so reposts carry the current failing checks."""
        bridge, client, _log, advance = _make_gated_bridge(
            {"critical": 300, "warning": 600}
        )
        _drive_warning_firing(bridge, client, advance)

        checks = [
            {"name": "Gateway Ping", "status": "critical", "detail": "timeout 9s"}
        ]
        _run(bridge.sync({"spa": _checker("critical", checks=checks)}))

        assert "spa" in bridge.pending_alerts
        active = bridge.active_alerts["spa"]
        assert active["labels"]["severity"] == "warning"
        assert active["annotations"]["description"] == "Gateway Ping: timeout 9s"

    def test_escalation_withheld_pending_while_warning_stays_firing(self):
        """A warning→critical escalation is withheld pending: no post, the
        warning keeps firing, and the checker enters pending_alerts."""
        bridge, client, _log, advance = _make_gated_bridge(
            {"critical": 300, "warning": 600}
        )
        _drive_warning_firing(bridge, client, advance)
        assert bridge.active_alerts["spa"]["labels"]["severity"] == "warning"
        client.post_alerts.reset_mock()

        _run(bridge.sync({"spa": _checker("critical")}))

        client.post_alerts.assert_not_awaited()
        assert bridge.active_alerts["spa"]["labels"]["severity"] == "warning"
        assert "spa" in bridge.pending_alerts

    def test_escalation_promotes_after_threshold_resolving_warning(self):
        """Once the critical escalation is sustained for its for-duration the
        warning is resolved and the critical raised in one 2-alert batch."""
        bridge, client, _log, advance = _make_gated_bridge(
            {"critical": 300, "warning": 600}
        )
        _drive_warning_firing(bridge, client, advance)

        _run(bridge.sync({"spa": _checker("critical")}))      # escalation pending
        client.post_alerts.reset_mock()
        advance(300)
        _run(bridge.sync({"spa": _checker("critical")}))      # promote

        client.post_alerts.assert_awaited_once()
        batch = _batches(client)[0]
        assert len(batch) == 2
        resolved, fresh = batch
        # Resolved copy of the firing warning.
        assert resolved["labels"]["severity"] == "warning"
        assert resolved.get("endsAt")
        # Fresh critical — still firing, new startsAt, no endsAt.
        assert fresh["labels"]["severity"] == "critical"
        assert fresh.get("startsAt")
        assert "endsAt" not in fresh

        assert bridge.active_alerts["spa"]["labels"]["severity"] == "critical"
        assert bridge.pending_alerts == {}

    def test_de_escalation_before_threshold_drops_pending_and_new_window(self):
        """De-escalating back to warning before promotion drops the pending
        escalation with no post; a later critical opens a NEW pending window
        (a short advance under threshold does not promote)."""
        bridge, client, _log, advance = _make_gated_bridge(
            {"critical": 300, "warning": 600}
        )
        _drive_warning_firing(bridge, client, advance)
        client.post_alerts.reset_mock()

        # Escalate to critical → pending.
        _run(bridge.sync({"spa": _checker("critical")}))
        assert "spa" in bridge.pending_alerts

        # Fall back to warning before the threshold → pending dropped.
        _run(bridge.sync({"spa": _checker("warning")}))
        client.post_alerts.assert_not_awaited()
        assert "spa" not in bridge.pending_alerts
        assert bridge.active_alerts["spa"]["labels"]["severity"] == "warning"

        # Re-escalate → brand-new pending window; a short advance under the
        # critical for-duration must not promote.
        _run(bridge.sync({"spa": _checker("critical")}))
        assert "spa" in bridge.pending_alerts
        advance(200)
        _run(bridge.sync({"spa": _checker("critical")}))

        client.post_alerts.assert_not_awaited()
        assert "spa" in bridge.pending_alerts
        assert bridge.active_alerts["spa"]["labels"]["severity"] == "warning"

    def test_de_escalation_is_immediate_no_pending(self):
        """critical firing → warning applies immediately: one 2-alert batch
        (resolve critical + raise warning), no pending phase."""
        bridge, client, _log, advance = _make_gated_bridge(
            {"critical": 300, "warning": 600}
        )
        # Promote to a firing critical first.
        _run(bridge.sync({"spa": _checker("critical")}))      # pending
        advance(300)
        _run(bridge.sync({"spa": _checker("critical")}))      # firing critical
        assert bridge.active_alerts["spa"]["labels"]["severity"] == "critical"
        client.post_alerts.reset_mock()

        _run(bridge.sync({"spa": _checker("warning")}))

        client.post_alerts.assert_awaited_once()
        batch = _batches(client)[0]
        assert len(batch) == 2
        resolved, fresh = batch
        assert resolved["labels"]["severity"] == "critical"
        assert resolved.get("endsAt")
        assert fresh["labels"]["severity"] == "warning"
        assert "endsAt" not in fresh
        assert bridge.active_alerts["spa"]["labels"]["severity"] == "warning"
        assert "spa" not in bridge.pending_alerts

    def test_recovery_while_escalation_pending_resolves_only_warning(self):
        """A checker that recovers to ok while a critical escalation is pending
        posts a single resolve for the firing warning; pending and active clear."""
        bridge, client, _log, advance = _make_gated_bridge(
            {"critical": 300, "warning": 600}
        )
        _drive_warning_firing(bridge, client, advance)

        _run(bridge.sync({"spa": _checker("critical")}))      # escalation pending
        assert "spa" in bridge.pending_alerts
        client.post_alerts.reset_mock()

        _run(bridge.sync({"spa": _checker("ok")}))

        client.post_alerts.assert_awaited_once()
        batch = _batches(client)[0]
        assert len(batch) == 1
        resolved = batch[0]
        assert resolved["labels"]["severity"] == "warning"
        assert resolved.get("endsAt")
        assert bridge.pending_alerts == {}
        assert bridge.active_alerts == {}

    # Bullet 6 (bare bridge / for=0: warning→critical re-raises immediately) is
    # covered by TestEscalation.test_warning_to_critical_resolves_old_and_raises
    # _new_in_one_batch above — intentionally not duplicated here.


# ---------------------------------------------------------------------------
# Tests — Repair hold (withhold critical promotion while auto-repair runs)
# ---------------------------------------------------------------------------

class TestRepairHold:
    def test_critical_held_while_repair_in_progress(self):
        """A due critical promotion is withheld while auto-repair is
        in_progress: still pending, nothing posted, past the for-threshold."""
        bridge, client, _log, advance = _make_gated_bridge({"critical": 300})
        rc = {"status": "in_progress"}

        _run(bridge.sync({"spa": _checker("critical", repair_state=rc)}))  # pending
        assert "spa" in bridge.pending_alerts
        client.post_alerts.assert_not_awaited()

        advance(300)
        _run(bridge.sync({"spa": _checker("critical", repair_state=rc)}))  # held

        client.post_alerts.assert_not_awaited()
        assert "spa" in bridge.pending_alerts
        assert "spa" not in bridge.active_alerts

    def test_hold_released_once_cap_reached(self):
        """Once total pending time reaches repair_hold_cap_s (1800s default) the
        critical promotes even though repair is still in_progress."""
        bridge, client, _log, advance = _make_gated_bridge({"critical": 300})
        rc = {"status": "in_progress"}

        _run(bridge.sync({"spa": _checker("critical", repair_state=rc)}))  # t=0 pending
        advance(300)
        _run(bridge.sync({"spa": _checker("critical", repair_state=rc)}))  # t=300 held
        client.post_alerts.assert_not_awaited()

        advance(1500)  # total pending elapsed = 1800 == cap
        _run(bridge.sync({"spa": _checker("critical", repair_state=rc)}))  # promote

        client.post_alerts.assert_awaited_once()
        batch = _batches(client)[0]
        assert len(batch) == 1
        assert batch[0]["labels"]["severity"] == "critical"
        assert "spa" in bridge.active_alerts
        assert "spa" not in bridge.pending_alerts

    def test_success_holds_then_recovery_suppresses_no_page(self):
        """The 2026-07-09 race: repair reports success one report before the
        checker's recovered (ok) status lands.  The hold must cover ``success``
        so the pending critical is not promoted on the stale critical snapshot
        — then the recovered report suppresses it with no page."""
        bridge, client, _log, advance = _make_gated_bridge({"critical": 300})

        _run(bridge.sync({
            "spa": _checker("critical", repair_state={"status": "in_progress"})
        }))  # pending
        advance(300)
        # Repair flips to success, but the checker snapshot is still the stale
        # 'critical' (its ok status rides the next report).  Must NOT promote.
        _run(bridge.sync({
            "spa": _checker("critical", repair_state={"status": "success"})
        }))
        client.post_alerts.assert_not_awaited()
        assert "spa" in bridge.pending_alerts
        assert "spa" not in bridge.active_alerts

        # Next report: checker actually recovered → pending dropped, no page.
        _run(bridge.sync({"spa": _checker("ok")}))
        client.post_alerts.assert_not_awaited()
        assert bridge.pending_alerts == {}
        assert bridge.active_alerts == {}

    def test_illusory_success_still_pages_at_cap(self):
        """A success that doesn't actually recover (checker stays critical)
        must not be held forever — the cap still forces the page."""
        bridge, client, _log, advance = _make_gated_bridge({"critical": 300})
        rc = {"status": "success"}

        _run(bridge.sync({"spa": _checker("critical", repair_state=rc)}))  # pending
        advance(300)
        _run(bridge.sync({"spa": _checker("critical", repair_state=rc)}))  # held
        client.post_alerts.assert_not_awaited()

        advance(1500)  # total pending == cap (1800s)
        _run(bridge.sync({"spa": _checker("critical", repair_state=rc)}))  # promote
        client.post_alerts.assert_awaited_once()
        assert "spa" in bridge.active_alerts

    def test_failed_repair_releases_hold_at_threshold(self):
        """A repair that fails releases the hold: the critical promotes at the
        for-threshold on the report where repair status becomes failed."""
        bridge, client, _log, advance = _make_gated_bridge({"critical": 300})

        _run(
            bridge.sync(
                {"spa": _checker("critical", repair_state={"status": "pending"})}
            )
        )  # pending
        advance(300)
        _run(
            bridge.sync(
                {"spa": _checker("critical", repair_state={"status": "failed"})}
            )
        )  # promote — repair no longer active

        client.post_alerts.assert_awaited_once()
        assert "spa" in bridge.active_alerts
        assert "spa" not in bridge.pending_alerts

    def test_warning_is_not_held_by_repair(self):
        """The repair hold only applies to critical: a warning promotes on
        schedule even while repair is in_progress."""
        bridge, client, _log, advance = _make_gated_bridge(
            {"critical": 300, "warning": 600}
        )
        rc = {"status": "in_progress"}

        _run(bridge.sync({"spa": _checker("warning", repair_state=rc)}))  # pending
        advance(600)
        _run(bridge.sync({"spa": _checker("warning", repair_state=rc)}))  # promote

        client.post_alerts.assert_awaited_once()
        assert bridge.active_alerts["spa"]["labels"]["severity"] == "warning"
        assert "spa" not in bridge.pending_alerts

    def test_recovery_while_held_never_posts(self):
        """A checker held by an active repair that then recovers to ok never
        pages: no post at all, pending cleared."""
        bridge, client, _log, advance = _make_gated_bridge({"critical": 300})
        rc = {"status": "in_progress"}

        _run(bridge.sync({"spa": _checker("critical", repair_state=rc)}))  # pending
        advance(300)
        _run(bridge.sync({"spa": _checker("critical", repair_state=rc)}))  # held
        _run(bridge.sync({"spa": _checker("ok")}))                         # recover

        client.post_alerts.assert_not_awaited()
        assert bridge.pending_alerts == {}
        assert bridge.active_alerts == {}

    def test_cap_zero_disables_hold(self):
        """repair_hold_cap_s=0 disables the hold entirely: the critical
        promotes at the for-threshold even with repair in_progress."""
        bridge, client, _log, advance = _make_gated_bridge(
            {"critical": 300}, repair_hold_cap_s=0
        )
        rc = {"status": "in_progress"}

        _run(bridge.sync({"spa": _checker("critical", repair_state=rc)}))  # pending
        advance(300)
        _run(bridge.sync({"spa": _checker("critical", repair_state=rc)}))  # promote

        client.post_alerts.assert_awaited_once()
        assert "spa" in bridge.active_alerts
        assert "spa" not in bridge.pending_alerts

    def test_for_zero_override_raises_immediately_despite_repair(self):
        """A checker with a for=0 override never enters the pending gate, so it
        can never be held — it raises immediately even with repair in_progress."""
        bridge, client, _log, _advance = _make_gated_bridge(
            {"critical": 300}, for_overrides={"x": {"critical": 0}}
        )
        rc = {"status": "in_progress"}

        _run(bridge.sync({"x": _checker("critical", repair_state=rc)}))

        client.post_alerts.assert_awaited_once()
        assert "x" in bridge.active_alerts
        assert "x" not in bridge.pending_alerts
