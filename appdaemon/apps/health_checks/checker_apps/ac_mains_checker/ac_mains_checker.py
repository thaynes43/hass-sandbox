"""AC Mains Health Checker — auto-discovers ``ac_mains_disconnected`` sensors.

Mains-powered Z-Wave devices with battery backup (e.g. Zooz ZAC38 range
extenders) expose a ``binary_sensor.<device>_ac_mains_disconnected`` via the
Notification command class.  When wall power drops the device silently falls
back to its internal battery and keeps working — so nothing appears broken
until the battery runs flat days later and the node dies outright.

This checker watches that sensor directly so the *power loss* pages, not the
downstream node death.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

# Add health_checks package root so we can import shared utilities if needed
_health_checks_root = str(Path(__file__).resolve().parents[2])
if _health_checks_root not in sys.path:
    sys.path.insert(0, _health_checks_root)

import hassapi as hass

# Suffixes to strip from friendly_name for cleaner display names (case-insensitive)
_AC_MAINS_SUFFIXES = [
    " ac mains disconnected",
    " ac mains disconnect",
]

# Statuses a caller may configure for the "mains lost" condition.
_VALID_DISCONNECTED_STATUSES = ("critical", "degraded", "warning")


class AcMainsChecker(hass.Hass):
    """Monitors ``ac_mains_disconnected`` binary sensors for loss of wall power.

    Discovers entities automatically using ``entity_patterns`` (include/exclude
    regexes matched against the entity_id via ``re.search``).

    Unlike the battery sensors, these entities carry no ``device_class``, so
    discovery is pattern-driven only — which makes the exclude list load
    bearing.  Battery-only Z-Wave devices (Q Sensors, battery motion sensors)
    also expose this sensor and report ``on`` permanently, because they have
    no AC mains to lose.  Those must be excluded or they alert forever.
    """

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        args = self.args or {}
        self._checker_id: str = args.get("checker_id", "ac_mains")
        self._checker_name: str = args.get("checker_name", "AC Mains")
        self._check_interval_s: int = int(args.get("check_interval_s", 300))

        # Status reported when a device reports mains disconnected. Power loss
        # is time-sensitive (see the UPS checker precedent), so this defaults
        # to "critical"; configurable for installs that prefer UI-only.
        configured_status = str(args.get("disconnected_status", "critical")).lower()
        if configured_status not in _VALID_DISCONNECTED_STATUSES:
            self.log(
                f"Invalid disconnected_status={configured_status!r}, "
                f"falling back to 'critical' "
                f"(valid: {', '.join(_VALID_DISCONNECTED_STATUSES)})",
                level="WARNING",
            )
            configured_status = "critical"
        self._disconnected_status: str = configured_status

        # Instance-level dependencies
        self._health_dependencies: List[dict] = list(
            args.get("health_dependencies", [])
        )

        # Entity discovery patterns
        self._include_patterns: List[re.Pattern] = []
        self._exclude_patterns: List[re.Pattern] = []
        for pattern_cfg in args.get("entity_patterns", []):
            if "include" in pattern_cfg:
                self._include_patterns.append(re.compile(pattern_cfg["include"]))
            if "exclude" in pattern_cfg:
                self._exclude_patterns.append(re.compile(pattern_cfg["exclude"]))

        # Discovered entities: entity_id -> display_name
        self._entities: Dict[str, str] = {}

        self.log(
            f"AcMainsChecker initializing: id={self._checker_id}, "
            f"includes={len(self._include_patterns)}, "
            f"excludes={len(self._exclude_patterns)}, "
            f"disconnected_status={self._disconnected_status}, "
            f"interval={self._check_interval_s}s",
            level="INFO",
        )

        self.run_in(self._on_startup, 0)

    def _on_startup(self, kwargs: Any) -> None:
        """run_in callback — launches the async startup coroutine."""
        self.create_task(self._async_startup())

    async def _async_startup(self) -> None:
        """Discover entities, register with controller, set up listeners and timer."""
        await self._discover_entities()

        if not self._entities:
            self.log(
                f"No entities matched patterns for checker '{self._checker_id}'",
                level="WARNING",
            )

        self._register()

        # Listen for controller ready (re-register if controller restarts)
        self.listen_event(self._on_controller_ready, "health_check_controller_ready")

        # Listen for force-recheck requests
        self.listen_event(self._on_recheck, "health_check_recheck")

        # Run first check after a short delay, then start periodic timer
        self.run_in(self._first_check, 5)

        self.log(
            f"AcMainsChecker '{self._checker_name}' started with "
            f"{len(self._entities)} entities",
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
    # Entity discovery
    # ------------------------------------------------------------------

    async def _discover_entities(self) -> None:
        """Discover ac_mains sensors matching configured regex patterns.

        These entities expose no ``device_class``, so unlike BatteryChecker
        there is no attribute filter to fall back on — include/exclude
        patterns are the only selector.
        """
        all_states = await self.get_state() or {}
        matched: Dict[str, str] = {}

        for entity_id, state_obj in all_states.items():
            if not isinstance(state_obj, dict):
                continue

            attrs = state_obj.get("attributes", {})
            if not isinstance(attrs, dict):
                attrs = {}

            # Check include patterns
            included = any(p.search(entity_id) for p in self._include_patterns)
            if not included:
                continue

            # Check exclude patterns
            excluded = any(p.search(entity_id) for p in self._exclude_patterns)
            if excluded:
                continue

            # Build display name by stripping the AC mains suffix
            friendly_name = attrs.get("friendly_name", entity_id)
            display_name = friendly_name
            lower = display_name.lower()
            for suffix in _AC_MAINS_SUFFIXES:
                if lower.endswith(suffix):
                    display_name = display_name[: len(display_name) - len(suffix)]
                    break

            matched[entity_id] = display_name

        self._entities = matched

        # Log discovered entities for validation
        self.log(
            f"Discovered {len(self._entities)} AC mains entities for checker "
            f"'{self._checker_id}':",
            level="INFO",
        )
        for entity_id, display_name in sorted(self._entities.items()):
            self.log(f"  - {entity_id} ({display_name})", level="INFO")

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def _register(self) -> None:
        """Fire registration event to the controller."""
        check_names = sorted(self._entities.values())

        # Build dependencies from instance-level health_dependencies
        dep_map: Dict[str, List[str]] = {}
        for dep in self._health_dependencies:
            dep_id = dep.get("checker_id", "") if isinstance(dep, dict) else str(dep)
            if dep_id:
                dep_map.setdefault(dep_id, []).extend(check_names)

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

    def _on_controller_ready(self, event_name: str, data: dict, kwargs: Any) -> None:
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
        results: List[Dict[str, Any]] = []
        metrics: List[Dict[str, Any]] = []

        for entity_id, display_name in sorted(self._entities.items()):
            result = self._evaluate_entity(entity_id, display_name)
            # Internal-only key used to populate the metrics payload below;
            # never sent as part of a check's "results" entry.
            metric_value = result.pop("_metric_value", None)
            results.append(result)
            if metric_value is not None:
                metrics.append({
                    "name": "ac_mains_disconnected",
                    "value": metric_value,
                    "type": "gauge",
                    "labels": {"device": display_name},
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
        degraded_count = sum(1 for r in results if r["status"] == "degraded")
        crit_count = sum(1 for r in results if r["status"] == "critical")
        unknown_count = sum(1 for r in results if r["status"] == "unknown")
        self.log(
            f"Check complete: {ok_count} ok, {warn_count} warning, "
            f"{degraded_count} degraded, {crit_count} critical, "
            f"{unknown_count} unknown",
            level="INFO"
            if crit_count == 0
            and warn_count == 0
            and degraded_count == 0
            and unknown_count == 0
            else "WARNING",
        )

    def _evaluate_entity(self, entity_id: str, display_name: str) -> Dict[str, Any]:
        """Evaluate a single ac_mains_disconnected entity.

        ``on``  → the device has lost wall power and is running on its backup
        battery.  Reported at ``disconnected_status`` (default ``critical``).

        ``off`` → mains present. ok.

        Unavailable / unknown / missing is *no data*, not a power loss, so it
        reports ``unknown`` (which never pages) rather than critical — the same
        doctrine BatteryChecker uses.  A device that drops off entirely is a
        Z-Wave connectivity failure owned by the ``zwave`` checker, which this
        checker declares as a dependency; treating it as mains loss would
        double-page the whole fleet every time the Z-Wave driver restarts.
        """
        try:
            state = self.get_state(entity_id)
        except Exception as exc:
            return {
                "name": display_name,
                "status": "unknown",
                "detail": f"error reading state: {exc}",
            }

        if state is None or str(state) in ("unavailable", "unknown"):
            return {
                "name": display_name,
                "status": "unknown",
                "detail": f"state: {state or 'not found'}",
            }

        normalized = str(state).lower()

        if normalized == "on":
            return {
                "name": display_name,
                "status": self._disconnected_status,
                "detail": "AC mains disconnected — running on backup battery",
                "_metric_value": 1,
            }

        if normalized == "off":
            return {
                "name": display_name,
                "status": "ok",
                "detail": "AC mains connected",
                "_metric_value": 0,
            }

        return {
            "name": display_name,
            "status": "unknown",
            "detail": f"unexpected state: {state}",
        }
