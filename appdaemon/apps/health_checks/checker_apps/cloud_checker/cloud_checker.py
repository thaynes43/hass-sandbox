"""Cloud Connectivity Checker — verifies internet reachability via HTTP GET.

Performs an HTTP GET against one or more configured URLs to verify that the
host running AppDaemon has internet access.  Each URL becomes a named check.

Supports a ``force_fail`` mode for testing dependency override behaviour
without actually performing HTTP checks.

Communication with the controller is event-only (never ``get_app``).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

# Add health_checks package root so we can import shared utilities
_health_checks_root = str(Path(__file__).resolve().parents[2])
if _health_checks_root not in sys.path:
    sys.path.insert(0, _health_checks_root)

import hassapi as hass

from shared.check_utils import http_check


class CloudChecker(hass.Hass):
    """Checks internet connectivity by HTTP GET against configured URLs."""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        args = self.args or {}

        # Identity
        self._checker_id: str = args.get("checker_id", "cloud")
        self._checker_name: str = args.get("checker_name", "Cloud")

        # Timing
        self._check_interval_s: int = int(args.get("check_interval_s", 120))
        self._check_timeout_s: int = int(args.get("check_timeout_s", 5))

        # URL checks: list of {url: ..., name: ...}
        raw_checks = args.get("checks", [])
        self._checks: List[Dict[str, str]] = []
        for entry in raw_checks:
            url = entry.get("url", "")
            name = entry.get("name", url)
            if url:
                self._checks.append({"url": url, "name": name})

        # force_fail: skip real HTTP check and always report critical
        self._force_fail: bool = bool(args.get("force_fail", False))

        self.log(
            f"CloudChecker initialising: id={self._checker_id}, "
            f"name={self._checker_name}, "
            f"checks={[c['name'] for c in self._checks]}, "
            f"force_fail={self._force_fail}",
            level="INFO",
        )

        self.run_in(self._on_startup, 0)

    def _on_startup(self, kwargs: Any) -> None:
        """run_in callback — launches the async startup coroutine."""
        self.create_task(self._async_startup())

    async def _async_startup(self) -> None:
        """Register with controller, set up listeners and timer."""
        self._register()

        # Listen for controller events
        self.listen_event(
            self._on_controller_ready, "health_check_controller_ready"
        )
        self.listen_event(self._on_recheck, "health_check_recheck")

        # Schedule first check + periodic timer
        self.run_in(self._first_check, 5)

        self.log("CloudChecker started", level="INFO")

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def _register(self) -> None:
        """Fire registration event to the controller."""
        check_names = [c["name"] for c in self._checks]
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
        self.create_task(self._run_checks())

    def _on_recheck(self, event_name: str, data: dict, kwargs: Any) -> None:
        """Run check immediately on force-recheck request."""
        self.log(
            f"Force recheck requested for '{self._checker_name}'",
            level="INFO",
        )
        self.create_task(self._run_checks())

    def _first_check(self, kwargs: Any) -> None:
        """Run the first check, then start periodic timer."""
        self.create_task(self._run_checks())
        self.run_every(
            self._check_tick,
            f"now+{self._check_interval_s}",
            self._check_interval_s,
        )

    def _check_tick(self, kwargs: Any) -> None:
        """Periodic timer callback."""
        self.create_task(self._run_checks())

    # ------------------------------------------------------------------
    # Check execution
    # ------------------------------------------------------------------

    async def _run_checks(self) -> None:
        """Execute all configured URL checks and report results."""
        results: List[Dict[str, str]] = []

        for check in self._checks:
            name = check["name"]
            url = check["url"]

            if self._force_fail:
                results.append({
                    "name": name,
                    "status": "critical",
                    "detail": "cloud check disabled (testing)",
                })
            else:
                try:
                    result = await http_check(url, timeout_s=self._check_timeout_s)
                    results.append({
                        "name": name,
                        "status": result["status"],
                        "detail": result["detail"],
                    })
                except Exception as exc:
                    self.log(
                        f"HTTP check failed for {url}: {exc!r}", level="ERROR"
                    )
                    results.append({
                        "name": name,
                        "status": "critical",
                        "detail": f"Error: {exc}",
                    })

        self.fire_event(
            "health_check_command",
            command="report_status",
            payload=json.dumps({
                "checker_id": self._checker_id,
                "results": results,
            }),
        )

        status_parts = [f"{r['name']}={r['status']}" for r in results]
        self.log(
            f"Check cycle complete for '{self._checker_name}': "
            f"{', '.join(status_parts)}",
            level="INFO" if any(r["status"] != "ok" for r in results) else "DEBUG",
        )
