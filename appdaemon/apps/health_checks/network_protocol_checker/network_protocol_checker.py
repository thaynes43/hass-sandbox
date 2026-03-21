"""Network Protocol Health Checker — generic, config-driven checker app.

A single class that can be instantiated multiple times (Zigbee, Z-Wave,
Thread, etc.) with different configuration.  Each instance performs three
checks on a configurable interval:

1. **Entity state** — verify an HA entity matches an expected healthy state
2. **Radio ping** — ICMP ping a PoE radio/coordinator hostname
3. **Web UI** — HTTP GET a management web UI URL

Results are reported to the Health Check Controller via HA events.
This app never calls ``get_app()`` — communication is event-only so
the controller can run in production while new checkers are developed
on a laptop.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List

# Add health_checks package root so we can import shared utilities
_health_checks_root = str(Path(__file__).resolve().parents[1])
if _health_checks_root not in sys.path:
    sys.path.insert(0, _health_checks_root)

import hassapi as hass

from shared.check_utils import http_check, ping_check

logger = logging.getLogger(__name__)


class NetworkProtocolChecker(hass.Hass):
    """Config-driven health checker for network protocol stacks."""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        args = self.args or {}

        # Identity
        self._checker_id: str = args.get("checker_id", "unknown")
        self._checker_name: str = args.get("checker_name", self._checker_id)

        # Entity check
        self._entity_id: str = args.get("entity_id", "")
        # YAML may coerce "on"/"off" to bool True/False — reverse it
        raw_state = args.get("entity_healthy_state", "")
        if isinstance(raw_state, bool):
            self._entity_healthy_state = "on" if raw_state else "off"
        else:
            self._entity_healthy_state = str(raw_state)
        self._entity_check_name: str = args.get("entity_check_name", "Entity State")

        # Radio ping check
        self._radio_host: str = args.get("radio_host", "")
        self._radio_check_name: str = args.get("radio_check_name", "Radio Ping")

        # Web UI check
        self._web_ui_url: str = args.get("web_ui_url", "")
        self._web_ui_check_name: str = args.get("web_ui_check_name", "Web UI")

        # Timing
        self._check_interval_s: int = int(args.get("check_interval_s", 180))

        self.log(
            f"NetworkProtocolChecker initialising: id={self._checker_id}, "
            f"name={self._checker_name}, entity={self._entity_id}, "
            f"radio={self._radio_host}, web_ui={self._web_ui_url}, "
            f"interval={self._check_interval_s}s",
            level="INFO",
        )

        self.run_in(self._on_startup, 0)

    def _on_startup(self, kwargs: Any) -> None:
        """run_in callback — launches the async startup coroutine."""
        self.create_task(self._async_startup())

    async def _async_startup(self) -> None:
        """Register with controller, set up listeners and timer."""
        # Register with controller
        self._register()

        # Listen for controller ready (re-register if controller restarts)
        self.listen_event(self._on_controller_ready, "health_check_controller_ready")

        # Listen for force-recheck requests
        self.listen_event(self._on_recheck, "health_check_recheck")

        # Run first check after a short delay, then start periodic timer
        self.run_in(self._first_check, 5)

        self.log(
            f"NetworkProtocolChecker '{self._checker_name}' started",
            level="INFO",
        )

    def _first_check(self, kwargs: Any) -> None:
        """Run the first check cycle immediately, then start periodic timer."""
        self.create_task(self._run_checks())
        self.run_every(
            self._check_tick,
            f"now+{self._check_interval_s}",
            self._check_interval_s,
        )

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def _register(self) -> None:
        """Fire registration event to the controller."""
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
        """Build list of check names based on configured checks."""
        names = []
        if self._entity_id:
            names.append(self._entity_check_name)
        if self._radio_host:
            names.append(self._radio_check_name)
        if self._web_ui_url:
            names.append(self._web_ui_check_name)
        return names

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_controller_ready(
        self, event_name: str, data: dict, kwargs: Any
    ) -> None:
        """Re-register when controller (re)starts."""
        self.log(
            f"Controller ready — re-registering '{self._checker_name}'",
            level="INFO",
        )
        self._register()

    def _on_recheck(self, event_name: str, data: dict, kwargs: Any) -> None:
        """Run checks immediately on force-recheck request."""
        self.log(
            f"Force recheck requested for '{self._checker_name}'",
            level="INFO",
        )
        self.create_task(self._run_checks())

    def _check_tick(self, kwargs: Any) -> None:
        """Periodic timer callback — launches async check cycle."""
        self.create_task(self._run_checks())

    # ------------------------------------------------------------------
    # Check execution
    # ------------------------------------------------------------------

    async def _run_checks(self) -> None:
        """Execute all configured checks and report results."""
        results: List[Dict[str, str]] = []

        # 1. Entity state check
        if self._entity_id:
            result = await self._check_entity_state()
            results.append(result)

        # 2. Radio ping check
        if self._radio_host:
            result = await self._check_radio_ping()
            results.append(result)

        # 3. Web UI check
        if self._web_ui_url:
            result = await self._check_web_ui()
            results.append(result)

        # Report to controller
        self.fire_event(
            "health_check_command",
            command="report_status",
            payload=json.dumps({
                "checker_id": self._checker_id,
                "results": results,
            }),
        )

        status_parts = [f"{r['name']}={r['status']}" for r in results]
        any_bad = any(r["status"] != "ok" for r in results)
        self.log(
            f"Check cycle complete for '{self._checker_name}': "
            f"{', '.join(status_parts)}",
            level="INFO",
        )

    async def _check_entity_state(self) -> Dict[str, str]:
        """Check whether the monitored HA entity is in its healthy state."""
        try:
            state = await self.get_state(self._entity_id)
            if state is None:
                return {
                    "name": self._entity_check_name,
                    "status": "critical",
                    "detail": "Entity not found",
                }
            if str(state) == self._entity_healthy_state:
                return {
                    "name": self._entity_check_name,
                    "status": "ok",
                    "detail": str(state),
                }
            return {
                "name": self._entity_check_name,
                "status": "critical",
                "detail": f"Expected '{self._entity_healthy_state}', got '{state}'",
            }
        except Exception as exc:
            self.log(
                f"Entity state check failed for {self._entity_id}: {exc!r}",
                level="ERROR",
            )
            return {
                "name": self._entity_check_name,
                "status": "critical",
                "detail": f"Error: {exc}",
            }

    async def _check_radio_ping(self) -> Dict[str, str]:
        """Ping the radio/coordinator host."""
        try:
            result = await ping_check(self._radio_host)
            return {
                "name": self._radio_check_name,
                "status": result["status"],
                "detail": result["detail"],
            }
        except Exception as exc:
            self.log(
                f"Radio ping check failed for {self._radio_host}: {exc!r}",
                level="ERROR",
            )
            return {
                "name": self._radio_check_name,
                "status": "critical",
                "detail": f"Error: {exc}",
            }

    async def _check_web_ui(self) -> Dict[str, str]:
        """HTTP GET the web UI URL."""
        try:
            result = await http_check(self._web_ui_url)
            return {
                "name": self._web_ui_check_name,
                "status": result["status"],
                "detail": result["detail"],
            }
        except Exception as exc:
            self.log(
                f"Web UI check failed for {self._web_ui_url}: {exc!r}",
                level="ERROR",
            )
            return {
                "name": self._web_ui_check_name,
                "status": "critical",
                "detail": f"Error: {exc}",
            }
