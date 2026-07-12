"""UPS Health Checker — monitors UPS battery charge and load utilization.

Each configured UPS device produces two checks: one for battery charge (low = bad)
and one for load utilization (high = bad).  Thresholds are configurable per device.

Communication with the controller is event-only (never ``get_app``).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

# Add health_checks package root so we can import shared utilities if needed
_health_checks_root = str(Path(__file__).resolve().parents[2])
if _health_checks_root not in sys.path:
    sys.path.insert(0, _health_checks_root)

import hassapi as hass


class UpsChecker(hass.Hass):
    """Monitors UPS battery charge and load utilization with configurable thresholds."""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        args = self.args or {}

        # Identity
        self._checker_id: str = args.get("checker_id", "ups")
        self._checker_name: str = args.get("checker_name", "UPS")

        # Timing
        self._check_interval_s: int = int(args.get("check_interval_s", 120))

        # UPS device configs
        self._devices: List[Dict[str, Any]] = []
        for dev_cfg in args.get("ups_devices", []):
            self._devices.append({
                "name": dev_cfg["name"],
                "battery_entity": dev_cfg["battery_entity"],
                "load_entity": dev_cfg["load_entity"],
                "battery_warning": float(dev_cfg.get("battery_warning", 90)),
                "battery_critical": float(dev_cfg.get("battery_critical", 50)),
                "load_warning": float(dev_cfg.get("load_warning", 90)),
                "load_critical": float(dev_cfg.get("load_critical", 100)),
            })

        # Last successfully-parsed numeric reading per entity_id, used to
        # build the metrics payload in _run_checks without an extra
        # get_state() call (the check methods already read + parse it).
        self._last_metric_value: Dict[str, float] = {}

        self.log(
            f"UpsChecker initializing: id={self._checker_id}, "
            f"devices={len(self._devices)}, interval={self._check_interval_s}s",
            level="INFO",
        )

        self.run_in(self._on_startup, 0)

    def _on_startup(self, kwargs: Any) -> None:
        """run_in callback — launches the async startup coroutine."""
        self.create_task(self._async_startup())

    async def _async_startup(self) -> None:
        """Register with controller, set up listeners and timer."""
        self._register()

        # Listen for controller ready (re-register if controller restarts)
        self.listen_event(self._on_controller_ready, "health_check_controller_ready")

        # Listen for force-recheck requests
        self.listen_event(self._on_recheck, "health_check_recheck")

        # Run first check after a short delay, then start periodic timer
        self.run_in(self._first_check, 5)

        self.log(
            f"UpsChecker '{self._checker_name}' started, monitoring {len(self._devices)} devices",
            level="INFO",
        )

    def _first_check(self, kwargs: Any) -> None:
        """Run the first check cycle immediately, then start periodic timer."""
        self._run_checks()
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
        check_names = []
        for dev in self._devices:
            check_names.append(f"{dev['name']} Battery")
            check_names.append(f"{dev['name']} Load")

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
        self._run_checks()

    def _on_recheck(self, event_name: str, data: dict, kwargs: Any) -> None:
        """Run checks immediately on force-recheck request."""
        self.log(
            f"Force recheck requested for '{self._checker_name}'",
            level="DEBUG",
        )
        self._run_checks()

    def _check_tick(self, kwargs: Any) -> None:
        """Periodic timer callback."""
        self._run_checks()

    # ------------------------------------------------------------------
    # Check execution
    # ------------------------------------------------------------------

    def _run_checks(self) -> None:
        """Execute all configured checks and report results."""
        results: List[Dict[str, str]] = []
        metrics: List[Dict[str, Any]] = []

        for device in self._devices:
            batt_status, batt_detail = self._check_battery(device)
            results.append({
                "name": f"{device['name']} Battery",
                "status": batt_status,
                "detail": batt_detail,
            })
            batt_value = self._last_metric_value.get(device["battery_entity"])
            if batt_value is not None:
                metrics.append({
                    "name": "battery_percent",
                    "value": batt_value,
                    "type": "gauge",
                    "labels": {"device": device["name"]},
                })

            load_status, load_detail = self._check_load(device)
            results.append({
                "name": f"{device['name']} Load",
                "status": load_status,
                "detail": load_detail,
            })
            load_value = self._last_metric_value.get(device["load_entity"])
            if load_value is not None:
                metrics.append({
                    "name": "load_percent",
                    "value": load_value,
                    "type": "gauge",
                    "labels": {"device": device["name"]},
                })

        payload: Dict[str, Any] = {
            "checker_id": self._checker_id,
            "results": results,
        }
        if metrics:
            payload["metrics"] = metrics

        self.fire_event(
            "health_check_command",
            command="report_status",
            payload=json.dumps(payload),
        )

        ok_count = sum(1 for r in results if r["status"] == "ok")
        warn_count = sum(1 for r in results if r["status"] == "warning")
        crit_count = sum(1 for r in results if r["status"] == "critical")
        self.log(
            f"Check complete: {ok_count} ok, {warn_count} warning, {crit_count} critical",
            level="INFO" if crit_count == 0 and warn_count == 0 else "WARNING",
        )

    def _check_battery(self, device: Dict[str, Any]) -> tuple:
        """Check battery charge level (low = bad). Returns (status, detail)."""
        entity_id = device["battery_entity"]
        # Clear any prior reading so a currently-unavailable/non-numeric
        # state never leaves a stale value behind for the metrics payload.
        self._last_metric_value.pop(entity_id, None)

        try:
            state = self.get_state(entity_id)
        except Exception as exc:
            return ("critical", f"error reading state: {exc}")

        if state is None or state in ("unavailable", "unknown"):
            return ("critical", f"state: {state or 'not found'}")

        try:
            value = float(state)
        except (ValueError, TypeError):
            return ("critical", f"non-numeric state: {state}")

        self._last_metric_value[entity_id] = value

        warn = device.get("battery_warning", 90)
        crit = device.get("battery_critical", 50)

        if value < crit:
            return ("critical", f"{value:.0f}% (critical <{crit:.0f}%)")
        if value < warn:
            return ("warning", f"{value:.0f}% (warning <{warn:.0f}%)")
        return ("ok", f"{value:.0f}%")

    def _check_load(self, device: Dict[str, Any]) -> tuple:
        """Check load utilization (high = bad). Returns (status, detail)."""
        entity_id = device["load_entity"]
        # Clear any prior reading so a currently-unavailable/non-numeric
        # state never leaves a stale value behind for the metrics payload.
        self._last_metric_value.pop(entity_id, None)

        try:
            state = self.get_state(entity_id)
        except Exception as exc:
            return ("critical", f"error reading state: {exc}")

        if state is None or state in ("unavailable", "unknown"):
            return ("critical", f"state: {state or 'not found'}")

        try:
            value = float(state)
        except (ValueError, TypeError):
            return ("critical", f"non-numeric state: {state}")

        self._last_metric_value[entity_id] = value

        warn = device.get("load_warning", 90)
        crit = device.get("load_critical", 100)

        if value >= crit:
            return ("critical", f"{value:.0f}% (critical \u2265{crit:.0f}%)")
        if value >= warn:
            return ("warning", f"{value:.0f}% (warning \u2265{warn:.0f}%)")
        return ("ok", f"{value:.0f}%")
