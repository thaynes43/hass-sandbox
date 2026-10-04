"""Health Check Controller — aggregates status from decoupled checker apps.

Provisions HA entities on startup, maintains a heartbeat so the frontend
can detect when AppDaemon is offline, listens for checker registration and
status reports via the HA event bus, and publishes aggregated health state
to a virtual sensor for the custom Lovelace cards.

When ``alertmanager_url`` is configured, the controller also mirrors
checker health into the cluster's Alertmanager via
:class:`health_checks.shared.alertmanager_bridge.AlertmanagerBridge`:
one alert per non-ok checker, re-posted every
``alertmanager_repost_interval_s`` (must stay under Alertmanager's
``resolve_timeout``) and resolved with an immediate ``endsAt`` post when
the checker recovers.

Checkers can be **muted** from the Lovelace card (``mute_checker`` /
``unmute_checker`` relay commands, optional ``duration_s``): a muted
checker still reports status but never reaches Alertmanager, so it cannot
page.  Mute state persists in ``input_text.health_check_mute_<checker_id>``
helpers (lazily provisioned) and is restored when checkers re-register
after a restart; timed mutes are lifted by the heartbeat tick.

Communication with checker apps is **event-only** (never ``get_app``),
allowing the controller to run in production Kubernetes while new checkers
are developed on a laptop.

A **registration watchdog** rides the heartbeat tick: a checker that is
expected (an enabled checker app in this AppDaemon instance, read from the
admin namespace, or a ``checker_id`` another registered checker declares
as a dependency) but has not registered is first restarted (bounded
backoff, once AppDaemon has finished starting the app), and if it is still
missing after ``registration_grace_s`` it is published as a synthetic
``critical`` checker so it reaches Alertmanager like any other failure.  Without this, a checker that died
before registering vanished silently and masked its dependents as
``unknown`` (incident 2026-10-03, hass-sandbox#226).
"""

from __future__ import annotations

import copy
import datetime
import inspect
import json
import logging
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

# AppDaemon only adds apps/ to sys.path — add appdaemon root for providers.
sys.path.append(str(Path(__file__).resolve().parents[3]))

import hassapi as hass

from providers.alertmanager import AlertmanagerClient
from providers.ha_provisioner import HAProvisioner
from providers.metrics import get_metrics, prometheus_available, start_metrics_server

from health_checks.shared.alertmanager_bridge import AlertmanagerBridge

logger = logging.getLogger(__name__)

SENSOR_ENTITY_ID = "sensor.health_check_status"
HEARTBEAT_ENTITY_ID = "input_datetime.appdaemon_heartbeat"

_DEFAULT_RETENTION = timedelta(days=1, hours=12)  # 1 day 12 hours = 129600 s

# Registration watchdog: which AppDaemon apps count as checkers, and how a
# checker that never registered is published.
CHECKER_MODULE_PREFIX = "health_checks.checker_apps."
REGISTRATION_CHECK_NAME = "Registration"
NOT_REGISTERED_ALERTNAME = "HealthCheckerNotRegistered"
# AppDaemon's per-app lifecycle state (``app.<name>`` in the admin
# namespace) while it has not finished starting the app.  After start-up the
# state is ``idle``, ``initialize_error``, ``compile_error``, ``terminated``
# or, while a callback runs, ``"<callback> for <app>"``.
APP_STARTING_STATES = frozenset({"loaded", "created", "initializing"})
# Lifecycle states in which an app that registered earlier is no longer
# running (a single-app reload whose initialize() raised, a stopped app).
APP_DEAD_STATES = frozenset({"initialize_error", "compile_error", "terminated"})


def discover_configured_checkers(
    admin_states: Mapping[str, Any], exclude_app: Optional[str] = None
) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """Map ``checker_id`` → ``{"app", "name", "alerting_enabled", "app_state"}``
    for the enabled checker apps in this AppDaemon instance.

    ``admin_states`` is AppDaemon's admin namespace (``get_state(
    namespace="admin")``): one ``app.<name>`` entity per enabled app, whose
    state is the app's lifecycle state and whose ``args`` attribute is the
    app's config.  A checker app is one whose module lives under
    ``health_checks.checker_apps``.

    Returns ``(mapping, problems)``; ``problems`` are human-readable reasons
    an app could not be tracked (no ``checker_id``, duplicate id).
    """
    mapping: Dict[str, Dict[str, Any]] = {}
    problems: List[str] = []
    for entity_id, entity in admin_states.items():
        if not str(entity_id).startswith("app.") or not isinstance(entity, Mapping):
            continue
        app_name = str(entity_id)[len("app."):]
        if app_name == exclude_app:
            continue
        args = (entity.get("attributes") or {}).get("args")
        if not isinstance(args, Mapping):
            continue
        module = str(args.get("module") or "")
        if args.get("disable") or "class" not in args or not module.startswith(
            CHECKER_MODULE_PREFIX
        ):
            continue
        checker_id = str(args.get("checker_id") or "")
        if not checker_id:
            problems.append(
                f"checker app '{app_name}' has no checker_id in its config — "
                "the registration watchdog cannot track it"
            )
            continue
        if checker_id in mapping:
            problems.append(
                f"checker apps '{mapping[checker_id]['app']}' and '{app_name}' "
                f"share checker_id '{checker_id}' — tracking the first"
            )
            continue
        alerting = args.get("alerting")
        mapping[checker_id] = {
            "app": app_name,
            "name": str(args.get("checker_name") or checker_id),
            "alerting_enabled": not (
                isinstance(alerting, Mapping) and alerting.get("enabled") is False
            ),
            "app_state": entity.get("state"),
        }
    return mapping, problems


def _parse_retention(s: str) -> timedelta:
    """Parse a DD:HH:MM:SS or HH:MM:SS string into a timedelta.

    Falls back to the default retention if the string is unparseable.
    """
    try:
        parts = s.split(":")
        if len(parts) == 4:
            return timedelta(
                days=int(parts[0]),
                hours=int(parts[1]),
                minutes=int(parts[2]),
                seconds=int(parts[3]),
            )
        elif len(parts) == 3:
            return timedelta(
                hours=int(parts[0]),
                minutes=int(parts[1]),
                seconds=int(parts[2]),
            )
    except (ValueError, IndexError):
        pass
    return _DEFAULT_RETENTION


