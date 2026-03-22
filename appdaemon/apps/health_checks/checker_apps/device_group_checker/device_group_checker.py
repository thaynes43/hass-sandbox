"""Device Group Health Checker — monitors multiple devices as a single checker.

Each device can have one or more entity checks and an optional IP ping.
All devices are reported as a single checker to avoid dashboard clutter.
No repair support — use RepairableDeviceGroupChecker for repair capability.

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

from shared.check_utils import ping_check

logger = logging.getLogger(__name__)


class DeviceGroupChecker(hass.Hass):
    """Config-driven health checker for a group of devices."""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        args = self.args or {}

        # Identity
        self._checker_id: str = args.get("checker_id", "device_group")
        self._checker_name: str = args.get("checker_name", self._checker_id)

        # Timing
        self._check_interval_s: int = int(args.get("check_interval_s", 180))

        # Device list — each device has name, optional ip, and list of entity checks
        raw_devices = args.get("devices", [])
        self._devices: List[Dict[str, Any]] = []
        for dev in raw_devices:
            entities = []
            for e in dev.get("entities", []):
                raw_healthy = e.get("healthy_state")
                if raw_healthy is None or raw_healthy == "":
                    healthy = ""  # empty means "not unavailable/unknown"
                elif isinstance(raw_healthy, bool):
                    # YAML coerces "on"/"off" to bool — reverse it
                    healthy = "on" if raw_healthy else "off"
                else:
                    healthy = str(raw_healthy)
                entities.append({
                    "entity_id": e.get("entity_id", ""),
                    "healthy_state": healthy,
                    "name": e.get("name", e.get("entity_id", "Entity")),
                })
            self._devices.append({
                "name": dev.get("name", "Device"),
                "ip": dev.get("ip", ""),
                "entities": entities,
            })

        self.log(
            f"DeviceGroupChecker initialising: id={self._checker_id}, "
            f"name={self._checker_name}, "
            f"devices={[d['name'] for d in self._devices]}",
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
            f"DeviceGroupChecker '{self._checker_name}' started", level="INFO"
        )

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def _register(self) -> None:
        check_names = self._build_check_names()
        payload = self._build_register_payload(check_names)
        self.fire_event(
            "health_check_command",
            command="register_checker",
            payload=json.dumps(payload),
        )
        self.log(
            f"Registered '{self._checker_name}' with checks: {check_names}",
            level="INFO",
        )

    def _build_check_names(self) -> List[str]:
        names = []
        for dev in self._devices:
            for entity_conf in dev["entities"]:
                names.append(f"{dev['name']} {entity_conf['name']}")
            if dev.get("ip"):
                names.append(f"{dev['name']} Ping")
        return names

    def _build_register_payload(self, check_names: List[str]) -> Dict[str, Any]:
        """Build the register_checker payload. Subclasses can extend."""
        return {
            "checker_id": self._checker_id,
            "checker_name": self._checker_name,
            "check_names": check_names,
        }

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
        for dev in self._devices:
            for entity_conf in dev["entities"]:
                check_name = f"{dev['name']} {entity_conf['name']}"
                results.append(
                    await self._check_entity_state(check_name, entity_conf)
                )
            if dev.get("ip"):
                results.append(await self._check_device_ping(dev))
        return results

    def _build_report_payload(self, results: List[Dict[str, str]]) -> Dict[str, Any]:
        """Build the report_status payload. Subclasses can extend to add repair_state."""
        return {
            "checker_id": self._checker_id,
            "results": results,
        }

    async def _check_device_ping(self, dev: dict) -> Dict[str, str]:
        name = f"{dev['name']} Ping"
        try:
            result = await ping_check(dev["ip"])
            return {
                "name": name,
                "status": result["status"],
                "detail": result["detail"],
            }
        except Exception as exc:
            self.log(
                f"Ping check failed for {dev['ip']}: {exc!r}", level="ERROR"
            )
            return {
                "name": name,
                "status": "critical",
                "detail": f"Error: {exc}",
            }

    async def _check_entity_state(
        self, check_name: str, entity_conf: dict
    ) -> Dict[str, str]:
        entity_id = entity_conf["entity_id"]
        healthy_state = entity_conf["healthy_state"]

        try:
            state = await self.get_state(entity_id)
            if state is None or str(state) in ("unavailable", "unknown"):
                return {
                    "name": check_name,
                    "status": "critical",
                    "detail": f"State: {state}",
                }
            if not healthy_state:
                # No specific state required — just not unavailable/unknown
                return {"name": check_name, "status": "ok", "detail": str(state)}
            if str(state) == healthy_state:
                return {"name": check_name, "status": "ok", "detail": str(state)}
            return {
                "name": check_name,
                "status": "critical",
                "detail": f"Expected '{healthy_state}', got '{state}'",
            }
        except Exception as exc:
            self.log(
                f"Entity check failed for {entity_id}: {exc!r}", level="ERROR"
            )
            return {
                "name": check_name,
                "status": "critical",
                "detail": f"Error: {exc}",
            }
