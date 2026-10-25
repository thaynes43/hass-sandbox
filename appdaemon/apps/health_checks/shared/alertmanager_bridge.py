"""Alertmanager bridge — mirrors checker health into Alertmanager alerts.

Pure decision logic, no HTTP: the controller injects a
:class:`providers.alertmanager.AlertmanagerClient` and this module decides
when to raise, refresh, and resolve one alert per unhealthy checker stream.
Legacy checkers use one stream. A checker that declares the fixed physical and
technical conditions has one independent stream for each condition while
retaining its original checker ID for Alertmanager labels and gates.

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
      ignore_for_overrides: false         # true = default for-gate only

The optional condition contract is deliberately small and fixed::

    alerting:
      conditions:
        physical:
          alertname: CigarsUnhealthy
          labels:
            scope: host
        technical:
          alertname: CigarsTelemetryFailure

Only ``physical`` and ``technical`` are accepted.  Valid physical labels may
mark an owner-action alert ``scope=host``; technical alerts stay eligible for
the responder.  Invalid condition configuration falls back to the ordinary
responder-eligible checker alert rather than dropping a failure.

``ignore_for_overrides`` is set by the controller's registration watchdog on
its synthetic "not registered" checker: per-checker ``for_overrides`` were
chosen for what a running checker reports (``ups: critical: 0``), not for
an app that never started, so that alert always uses the global default.
"""

from __future__ import annotations

import asyncio
import datetime
import re
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

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

# A legacy checker alert keeps its existing string key.  Split physical and
# technical conditions use a tuple, which cannot collide with an ordinary
# checker_id (checker IDs are strings throughout the controller protocol).
AlertKey = Union[str, Tuple[str, str]]

