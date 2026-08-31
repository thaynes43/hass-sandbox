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

**Escalation gate.**  Severity *escalations* of an already-firing alert
(warning → critical) go through the same for-duration gate: the firing
warning stays up while the critical escalation is held pending, and only
after the checker has stayed critical for ``>= for(critical)`` seconds is
the warning resolved and the critical raised.  If the checker de-escalates
while the escalation is pending, the pending escalation is dropped silently
and the warning keeps firing — a warning↔critical flapper can never page.
De-escalations (critical → warning) apply immediately, as does everything
when ``for=0``.

**Improvement hold (resolve/de-escalation hysteresis).**  The mirror image
of the for-duration gate: once an alert fires, any *improvement* — resolve
(checker back to ok) or de-escalation (critical → warning) — must be
sustained for ``improve_hold_s`` seconds before the bridge acts on it.
Until then the firing alert stays up unchanged (annotations refreshed).
If the checker deteriorates again inside the hold window the improvement
is discarded and nothing was ever posted — so a genuinely oscillating
condition (a Wi-Fi fan flapping every few minutes for hours, observed
2026-08-31) produces exactly one ``[FIRING]`` page and, once truly
stable, one ``[RESOLVED]`` page, instead of a page pair per flap.
``send_resolved: true`` on the Pushover receiver makes every resolve a
notification, which is what turns unheld flapping into a page storm.
``improve_hold_s=0`` (default) disables the hold and restores the old
act-immediately behaviour.

**Repair hold.**  A checker whose auto-repair is scheduled, running, or
just-succeeded (``repair_state.status`` of ``pending`` / ``in_progress`` /
``success``) gets a chance to fix itself before anyone is paged: promotion
of a pending *critical* alert is withheld while repair is active, up to
``repair_hold_cap_s`` total pending time (default 1800 s) so a stuck repair
can never silence a real outage.  ``success`` is held to bridge the
recovery-propagation gap — a repair reports success one status report before
the checker's recovered (ok) status lands, and promoting on the stale
critical snapshot in that window false-pages for an already-fixed problem.
A *failed* repair releases the hold on the next report (page — human help is
needed).  Checkers with ``for=0`` (explicit "page now" overrides) never
enter the pending gate and are therefore never held.

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

# Rank used to distinguish escalations (gated) from de-escalations (immediate).
_SEVERITY_RANK = {"warning": 1, "critical": 2}

