"""Basic Device Health Checker — generic, config-driven device monitor.

A simple checker for devices that need entity state monitoring and an
optional IP ping.  Each instance monitors one device with configurable
checks:

1. **Entity checks** — verify one or more HA entities match expected states
2. **IP ping** — ICMP ping the device (optional)

No repair support — this is a lightweight monitor for devices that
cannot be auto-repaired from AppDaemon.

Communication with the controller is event-only (never ``get_app``).
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Add health_checks package root so we can import shared utilities
_health_checks_root = str(Path(__file__).resolve().parents[2])
if _health_checks_root not in sys.path:
    sys.path.insert(0, _health_checks_root)

import hassapi as hass

from shared.check_utils import apply_cross_check, ping_check

logger = logging.getLogger(__name__)


class BasicDeviceChecker(hass.Hass):
    """Config-driven health checker for a single device."""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        args = self.args or {}

        # Identity
        self._checker_id: str = args.get("checker_id", "device")
        self._checker_name: str = args.get("checker_name", self._checker_id)

        # IP ping (optional)
        self._ping_host: str = args.get("ping_host", "")
        self._ping_check_name: str = args.get("ping_check_name", "Ping")

        # Entity checks (list of dicts with entity_id, healthy_state, name)
        # healthy_state can be:
        #   - a specific value (e.g. "active", "ok") — exact match
        #   - omitted or empty — any state except unavailable/unknown is ok
        raw_entities = args.get("entities", [])
        self._entities: List[Dict[str, str]] = []
        for e in raw_entities:
            raw_healthy = e.get("healthy_state")
            if raw_healthy is None or raw_healthy == "":
                healthy = ""  # empty means "not unavailable/unknown"
            elif isinstance(raw_healthy, bool):
                # YAML coerces "on"/"off" to bool — reverse it
                healthy = "on" if raw_healthy else "off"
            else:
                healthy = str(raw_healthy)
            self._entities.append({
                "entity_id": e.get("entity_id", ""),
                "healthy_state": healthy,
                "name": e.get("name", e.get("entity_id", "Entity")),
            })

        # Timing
        self._check_interval_s: int = int(args.get("check_interval_s", 180))

        # Repair events pending delivery to the controller — drained into the
        # next report_status payload by _build_report_payload (edge events,
        # delivered once). Only populated by repair-capable subclasses.
        self._pending_repair_events: List[Dict[str, Any]] = []

        self.log(
            f"BasicDeviceChecker initialising: id={self._checker_id}, "
            f"name={self._checker_name}, ping={self._ping_host}, "
            f"entities={len(self._entities)}",
            level="INFO",
        )

        self.run_in(self._on_startup, 0)

    def _on_startup(self, kwargs: Any) -> None:
        self.create_task(self._async_startup())

    async def _async_startup(self) -> None:
        self._register()

        self.listen_event(
            self._on_controller_ready, "health_check_controller_ready"
        )
        self.listen_event(self._on_recheck, "health_check_recheck")

        self.run_in(self._first_check, 5)
        self.log(
            f"BasicDeviceChecker '{self._checker_name}' started", level="INFO"
        )

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def _register(self) -> None:
        check_names = self._build_check_names()
        self.fire_event(
            "health_check_command",
            command="register_checker",
            payload=json.dumps({
                "checker_id": self._checker_id,
                "checker_name": self._checker_name,
                "check_names": check_names,
            }),
        )
        self.log(
            f"Registered '{self._checker_name}' with checks: {check_names}",
            level="INFO",
        )

    def _build_check_names(self) -> List[str]:
        names = []
        if self._ping_host:
            names.append(self._ping_check_name)
        for e in self._entities:
            names.append(e["name"])
        return names

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_controller_ready(
        self, event_name: str, data: dict, kwargs: Any
    ) -> None:
        self.log(
            f"Controller ready — re-registering '{self._checker_name}'",
            level="INFO",
        )
        self._register()

    def _on_recheck(self, event_name: str, data: dict, kwargs: Any) -> None:
        self.log(
            f"Force recheck requested for '{self._checker_name}'",
            level="INFO",
        )
        self.create_task(self._run_checks())

    def _first_check(self, kwargs: Any) -> None:
        self.create_task(self._run_checks())
        self.run_every(
            self._check_tick,
            f"now+{self._check_interval_s}",
            self._check_interval_s,
        )

    def _check_tick(self, kwargs: Any) -> None:
        self.create_task(self._run_checks())

    # ------------------------------------------------------------------
    # Check execution
    # ------------------------------------------------------------------

    async def _run_checks(self) -> None:
        results = await self._run_checks_only()
        apply_cross_check(results)

        payload = self._build_report_payload(results)
        self.fire_event(
            "health_check_command",
            command="report_status",
            payload=json.dumps(payload),
        )

        status_parts = [f"{r['name']}={r['status']}" for r in results]
        self.log(
            f"Check cycle complete for '{self._checker_name}': "
            f"{', '.join(status_parts)}",
            level="INFO",
        )

    async def _run_checks_only(self) -> List[Dict[str, str]]:
        """Run all checks and return results without reporting."""
        results: List[Dict[str, str]] = []
        if self._ping_host:
            results.append(await self._check_ping())
        for entity_conf in self._entities:
            results.append(await self._check_entity_state(entity_conf))
        return results

    def _build_report_payload(self, results: List[Dict[str, str]]) -> Dict[str, Any]:
        """Build the report_status payload. Subclasses can extend to add repair_state.

        Drains any pending repair_events buffered by a repair-capable
        subclass so they ride along on the very next report_status call —
        these are one-shot edge events and must never be sent twice.
        """
        payload: Dict[str, Any] = {
            "checker_id": self._checker_id,
            "results": results,
        }
        if self._pending_repair_events:
            payload["repair_events"] = self._pending_repair_events
            self._pending_repair_events = []
        return payload

    async def _check_ping(self) -> Dict[str, str]:
        try:
            result = await ping_check(self._ping_host)
            return {
                "name": self._ping_check_name,
                "status": result["status"],
                "detail": result["detail"],
            }
        except Exception as exc:
            self.log(f"Ping check failed: {exc!r}", level="ERROR")
            return {
                "name": self._ping_check_name,
                "status": "critical",
                "detail": f"Error: {exc}",
            }

    async def _check_entity_state(self, entity_conf: dict) -> Dict[str, str]:
        entity_id = entity_conf["entity_id"]
        healthy_state = entity_conf["healthy_state"]
        name = entity_conf["name"]

        try:
            state = await self.get_state(entity_id)
            if state is None or str(state) in ("unavailable", "unknown"):
                return {
                    "name": name,
                    "status": "critical",
                    "detail": f"State: {state}",
                }
            if not healthy_state:
                # No specific state required — just not unavailable/unknown
                return {"name": name, "status": "ok", "detail": str(state)}
            if str(state) == healthy_state:
                return {"name": name, "status": "ok", "detail": str(state)}
            return {
                "name": name,
                "status": "critical",
                "detail": f"Expected '{healthy_state}', got '{state}'",
            }
        except Exception as exc:
            self.log(
                f"Entity check failed for {entity_id}: {exc!r}", level="ERROR"
            )
            return {
                "name": name,
                "status": "critical",
                "detail": f"Error: {exc}",
            }