_CONDITION_NAMES = ("physical", "technical")
_RESERVED_LABELS = {"alertname", "severity", "source", "checker"}
_LABEL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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
        # AlertKey → firing alert dict ({"labels", "annotations", "startsAt"}).
        # Legacy checkers retain their checker_id string key; condition-aware
        # checkers use (checker_id, condition), so the two streams are fully
        # independent without changing the controller's public checker IDs.
        self._active: Dict[AlertKey, Dict[str, Any]] = {}
        # AlertKey → {"desired": alert dict, "since": aware datetime}.
        # Alerts withheld by the for-duration gate until they have been
        # unhealthy long enough to promote into _active.
        self._pending: Dict[AlertKey, Dict[str, Any]] = {}
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
        # AlertKey → {"since": aware datetime} while an active alert's
        # checker is continuously reporting *better* than the firing
        # severity (improvement hold in progress).
        self._improving: Dict[AlertKey, Dict[str, Any]] = {}
        # Avoid writing the same malformed-condition/category warning on every
        # report. A later valid registration naturally stops using the entry.
        self._invalid_conditions_logged: set[str] = set()
        self._invalid_categories_logged: set[Tuple[str, str]] = set()
        # Injectable clock so tests can drive the for-duration gate.
        self._now_fn: Callable[[], datetime.datetime] = now_fn or (
            lambda: datetime.datetime.now(datetime.timezone.utc)
        )
        # Serialises sync() and repost_active(): without it a repost that
        # snapshotted the active set could land at Alertmanager AFTER a
        # concurrent sync's resolve post, resurrecting a resolved alert.
        self._post_lock = asyncio.Lock()

    def _for_seconds(
        self, checker_id: str, severity: str, ignore_overrides: bool = False
    ) -> int:
        """Resolve the for-duration (seconds) for a checker_id + severity.

        Per-checker override wins over the global default unless
        ``ignore_overrides``; an unconfigured severity falls back to 0
        (raise immediately).
        """
        override = None if ignore_overrides else self._for_overrides.get(checker_id)
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
    def active_alerts(self) -> Dict[AlertKey, Dict[str, Any]]:
        """Currently-firing alerts keyed by checker ID or condition stream."""
        return self._active

    @property
    def pending_alerts(self) -> Dict[AlertKey, Dict[str, Any]]:
        """Alerts held by the for-duration gate, keyed by alert stream."""
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

        # A normal checker has one stream keyed by its string checker_id. The
        # scoped checkers below may expose two streams keyed by a tuple. Build
        # every stream first so a removed condition can be resolved while its
        # checker still exists in the snapshot (a missing *checker* remains
        # intentionally visible via the re-post loop).
        desired_streams: Dict[AlertKey, Tuple[str, Dict[str, Any]]] = {}
        for checker_id, checker in snapshot.items():
            for stream_key, stream_checker in self._condition_streams(
                checker_id, checker
            ):
                desired_streams[stream_key] = (checker_id, stream_checker)

        for checker_id in snapshot:
            live_keys = {
                key
                for key, (owner_id, _checker) in desired_streams.items()
                if owner_id == checker_id
            }
            stale_keys = {
                key
                for key in (
                    set(self._active) | set(self._pending) | set(self._improving)
                )
                if self._key_belongs_to_checker(key, checker_id) and key not in live_keys
            }
            for stream_key in stale_keys:
                self._pending.pop(stream_key, None)
                self._improving.pop(stream_key, None)
                active = self._active.pop(stream_key, None)
                if active is not None:
                    to_post.append(self._resolved_copy(active))
                    self._log(
                        f"Alert resolved for checker '{self._stream_name(stream_key)}' "
                        f"({active['labels'].get('alertname')}) — alert condition removed",
                        level="INFO",
                    )

        for stream_key, (checker_id, checker) in desired_streams.items():
            display_name = self._stream_name(stream_key)
            desired = self._build_desired(checker_id, checker)
            active = self._active.get(stream_key)
            pending = self._pending.get(stream_key)

            if desired is None:
                # Recovered / ok / alerting disabled.
                # A tile mute is an explicit operator command. It must end
                # every condition now, rather than leaving a host-scoped or
                # technical alert firing through the normal recovery hold.
                alerting_disabled = not (checker.get("alerting") or {}).get(
                    "enabled", True
                )
                if pending is not None:
                    elapsed = (now - pending["since"]).total_seconds()
                    del self._pending[stream_key]
                    self._log(
                        f"Alert suppressed for checker '{display_name}' — recovered "
                        f"after {elapsed:.0f}s pending (never reached for-threshold)",
                        level="INFO",
                    )
                if active is not None:
                    if not alerting_disabled and self._improve_holding(
                        stream_key,
                        now,
                        f"resolve of severity="
                        f"{active['labels'].get('severity')} (checker recovered)",
                    ):
                        continue
                    self._improving.pop(stream_key, None)
                    to_post.append(self._resolved_copy(active))
                    del self._active[stream_key]
                    self._log(
                        f"Alert resolved for checker '{display_name}' "
                        f"({active['labels'].get('alertname')})",
                        level="INFO",
                    )
                else:
                    self._improving.pop(stream_key, None)
                continue

            # desired is non-None — the checker is unhealthy.
            severity = desired["labels"].get("severity", "")
            for_s = self._for_seconds(
                checker_id,
                severity,
                ignore_overrides=bool(
                    (checker.get("alerting") or {}).get("ignore_for_overrides")
                ),
            )

            if active is not None:
                if active["labels"] == desired["labels"]:
                    # Same identity — refresh annotations; the re-post timer
                    # keeps the alert alive in Alertmanager.  Any pending
                    # escalation is dropped: the checker fell back to the
                    # severity that is already firing before promotion.
                    active["annotations"] = desired["annotations"]
                    if self._improving.pop(stream_key, None) is not None:
                        self._log(
                            f"Improvement discarded for checker '{display_name}' "
                            f"— back at severity={severity} inside the hold "
                            f"window",
                            level="INFO",
                        )
                    if pending is not None:
                        del self._pending[stream_key]
                        self._log(
                            f"Escalation dropped for checker '{display_name}' — "
                            f"returned to severity={severity} before promotion",
                            level="INFO",
                        )
                    continue

                escalating = _SEVERITY_RANK.get(severity, 0) > _SEVERITY_RANK.get(
                    active["labels"].get("severity", ""), 0
                )
                if escalating:
                    if self._improving.pop(stream_key, None) is not None:
                        self._log(
                            f"Improvement discarded for checker '{display_name}' "
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
                        self._pending[stream_key] = {"desired": desired, "since": now}
                        self._log(
                            f"Escalation pending for checker '{display_name}': "
                            f"severity={severity} — withholding until sustained "
                            f"for {for_s}s",
                            level="INFO",
                        )
                        continue
                    pending["desired"] = desired
                    if not self._promotion_due(
                        display_name, checker, pending, severity, for_s, now
                    ):
                        continue
                    elapsed = (now - pending["since"]).total_seconds()
                    del self._pending[stream_key]
                    self._log(
                        f"Escalation promoted for checker '{display_name}' after "
                        f"{elapsed:.0f}s sustained (for={for_s}s): "
                        f"severity={severity}",
                        level="INFO",
                    )
                elif pending is not None:
                    # De-escalation (or identity change with for=0) — clear
                    # whatever escalation was pending.
                    del self._pending[stream_key]

                if not escalating and self._improve_holding(
                    stream_key, now, f"de-escalation to severity={severity}"
                ):
                    # Keep the higher-severity alert firing through the hold
                    # window, with fresh annotations so the UI shows what is
                    # failing right now.
                    active["annotations"] = desired["annotations"]
                    continue
                self._improving.pop(stream_key, None)

                # Labels define alert identity — resolve the old one and
                # raise the new one.
                to_post.append(self._resolved_copy(active))
                desired["startsAt"] = _utcnow_iso()
                self._active[stream_key] = desired
                to_post.append(dict(desired))
                self._log(
                    f"Alert re-raised for checker '{display_name}' with new "
                    f"labels: severity={desired['labels'].get('severity')}",
                    level="INFO",
                )
                continue

            # Not yet firing — apply the for-duration gate before raising.
            if pending is None:
                if for_s <= 0:
                    desired["startsAt"] = _utcnow_iso()
                    self._active[stream_key] = desired
                    to_post.append(dict(desired))
                    self._log(
                        f"Alert raised for checker '{display_name}': "
                        f"{desired['labels'].get('alertname')} "
                        f"severity={severity}",
                        level="INFO",
                    )
                else:
                    self._pending[stream_key] = {"desired": desired, "since": now}
                    self._log(
                        f"Alert pending for checker '{display_name}': "
                        f"{desired['labels'].get('alertname')} severity={severity} "
                        f"— withholding until unhealthy for {for_s}s",
                        level="INFO",
                    )
                continue

            # Already pending — refresh the desired payload, keep the clock,
            # and promote once it has been unhealthy for >= for_s (and any
            # active auto-repair has had its chance).
            pending["desired"] = desired
            if self._promotion_due(
                display_name, checker, pending, severity, for_s, now
            ):
                elapsed = (now - pending["since"]).total_seconds()
                desired["startsAt"] = _utcnow_iso()
                self._active[stream_key] = desired
                del self._pending[stream_key]
                to_post.append(dict(desired))
                self._log(
                    f"Alert promoted for checker '{display_name}' after "
                    f"{elapsed:.0f}s unhealthy (for={for_s}s): severity={severity}",
                    level="INFO",
                )
            # else: still within the grace window — keep withholding.

        # Checkers that vanished from the snapshot keep their alerts firing
        # via the re-post loop (a dead checker should stay visible).  A
        # vanished checker that was only *pending* stays pending until it
        # reports again — a single unconfirmed blip never pages.

        await self._post(to_post)

    async def forget(self, checker_id: str, reason: str = "checker removed") -> None:
        """Resolve a checker's alerts now and drop all state kept for it.

        For a checker that has left the controller for good (e.g. a missing
        checker whose app was disabled).  ``sync`` deliberately keeps a
        vanished checker's alert firing, and the improvement hold exists for
        flapping checkers, not departed ones, so neither applies here.
        """
        async with self._post_lock:
            keys = {
                key
                for key in (
                    set(self._active) | set(self._pending) | set(self._improving)
                )
                if self._key_belongs_to_checker(key, checker_id)
            }
            resolved = []
            for key in keys:
                self._pending.pop(key, None)
                self._improving.pop(key, None)
                active = self._active.pop(key, None)
                if active is None:
                    continue
                self._log(
                    f"Alert resolved for checker '{self._stream_name(key)}' "
                    f"({active['labels'].get('alertname')}) — {reason}",
                    level="INFO",
                )
                resolved.append(self._resolved_copy(active))
            await self._post(resolved)

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

    @staticmethod
    def _key_belongs_to_checker(key: AlertKey, checker_id: str) -> bool:
        """Whether an alert state key belongs to *checker_id*."""
        return key == checker_id or (
            isinstance(key, tuple) and len(key) == 2 and key[0] == checker_id
        )

    @staticmethod
    def _stream_name(key: AlertKey) -> str:
        """Human-readable stream name for bridge logs."""
        if isinstance(key, tuple):
            return f"{key[0]} ({key[1]})"
        return key

    def _condition_streams(
        self, checker_id: str, checker: Dict[str, Any]
    ) -> List[Tuple[AlertKey, Dict[str, Any]]]:
        """Return legacy or physical/technical Alertmanager streams.

        ``alerting.conditions`` deliberately supports only the two named
        conditions this health domain needs.  A complete configuration has a
        physical condition with the installed responder-skip label
        ``scope=host`` and a responder-eligible technical condition.  Any
        malformed/incomplete configuration falls back to the legacy aggregate
        stream without a host scope, so a bad config can never discard a
        failure or suppress a technical alert.
        """
        alerting = checker.get("alerting") or {}
        conditions = alerting.get("conditions") if isinstance(alerting, dict) else None
        if conditions is None:
            return [(checker_id, checker)]

        parsed = self._parse_conditions(checker_id, conditions)
        if parsed is None:
            return [(checker_id, checker)]

        categorized: Dict[str, List[Dict[str, Any]]] = {
            condition: [] for condition in _CONDITION_NAMES
        }
        for check in checker.get("checks", []):
            status = check.get("status", "unknown")
            if status in ("ok", "unknown"):
                continue
            category = check.get("alert_category")
            if category not in _CONDITION_NAMES:
                # A malformed marker must not make a failing result disappear.
                # Technical is deliberately responder-eligible and has no
                # host-scope label, making it the safe fail-open destination.
                category = "technical"
                invalid_key = (checker_id, str(check.get("name", "?")))
                if invalid_key not in self._invalid_categories_logged:
                    self._invalid_categories_logged.add(invalid_key)
                    self._log(
                        f"Alert condition metadata invalid for checker "
                        f"'{checker_id}', check '{check.get('name', '?')}' — "
                        "routing it to responder-eligible technical alert",
                        level="WARNING",
                    )
            categorized[category].append(dict(check))

        streams: List[Tuple[AlertKey, Dict[str, Any]]] = []
        for condition in _CONDITION_NAMES:
            condition_checks = categorized[condition]
            stream_alerting = {
                **alerting,
                "alertname": parsed[condition]["alertname"],
                "labels": dict(parsed[condition]["labels"]),
            }
            stream_checker = {
                **checker,
                "status": self._worst_status(condition_checks),
                "checks": condition_checks,
                "alerting": stream_alerting,
                # Validated condition labels never come from a legacy
                # checker-level field, so an invalid config cannot carry a
                # host-scope label into its responder-eligible fallback.
                "_alert_labels": dict(parsed[condition]["labels"]),
            }
            streams.append(((checker_id, condition), stream_checker))
        return streams

    def _parse_conditions(
        self, checker_id: str, conditions: Any
    ) -> Optional[Dict[str, Dict[str, Any]]]:
        """Validate and sanitize the fixed physical/technical contract.

        Invalid user configuration fails open to the legacy stream.  This is
        intentionally stricter than a generic arbitrary-label mechanism:
        Alertmanager's identity labels must remain bridge-owned, and the only
        responder-skip label allowed here is the physical condition's fixed
        ``scope=host``.
        """
        if not isinstance(conditions, dict) or set(conditions) != set(_CONDITION_NAMES):
            self._log_invalid_conditions(checker_id, "must define exactly physical and technical")
            return None

        parsed: Dict[str, Dict[str, Any]] = {}
        for condition in _CONDITION_NAMES:
            raw = conditions.get(condition)
            if not isinstance(raw, dict):
                self._log_invalid_conditions(checker_id, f"{condition} must be a mapping")
                return None
            alertname = raw.get("alertname")
            if not isinstance(alertname, str) or not alertname.strip():
                self._log_invalid_conditions(checker_id, f"{condition}.alertname is required")
                return None
            raw_labels = raw.get("labels", {})
            if not isinstance(raw_labels, dict):
                self._log_invalid_conditions(checker_id, f"{condition}.labels must be a mapping")
                return None

            labels: Dict[str, str] = {}
            for label, value in raw_labels.items():
                if (
                    not isinstance(label, str)
                    or not _LABEL_NAME_RE.fullmatch(label)
                    or label in _RESERVED_LABELS
                    or not isinstance(value, (str, int, float, bool))
                ):
                    self._log_invalid_conditions(
                        checker_id, f"{condition}.labels contains an invalid or reserved label"
                    )
                    return None
                labels[label] = str(value)

            if condition == "physical":
                if labels.get("scope") != "host":
                    self._log_invalid_conditions(
                        checker_id, "physical.labels.scope must be host"
                    )
                    return None
            elif "scope" in labels:
                self._log_invalid_conditions(
                    checker_id, "technical.labels may not set scope"
                )
                return None
            parsed[condition] = {"alertname": alertname.strip(), "labels": labels}
        self._invalid_conditions_logged.discard(checker_id)
        return parsed

    def _log_invalid_conditions(self, checker_id: str, reason: str) -> None:
        """Warn once that a condition config is using legacy fail-open mode."""
        if checker_id in self._invalid_conditions_logged:
            return
        self._invalid_conditions_logged.add(checker_id)
        self._log(
            f"Invalid alert conditions for checker '{checker_id}' ({reason}) — "
            "using responder-eligible aggregate alert",
            level="WARNING",
        )

    @staticmethod
    def _worst_status(checks: List[Dict[str, Any]]) -> str:
        """Return the legacy aggregate status for one condition's checks."""
        statuses = {check.get("status", "unknown") for check in checks}
        if "critical" in statuses:
            return "critical"
        if "degraded" in statuses:
            return "degraded"
        if "warning" in statuses:
            return "warning"
        return "ok"

    def _improve_holding(
        self, stream_key: AlertKey, now: datetime.datetime, what: str
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
        imp = self._improving.get(stream_key)
        if imp is None:
            self._improving[stream_key] = {"since": now}
            self._log(
                f"Improvement hold for checker '{self._stream_name(stream_key)}' — {what} "
                f"withheld until sustained for {self._improve_hold_s}s",
                level="INFO",
            )
            return True
        elapsed = (now - imp["since"]).total_seconds()
        if elapsed < self._improve_hold_s:
            return True
        del self._improving[stream_key]
        self._log(
            f"Improvement sustained for checker '{self._stream_name(stream_key)}' "
            f"({elapsed:.0f}s ≥ {self._improve_hold_s}s) — applying {what}",
            level="INFO",
        )
        return False

    def _promotion_due(
        self,
        display_name: str,
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
                        f"Repair hold for checker '{display_name}' — critical "
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

        labels = {
            "alertname": alertname,
            "severity": severity,
            "source": SOURCE_LABEL,
            "checker": checker_id,
        }
        # Condition configs are validated before they reach this point. Keep
        # this second guard so a hand-built snapshot cannot override the
        # bridge-owned Alertmanager identity labels.
        for label, value in (checker.get("_alert_labels") or {}).items():
            if (
                isinstance(label, str)
                and _LABEL_NAME_RE.fullmatch(label)
                and label not in _RESERVED_LABELS
                and isinstance(value, str)
            ):
                labels[label] = value

        return {
            "labels": labels,
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
