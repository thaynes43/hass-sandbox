"""Temperature/Humidity Health Checker — monitors environmental sensors with warning/critical thresholds."""

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


class TempHumidityChecker(hass.Hass):
    """Monitors temperature and/or humidity sensors with configurable thresholds."""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        args = self.args or {}
        self._checker_id: str = args.get("checker_id", "temp_humidity")
        self._checker_name: str = args.get("checker_name", "Temp/Humidity")
        self._check_interval_s: int = int(args.get("check_interval_s", 120))

        # Default thresholds
        self._defaults: Dict[str, float] = {
            "temp_low_warning": float(args.get("temp_low_warning", 60)),
            "temp_high_warning": float(args.get("temp_high_warning", 70)),
            "temp_low_critical": float(args.get("temp_low_critical", 58)),
            "temp_high_critical": float(args.get("temp_high_critical", 72)),
            "humidity_low_warning": float(args.get("humidity_low_warning", 62)),
            "humidity_high_warning": float(args.get("humidity_high_warning", 68)),
            "humidity_low_critical": float(args.get("humidity_low_critical", 60)),
            "humidity_high_critical": float(args.get("humidity_high_critical", 70)),
        }

        # Parse sensor configs
        self._sensors: List[Dict[str, Any]] = []
        for sensor_cfg in args.get("sensors", []):
            self._sensors.append({
                "entity_id": sensor_cfg["entity_id"],
                "name": sensor_cfg.get("name", sensor_cfg["entity_id"]),
                "type": sensor_cfg.get("type", "humidity"),
                "dependency": sensor_cfg.get("dependency"),
                # Per-sensor threshold overrides (fall back to defaults)
                "temp_low_warning": float(sensor_cfg.get("temp_low_warning", self._defaults["temp_low_warning"])),
                "temp_high_warning": float(sensor_cfg.get("temp_high_warning", self._defaults["temp_high_warning"])),
                "temp_low_critical": float(sensor_cfg.get("temp_low_critical", self._defaults["temp_low_critical"])),
                "temp_high_critical": float(sensor_cfg.get("temp_high_critical", self._defaults["temp_high_critical"])),
                "humidity_low_warning": float(sensor_cfg.get("humidity_low_warning", self._defaults["humidity_low_warning"])),
                "humidity_high_warning": float(sensor_cfg.get("humidity_high_warning", self._defaults["humidity_high_warning"])),
                "humidity_low_critical": float(sensor_cfg.get("humidity_low_critical", self._defaults["humidity_low_critical"])),
                "humidity_high_critical": float(sensor_cfg.get("humidity_high_critical", self._defaults["humidity_high_critical"])),
            })

        self.log(
            f"TempHumidityChecker initializing: id={self._checker_id}, "
            f"sensors={len(self._sensors)}, interval={self._check_interval_s}s",
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

        # Log sensor configuration
        self.log(f"Monitoring {len(self._sensors)} sensors:", level="INFO")
        for s in self._sensors:
            dep_str = f" (depends on {s['dependency']})" if s.get("dependency") else ""
            self.log(
                f"  - {s['entity_id']} ({s['name']}, type={s['type']}){dep_str}",
                level="INFO",
            )

        self.log(
            f"TempHumidityChecker '{self._checker_name}' started",
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
        check_names = [s["name"] for s in self._sensors]

        # Build dependencies grouped by dependency checker_id
        dep_map: Dict[str, List[str]] = {}
        for s in self._sensors:
            dep = s.get("dependency")
            if dep:
                dep_map.setdefault(dep, []).append(s["name"])

        dependencies = [
            {"checker_id": dep_id, "affects_checks": checks}
            for dep_id, checks in dep_map.items()
        ]

        payload: Dict[str, Any] = {
            "checker_id": self._checker_id,
            "checker_name": self._checker_name,
            "check_names": check_names,
        }
        if dependencies:
            payload["dependencies"] = dependencies

        self.fire_event(
            "health_check_command",
            command="register_checker",
            payload=json.dumps(payload),
        )
        self.log(
            f"Registered '{self._checker_name}' with {len(check_names)} checks, "
            f"dependencies: {list(dep_map.keys())}",
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

        for sensor in self._sensors:
            status, detail = self._evaluate_sensor(sensor)
            results.append({
                "name": sensor["name"],
                "status": status,
                "detail": detail,
            })

        self.fire_event(
            "health_check_command",
            command="report_status",
            payload=json.dumps({
                "checker_id": self._checker_id,
                "results": results,
            }),
        )

        ok_count = sum(1 for r in results if r["status"] == "ok")
        warn_count = sum(1 for r in results if r["status"] == "warning")
        crit_count = sum(1 for r in results if r["status"] == "critical")
        self.log(
            f"Check complete: {ok_count} ok, {warn_count} warning, {crit_count} critical",
            level="INFO" if crit_count == 0 and warn_count == 0 else "WARNING",
        )

    def _evaluate_sensor(self, sensor: Dict[str, Any]) -> tuple:
        """Evaluate a single sensor. Returns (status, detail)."""
        entity_id = sensor["entity_id"]

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

        sensor_type = sensor["type"]

        if sensor_type == "humidity":
            return self._check_range(
                value, "humidity",
                sensor["humidity_low_warning"], sensor["humidity_high_warning"],
                sensor["humidity_low_critical"], sensor["humidity_high_critical"],
            )
        elif sensor_type == "temperature":
            return self._check_range(
                value, "temperature",
                sensor["temp_low_warning"], sensor["temp_high_warning"],
                sensor["temp_low_critical"], sensor["temp_high_critical"],
            )
        else:
            # "both" — check both and return worst status
            temp_status, temp_detail = self._check_range(
                value, "temperature",
                sensor["temp_low_warning"], sensor["temp_high_warning"],
                sensor["temp_low_critical"], sensor["temp_high_critical"],
            )
            hum_status, hum_detail = self._check_range(
                value, "humidity",
                sensor["humidity_low_warning"], sensor["humidity_high_warning"],
                sensor["humidity_low_critical"], sensor["humidity_high_critical"],
            )
            severity = {"ok": 0, "warning": 1, "critical": 2}
            if severity.get(temp_status, 0) >= severity.get(hum_status, 0):
                return (temp_status, temp_detail)
            return (hum_status, hum_detail)

    def _check_range(
        self, value: float, metric: str,
        low_warn: float, high_warn: float,
        low_crit: float, high_crit: float,
    ) -> tuple:
        """Check a value against warning and critical ranges. Returns (status, detail)."""
        if value < low_crit:
            return ("critical", f"{metric}: {value:.1f} (below critical {low_crit})")
        elif value > high_crit:
            return ("critical", f"{metric}: {value:.1f} (above critical {high_crit})")
        elif value < low_warn:
            return ("warning", f"{metric}: {value:.1f} (below warning {low_warn})")
        elif value > high_warn:
            return ("warning", f"{metric}: {value:.1f} (above warning {high_warn})")
        else:
            return ("ok", f"{metric}: {value:.1f}")