# Repair states during which a pending critical promotion is withheld.
# ``success`` is held too, to bridge the recovery-propagation gap: when a
# repair reports success the checker has just recovered, but its recovered
# (ok) status rides the *next* status report.  Promoting on the stale
# critical snapshot in that window false-pages for a problem auto-repair
# already fixed — observed 2026-07-09, where a UniFi Protect reload succeeded
# and the page fired 23 ms later, only to resolve on the next poll ~3 min on.
# An illusory success (checker still critical) simply re-arms repair
# (success -> pending/in_progress), so it stays held; ``repair_hold_cap_s``
# still forces promotion if a real recovery never lands.
_REPAIR_HOLD_STATES = ("pending", "in_progress", "success")

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
        repair_hold_cap_s: int = 1800,
        improve_hold_s: int = 0,
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
        # Cap on how long an active repair may withhold a due critical
        # promotion (total pending time, seconds).
        try:
            self._repair_hold_cap_s: int = max(0, int(repair_hold_cap_s))
        except (TypeError, ValueError):
            self._repair_hold_cap_s = 1800
        # Improvement hold: how long a resolve/de-escalation must be
        # sustained before it is applied.  0 = act immediately.
        try:
            self._improve_hold_s: int = max(0, int(improve_hold_s))
        except (TypeError, ValueError):
            self._improve_hold_s = 0
        # checker_id → {"since": aware datetime} while an active alert's
        # checker is continuously reporting *better* than the firing
        # severity (improvement hold in progress).
        self._improving: Dict[str, Dict[str, Any]] = {}
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
                    if self._improve_holding(
                        checker_id,
                        now,
                        f"resolve of severity="
                        f"{active['labels'].get('severity')} (checker recovered)",
                    ):
                        continue
                    to_post.append(self._resolved_copy(active))
                    del self._active[checker_id]
                    self._log(
                        f"Alert resolved for checker '{checker_id}' "
                        f"({active['labels'].get('alertname')})",
                        level="INFO",
                    )
                else:
                    self._improving.pop(checker_id, None)
                continue

            # desired is non-None — the checker is unhealthy.
            severity = desired["labels"].get("severity", "")
            for_s = self._for_seconds(checker_id, severity)

            if active is not None:
                if active["labels"] == desired["labels"]:
                    # Same identity — refresh annotations; the re-post timer
                    # keeps the alert alive in Alertmanager.  Any pending
                    # escalation is dropped: the checker fell back to the
                    # severity that is already firing before promotion.
                    active["annotations"] = desired["annotations"]
                    if self._improving.pop(checker_id, None) is not None:
                        self._log(
                            f"Improvement discarded for checker '{checker_id}' "
                            f"— back at severity={severity} inside the hold "
                            f"window",
                            level="INFO",
                        )
                    if pending is not None:
                        del self._pending[checker_id]
                        self._log(
                            f"Escalation dropped for checker '{checker_id}' — "
                            f"returned to severity={severity} before promotion",
                            level="INFO",
                        )
                    continue

                escalating = _SEVERITY_RANK.get(severity, 0) > _SEVERITY_RANK.get(
                    active["labels"].get("severity", ""), 0
                )
                if escalating:
                    if self._improving.pop(checker_id, None) is not None:
                        self._log(
                            f"Improvement discarded for checker '{checker_id}' "
                            f"— escalating to severity={severity} inside the "
                            f"hold window",
                            level="INFO",
                        )
                if escalating and for_s > 0:
                    # Escalations go through the for-duration gate too — the
                    # currently-firing lower severity stays up meanwhile.
                    # Keep its annotations fresh so Alertmanager/Grafana show
                    # the current failing checks throughout the gate window.
                    active["annotations"] = desired["annotations"]
                    if pending is None:
                        self._pending[checker_id] = {"desired": desired, "since": now}
                        self._log(
                            f"Escalation pending for checker '{checker_id}': "
                            f"severity={severity} — withholding until sustained "
                            f"for {for_s}s",
                            level="INFO",
                        )
                        continue
                    pending["desired"] = desired
                    if not self._promotion_due(
                        checker_id, checker, pending, severity, for_s, now
                    ):
                        continue
                    elapsed = (now - pending["since"]).total_seconds()
                    del self._pending[checker_id]
                    self._log(
                        f"Escalation promoted for checker '{checker_id}' after "
                        f"{elapsed:.0f}s sustained (for={for_s}s): "
                        f"severity={severity}",
                        level="INFO",
                    )
                elif pending is not None:
                    # De-escalation (or identity change with for=0) — clear
                    # whatever escalation was pending.
                    del self._pending[checker_id]

                if not escalating and self._improve_holding(
                    checker_id, now, f"de-escalation to severity={severity}"
                ):
                    # Keep the higher-severity alert firing through the hold
                    # window, with fresh annotations so the UI shows what is
                    # failing right now.
                    active["annotations"] = desired["annotations"]
                    continue
                self._improving.pop(checker_id, None)

                # Labels define alert identity — resolve the old one and
                # raise the new one.
                to_post.append(self._resolved_copy(active))
                desired["startsAt"] = _utcnow_iso()
                self._active[checker_id] = desired
                to_post.append(dict(desired))
                self._log(
                    f"Alert re-raised for checker '{checker_id}' with new "
                    f"labels: severity={desired['labels'].get('severity')}",
                    level="INFO",
                )
                continue

            # Not yet firing — apply the for-duration gate before raising.
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
            # and promote once it has been unhealthy for >= for_s (and any
            # active auto-repair has had its chance).
            pending["desired"] = desired
            if self._promotion_due(checker_id, checker, pending, severity, for_s, now):
                elapsed = (now - pending["since"]).total_seconds()
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

    def _improve_holding(
        self, checker_id: str, now: datetime.datetime, what: str
    ) -> bool:
        """Track an improvement of a firing alert; True while it is held.

        The hold window starts at the first improved report and survives
        changes *between* improved states (warning → ok keeps the same
        clock — the checker has been continuously better than the firing
        severity throughout).  Any sighting at or above the firing
        severity clears the entry via the caller, restarting the window
        on the next improvement.
        """
        if self._improve_hold_s <= 0:
            return False
        imp = self._improving.get(checker_id)
        if imp is None:
            self._improving[checker_id] = {"since": now}
            self._log(
                f"Improvement hold for checker '{checker_id}' — {what} "
                f"withheld until sustained for {self._improve_hold_s}s",
                level="INFO",
            )
            return True
        elapsed = (now - imp["since"]).total_seconds()
        if elapsed < self._improve_hold_s:
            return True
        del self._improving[checker_id]
        self._log(
            f"Improvement sustained for checker '{checker_id}' "
            f"({elapsed:.0f}s ≥ {self._improve_hold_s}s) — applying {what}",
            level="INFO",
        )
        return False

    def _promotion_due(
        self,
        checker_id: str,
        checker: Dict[str, Any],
        pending: Dict[str, Any],
        severity: str,
        for_s: int,
        now: datetime.datetime,
    ) -> bool:
        """Return True when a pending alert should promote to firing.

        The for-duration must have elapsed, and — for critical alerts — any
        active auto-repair gets a chance to finish first: promotion is
        withheld while ``repair_state.status`` is pending/in_progress/success
        (the last to let a just-succeeded repair's recovery propagate before
        promoting on a stale snapshot), up to ``repair_hold_cap_s`` total
        pending time.
        """
        elapsed = (now - pending["since"]).total_seconds()
        if elapsed < for_s:
            return False

        if severity == "critical" and self._repair_hold_cap_s > 0:
            repair_status = (checker.get("repair_state") or {}).get("status")
            if (
                repair_status in _REPAIR_HOLD_STATES
                and elapsed < self._repair_hold_cap_s
            ):
                if not pending.get("repair_hold_logged"):
                    pending["repair_hold_logged"] = True
                    self._log(
                        f"Repair hold for checker '{checker_id}' — critical "
                        f"promotion withheld while auto-repair is "
                        f"{repair_status} (cap {self._repair_hold_cap_s}s)",
                        level="INFO",
                    )
                return False

        return True

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
