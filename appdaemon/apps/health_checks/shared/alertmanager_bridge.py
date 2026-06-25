"""Alertmanager bridge — mirrors checker health into Alertmanager alerts.

Pure decision logic, no HTTP: the controller injects a
:class:`providers.alertmanager.AlertmanagerClient` and this module decides
when to raise, refresh, and resolve one alert per unhealthy checker.

Behaviour:

- checker status ``critical`` → ``severity=critical`` (pages the phone —
  the cluster's Alertmanager routes only critical to Pushover)
- ``warning`` / ``degraded`` → ``severity=warning`` (visible in
  Alertmanager/Grafana UI only)
- ``ok`` / ``unknown`` → resolve (``unknown`` is typically a dependency
  outage already alerted by the dependency's own checker)

While an alert fires, the controller re-posts it on a timer that must stay
below Alertmanager's ``resolve_timeout`` (5m in this cluster).  On clear,
one final post with ``endsAt=now`` produces an immediate ``[RESOLVED]``
notification.

**For-duration gate (flap suppression).**  A checker that is unhealthy for
only a poll or two — a transient blip — should not page.  Each severity has
a configurable ``for`` duration: when a checker first goes non-ok the alert
is held *pending* (nothing is posted) and is only promoted to firing once
the checker has stayed non-ok for ``>= for`` seconds, measured across
subsequent status reports (Prometheus ``for:`` semantics — promotion happens
on a later report, so a single-sample blip can never be promoted).  If the
checker recovers while pending, the alert is dropped silently: no page, no
``[RESOLVED]`` noise.  ``for=0`` (the default when unconfigured) restores the
original raise-immediately behaviour.  Durations are resolved per checker via
``default_for_seconds`` (keyed by severity) with optional per-checker
overrides in ``for_overrides`` (keyed by ``checker_id`` then severity); both
are injected by the controller from its ``apps.yaml`` config.

Alertmanager being down must never break health checking: every client
call is wrapped; failures are logged and retried by the next sync or
re-post tick.  An unsent "raise" stays in the active set so the re-post
loop delivers it once Alertmanager returns; an unsent "resolve" falls back
to Alertmanager's resolve_timeout.

Per-checker configuration arrives via the ``alerting`` block of the
registration payload::

    alerting:
      enabled: true                       # default true
      alertname: ProtectEventStreamFrozen # default <CheckerName>Unhealthy
"""

from __future__ import annotations

import asyncio
import datetime
import re
from typing import Any, Callable, Dict, List, Optional

SEVERITY_BY_STATUS = {
    "critical": "critical",
    "degraded": "warning",
    "warning": "warning",
}

SOURCE_LABEL = "appdaemon-health-check"

_MAX_DESCRIPTION_LEN = 600


def default_alertname(checker_name: str, checker_id: str) -> str:
    """Derive an alertname like ``WestfordSpaUnhealthy`` from the checker name."""
    base = checker_name or checker_id or "UnknownChecker"
    words = [w for w in re.split(r"[^0-9a-zA-Z]+", base) if w]
    camel = "".join(w[:1].upper() + w[1:] for w in words)
    return f"{camel or 'UnknownChecker'}Unhealthy"


