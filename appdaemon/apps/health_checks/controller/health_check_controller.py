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

Communication with checker apps is **event-only** (never ``get_app``),
allowing the controller to run in production Kubernetes while new checkers
are developed on a laptop.
"""

from __future__ import annotations

import copy
import datetime
import json
import logging
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

# AppDaemon only adds apps/ to sys.path — add appdaemon root for providers.
sys.path.append(str(Path(__file__).resolve().parents[3]))

import hassapi as hass

from providers.alertmanager import AlertmanagerClient
from providers.ha_provisioner import HAProvisioner

from health_checks.shared.alertmanager_bridge import AlertmanagerBridge

logger = logging.getLogger(__name__)

SENSOR_ENTITY_ID = "sensor.health_check_status"
HEARTBEAT_ENTITY_ID = "input_datetime.appdaemon_heartbeat"

_DEFAULT_RETENTION = timedelta(days=1, hours=12)  # 1 day 12 hours = 129600 s


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

        self._alert_bridge: Optional[AlertmanagerBridge] = None
        if self._alertmanager_url:
            self._alert_bridge = AlertmanagerBridge(
                AlertmanagerClient(self._alertmanager_url),
                log_fn=self.log,
                default_for_seconds=alert_for_seconds,
                for_overrides=alert_for_overrides,
            )

        self.log(
            f"HealthCheckController initialising: "
            f"heartbeat_interval={self._heartbeat_interval_s}s, "
            f"alert_history_max={self._alert_history_max}, "
            f"alert_retention={self._alert_retention}, "
            f"alertmanager={'enabled (' + self._alertmanager_url + ')' if self._alert_bridge else 'disabled'}, "
            f"alert_for_seconds={alert_for_seconds or '{}'}, "
            f"alert_for_overrides={alert_for_overrides or '{}'}",
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
        self._checkers[checker_id] = {
            "name": checker_name,
            "status": "unknown",
            "last_check": None,
            "checks": [
                {"name": n, "status": "unknown", "detail": "", "last_changed": None}
                for n in check_names
            ],
            "alert_history": self._checkers.get(checker_id, {}).get(
                "alert_history", []
            ),
            "supports_repair": bool(payload.get("supports_repair", False)),
            "repair_state": payload.get("repair_state"),
            "dependencies": payload.get("dependencies", []),
            "alerting": payload.get("alerting") or {},
        }

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
        self._publish_status()

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

    # ------------------------------------------------------------------
    # Dependency resolution
    # ------------------------------------------------------------------

    def _resolve_dependencies(self, checker_id: str, checker: dict) -> dict:
        """Return a copy of checker with dependency-affected checks overridden.

        When a dependency checker is unhealthy (critical/degraded) or missing
        entirely, the checks that depend on it are overridden to ``unknown``
        in the published view. The internal ``_checkers`` state is never
        modified here.

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
            # Treat missing dependency as unhealthy
            if dep_checker is None or dep_checker["status"] in ("critical", "degraded"):
                dep_name = dep_checker["name"] if dep_checker else dep_id
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

    def _checker_attrs(self, c: dict, is_dependency: bool = False) -> dict:
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
        }

    def _publish_status(self) -> None:
        """Publish aggregated health status to the HA sensor."""
        # Build resolved views (dependency overrides applied) for status computation
        resolved = {
            cid: self._resolve_dependencies(cid, c)
            for cid, c in self._checkers.items()
        }

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
                cid: self._checker_attrs(c, is_dependency=(cid in dep_ids))
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

        # Mirror the resolved view into Alertmanager.  Copies are taken so
        # later state mutations can't race the async post.
        if self._alert_bridge is not None:
            snapshot = {
                cid: {
                    "name": c["name"],
                    "status": c["status"],
                    "checks": [dict(ch) for ch in c["checks"]],
                    "alerting": c.get("alerting") or {},
                    "repair_state": copy.deepcopy(c.get("repair_state")),
                }
                for cid, c in resolved.items()
            }
            self.create_task(self._alert_bridge.sync(snapshot))
