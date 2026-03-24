"""MQTT Device Checker -- dual HA entity state + MQTT last-seen monitoring.

Discovers HA entities via configurable regex patterns, then monitors each
device via both its HA entity state and direct MQTT message tracking from
Zigbee2MQTT.  This dual-check approach distinguishes between device failures
and HA integration failures:

- HA entity fails + MQTT recent  -> **warning** (likely integration issue)
- MQTT stale + HA entity ok      -> **warning** (device quiet but reachable)
- Both fail                      -> **critical** (device or network down)

MQTT checks can declare a dependency on a protocol checker (e.g. Zigbee)
so they show as ``unknown`` when the protocol itself is down.

Communication with the controller is event-only (never ``get_app``).
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

# Add health_checks package root so we can import shared utilities
_health_checks_root = str(Path(__file__).resolve().parents[2])
if _health_checks_root not in sys.path:
    sys.path.insert(0, _health_checks_root)

import hassapi as hass


class MqttDeviceChecker(hass.Hass):
    """Monitors devices via HA entity state and MQTT message recency."""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        args = self.args or {}

        # Identity
        self._checker_id: str = args.get("checker_id", "mqtt_devices")
        self._checker_name: str = args.get("checker_name", self._checker_id)

        # Timing
        self._check_interval_s: int = int(args.get("check_interval_s", 300))
        self._mqtt_stale_s: int = int(args.get("mqtt_stale_s", 21600))

        # MQTT
        self._mqtt_namespace: str = args.get("mqtt_namespace", "mqtt")
        self._mqtt_topic_prefix: str = args.get(
            "mqtt_topic_prefix", "zigbee2mqtt"
        )

        # Dependency on protocol checker (e.g. zigbee)
        self._protocol_dependency_id: str = args.get(
            "protocol_dependency_id", ""
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

        # Discovered entities: entity_id -> friendly_name
        self._entities: Dict[str, str] = {}
        # MQTT tracking: friendly_name -> last message timestamp (epoch)
        self._mqtt_last_seen: Dict[str, float] = {}
        # Ignore retained messages: only count messages after this time
        self._mqtt_accept_after: float = 0.0

        self.log(
            f"MqttDeviceChecker initialising: id={self._checker_id}, "
            f"includes={len(self._include_patterns)}, "
            f"excludes={len(self._exclude_patterns)}, "
            f"stale_threshold={self._mqtt_stale_s}s",
            level="INFO",
        )

        self.run_in(self._on_startup, 0)

    def _on_startup(self, kwargs: Any) -> None:
        """run_in callback -- launches the async startup coroutine."""
        self.create_task(self._async_startup())

    async def _async_startup(self) -> None:
        """Discover entities, register, set up listeners and timer."""
        await self._discover_entities()

        if not self._entities:
            self.log(
                "No entities matched patterns for checker "
                f"'{self._checker_id}'",
                level="WARNING",
            )

        # Register with controller
        self._register()

        # Listen for MQTT messages to track device activity
        try:
            self.listen_event(
                self._on_mqtt_message,
                "MQTT_MESSAGE",
                namespace=self._mqtt_namespace,
            )
            # Ignore retained messages delivered on subscribe — only count
            # messages arriving after a short grace period
            self._mqtt_accept_after = time.time() + 5
            self.log(
                f"Listening for MQTT messages (filtering for "
                f"'{self._mqtt_topic_prefix}/', ignoring first 5s for retained)",
                level="INFO",
            )
        except Exception as exc:
            self.log(
                f"Failed to listen for MQTT messages: {exc!r}", level="ERROR"
            )

        # Listen for controller events
        self.listen_event(
            self._on_controller_ready, "health_check_controller_ready"
        )
        self.listen_event(self._on_recheck, "health_check_recheck")

        # Schedule first check + periodic timer
        self.run_in(self._first_check, 10)

        self.log(
            f"MqttDeviceChecker '{self._checker_name}' started with "
            f"{len(self._entities)} entities",
            level="INFO",
        )

    # ------------------------------------------------------------------
    # Entity discovery
    # ------------------------------------------------------------------

    async def _discover_entities(self) -> None:
        """Discover HA entities matching configured regex patterns."""
        all_states = await self.get_state() or {}
        matched: Dict[str, str] = {}

        for entity_id, state_obj in all_states.items():
            if not isinstance(state_obj, dict):
                continue

            # Check include patterns
            included = any(
                p.search(entity_id) for p in self._include_patterns
            )
            if not included:
                continue

            # Check exclude patterns
            excluded = any(
                p.search(entity_id) for p in self._exclude_patterns
            )
            if excluded:
                continue

            attrs = state_obj.get("attributes", {})
            if not isinstance(attrs, dict):
                attrs = {}
            friendly_name = attrs.get("friendly_name", entity_id)
            matched[entity_id] = friendly_name

        self._entities = matched

        # Log discovered entities for validation
        self.log(
            f"Discovered {len(self._entities)} entities for checker "
            f"'{self._checker_id}':",
            level="INFO",
        )
        for entity_id, friendly_name in sorted(self._entities.items()):
            self.log(f"  - {entity_id} ({friendly_name})", level="INFO")

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def _build_check_names(self) -> List[str]:
        """Build the list of check names for all discovered entities."""
        names: List[str] = []
        for entity_id in sorted(self._entities.keys()):
            short = self._short_name(entity_id)
            names.append(f"{short} State")
            names.append(f"{short} MQTT")
        return names

    def _register(self) -> None:
        """Fire registration event to the controller."""
        check_names = self._build_check_names()

        # Build dependencies: MQTT checks depend on protocol checker
        dependencies: List[dict] = []
        if self._protocol_dependency_id:
            mqtt_checks = [n for n in check_names if n.endswith(" MQTT")]
            if mqtt_checks:
                dependencies.append({
                    "checker_id": self._protocol_dependency_id,
                    "affects_checks": mqtt_checks,
                })

        payload = {
            "checker_id": self._checker_id,
            "checker_name": self._checker_name,
            "check_names": check_names,
            "dependencies": dependencies,
        }

        self.fire_event(
            "health_check_command",
            command="register_checker",
            payload=json.dumps(payload),
        )
        self.log(
            f"Registered '{self._checker_name}' with {len(check_names)} "
            f"checks (dependencies: "
            f"{[d['checker_id'] for d in dependencies]})",
            level="INFO",
        )

    def _short_name(self, entity_id: str) -> str:
        """Generate a short display name from entity_id."""
        name = entity_id.split(".", 1)[-1] if "." in entity_id else entity_id
        return name.replace("_", " ").title()

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_controller_ready(
        self, event_name: str, data: dict, kwargs: Any
    ) -> None:
        """Re-register when controller (re)starts."""
        self.log(
            f"Controller ready -- re-registering '{self._checker_name}'",
            level="INFO",
        )
        self._register()
        self._run_checks()

    def _on_recheck(self, event_name: str, data: dict, kwargs: Any) -> None:
        """Run checks immediately on force-recheck request."""
        self.log(
            f"Force recheck requested for '{self._checker_name}'",
            level="INFO",
        )
        self._run_checks()

    def _first_check(self, kwargs: Any) -> None:
        """Run the first check, then start periodic timer."""
        self._run_checks()
        self.run_every(
            self._check_tick,
            f"now+{self._check_interval_s}",
            self._check_interval_s,
        )

    def _check_tick(self, kwargs: Any) -> None:
        """Periodic timer callback."""
        self._run_checks()

    # ------------------------------------------------------------------
    # MQTT message tracking
    # ------------------------------------------------------------------

    def _on_mqtt_message(
        self, event_name: str, data: dict, kwargs: Any
    ) -> None:
        """Track when we last received any MQTT message from each device."""
        topic = data.get("topic")
        if not topic:
            return  # Connection state events have topic=None

        # Only process messages under our topic prefix
        prefix = self._mqtt_topic_prefix + "/"
        if not topic.startswith(prefix):
            return

        # Extract device name: zigbee2mqtt/<device_name>[/...]
        remainder = topic[len(prefix):]
        device_name = remainder.split("/")[0] if remainder else ""
        if not device_name or device_name in ("bridge", "group"):
            return

        # Skip retained messages delivered during initial subscribe burst
        now = time.time()
        if now < self._mqtt_accept_after:
            return

        # Record the time we last saw a live message from this device
        self._mqtt_last_seen[device_name] = now

    # ------------------------------------------------------------------
    # Check execution
    # ------------------------------------------------------------------

    def _run_checks(self) -> None:
        """Run HA entity state + MQTT recency checks for all devices."""
        results: List[Dict[str, str]] = []
        now = time.time()

        for entity_id, friendly_name in sorted(self._entities.items()):
            short = self._short_name(entity_id)

            # HA Entity State Check
            ha_status, ha_detail = self._check_ha_entity(entity_id)

            # MQTT Recency Check (using friendly_name as Z2M device name)
            mqtt_status, mqtt_detail = self._check_mqtt_recency(
                friendly_name, now
            )

            # Cross-check logic:
            #   Both fail -> stay critical (device dead)
            #   Only one fails -> downgrade to warning
            both_bad = (
                ha_status in ("critical", "unknown")
                and mqtt_status in ("critical", "unknown")
            )
            if not both_bad:
                if ha_status == "critical":
                    ha_status = "warning"
                    ha_detail = f"{ha_detail} (MQTT ok)"
                if mqtt_status == "critical":
                    mqtt_status = "warning"
                    mqtt_detail = f"{mqtt_detail} (HA state ok)"

            results.append({
                "name": f"{short} State",
                "status": ha_status,
                "detail": ha_detail,
            })
            results.append({
                "name": f"{short} MQTT",
                "status": mqtt_status,
                "detail": mqtt_detail,
            })

        self.fire_event(
            "health_check_command",
            command="report_status",
            payload=json.dumps({
                "checker_id": self._checker_id,
                "results": results,
            }),
        )

        # Log MQTT match diagnostics
        expected_names = set(self._entities.values())
        tracked_names = set(self._mqtt_last_seen.keys())
        matched = expected_names & tracked_names
        unmatched_expected = expected_names - tracked_names
        extra_tracked = tracked_names - expected_names
        self.log(
            f"MQTT tracking: {len(tracked_names)} total seen, "
            f"{len(matched)}/{len(expected_names)} matched, "
            f"{len(unmatched_expected)} missing, {len(extra_tracked)} extra",
            level="INFO",
        )
        if unmatched_expected:
            self.log(
                f"  Missing: {sorted(unmatched_expected)[:5]}",
                level="INFO",
            )

        # Log summary
        ok_count = sum(1 for r in results if r["status"] == "ok")
        warn_count = sum(1 for r in results if r["status"] == "warning")
        crit_count = sum(1 for r in results if r["status"] == "critical")
        unk_count = sum(1 for r in results if r["status"] == "unknown")
        self.log(
            f"Check complete for '{self._checker_name}': "
            f"{ok_count} ok, {warn_count} warning, {crit_count} critical, "
            f"{unk_count} unknown ({len(self._entities)} devices)",
            level="INFO" if crit_count == 0 and warn_count == 0 else "WARNING",
        )

    def _check_ha_entity(self, entity_id: str) -> tuple:
        """Check HA entity state. Returns (status, detail)."""
        try:
            state = self.get_state(entity_id)
            if state is None or str(state) in ("unavailable", "unknown"):
                return ("critical", f"state: {state or 'not found'}")
            return ("ok", f"state: {state}")
        except Exception as exc:
            return ("critical", f"error: {exc}")

    def _check_mqtt_recency(self, friendly_name: str, now: float) -> tuple:
        """Check when we last saw an MQTT message from a device."""
        last_seen = self._mqtt_last_seen.get(friendly_name)

        if last_seen is None:
            return ("unknown", "no MQTT data yet")

        age_s = now - last_seen
        if age_s > self._mqtt_stale_s:
            return (
                "critical",
                f"last MQTT {self._format_age(age_s)} ago (stale)",
            )

        return ("ok", f"MQTT {self._format_age(age_s)} ago")

    @staticmethod
    def _format_age(seconds: float) -> str:
        """Format age in seconds to a human-readable string."""
        s = int(seconds)
        if s < 60:
            return f"{s}s"
        if s < 3600:
            return f"{s // 60}m"
        if s < 86400:
            return f"{s // 3600}h {(s % 3600) // 60}m"
        return f"{s // 86400}d {(s % 86400) // 3600}h"
