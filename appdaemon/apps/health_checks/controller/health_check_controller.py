"""Health Check Controller — aggregates status from decoupled checker apps.

Provisions HA entities on startup, maintains a heartbeat so the frontend
can detect when AppDaemon is offline, listens for checker registration and
status reports via the HA event bus, and publishes aggregated health state
to a virtual sensor for the custom Lovelace cards.

Communication with checker apps is **event-only** (never ``get_app``),
allowing the controller to run in production Kubernetes while new checkers
are developed on a laptop.
"""

from __future__ import annotations

import datetime
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# AppDaemon only adds apps/ to sys.path — add appdaemon root for providers.
sys.path.append(str(Path(__file__).resolve().parents[3]))

import hassapi as hass

from providers.ha_provisioner import HAProvisioner

logger = logging.getLogger(__name__)

SENSOR_ENTITY_ID = "sensor.health_check_status"
HEARTBEAT_ENTITY_ID = "input_datetime.appdaemon_heartbeat"


class HealthCheckController(hass.Hass):
    """AppDaemon app that aggregates health-check reports from decoupled checkers."""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        args = self.args or {}

        self._heartbeat_interval_s: int = int(args.get("heartbeat_interval_s", 60))
        self._alert_history_max: int = int(args.get("alert_history_max", 20))

        # State: registered checkers
        self._checkers: Dict[str, Dict[str, Any]] = {}

        self.log(
            f"HealthCheckController initialising: "
            f"heartbeat_interval={self._heartbeat_interval_s}s, "
            f"alert_history_max={self._alert_history_max}",
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

        # Publish initial state
        self._publish_status()

        # Signal to checkers that we are ready
        self.fire_event("health_check_controller_ready", {})

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
        now_iso = datetime.datetime.now().isoformat(timespec="seconds")
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
                    checker["alert_history"].insert(0, {
                        "timestamp": now_iso,
                        "check": name,
                        "from_status": old_status,
                        "to_status": status,
                        "detail": detail,
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
            elif status == "degraded" and worst_status != "critical":
                worst_status = "degraded"

        # Trim alert history
        checker["alert_history"] = checker["alert_history"][
            : self._alert_history_max
        ]

        checker["checks"] = new_checks
        checker["status"] = worst_status
        checker["last_check"] = now_iso

        self.log(
            f"Status report from '{checker['name']}': {worst_status} "
            f"({len(results)} checks)",
            level="INFO" if worst_status != "ok" else "DEBUG",
        )
        self._publish_status()

    def _handle_force_recheck(self, payload: dict) -> None:
        """Forward a force-recheck request to all checkers."""
        self.log("Broadcasting force recheck to all checkers", level="INFO")
        self.fire_event("health_check_recheck", {})

    # ------------------------------------------------------------------
    # Sensor publication
    # ------------------------------------------------------------------

    def _publish_status(self) -> None:
        """Publish aggregated health status to the HA sensor."""
        # Compute overall status
        if not self._checkers:
            overall = "unknown"
        else:
            statuses = [c["status"] for c in self._checkers.values()]
            if "critical" in statuses:
                overall = "critical"
            elif "degraded" in statuses:
                overall = "degraded"
            elif all(s == "ok" for s in statuses):
                overall = "ok"
            else:
                overall = "unknown"

        attrs = {
            "checkers": {
                cid: {
                    "name": c["name"],
                    "status": c["status"],
                    "last_check": c["last_check"],
                    "checks": c["checks"],
                    "alert_history": c["alert_history"],
                }
                for cid, c in self._checkers.items()
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