def _utcnow_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class AlertmanagerBridge:
    """Tracks firing alerts per checker and posts transitions to Alertmanager."""

    def __init__(
        self,
        client: Any,
        log_fn: Optional[Callable[..., None]] = None,
        default_for_seconds: Optional[Dict[str, Any]] = None,
        for_overrides: Optional[Dict[str, Dict[str, Any]]] = None,
        now_fn: Optional[Callable[[], datetime.datetime]] = None,
    ) -> None:
        self._client = client
        self._log = log_fn or (lambda msg, level="INFO": None)
        # checker_id → firing alert dict ({"labels", "annotations", "startsAt"})
        self._active: Dict[str, Dict[str, Any]] = {}
        # checker_id → {"desired": alert dict, "since": aware datetime}.
        # Alerts withheld by the for-duration gate until they have been
        # unhealthy long enough to promote into _active.
        self._pending: Dict[str, Dict[str, Any]] = {}
        # for-duration config: global default by severity + per-checker
        # overrides by checker_id then severity.  Empty → for=0 everywhere
        # (raise immediately, preserving the pre-gate behaviour).
        self._default_for_seconds: Dict[str, Any] = dict(default_for_seconds or {})
        self._for_overrides: Dict[str, Dict[str, Any]] = {
            cid: dict(sev_map) for cid, sev_map in (for_overrides or {}).items()
        }
        # Injectable clock so tests can drive the for-duration gate.
        self._now_fn: Callable[[], datetime.datetime] = now_fn or (
            lambda: datetime.datetime.now(datetime.timezone.utc)
        )
        # Serialises sync() and repost_active(): without it a repost that
        # snapshotted the active set could land at Alertmanager AFTER a
        # concurrent sync's resolve post, resurrecting a resolved alert.
        self._post_lock = asyncio.Lock()

    def _for_seconds(self, checker_id: str, severity: str) -> int:
        """Resolve the for-duration (seconds) for a checker_id + severity.

        Per-checker override wins over the global default; an unconfigured
        severity falls back to 0 (raise immediately).
        """
        override = self._for_overrides.get(checker_id)
        if override is not None and severity in override:
            try:
                return max(0, int(override[severity]))
            except (TypeError, ValueError):
                return 0
        try:
            return max(0, int(self._default_for_seconds.get(severity, 0)))
        except (TypeError, ValueError):
            return 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def active_alerts(self) -> Dict[str, Dict[str, Any]]:
        """Currently-firing alerts keyed by checker_id (for tests/inspection)."""
        return self._active

    @property
    def pending_alerts(self) -> Dict[str, Dict[str, Any]]:
        """Alerts held by the for-duration gate, keyed by checker_id."""
        return self._pending

    async def sync(self, snapshot: Dict[str, Dict[str, Any]]) -> None:
        """Reconcile alerts against a resolved checker snapshot.

        ``snapshot`` maps checker_id → ``{"name", "status", "checks",
        "alerting", "repair_state"}`` (the dependency-resolved view the
        controller publishes).  Transitions are computed synchronously so
        concurrent syncs cannot interleave mid-decision; the HTTP post
        happens once at the end, serialised against repost_active().
        """
        async with self._post_lock:
            await self._sync_locked(snapshot)

    async def _sync_locked(self, snapshot: Dict[str, Dict[str, Any]]) -> None:
        to_post: List[Dict[str, Any]] = []
        now = self._now_fn()

        for checker_id, checker in snapshot.items():
            desired = self._build_desired(checker_id, checker)
            active = self._active.get(checker_id)
            pending = self._pending.get(checker_id)

            if desired is None:
                # Recovered / ok / alerting disabled.
                if pending is not None:
                    elapsed = (now - pending["since"]).total_seconds()
                    del self._pending[checker_id]
                    self._log(
                        f"Alert suppressed for checker '{checker_id}' — recovered "
                        f"after {elapsed:.0f}s pending (never reached for-threshold)",
                        level="INFO",
                    )
                if active is not None:
                    to_post.append(self._resolved_copy(active))
                    del self._active[checker_id]
                    self._log(
                        f"Alert resolved for checker '{checker_id}' "
                        f"({active['labels'].get('alertname')})",
                        level="INFO",
                    )
                continue

            # desired is non-None — the checker is unhealthy.
            if active is not None:
                if active["labels"] != desired["labels"]:
                    # Labels define alert identity — resolve the old one and
                    # raise the new one (e.g. warning escalated to critical).
                    to_post.append(self._resolved_copy(active))
                    desired["startsAt"] = _utcnow_iso()
                    self._active[checker_id] = desired
                    to_post.append(dict(desired))
                    self._log(
                        f"Alert re-raised for checker '{checker_id}' with new "
                        f"labels: severity={desired['labels'].get('severity')}",
                        level="INFO",
                    )
                else:
                    # Same identity — refresh annotations; the re-post timer
                    # keeps the alert alive in Alertmanager.
                    active["annotations"] = desired["annotations"]
                continue

            # Not yet firing — apply the for-duration gate before raising.
            severity = desired["labels"].get("severity", "")
            for_s = self._for_seconds(checker_id, severity)

            if pending is None:
                if for_s <= 0:
                    desired["startsAt"] = _utcnow_iso()
                    self._active[checker_id] = desired
                    to_post.append(dict(desired))
                    self._log(
                        f"Alert raised for checker '{checker_id}': "
                        f"{desired['labels'].get('alertname')} "
                        f"severity={severity}",
                        level="INFO",
                    )
                else:
                    self._pending[checker_id] = {"desired": desired, "since": now}
                    self._log(
                        f"Alert pending for checker '{checker_id}': "
                        f"{desired['labels'].get('alertname')} severity={severity} "
                        f"— withholding until unhealthy for {for_s}s",
                        level="INFO",
                    )
                continue

            # Already pending — refresh the desired payload, keep the clock,
            # and promote once it has been unhealthy for >= for_s.
            pending["desired"] = desired
            elapsed = (now - pending["since"]).total_seconds()
            if elapsed >= for_s:
                desired["startsAt"] = _utcnow_iso()
                self._active[checker_id] = desired
                del self._pending[checker_id]
                to_post.append(dict(desired))
                self._log(
                    f"Alert promoted for checker '{checker_id}' after "
                    f"{elapsed:.0f}s unhealthy (for={for_s}s): severity={severity}",
                    level="INFO",
                )
            # else: still within the grace window — keep withholding.

        # Checkers that vanished from the snapshot keep their alerts firing
        # via the re-post loop (a dead checker should stay visible).  A
        # vanished checker that was only *pending* stays pending until it
        # reports again — a single unconfirmed blip never pages.

        await self._post(to_post)

    async def repost_active(self) -> None:
        """Re-post all firing alerts so they outlive resolve_timeout."""
        async with self._post_lock:
            if not self._active:
                return
            batch = [dict(alert) for alert in self._active.values()]
            await self._post(batch)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_desired(
        self, checker_id: str, checker: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Return the alert that should fire for this checker, or None."""
        alerting = checker.get("alerting") or {}
        if not alerting.get("enabled", True):
            return None

        status = checker.get("status", "unknown")
        severity = SEVERITY_BY_STATUS.get(status)
        if severity is None:
            return None

        name = checker.get("name", checker_id)
        alertname = alerting.get("alertname") or default_alertname(
            name, checker_id
        )

        failing = [
            c
            for c in checker.get("checks", [])
            if c.get("status") not in ("ok", "unknown")
        ]
        parts = [
            f"{c.get('name', '?')}: {c.get('detail') or c.get('status')}"
            for c in failing
        ]

        repair_state = checker.get("repair_state") or {}
        repair_status = repair_state.get("status")
        if repair_status == "in_progress":
            parts.append("auto-repair in progress")
        elif repair_status == "failed":
            detail = repair_state.get("detail", "")
            parts.append(f"auto-repair FAILED: {detail}" if detail else "auto-repair FAILED")

        description = "; ".join(parts) or f"{name} reported {status}"
        if len(description) > _MAX_DESCRIPTION_LEN:
            description = description[: _MAX_DESCRIPTION_LEN - 1] + "…"

        return {
            "labels": {
                "alertname": alertname,
                "severity": severity,
                "source": SOURCE_LABEL,
                "checker": checker_id,
            },
            "annotations": {
                "summary": f"{name} health check is {status}",
                "description": description,
            },
        }

    @staticmethod
    def _resolved_copy(alert: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "labels": dict(alert["labels"]),
            "annotations": dict(alert["annotations"]),
            "startsAt": alert.get("startsAt", _utcnow_iso()),
            "endsAt": _utcnow_iso(),
        }

    async def _post(self, alerts: List[Dict[str, Any]]) -> None:
        if not alerts:
            return
        try:
            await self._client.post_alerts(alerts)
        except Exception as exc:
            # Alertmanager downtime must not break health checking.
            self._log(
                f"Alertmanager post failed ({len(alerts)} alert(s)): {exc}",
                level="WARNING",
            )
