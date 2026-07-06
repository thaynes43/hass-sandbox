"""Battery Health Checker — auto-discovers battery sensors via entity patterns."""

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

from shared.check_utils import is_implausible_battery_drop

# Suffixes to strip from friendly_name for cleaner display names (case-insensitive)
_BATTERY_SUFFIXES = [
    " battery level",
    " battery",
]


class BatteryChecker(hass.Hass):
    """Monitors battery level sensors with configurable thresholds.

    Discovers entities automatically using ``entity_patterns`` config
    (include/exclude regexes) filtered to ``device_class=battery`` and
    ``unit_of_measurement=%``.
    """

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        args = self.args or {}
        self._checker_id: str = args.get("checker_id", "batteries")
        self._checker_name: str = args.get("checker_name", "Batteries")
        self._check_interval_s: int = int(args.get("check_interval_s", 300))

        # Default thresholds (percentage)
        self._warning_threshold: float = float(args.get("warning_threshold", 20))
        self._critical_threshold: float = float(args.get("critical_threshold", 10))

        # Disconnect-aware guard (opt-in): distinguishes a gateway/RF disconnect
        # (implausible drop from a healthy baseline straight to ~0%) from a
        # genuine gradual low battery. When enabled, an implausible drop is
        # reported as "warning" (UI-only) instead of "critical" (pages), since
        # a dedicated gateway checker (e.g. ShadeGatewayChecker) owns paging
        # for the disconnect itself. Default False keeps all other battery
        # groups' behavior unchanged.
        self._disconnect_aware: bool = bool(args.get("disconnect_aware", False))
        self._disconnect_healthy_floor: float = float(
            args.get("disconnect_healthy_floor", 40)
        )
        # entity_id -> last reading seen above critical_threshold (only
        # tracked/consulted when disconnect_aware is enabled).
        self._last_good_value: Dict[str, float] = {}

        # Instance-level dependencies
        self._health_dependencies: List[dict] = list(
            args.get("health_dependencies", [])
        )

        # Entity discovery patterns
        self._include_patterns: List[re.Pattern] = []
        self._exclude_patterns: List[re.Pattern] = []
        for pattern_cfg in args.get("entity_patterns", []):
            if "include" in pattern_cfg:
                self._include_patterns.append(
                    re.compile(pattern_cfg["include"])
                )
            if "exclude" in pattern_cfg:
                self._exclude_patterns.append(
                    re.compile(pattern_cfg["exclude"])
                )

        # Discovered entities: entity_id -> display_name
        self._entities: Dict[str, str] = {}

        self.log(
            f"BatteryChecker initializing: id={self._checker_id}, "
            f"includes={len(self._include_patterns)}, "
            f"excludes={len(self._exclude_patterns)}, "
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
            f"BatteryChecker '{self._checker_name}' started with "
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
        """Discover battery entities matching configured regex patterns.

        Filters to entities with ``device_class=battery`` and
        ``unit_of_measurement=%``, then applies include/exclude patterns.
        Strips common battery suffixes from friendly_name for cleaner
        display names.
        """
        all_states = await self.get_state() or {}
        matched: Dict[str, str] = {}

        for entity_id, state_obj in all_states.items():
            if not isinstance(state_obj, dict):
                continue

            attrs = state_obj.get("attributes", {})
            if not isinstance(attrs, dict):
                attrs = {}

            # Must be a battery percentage sensor
            if attrs.get("device_class") != "battery":
                continue
            if attrs.get("unit_of_measurement") != "%":
                continue

            # Check include patterns
            included = any(p.search(entity_id) for p in self._include_patterns)
            if not included:
                continue

            # Check exclude patterns
            excluded = any(p.search(entity_id) for p in self._exclude_patterns)
            if excluded:
                continue

            # Build display name by stripping battery suffixes
            friendly_name = attrs.get("friendly_name", entity_id)
            display_name = friendly_name
            lower = display_name.lower()
            for suffix in _BATTERY_SUFFIXES:
                if lower.endswith(suffix):
                    display_name = display_name[: len(display_name) - len(suffix)]
                    break

            matched[entity_id] = display_name

            # Seed disconnect-aware baseline from the current healthy state
            # so a real implausible drop can be detected on the very first
            # transition after startup (cold start never fabricates a
            # baseline from a low reading — only a healthy one).
            if self._disconnect_aware:
                try:
                    current_value = float(state_obj.get("state"))
                except (TypeError, ValueError):
                    current_value = None
                if current_value is not None and current_value > self._critical_threshold:
                    self._last_good_value[entity_id] = current_value

        self._entities = matched

        # Log discovered entities for validation
        self.log(
            f"Discovered {len(self._entities)} battery entities for checker "
            f"'{self._checker_id}':",
            level="INFO",
        )
        for entity_id, display_name in sorted(self._entities.items()):
            self.log(f"  - {entity_id} ({display_name})", level="INFO")

        if self._disconnect_aware:
            self.log(
                f"Disconnect-aware guard enabled for '{self._checker_id}': "
                f"seeded {len(self._last_good_value)}/{len(self._entities)} "
                f"healthy baselines (healthy_floor={self._disconnect_healthy_floor:.0f}%)",
                level="INFO",
            )

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

        for entity_id, display_name in sorted(self._entities.items()):
            result = self._evaluate_entity(entity_id, display_name)
            results.append(result)

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

    def _evaluate_entity(self, entity_id: str, display_name: str) -> Dict[str, str]:
        """Evaluate a single battery entity. Returns a result dict."""
        try:
            state = self.get_state(entity_id)
        except Exception as exc:
            return {"name": display_name, "status": "critical", "detail": f"error reading state: {exc}"}

        if state is None or str(state) in ("unavailable", "unknown"):
            return {"name": display_name, "status": "critical", "detail": f"state: {state or 'not found'}"}

        try:
            value = float(state)
        except (ValueError, TypeError):
            return {"name": display_name, "status": "critical", "detail": f"non-numeric state: {state}"}

        if value <= self._critical_threshold:
            prev_good = self._last_good_value.get(entity_id)
            if self._disconnect_aware and is_implausible_battery_drop(
                prev_good, value, self._disconnect_healthy_floor, self._critical_threshold
            ):
                result = {
                    "name": display_name,
                    "status": "warning",
                    "detail": (
                        f"suspected gateway disconnect (was {prev_good:.0f}%, now "
                        f"{value:.0f}%) — see Shade Gateway"
                    ),
                }
            else:
                result = {
                    "name": display_name,
                    "status": "critical",
                    "detail": f"{value:.0f}% (critical ≤{self._critical_threshold:.0f}%)",
                }
        elif value <= self._warning_threshold:
            result = {
                "name": display_name,
                "status": "warning",
                "detail": f"{value:.0f}% (warning ≤{self._warning_threshold:.0f}%)",
            }
        else:
            result = {"name": display_name, "status": "ok", "detail": f"{value:.0f}%"}

        # Update the healthy baseline whenever we see a reading above the
        # critical threshold, so the next implausible-drop check (in this
        # cycle or a future one) has a fresh comparison point.
        if self._disconnect_aware and value > self._critical_threshold:
            self._last_good_value[entity_id] = value

        return result