class HealthCheckController(hass.Hass):
    """AppDaemon app that aggregates health-check reports from decoupled checkers."""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        args = self.args or {}

        self._heartbeat_interval_s: int = int(args.get("heartbeat_interval_s", 60))
        self._alert_history_max: int = int(args.get("alert_history_max", 50))

        retention_str: str = args.get("alert_retention", "1:12:00:00")
        self._alert_retention: timedelta = _parse_retention(str(retention_str))

        # State: registered checkers
        self._checkers: Dict[str, Dict[str, Any]] = {}
        # Track last known repair status per checker for transition detection
        self._repair_statuses: Dict[str, str] = {}  # checker_id → last repair status
        # Muted checkers: checker_id → {"until": iso-str | None}.  Muted
        # checkers never reach Alertmanager (no Pushover pages); state is
        # persisted per checker in input_text.health_check_mute_<checker_id>
        # and rebuilt on registration after a restart.
        self._mutes: Dict[str, Dict[str, Any]] = {}

        # Alertmanager bridge (optional — enabled when alertmanager_url is set)
        self._alertmanager_url: str = str(args.get("alertmanager_url") or "")
        self._alert_repost_interval_s: int = int(
            args.get("alertmanager_repost_interval_s", 120)
        )

        # For-duration gate: how long a checker must stay non-ok before its
        # alert pages, by severity, with optional per-checker overrides.
        # Unset → 0 (raise immediately, the pre-gate behaviour).
        alert_for_seconds: Dict[str, Any] = dict(args.get("alert_for_seconds") or {})
        alert_for_overrides: Dict[str, Any] = dict(
            args.get("alert_for_overrides") or {}
        )

        # Cap on how long an active auto-repair may withhold a due critical
        # promotion (see AlertmanagerBridge repair hold).
        alert_repair_hold_cap_s = int(args.get("alert_repair_hold_cap_s", 1800))

        # Improvement hold: a firing alert's resolve/de-escalation must be
        # sustained this long before it is applied, so an oscillating
        # condition pages once instead of once per flap (see
        # AlertmanagerBridge improvement hold). 0 = act immediately.
        alert_improve_hold_s = int(args.get("alert_improve_hold_s", 0))

        # Registration watchdog (see module docstring).  Expected checkers
        # that have not registered: checker_id → tracking entry.
        self._reg_watchdog_enabled: bool = bool(
            args.get("registration_watchdog_enabled", True)
        )
        self._reg_grace_s: int = max(0, int(args.get("registration_grace_s", 300)))
        self._reg_restart_after_s: int = max(
            1, int(args.get("registration_restart_after_s", 120))
        )
        self._reg_restart_attempts: int = max(
            0, int(args.get("registration_restart_attempts", 3))
        )
        self._unregistered: Dict[str, Dict[str, Any]] = {}
        self._reg_logged_problems: Set[str] = set()
        self._reg_all_registered_logged: bool = False
        # Last successful read of the configured checker apps: a failed read
        # keeps tracking on this rather than resetting the watchdog's clocks.
        self._reg_last_configured: Dict[str, Dict[str, Any]] = {}
        self._reg_can_restart: bool = True
        self._reg_armed_logged: bool = False
        self._reg_first_tick_at: Optional[float] = None
        # Alert history of a registration voided because its app died, kept
        # for when the checker registers again.
        self._reg_saved_history: Dict[str, List[Dict[str, Any]]] = {}

        # Prometheus metrics exposition (generic across all checkers).
        self._metrics_enabled: bool = bool(
            args.get("metrics_enabled", True)
        ) and prometheus_available()
        self._metrics_port: int = int(args.get("metrics_port", 9100))
        self._metrics = get_metrics()

        self._alert_bridge: Optional[AlertmanagerBridge] = None
        if self._alertmanager_url:
            self._alert_bridge = AlertmanagerBridge(
                AlertmanagerClient(self._alertmanager_url),
                log_fn=self.log,
                default_for_seconds=alert_for_seconds,
                for_overrides=alert_for_overrides,
                repair_hold_cap_s=alert_repair_hold_cap_s,
                improve_hold_s=alert_improve_hold_s,
            )

        self.log(
            f"HealthCheckController initialising: "
            f"heartbeat_interval={self._heartbeat_interval_s}s, "
            f"alert_history_max={self._alert_history_max}, "
            f"alert_retention={self._alert_retention}, "
            f"alertmanager={'enabled (' + self._alertmanager_url + ')' if self._alert_bridge else 'disabled'}, "
            f"alert_for_seconds={alert_for_seconds or '{}'}, "
            f"alert_for_overrides={alert_for_overrides or '{}'}, "
            f"alert_improve_hold_s={alert_improve_hold_s}, "
            f"registration_watchdog="
            + (
                f"grace={self._reg_grace_s}s restart_after={self._reg_restart_after_s}s "
                f"max_restarts={self._reg_restart_attempts}"
                if self._reg_watchdog_enabled
                else "disabled"
            ),
            level="INFO",
        )

        self.run_in(self._on_startup, 0)

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def _on_startup(self, kwargs: Any) -> None:
        """run_in callback — launches the async startup coroutine."""
        self.create_task(self._async_startup())

    async def _async_startup(self) -> None:
        """Provision entities, register listeners, start heartbeat."""
        await self._provision_entities()

        # Start the Prometheus exposition server (once per process).
        if self._metrics_enabled:
            started = start_metrics_server(self._metrics_port)
            self.log(
                f"Prometheus metrics {'enabled on :' + str(self._metrics_port) if started else 'unavailable'}",
                level="INFO",
            )

        # Listen for commands from checkers and from the relay script
        self.listen_event(self._on_command, "health_check_command")

        # Start heartbeat timer
        self.run_every(self._heartbeat_tick, "now", self._heartbeat_interval_s)

        # Keep firing alerts alive in Alertmanager (re-post < resolve_timeout)
        if self._alert_bridge is not None:
            self.run_every(
                self._alert_repost_tick,
                f"now+{self._alert_repost_interval_s}",
                self._alert_repost_interval_s,
            )

        # Publish initial state
        self._publish_status()

        # Signal to checkers that we are ready
        self.fire_event("health_check_controller_ready")

        self.log("HealthCheckController started — ready event fired", level="INFO")

        if self._reg_watchdog_enabled:
            self._reg_can_restart = callable(getattr(self, "restart_app", None))
            if not self._reg_can_restart:
                self.log(
                    "Registration watchdog: AppDaemon offers no restart_app() — "
                    "missing checkers will be reported but not restarted",
                    level="WARNING",
                )
            # The app list is read on the first heartbeat tick, not here:
            # this coroutine runs on AppDaemon's event loop, where get_state()
            # returns a Task instead of the states.

    async def _provision_entities(self) -> None:
        """Create the heartbeat helper and relay script if they don't exist."""
        ha_url = self.args.get("ha_url")
        ha_token_env = self.args.get("ha_token_env")
        if not ha_url or not ha_token_env:
            self.log(
                "ha_url / ha_token_env not configured — skipping provisioning",
                level="WARNING",
            )
            return

        prov = HAProvisioner(ha_url=ha_url, ha_token_env=ha_token_env)

        # Provision heartbeat input_datetime
        try:
            created = await prov.ensure_helper(
                "input_datetime",
                "AppDaemon Heartbeat",
                has_date=True,
                has_time=True,
            )
            msg = "created" if created else "already exists"
            self.log(
                f"Helper {HEARTBEAT_ENTITY_ID} {msg}",
                level="INFO" if created else "DEBUG",
            )
        except Exception as exc:
            self.log(
                f"Failed to provision heartbeat helper: {exc!r}",
                level="ERROR",
            )

        # Provision relay script
        try:
            created = await prov.ensure_script("health_check_relay", {
                "alias": "Health Check Relay",
                "description": "Relays dashboard commands to the health check controller",
                "mode": "queued",
                "max": 10,
                "fields": {
                    "command": {
                        "name": "Command",
                        "description": "Command name",
                        "required": True,
                        "selector": {"text": {}},
                    },
                    "payload": {
                        "name": "Payload",
                        "description": "JSON-encoded command data",
                        "required": False,
                        "selector": {"text": {}},
                    },
                },
                "sequence": [{
                    "event": "health_check_command",
                    "event_data": {
                        "command": "{{ command }}",
                        "payload": "{{ payload | default('{}') }}",
                    },
                }],
            })
            msg = "created" if created else "already exists"
            self.log(
                f"Relay script.health_check_relay {msg}",
                level="INFO" if created else "DEBUG",
            )
        except Exception as exc:
            self.log(
                f"Failed to provision relay script: {exc!r}",
                level="ERROR",
            )

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    def _alert_repost_tick(self, kwargs: Any) -> None:
        """Re-post firing alerts so they outlive Alertmanager's resolve_timeout."""
        if self._alert_bridge is not None:
            self.create_task(self._alert_bridge.repost_active())

    def _heartbeat_tick(self, kwargs: Any) -> None:
        """Update the heartbeat helper with the current timestamp."""
        try:
            self._expire_mutes()
        except Exception as exc:
            self.log(f"Mute expiry check failed: {exc!r}", level="ERROR")

        if self._reg_watchdog_enabled:
            try:
                self._registration_watchdog_tick()
            except Exception as exc:
                self.log(f"Registration watchdog failed: {exc!r}", level="ERROR")

        now = datetime.datetime.now()
        dt_str = now.strftime("%Y-%m-%d %H:%M:%S")
        try:
            self.call_service(
                "input_datetime/set_datetime",
                entity_id=HEARTBEAT_ENTITY_ID,
                datetime=dt_str,
            )
            self.log(f"Heartbeat updated: {dt_str}", level="DEBUG")
        except Exception as exc:
            self.log(f"Heartbeat update failed: {exc!r}", level="ERROR")

    # ------------------------------------------------------------------
    # Event handling
    # ------------------------------------------------------------------

    def _on_command(self, event_name: str, data: dict, kwargs: Any) -> None:
        """Route commands from checker apps and the relay script."""
        cmd = data.get("command")

        # Relay script wraps payload as JSON string; direct events may pass a dict
        raw_payload = data.get("payload", "{}")
        if isinstance(raw_payload, str):
            try:
                payload = json.loads(raw_payload)
            except (json.JSONDecodeError, TypeError):
                payload = {}
        else:
            payload = raw_payload or {}

        self.log(f"Received command: {cmd}", level="DEBUG")

        if cmd == "register_checker":
            self._handle_register(payload)
        elif cmd == "report_status":
            self._handle_report_status(payload)
        elif cmd == "force_recheck":
            self._handle_force_recheck(payload)
        elif cmd == "start_repair":
            self._handle_start_repair(payload)
        elif cmd == "cancel_repair":
            self._handle_cancel_repair(payload)
        elif cmd == "update_repair_config":
            self._handle_update_repair_config(payload)
        elif cmd == "clear_alert_history":
            self._handle_clear_alert_history(payload)
        elif cmd == "mute_checker":
            self._handle_mute(payload)
        elif cmd == "unmute_checker":
            self._handle_unmute(payload)
        elif cmd == "record_note":
            self._handle_record_note(payload)
        else:
            self.log(f"Unknown health_check_command: {cmd!r}", level="WARNING")

    def _handle_register(self, payload: dict) -> None:
        """Register a health checker app."""
        checker_id = payload.get("checker_id", "")
        checker_name = payload.get("checker_name", checker_id)
        check_names = payload.get("check_names", [])

        if not checker_id:
            self.log("register_checker missing checker_id", level="WARNING")
            return

        is_new = checker_id not in self._checkers
        self._clear_unregistered(checker_id, reason="registered")
        self._checkers[checker_id] = {
            "name": checker_name,
            "status": "unknown",
            "last_check": None,
            "checks": [
                {"name": n, "status": "unknown", "detail": "", "last_changed": None}
                for n in check_names
            ],
            "alert_history": self._checkers.get(checker_id, {}).get(
                "alert_history"
            )
            or self._reg_saved_history.pop(checker_id, []),
            "supports_repair": bool(payload.get("supports_repair", False)),
            "repair_state": payload.get("repair_state"),
            "dependencies": payload.get("dependencies", []),
            "alerting": payload.get("alerting") or {},
        }

        # Rebuild persisted mute state (survives controller restarts).
        persisted_mute = self._load_persisted_mute(checker_id)
        if persisted_mute is not None:
            self._mutes[checker_id] = persisted_mute
            until = persisted_mute.get("until")
            self.log(
                f"Checker '{checker_id}' is muted "
                f"({'until ' + until if until else 'indefinitely'}) — restored "
                "from persisted state",
                level="INFO",
            )

        action = "Registered" if is_new else "Re-registered"
        self.log(
            f"{action} checker '{checker_name}' (id={checker_id}), "
            f"checks={check_names}",
            level="INFO",
        )
        self._publish_status()

    def _handle_report_status(self, payload: dict) -> None:
        """Process a status report from a checker app."""
        checker_id = payload.get("checker_id", "")
        if not checker_id or checker_id not in self._checkers:
            self.log(
                f"report_status for unknown checker: {checker_id!r}",
                level="WARNING",
            )
            return

        checker = self._checkers[checker_id]
        now = datetime.datetime.now()
        now_iso = now.isoformat(timespec="seconds")
        results: List[Dict[str, str]] = payload.get("results", [])

        # Build a lookup from existing checks for tracking changes
        old_checks = {c["name"]: c for c in checker["checks"]}

        new_checks = []
        worst_status = "ok"
        for result in results:
            name = result.get("name", "")
            status = result.get("status", "unknown")
            detail = result.get("detail", "")

            old = old_checks.get(name, {})
            old_status = old.get("status", "unknown")

            # Track when status changed
            if status != old_status:
                last_changed = now_iso
                # Record alert if transitioning to/from non-ok
                if status != "ok" or old_status not in ("ok", "unknown"):
                    previous_state_entered = old.get("last_changed")
                    if previous_state_entered is not None:
                        try:
                            prev_dt = datetime.datetime.fromisoformat(
                                previous_state_entered
                            )
                            previous_state_duration_s = (
                                now - prev_dt
                            ).total_seconds()
                        except (ValueError, TypeError):
                            previous_state_duration_s = None
                    else:
                        previous_state_duration_s = None

                    checker["alert_history"].insert(0, {
                        "timestamp": now_iso,
                        "check": name,
                        "from_status": old_status,
                        "to_status": status,
                        "detail": detail,
                        "previous_state_entered": previous_state_entered,
                        "previous_state_duration_s": previous_state_duration_s,
                    })
            else:
                last_changed = old.get("last_changed")

            new_checks.append({
                "name": name,
                "status": status,
                "detail": detail,
                "last_changed": last_changed,
            })

            if status == "critical":
                worst_status = "critical"
            elif status == "degraded" and worst_status not in ("critical",):
                worst_status = "degraded"
            elif status == "warning" and worst_status not in ("critical", "degraded"):
                worst_status = "warning"
            elif status == "unknown" and worst_status not in ("critical", "degraded", "warning"):
                worst_status = "unknown"

        # Prune alerts older than retention period (TTL), then cap at max
        cutoff = now - self._alert_retention
        cutoff_iso = cutoff.isoformat(timespec="seconds")
        checker["alert_history"] = [
            a for a in checker["alert_history"]
            if a.get("timestamp", "") >= cutoff_iso
        ]
        checker["alert_history"] = checker["alert_history"][: self._alert_history_max]

        # Store repair state if reported by the checker
        repair_state = payload.get("repair_state")
        if repair_state is not None:
            checker["repair_state"] = repair_state

        # Record repair lifecycle transitions in alert history
        if repair_state:
            new_repair_status = repair_state.get("status", "idle")
            old_repair_status = self._repair_statuses.get(checker_id, "idle")

            if new_repair_status != old_repair_status:
                self._repair_statuses[checker_id] = new_repair_status

                # Record meaningful transitions (skip idle→idle, idle→pending is noise)
                interesting = {"in_progress", "success", "failed"}
                if new_repair_status in interesting or old_repair_status in interesting:
                    checker["alert_history"].insert(0, {
                        "timestamp": now_iso,
                        "check": "Auto-Repair",
                        "from_status": old_repair_status,
                        "to_status": new_repair_status,
                        "detail": repair_state.get("detail", ""),
                        "is_repair_event": True,
                    })

        # Repair-status-only reports (results=[]) must not wipe the checks
        # list or reset the checker to ok — that would falsely resolve any
        # Alertmanager alert mid-repair.  Only full reports replace state.
        if results:
            checker["checks"] = new_checks
            checker["status"] = worst_status
            checker["last_check"] = now_iso
            self.log(
                f"Status report from '{checker['name']}': {worst_status} "
                f"({len(results)} checks)",
                level="INFO",
            )
        else:
            self.log(
                f"Repair-state-only report from '{checker['name']}' "
                f"(status stays {checker['status']})",
                level="DEBUG",
            )

        # Forward explicit metric payloads (repair completions + domain values)
        # to the exporter. Processed for both full and repair-only reports.
        if self._metrics_enabled:
            self._ingest_reported_metrics(checker_id, payload)

        self._publish_status()

    def _ingest_reported_metrics(self, checker_id: str, payload: dict) -> None:
        """Feed the exporter from a checker's optional ``repair_events`` and
        ``metrics`` payload fields (see the checker→controller protocol)."""
        for ev in payload.get("repair_events") or []:
            try:
                self._metrics.record_repair_event(
                    checker_id,
                    result=ev.get("result", ""),
                    duration_s=ev.get("duration_s"),
                    device=str(ev.get("device", "") or ""),
                )
            except Exception as exc:
                self.log(f"repair_event metric failed: {exc!r}", level="WARNING")
        for m in payload.get("metrics") or []:
            try:
                self._metrics.record_custom(
                    checker_id,
                    name=m["name"],
                    value=m["value"],
                    metric_type=m.get("type", "gauge"),
                    labels=m.get("labels") or {},
                )
            except Exception as exc:
                self.log(f"custom metric failed: {exc!r}", level="WARNING")

    def _alert_severity_counts(
        self, alerts: Optional[Dict[str, Dict[str, Any]]]
    ) -> Dict[str, int]:
        """Count alerts by severity from a bridge active/pending mapping."""
        counts: Dict[str, int] = {}
        for a in (alerts or {}).values():
            sev = ((a or {}).get("labels") or {}).get("severity", "")
            if sev:
                counts[sev] = counts.get(sev, 0) + 1
        return counts

    def _handle_force_recheck(self, payload: dict) -> None:
        """Forward a force-recheck request to all checkers."""
        self.log("Broadcasting force recheck to all checkers", level="INFO")
        self.fire_event("health_check_recheck")

    def _handle_start_repair(self, payload: dict) -> None:
        """Forward a manual repair request to the target checker."""
        checker_id = payload.get("checker_id", "")
        checker = self._checkers.get(checker_id)
        if not checker:
            self.log(
                f"start_repair for unknown checker: {checker_id!r}",
                level="WARNING",
            )
            return
        if not checker.get("supports_repair"):
            self.log(
                f"start_repair rejected — checker '{checker_id}' does not support repair",
                level="WARNING",
            )
            return
        self.log(f"Forwarding start_repair to checker '{checker_id}'", level="INFO")
        self.fire_event(
            f"health_check_repair_{checker_id}",
            action="start_repair",
        )

    def _handle_cancel_repair(self, payload: dict) -> None:
        """Forward a cancel repair request to the target checker."""
        checker_id = payload.get("checker_id", "")
        checker = self._checkers.get(checker_id)
        if not checker:
            self.log(
                f"cancel_repair for unknown checker: {checker_id!r}",
                level="WARNING",
            )
            return
        if not checker.get("supports_repair"):
            self.log(
                f"cancel_repair rejected — checker '{checker_id}' does not support repair",
                level="WARNING",
            )
            return
        self.log(f"Forwarding cancel_repair to checker '{checker_id}'", level="INFO")
        self.fire_event(
            f"health_check_repair_{checker_id}",
            action="cancel_repair",
        )

    def _handle_update_repair_config(self, payload: dict) -> None:
        """Forward repair config updates to the target checker."""
        checker_id = payload.get("checker_id", "")
        checker = self._checkers.get(checker_id)
        if not checker:
            self.log(
                f"update_repair_config for unknown checker: {checker_id!r}",
                level="WARNING",
            )
            return
        if not checker.get("supports_repair"):
            self.log(
                f"update_repair_config rejected — checker '{checker_id}' "
                "does not support repair",
                level="WARNING",
            )
            return
        self.log(
            f"Forwarding update_repair_config to checker '{checker_id}'",
            level="INFO",
        )
        self.fire_event(
            f"health_check_repair_{checker_id}",
            action="update_repair_config",
            auto_repair_enabled=payload.get("auto_repair_enabled"),
            auto_repair_delay_min=payload.get("auto_repair_delay_min"),
        )

    def _handle_clear_alert_history(self, payload: dict) -> None:
        """Clear alert history for all checkers or a specific checker."""
        checker_id = payload.get("checker_id")
        if checker_id:
            if checker_id in self._checkers:
                self._checkers[checker_id]["alert_history"] = []
                self.log(
                    f"Cleared alert history for checker '{checker_id}'",
                    level="INFO",
                )
            else:
                self.log(
                    f"clear_alert_history for unknown checker: {checker_id!r}",
                    level="WARNING",
                )
        else:
            for c in self._checkers.values():
                c["alert_history"] = []
            self.log("Cleared all alert history", level="INFO")
        self._publish_status()

    def _handle_record_note(self, payload: dict) -> None:
        """Insert a triage note into a checker's alert history.

        Lets automation (e.g. a triage agent) leave an audit trail visible
        in the detail card: ``{checker_id, note, source?}``.  Notes ride the
        normal alert-history retention/cap.
        """
        checker_id = payload.get("checker_id", "")
        checker = self._checkers.get(checker_id)
        if not checker:
            self.log(
                f"record_note for unknown checker: {checker_id!r}",
                level="WARNING",
            )
            return
        note = str(payload.get("note", "")).strip()
        if not note:
            self.log("record_note with empty note — ignored", level="WARNING")
            return
        # Cap note length to protect the sensor's ~16 KB attribute budget.
        if len(note) > 280:
            note = note[:279] + "…"
        source = str(payload.get("source", "") or "agent").strip() or "agent"

        checker["alert_history"].insert(0, {
            "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
            "check": source,
            "from_status": source,
            "to_status": "note",
            "detail": note,
            "is_note_event": True,
        })
        checker["alert_history"] = checker["alert_history"][: self._alert_history_max]
        self.log(
            f"Note recorded for checker '{checker_id}' from '{source}': {note}",
            level="INFO",
        )
        self._publish_status()

    # ------------------------------------------------------------------
    # Muting (per-checker alert suppression)
    # ------------------------------------------------------------------

    def _mute_helper_entity(self, checker_id: str) -> str:
        return f"input_text.health_check_mute_{checker_id}"

    def _mute_helper_name(self, checker_id: str) -> str:
        return f"Health Check Mute {checker_id.replace('_', ' ').title()}"

    def _load_persisted_mute(self, checker_id: str) -> Optional[Dict[str, Any]]:
        """Read the mute helper for a checker; None = not muted.

        An expired or unparseable mute is treated as not muted.
        """
        try:
            raw = self.get_state(self._mute_helper_entity(checker_id))
        except Exception:
            return None
        if not raw or raw in ("unknown", "unavailable"):
            return None
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(data, dict) or not data.get("muted"):
            return None
        until = data.get("until") or None
        if until:
            try:
                if datetime.datetime.fromisoformat(until) <= datetime.datetime.now():
                    return None  # expired while we were away
            except (ValueError, TypeError):
                until = None
        return {"until": until}

    def _record_mute_event(
        self, checker_id: str, to_status: str, detail: str
    ) -> None:
        """Insert a mute/unmute transition into the checker's alert history."""
        checker = self._checkers.get(checker_id)
        if not checker:
            return
        checker["alert_history"].insert(0, {
            "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
            "check": "Alerting",
            "from_status": "unmuted" if to_status == "muted" else "muted",
            "to_status": to_status,
            "detail": detail,
            "is_mute_event": True,
        })

    def _handle_mute(self, payload: dict) -> None:
        """Mute a checker's alerts (optional duration_s; absent = indefinite)."""
        checker_id = payload.get("checker_id", "")
        if not self._is_mutable_checker(checker_id):
            self.log(
                f"mute_checker for unknown checker: {checker_id!r}",
                level="WARNING",
            )
            return

        until: Optional[str] = None
        duration_s = payload.get("duration_s")
        if duration_s is not None:
            try:
                duration_s = int(duration_s)
            except (TypeError, ValueError):
                duration_s = 0
            if duration_s > 0:
                until = (
                    datetime.datetime.now() + timedelta(seconds=duration_s)
                ).isoformat(timespec="seconds")

        self._mutes[checker_id] = {"until": until}
        detail = f"muted until {until}" if until else "muted indefinitely"
        self.log(f"Checker '{checker_id}' {detail}", level="INFO")
        self._record_mute_event(checker_id, "muted", detail)
        self._publish_status()
        self.create_task(self._persist_mute(checker_id, True, until))

    def _handle_unmute(self, payload: dict) -> None:
        checker_id = payload.get("checker_id", "")
        if not self._is_mutable_checker(checker_id):
            self.log(
                f"unmute_checker for unknown checker: {checker_id!r}",
                level="WARNING",
            )
            return
        self._unmute(checker_id, reason="unmuted from UI")

    def _unmute(self, checker_id: str, reason: str) -> None:
        """Shared unmute path for UI commands and expiry."""
        if checker_id not in self._mutes:
            return
        del self._mutes[checker_id]
        self.log(f"Checker '{checker_id}' unmuted ({reason})", level="INFO")
        self._record_mute_event(checker_id, "unmuted", reason)
        self._publish_status()
        self.create_task(self._persist_mute(checker_id, False, None))

    def _expire_mutes(self) -> None:
        """Lift any timed mutes whose expiry has passed."""
        now = datetime.datetime.now()
        expired = []
        for cid, mute in self._mutes.items():
            until = mute.get("until")
            if not until:
                continue
            try:
                if datetime.datetime.fromisoformat(until) <= now:
                    expired.append(cid)
            except (ValueError, TypeError):
                expired.append(cid)  # unparseable expiry — fail open (unmute)
        for cid in expired:
            self._unmute(cid, reason="mute expired")

    async def _persist_mute(
        self, checker_id: str, muted: bool, until: Optional[str]
    ) -> None:
        """Write mute state to the checker's input_text helper (lazily provisioned)."""
        entity_id = self._mute_helper_entity(checker_id)
        value = json.dumps({"muted": muted, "until": until})

        ha_url = self.args.get("ha_url")
        ha_token_env = self.args.get("ha_token_env")
        if ha_url and ha_token_env:
            try:
                prov = HAProvisioner(ha_url=ha_url, ha_token_env=ha_token_env)
                created = await prov.ensure_helper(
                    "input_text",
                    self._mute_helper_name(checker_id),
                    max=255,
                )
                if created:
                    self.log(f"Provisioned mute helper {entity_id}", level="INFO")
            except Exception as exc:
                self.log(
                    f"Failed to provision mute helper {entity_id}: {exc!r}",
                    level="ERROR",
                )
        try:
            await self.call_service(
                "input_text/set_value", entity_id=entity_id, value=value
            )
        except Exception as exc:
            self.log(
                f"Failed to persist mute state to {entity_id}: {exc!r} — "
                "mute is active in memory but won't survive a restart",
                level="ERROR",
            )

    # ------------------------------------------------------------------
    # Registration watchdog (checkers that are expected but never register)
    # ------------------------------------------------------------------

    def _monotonic(self) -> float:
        """Clock for the watchdog's timers (patched in tests)."""
        return time.monotonic()

    def _configured_checkers(self) -> Optional[Dict[str, Dict[str, Any]]]:
        """Checker apps enabled in this AppDaemon instance, or None on a failed read.

        Read from AppDaemon's admin namespace (``app.<name>`` entities carry
        each app's config and lifecycle state).  A failed read returns None
        so the caller keeps its last good list instead of forgetting
        checkers; a read that succeeds but finds no checker app is warned
        about, since it means the watchdog can restart nothing.
        """
        try:
            admin_states = self.get_state(namespace="admin")
            if not isinstance(admin_states, Mapping):
                raise TypeError(
                    f"expected a mapping, got {type(admin_states).__name__}"
                )
        except Exception as exc:
            self._log_registration_problem(
                "admin-read",
                f"Registration watchdog cannot read AppDaemon's app states "
                f"({exc!r}) — keeping the last list it read "
                f"({len(self._reg_last_configured)} checker apps)",
            )
            return None
        mapping, problems = discover_configured_checkers(
            admin_states, exclude_app=getattr(self, "name", None)
        )
        for problem in problems:
            self._log_registration_problem(problem, f"Registration watchdog: {problem}")
        return mapping

    def _log_registration_problem(self, key: str, message: str) -> None:
        """Log a configuration problem once per controller lifetime."""
        if key in self._reg_logged_problems:
            return
        self._reg_logged_problems.add(key)
        self.log(message, level="WARNING")

    def _declared_dependencies(self) -> Dict[str, List[str]]:
        """checker_id → names of the registered checkers that depend on it."""
        declared: Dict[str, List[str]] = {}
        for checker in self._checkers.values():
            for dep in checker.get("dependencies") or []:
                dep_id = (dep or {}).get("checker_id", "")
                if dep_id:
                    declared.setdefault(dep_id, []).append(checker["name"])
        return declared

    def _registration_watchdog_tick(self) -> None:
        """Restart, then surface, expected checkers that have not registered.

        Runs on the heartbeat tick.  A checker is *expected* when it is an
        enabled checker app in this AppDaemon instance or a registered
        checker declares it as a dependency.  Normal start-up registers
        every checker within seconds; the MQTT lights' 30-minute warm-up is
        a *status* (``unknown``) after registration, so it is never touched.

        Restarts wait until AppDaemon has finished starting the app (its
        admin state has left loading/initializing), so a slow AppDaemon-wide
        re-initialisation is never interrupted.  Surfacing counts from when
        the checker became expected, so an app AppDaemon never manages to
        start still gets reported.
        """
        now = self._monotonic()
        read = self._configured_checkers()
        if read is not None:
            self._reg_last_configured = read
        configured = self._reg_last_configured
        self._log_armed_once(configured, now)

        changed = False
        # A checker that registered earlier but whose app has since died (a
        # single-app reload whose initialize() raised, a stopped app) is no
        # longer registered: void the stale registration so the path below
        # restarts and, if need be, surfaces it.  Fresh reads only.
        if read is not None:
            for checker_id, cfg in configured.items():
                if (
                    checker_id in self._checkers
                    and cfg.get("app_state") in APP_DEAD_STATES
                ):
                    self._void_registration(checker_id, cfg)
                    changed = True
        declared = self._declared_dependencies()
        expected = set(configured) | set(declared)

        # Stop tracking a checker that is genuinely no longer expected — only
        # on a successful read, so a failed read never resets the clocks.
        if read is not None:
            for checker_id in list(self._unregistered):
                if checker_id not in expected:
                    reason = "is no longer configured or depended on"
                    changed |= self._clear_unregistered(checker_id, reason=reason)
                    # Unconditionally: besides a surfaced synthetic alert, a
                    # voided registration can leave the checker's own alert
                    # firing before it was ever surfaced.  forget() on a
                    # checker the bridge does not know is a no-op.
                    self._forget_checker(checker_id, reason)

        for checker_id in sorted(expected - set(self._checkers)):
            entry = self._unregistered.get(checker_id)
            if entry is None:
                entry = {
                    "since": now,
                    "started_at": None,
                    "attempts": 0,
                    "next_restart_at": None,
                    "surfaced": False,
                    "surfaced_at": None,
                }
                self._unregistered[checker_id] = entry
            cfg = configured.get(checker_id)
            entry["app"] = cfg["app"] if cfg else None
            entry["name"] = cfg["name"] if cfg else checker_id
            entry["alerting_enabled"] = cfg["alerting_enabled"] if cfg else True
            entry["app_state"] = cfg.get("app_state") if cfg else None
            entry["declared_by"] = sorted(set(declared.get(checker_id, [])))
            age = now - entry["since"]

            # 1. Self-heal: restart the app, with a doubling backoff, once
            #    AppDaemon has finished its own attempt to start it.
            starting = bool(cfg) and cfg.get("app_state") in APP_STARTING_STATES
            if entry["app"] and not starting and entry["started_at"] is None:
                entry["started_at"] = now
                entry["next_restart_at"] = now + self._reg_restart_after_s
            if (
                entry["app"]
                and self._reg_can_restart
                and not starting
                and entry["attempts"] < self._reg_restart_attempts
                and entry["next_restart_at"] is not None
                and now >= entry["next_restart_at"]
            ):
                self._restart_unregistered(checker_id, entry, now)

            # 2. Surface: past the grace period it is a critical checker.
            if not entry["surfaced"] and age >= self._reg_grace_s:
                entry["surfaced"] = True
                entry["surfaced_at"] = datetime.datetime.now().isoformat(
                    timespec="seconds"
                )
                self._restore_mute_for_unregistered(checker_id)
                self.log(
                    f"Registration watchdog: checker '{checker_id}' "
                    f"({entry['name']}) is still not registered {age:.0f}s "
                    f"after it became expected — reporting it as critical. "
                    f"{self._unregistered_detail(checker_id, entry)}",
                    level="WARNING",
                )
                changed = True

        if (
            not self._unregistered
            and configured
            and not self._reg_all_registered_logged
        ):
            self._reg_all_registered_logged = True
            self.log(
                f"Registration watchdog: all {len(expected)} expected checkers "
                f"registered ({len(configured)} configured apps)",
                level="INFO",
            )

        # Re-publish while anything is surfaced so the Alertmanager bridge
        # sees the condition persist (its for-gate promotes on a later sync).
        if changed or any(e["surfaced"] for e in self._unregistered.values()):
            self._publish_status()

    def _log_armed_once(self, configured: Dict[str, Any], now: float) -> None:
        """Log the armed line once AppDaemon's app list has settled.

        The first heartbeat tick fires while AppDaemon may still be creating
        apps (an app has no admin entity until then), so the line waits for
        a read that found checker apps, or for ``registration_grace_s`` to
        pass, after which an empty list really is empty and is warned about.
        """
        if self._reg_armed_logged:
            return
        if self._reg_first_tick_at is None:
            self._reg_first_tick_at = now
        if not configured and now - self._reg_first_tick_at < self._reg_grace_s:
            return
        self._reg_armed_logged = True
        self.log(
            f"Registration watchdog armed: {len(configured)} configured "
            f"checker app(s) tracked, grace {self._reg_grace_s}s, first "
            f"restart {self._reg_restart_after_s}s after AppDaemon has "
            f"started the app, at most {self._reg_restart_attempts} "
            "restart(s) per checker",
            level="INFO",
        )
        if not configured:
            self.log(
                "Registration watchdog found no enabled app under "
                f"'{CHECKER_MODULE_PREFIX}' in AppDaemon's app states — it "
                "can only report missing dependencies, and cannot restart them",
                level="WARNING",
            )

    def _void_registration(self, checker_id: str, cfg: Dict[str, Any]) -> None:
        """Forget a registration whose app is no longer running."""
        checker = self._checkers.pop(checker_id)
        self._reg_saved_history[checker_id] = checker.get("alert_history") or []
        self.log(
            f"Registration watchdog: checker '{checker_id}' registered earlier "
            f"but its app '{cfg['app']}' is now {cfg.get('app_state')} — "
            "treating it as not registered",
            level="WARNING",
        )

    def _restart_unregistered(
        self, checker_id: str, entry: Dict[str, Any], now: float
    ) -> None:
        """Ask AppDaemon to restart a checker app that has not registered.

        ``restart_app`` goes through AppDaemon's ``app/restart`` admin
        service, which stops and starts the app in a task AppDaemon owns,
        so the restart is not tied to this callback.  Called from this sync
        callback it returns None once the restart is scheduled (verified
        against AppDaemon 4.5.13).
        """
        entry["attempts"] += 1
        # Doubling backoff: restart_after, then 2x, 4x, ... between attempts.
        entry["next_restart_at"] = now + self._reg_restart_after_s * (
            2 ** entry["attempts"]
        )
        app = entry["app"]
        attempt = f"attempt {entry['attempts']}/{self._reg_restart_attempts}"
        try:
            result = self.restart_app(app)
        except Exception as exc:
            self.log(
                f"Registration watchdog: restart of app '{app}' for checker "
                f"'{checker_id}' failed ({attempt}): {exc!r}",
                level="ERROR",
            )
            return
        if inspect.iscoroutine(result):
            # Only if a future AppDaemon made restart_app a bare coroutine:
            # it would never run, so say so rather than claim a restart.
            result.close()
            self.log(
                f"Registration watchdog: restart_app('{app}') returned an "
                f"un-awaited coroutine — app NOT restarted ({attempt})",
                level="ERROR",
            )
            return
        self.log(
            f"Registration watchdog: restarted app '{app}' ({attempt}) — "
            f"checker '{checker_id}' had not registered "
            f"{now - entry['started_at']:.0f}s after AppDaemon started it",
            level="WARNING",
        )

    def _clear_unregistered(self, checker_id: str, reason: str) -> bool:
        """Stop tracking a checker; returns True if it had been surfaced.

        On registration the real checker replaces the synthetic one in the
        snapshot and the bridge resolves the alert through its normal path
        (improvement hold included).
        """
        entry = self._unregistered.pop(checker_id, None)
        if entry is None:
            return False
        if entry["attempts"] or entry["surfaced"]:
            restarts = entry["attempts"]
            self.log(
                f"Registration watchdog: checker '{checker_id}' {reason} after "
                f"{self._monotonic() - entry['since']:.0f}s "
                f"({restarts} restart{'s' if restarts != 1 else ''}"
                f"{', had been reported critical' if entry['surfaced'] else ''})",
                level="INFO",
            )
        return bool(entry["surfaced"])

    def _forget_checker(self, checker_id: str, reason: str) -> None:
        """Resolve any alert and drop the metrics of an unregistered checker
        that is no longer expected (its app was disabled or removed).

        Nothing will publish it again, and the bridge deliberately keeps a
        vanished checker's alert firing — the synthetic not-registered alert,
        or the checker's own alert from before its registration was voided —
        so without this the page would be re-posted for ever with no tile to
        mute it from.
        """
        if self._alert_bridge is not None:
            self.create_task(self._alert_bridge.forget(checker_id, reason))
        if self._metrics_enabled:
            self._metrics.remove_checker(checker_id)

    def _restore_mute_for_unregistered(self, checker_id: str) -> None:
        """Honour a mute persisted before the checker went missing."""
        if checker_id in self._mutes:
            return
        persisted = self._load_persisted_mute(checker_id)
        if persisted is not None:
            self._mutes[checker_id] = persisted

    def _is_mutable_checker(self, checker_id: str) -> bool:
        """Registered checkers, and surfaced unregistered ones, can be muted."""
        if checker_id in self._checkers:
            return True
        entry = self._unregistered.get(checker_id)
        return bool(entry and entry["surfaced"])

    def _unregistered_detail(self, checker_id: str, entry: Dict[str, Any]) -> str:
        """Human-readable detail for an expected checker that never registered."""
        missing_s = self._monotonic() - entry["since"]
        duration = (
            f"{missing_s:.0f}s" if missing_s < 120 else f"{round(missing_s / 60)} min"
        )
        parts = [f"not registered with the controller after {duration}"]
        declared_by = entry.get("declared_by") or []
        if declared_by:
            parts.append("dependents masked: " + ", ".join(declared_by))
        app = entry.get("app")
        attempts = entry["attempts"]
        if not app:
            parts.append(
                f"no checker app with checker_id '{checker_id}' is configured "
                "in this AppDaemon instance, so it cannot be restarted"
            )
        elif entry.get("app_state") in APP_STARTING_STATES:
            parts.append(
                f"AppDaemon has not finished starting app '{app}' "
                f"(state: {entry['app_state']})"
            )
        elif attempts == 0:
            parts.append(f"restart of app '{app}' pending")
        elif attempts < self._reg_restart_attempts:
            parts.append(
                f"{attempts} automatic restart(s) of app '{app}' did not help; "
                "retrying"
            )
        else:
            parts.append(
                f"{attempts} automatic restart(s) of app '{app}' did not help — "
                "check the AppDaemon error log"
            )
        return "; ".join(parts)

    def _unregistered_views(self) -> Dict[str, Dict[str, Any]]:
        """Synthetic checker entries for surfaced unregistered checkers."""
        views: Dict[str, Dict[str, Any]] = {}
        for checker_id, entry in self._unregistered.items():
            if not entry["surfaced"] or checker_id in self._checkers:
                continue
            views[checker_id] = {
                "name": entry.get("name") or checker_id,
                "status": "critical",
                "last_check": None,
                "checks": [{
                    "name": REGISTRATION_CHECK_NAME,
                    "status": "critical",
                    "detail": self._unregistered_detail(checker_id, entry),
                    "last_changed": entry.get("surfaced_at"),
                }],
                "alert_history": [],
                "supports_repair": False,
                "repair_state": None,
                "dependencies": [],
                "alerting": {
                    "alertname": NOT_REGISTERED_ALERTNAME,
                    "enabled": entry.get("alerting_enabled", True),
                },
            }
        return views

    # ------------------------------------------------------------------
    # Dependency resolution
    # ------------------------------------------------------------------

    def _resolve_dependencies(self, checker_id: str, checker: dict) -> dict:
        """Return a copy of checker with dependency-affected checks overridden.

        When a dependency checker is unhealthy (critical/degraded) or missing
        entirely, the checks that depend on it are overridden to ``unknown``
        in the published view. The internal ``_checkers`` state is never
        modified here.  A missing dependency is named in the detail as
        ``"<name> (not registered)"``; the registration watchdog restarts it
        and, past ``registration_grace_s``, publishes it as a critical
        checker, so the masking never stays silent.

        A dependency that is merely ``unknown`` (registered but not yet — or
        no longer — reporting) does NOT mask its dependents: an unknown
        dependency raises no alert itself, so masking would let a genuine
        dependent failure go completely silent.
        """
        deps = checker.get("dependencies", [])
        if not deps:
            return checker

        # Determine which checks are affected by which unmet dependencies
        # Maps check_name -> set of dependency names that are down
        affected_by: dict = {}
        all_check_names = {c["name"] for c in checker["checks"]}
        for dep in deps:
            dep_id = dep.get("checker_id", "")
            dep_checker = self._checkers.get(dep_id)
            # Treat a missing (never registered) dependency as unhealthy and
            # say so: the registration watchdog restarts it and, past its
            # grace period, reports it as a critical checker of its own.
            if dep_checker is None:
                entry = self._unregistered.get(dep_id) or {}
                dep_name = f"{entry.get('name') or dep_id} (not registered)"
            elif dep_checker["status"] in ("critical", "degraded"):
                dep_name = dep_checker["name"]
            else:
                dep_name = None
            if dep_name is not None:
                affects = dep.get("affects_checks", [])
                targets = set(affects) if affects else all_check_names
                for check_name in targets:
                    affected_by.setdefault(check_name, set()).add(dep_name)

        if not affected_by:
            return checker

        # Create a modified copy — do NOT mutate the original
        modified = copy.deepcopy(checker)
        for check in modified["checks"]:
            dep_names = affected_by.get(check["name"])
            if dep_names:
                check["status"] = "unknown"
                names = ", ".join(sorted(dep_names))
                check["detail"] = f"dependency unavailable: {names}"

        # Recompute the checker-level status from modified checks
        statuses = [c["status"] for c in modified["checks"]]
        if "critical" in statuses:
            modified["status"] = "critical"
        elif "degraded" in statuses:
            modified["status"] = "degraded"
        elif "warning" in statuses:
            modified["status"] = "warning"
        elif all(s == "ok" for s in statuses):
            modified["status"] = "ok"
        else:
            modified["status"] = "unknown"

        return modified

    # ------------------------------------------------------------------
    # Sensor publication
    # ------------------------------------------------------------------

    def _checker_attrs(
        self, checker_id: str, c: dict, is_dependency: bool = False
    ) -> dict:
        """Extract the attributes to publish for a single checker.

        For checkers with many checks, only non-ok checks are included
        in the published ``checks`` list to stay within HA's ~16 KB
        WebSocket attribute limit.  A ``checks_summary`` dict is always
        present so the card can show counts.
        """
        all_checks = c["checks"]
        total = len(all_checks)
        non_ok = [ch for ch in all_checks if ch["status"] != "ok"]
        ok_count = total - len(non_ok)

        # If there are more than 20 checks, only publish non-ok ones
        # to keep the attribute payload small.
        if total > 20:
            published_checks = non_ok
        else:
            published_checks = all_checks

        return {
            "name": c["name"],
            "status": c["status"],
            "last_check": c["last_check"],
            "checks": published_checks,
            "checks_summary": {
                "total": total,
                "ok": ok_count,
                "non_ok": len(non_ok),
            },
            "alert_history": c["alert_history"],
            "supports_repair": c.get("supports_repair", False),
            "repair_state": c.get("repair_state"),
            "is_dependency": is_dependency,
            "muted": checker_id in self._mutes,
            "muted_until": self._mutes.get(checker_id, {}).get("until"),
        }

    def _publish_status(self) -> None:
        """Publish aggregated health status to the HA sensor."""
        # Build resolved views (dependency overrides applied) for status computation
        resolved = {
            cid: self._resolve_dependencies(cid, c)
            for cid, c in self._checkers.items()
        }
        # Expected checkers still unregistered past the watchdog's grace
        # period are published as critical so they reach Alertmanager.
        resolved.update(self._unregistered_views())

        # Compute which checker_ids are referenced as dependencies by other checkers
        dep_ids: set = set()
        for c in self._checkers.values():
            for d in c.get("dependencies", []):
                dep_ids.add(d.get("checker_id"))

        # Compute overall status from resolved (published) views
        if not resolved:
            overall = "unknown"
        else:
            statuses = [c["status"] for c in resolved.values()]
            if "critical" in statuses:
                overall = "critical"
            elif "degraded" in statuses:
                overall = "degraded"
            elif "warning" in statuses:
                overall = "warning"
            elif all(s == "ok" for s in statuses):
                overall = "ok"
            else:
                overall = "unknown"

        attrs = {
            "checkers": {
                cid: self._checker_attrs(cid, c, is_dependency=(cid in dep_ids))
                for cid, c in resolved.items()
            },
            "last_updated": datetime.datetime.now().isoformat(timespec="seconds"),
            "friendly_name": "Health Check Status",
            "icon": "mdi:heart-pulse",
        }

        self.log(
            f"Publishing sensor: state={overall}, checkers={list(self._checkers.keys())}",
            level="DEBUG",
        )
        self.set_state(SENSOR_ENTITY_ID, state=overall, attributes=attrs)

        # Mirror the resolved snapshot into Prometheus base gauges.
        if self._metrics_enabled:
            firing = pending = None
            if self._alert_bridge is not None:
                firing = self._alert_severity_counts(self._alert_bridge.active_alerts)
                pending = self._alert_severity_counts(self._alert_bridge.pending_alerts)
            self._metrics.update_snapshot(
                resolved, set(self._mutes.keys()), firing, pending
            )

        # Mirror the resolved view into Alertmanager.  Copies are taken so
        # later state mutations can't race the async post.  Muted checkers
        # are passed with alerting disabled — the bridge resolves any firing
        # alert and drops pendings, so no Pushover pages while muted.
        if self._alert_bridge is not None:
            snapshot = {
                cid: {
                    "name": c["name"],
                    "status": c["status"],
                    "checks": [dict(ch) for ch in c["checks"]],
                    "alerting": (
                        {**(c.get("alerting") or {}), "enabled": False}
                        if cid in self._mutes
                        else c.get("alerting") or {}
                    ),
                    "repair_state": copy.deepcopy(c.get("repair_state")),
                }
                for cid, c in resolved.items()
            }
            self.create_task(self._alert_bridge.sync(snapshot))
