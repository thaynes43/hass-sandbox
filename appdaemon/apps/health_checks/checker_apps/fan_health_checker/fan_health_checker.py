"""Fan Health Checker — monitors Modern Forms ceiling fans.

Performs two checks per fan on a configurable interval:

1. **Entity State** — verify the fan entity is not unavailable/unknown
2. **IP Ping** — ICMP ping the fan's IP address

All fans are reported as a single checker to avoid dashboard clutter.
Supports per-fan repair via a configurable HA script (e.g. zen32_hard_reset).
Each fan gets one auto-repair attempt before being marked failed.

Communication with the controller is event-only (never ``get_app``).
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Add health_checks package root so we can import shared utilities
_health_checks_root = str(Path(__file__).resolve().parents[2])
if _health_checks_root not in sys.path:
    sys.path.insert(0, _health_checks_root)

# Add appdaemon root for providers
_appdaemon_root = str(Path(__file__).resolve().parents[4])
if _appdaemon_root not in sys.path:
    sys.path.insert(0, _appdaemon_root)

import hassapi as hass

from providers.ha_provisioner import HAProvisioner
from shared.check_utils import apply_cross_check_per_device, ping_check

logger = logging.getLogger(__name__)

# Repair state constants
REPAIR_IDLE = "idle"
REPAIR_PENDING = "pending"
REPAIR_IN_PROGRESS = "in_progress"
REPAIR_SUCCESS = "success"
REPAIR_FAILED = "failed"

REPAIR_POLL_INTERVAL_S = 5


class FanHealthChecker(hass.Hass):
    """Health checker for Modern Forms ceiling fans with per-fan repair."""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        args = self.args or {}

        # Identity
        self._checker_id: str = args.get("checker_id", "fans")
        self._checker_name: str = args.get("checker_name", self._checker_id)

        # Fan list
        self._fans: List[Dict[str, str]] = args.get("fans", [])

        # Repair script (full entity ID, e.g. "script.zen32_hard_reset")
        self._repair_script: str = args.get("repair_script", "")

        # Timing
        self._check_interval_s: int = int(args.get("check_interval_s", 180))
        self._repair_recovery_wait_s: int = int(
            args.get("repair_recovery_wait_s", 300)
        )
        self._auto_repair_enabled_default: bool = bool(
            args.get("auto_repair_enabled_default", False)
        )
        self._auto_repair_delay_min_default: int = int(
            args.get("auto_repair_delay_min_default", 5)
        )

        # Per-fan repair state
        self._fan_repair_states: Dict[str, Dict[str, Any]] = {
            fan["name"]: {
                "status": REPAIR_IDLE,
                "detail": "",
                "last_repair_attempt": None,
            }
            for fan in self._fans
        }

        # Global repair tracking
        self._repair_status: str = REPAIR_IDLE
        self._unhealthy_since: Optional[datetime.datetime] = None
        self._auto_repair_deadline: Optional[datetime.datetime] = None
        self._repair_task: Optional[asyncio.Task] = None

        # Cached auto-repair config (updated each async check cycle)
        self._cached_auto_repair_enabled: bool = self._auto_repair_enabled_default
        self._cached_auto_repair_delay_min: int = self._auto_repair_delay_min_default

        self.log(
            f"FanHealthChecker initialising: id={self._checker_id}, "
            f"fans={[f['name'] for f in self._fans]}, "
            f"repair_script={self._repair_script}",
            level="INFO",
        )

        self.run_in(self._on_startup, 0)

    def _on_startup(self, kwargs: Any) -> None:
        self.create_task(self._async_startup())

    async def _async_startup(self) -> None:
        await self._provision_entities()
        await self._refresh_auto_repair_config()
        self._register()

        self.listen_event(
            self._on_controller_ready, "health_check_controller_ready"
        )
        self.listen_event(self._on_recheck, "health_check_recheck")
        self.listen_event(
            self._on_repair_command,
            f"health_check_repair_{self._checker_id}",
        )

        self.run_in(self._first_check, 5)
        self.log(
            f"FanHealthChecker '{self._checker_name}' started", level="INFO"
        )

    async def _provision_entities(self) -> None:
        ha_url = self.args.get("ha_url")
        ha_token_env = self.args.get("ha_token_env")
        if not ha_url or not ha_token_env:
            self.log(
                "ha_url / ha_token_env not configured — skipping provisioning",
                level="WARNING",
            )
            return

        prov = HAProvisioner(ha_url=ha_url, ha_token_env=ha_token_env)

        try:
            created = await prov.ensure_helper(
                "input_boolean",
                f"{self._checker_id} Health Auto Repair",
            )
            if created:
                self.log(
                    f"Provisioned input_boolean.{self._checker_id}_health_auto_repair",
                    level="INFO",
                )
        except Exception as exc:
            self.log(f"Failed to provision auto-repair toggle: {exc!r}", level="ERROR")

        try:
            created = await prov.ensure_helper(
                "input_number",
                f"{self._checker_id} Health Auto Repair Delay",
                min=1,
                max=60,
                step=1,
                unit_of_measurement="min",
                mode="box",
            )
            if created:
                entity_id = f"input_number.{self._checker_id}_health_auto_repair_delay"
                try:
                    self.call_service(
                        "input_number/set_value",
                        entity_id=entity_id,
                        value=self._auto_repair_delay_min_default,
                    )
                except Exception:
                    pass
                self.log(f"Provisioned {entity_id}", level="INFO")
        except Exception as exc:
            self.log(
                f"Failed to provision auto-repair delay helper: {exc!r}",
                level="ERROR",
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
                "supports_repair": True,
                "repair_state": self._build_repair_state(),
            }),
        )
        self.log(
            f"Registered '{self._checker_name}' with {len(check_names)} checks",
            level="INFO",
        )

    def _build_check_names(self) -> List[str]:
        names = []
        for fan in self._fans:
            names.append(f"{fan['name']} State")
            if fan.get("ip"):
                names.append(f"{fan['name']} Ping")
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

    def _on_repair_command(
        self, event_name: str, data: dict, kwargs: Any
    ) -> None:
        action = data.get("action", "")
        if action == "start_repair":
            self.log("Manual repair requested for fans", level="INFO")
            self._start_manual_repair()
        elif action == "update_repair_config":
            self._update_repair_config(data)

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
        await self._refresh_auto_repair_config()
        results: List[Dict[str, str]] = []

        for fan in self._fans:
            results.append(await self._check_fan_entity(fan))
            if fan.get("ip"):
                results.append(await self._check_fan_ping(fan))

        # Auto-reset fan repair states for fans that recovered naturally
        self._reset_recovered_fans(results)

        # Evaluate auto-repair (skip if any repair is in progress)
        if not self._is_any_repair_active():
            self._evaluate_auto_repair(results)

        # Cross-check: downgrade critical→warning for partial failures per fan
        # (after auto-repair eval so repair triggers see raw statuses)
        apply_cross_check_per_device(
            results, [f["name"] for f in self._fans]
        )

        # Report
        payload: Dict[str, Any] = {
            "checker_id": self._checker_id,
            "results": results,
            "repair_state": self._build_repair_state(),
        }
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

    async def _check_fan_entity(self, fan: dict) -> Dict[str, str]:
        name = f"{fan['name']} State"
        try:
            state = await self.get_state(fan["entity_id"])
            if state is None or str(state) in ("unavailable", "unknown"):
                return {
                    "name": name,
                    "status": "critical",
                    "detail": f"State: {state}",
                }
            return {"name": name, "status": "ok", "detail": str(state)}
        except Exception as exc:
            self.log(
                f"Entity check failed for {fan['entity_id']}: {exc!r}",
                level="ERROR",
            )
            return {"name": name, "status": "critical", "detail": f"Error: {exc}"}

    async def _check_fan_ping(self, fan: dict) -> Dict[str, str]:
        name = f"{fan['name']} Ping"
        try:
            result = await ping_check(fan["ip"])
            return {
                "name": name,
                "status": result["status"],
                "detail": result["detail"],
            }
        except Exception as exc:
            self.log(
                f"Ping check failed for {fan['ip']}: {exc!r}", level="ERROR"
            )
            return {"name": name, "status": "critical", "detail": f"Error: {exc}"}

    # ------------------------------------------------------------------
    # Fan failure helpers
    # ------------------------------------------------------------------

    def _get_failing_fans(
        self, results: List[Dict[str, str]]
    ) -> List[Dict[str, str]]:
        """Return fan configs whose checks are failing."""
        failing = []
        for fan in self._fans:
            fan_results = [
                r for r in results if r["name"].startswith(fan["name"])
            ]
            if any(r["status"] != "ok" for r in fan_results):
                failing.append(fan)
        return failing

    def _check_single_fan_results(
        self, fan: dict, results: List[Dict[str, str]]
    ) -> bool:
        """Return True if all checks for a single fan are ok."""
        fan_results = [
            r for r in results if r["name"].startswith(fan["name"])
        ]
        return all(r["status"] == "ok" for r in fan_results)

    def _is_any_repair_active(self) -> bool:
        return any(
            s["status"] == REPAIR_IN_PROGRESS
            for s in self._fan_repair_states.values()
        )

    def _reset_recovered_fans(self, results: List[Dict[str, str]]) -> None:
        """Reset per-fan repair state to idle for fans that recovered naturally."""
        for fan in self._fans:
            if self._check_single_fan_results(fan, results):
                fr = self._fan_repair_states[fan["name"]]
                if fr["status"] in (REPAIR_FAILED, REPAIR_SUCCESS):
                    self.log(
                        f"{fan['name']} recovered — resetting repair state",
                        level="INFO",
                    )
                    fr["status"] = REPAIR_IDLE
                    fr["detail"] = ""

    # ------------------------------------------------------------------
    # Auto-repair logic
    # ------------------------------------------------------------------

    async def _refresh_auto_repair_config(self) -> None:
        """Read auto-repair config from HA helpers (async). Updates cached values."""
        try:
            entity_id = f"input_boolean.{self._checker_id}_health_auto_repair"
            enabled_state = await self.get_state(entity_id)
            self._cached_auto_repair_enabled = str(enabled_state) == "on"
        except Exception:
            pass

        try:
            entity_id = f"input_number.{self._checker_id}_health_auto_repair_delay"
            delay_state = await self.get_state(entity_id)
            self._cached_auto_repair_delay_min = int(float(delay_state))
        except Exception:
            pass

    def _read_auto_repair_config(self) -> tuple[bool, int]:
        """Return cached auto-repair config (sync-safe)."""
        return self._cached_auto_repair_enabled, self._cached_auto_repair_delay_min

    def _evaluate_auto_repair(self, results: List[Dict[str, str]]) -> None:
        failing_fans = self._get_failing_fans(results)
        all_ok = len(failing_fans) == 0

        # If all fans ok, cancel pending and reset
        if all_ok:
            if self._repair_status == REPAIR_PENDING:
                self.log("All fans ok — cancelling pending auto-repair", level="INFO")
            self._repair_status = REPAIR_IDLE
            self._auto_repair_deadline = None
            self._unhealthy_since = None
            return

        # Only count fans that haven't already been attempted
        repairable = [
            f for f in failing_fans
            if self._fan_repair_states[f["name"]]["status"] == REPAIR_IDLE
        ]

        # If no repairable fans left, just stay in current state
        if not repairable:
            return

        enabled, delay_min = self._read_auto_repair_config()
        if not enabled:
            if self._unhealthy_since is None:
                self._unhealthy_since = datetime.datetime.now()
            return

        now = datetime.datetime.now()

        if self._unhealthy_since is None:
            self._unhealthy_since = now

        deadline = self._unhealthy_since + datetime.timedelta(minutes=delay_min)

        if now >= deadline:
            # Start repairing the first repairable fan
            self.log(
                f"Auto-repair triggered — starting with {repairable[0]['name']}",
                level="INFO",
            )
            self._start_fan_repair(repairable[0])
        else:
            self._repair_status = REPAIR_PENDING
            self._auto_repair_deadline = deadline
            self.log(
                f"Repair pending — deadline {deadline.isoformat(timespec='seconds')}",
                level="INFO",
            )

    # ------------------------------------------------------------------
    # Repair execution
    # ------------------------------------------------------------------

    def _start_manual_repair(self) -> None:
        """Start manual repair for all failing fans."""
        if self._is_any_repair_active():
            self.log("Repair already in progress — ignoring", level="WARNING")
            return

        # Reset failed states so they can be retried
        for fr in self._fan_repair_states.values():
            if fr["status"] == REPAIR_FAILED:
                fr["status"] = REPAIR_IDLE
                fr["detail"] = ""

        self._repair_task = self.create_task(self._repair_all_failing())

    def _start_fan_repair(self, fan: dict) -> None:
        """Start repair for a single fan."""
        if self._is_any_repair_active():
            self.log("Repair already in progress — ignoring", level="WARNING")
            return

        # Set state before creating task so it's visible immediately
        fr = self._fan_repair_states[fan["name"]]
        fr["status"] = REPAIR_IN_PROGRESS
        fr["detail"] = "Starting repair..."
        fr["last_repair_attempt"] = datetime.datetime.now().isoformat(
            timespec="seconds"
        )
        self._repair_status = REPAIR_IN_PROGRESS
        self._auto_repair_deadline = None

        self._repair_task = self.create_task(self._execute_fan_repair(fan))

    async def _repair_all_failing(self) -> None:
        """Repair all currently-failing fans sequentially."""
        # Run a fresh check to get current state
        results = await self._run_health_checks_only()
        failing = self._get_failing_fans(results)
        repairable = [
            f for f in failing
            if self._fan_repair_states[f["name"]]["status"] == REPAIR_IDLE
        ]

        if not repairable:
            self.log("No fans to repair", level="INFO")
            return

        for fan in repairable:
            await self._execute_fan_repair(fan)

    async def _execute_fan_repair(self, fan: dict) -> None:
        """Call the repair script for a single fan and poll for recovery."""
        fan_name = fan["name"]
        fr = self._fan_repair_states[fan_name]

        # Ensure state is set (may already be set by _start_fan_repair)
        if fr["status"] != REPAIR_IN_PROGRESS:
            fr["status"] = REPAIR_IN_PROGRESS
            fr["detail"] = "Calling repair script..."
            fr["last_repair_attempt"] = datetime.datetime.now().isoformat(
                timespec="seconds"
            )
            self._repair_status = REPAIR_IN_PROGRESS
            self._auto_repair_deadline = None

        # Report immediately
        self._report_repair_status_only()

        try:
            # Parse repair script entity ID (e.g. "script.zen32_hard_reset")
            script_domain, script_name = self._repair_script.split(".", 1)
            service_call = f"{script_domain}/{script_name}"

            self.log(
                f"Calling {self._repair_script} for {fan_name}", level="INFO"
            )
            self.call_service(
                service_call,
                power_switch_entity=fan["power_switch"],
                relay_control_select_entity=fan["relay_control"],
                scene_control_select_entity=fan["scene_control"],
                unavailable_fan_entity=fan["entity_id"],
            )

            fr["detail"] = "Waiting for recovery..."
            self._report_repair_status_only()

            # Poll for recovery
            elapsed = 0
            while elapsed < self._repair_recovery_wait_s:
                await asyncio.sleep(REPAIR_POLL_INTERVAL_S)
                elapsed += REPAIR_POLL_INTERVAL_S

                results = await self._run_health_checks_only()
                if self._check_single_fan_results(fan, results):
                    fr["status"] = REPAIR_SUCCESS
                    fr["detail"] = f"Recovered after {elapsed}s"
                    self._repair_status = self._aggregate_repair_status()
                    self.log(
                        f"{fan_name} repair successful — recovered after {elapsed}s",
                        level="INFO",
                    )
                    self._report_repair_status_only()
                    return

                fr["detail"] = (
                    f"Waiting for recovery... {elapsed}s/{self._repair_recovery_wait_s}s"
                )

            # Timeout
            fr["status"] = REPAIR_FAILED
            fr["detail"] = (
                f"Did not recover after {self._repair_recovery_wait_s}s"
            )
            self._repair_status = self._aggregate_repair_status()
            self.log(
                f"{fan_name} repair failed — no recovery after "
                f"{self._repair_recovery_wait_s}s",
                level="WARNING",
            )
            self._report_repair_status_only()

        except Exception as exc:
            fr["status"] = REPAIR_FAILED
            fr["detail"] = f"Repair error: {exc}"
            self._repair_status = self._aggregate_repair_status()
            self.log(f"Repair error for {fan_name}: {exc!r}", level="ERROR")
            self._report_repair_status_only()

    async def _run_health_checks_only(self) -> List[Dict[str, str]]:
        """Run all health checks without reporting to controller."""
        results: List[Dict[str, str]] = []
        for fan in self._fans:
            results.append(await self._check_fan_entity(fan))
            if fan.get("ip"):
                results.append(await self._check_fan_ping(fan))
        return results

    def _report_repair_status_only(self) -> None:
        """Fire a status report with current repair state (no new check results)."""
        self.fire_event(
            "health_check_command",
            command="report_status",
            payload=json.dumps({
                "checker_id": self._checker_id,
                "results": [],
                "repair_state": self._build_repair_state(),
            }),
        )

    # ------------------------------------------------------------------
    # Repair config updates
    # ------------------------------------------------------------------

    def _update_repair_config(self, data: dict) -> None:
        auto_enabled = data.get("auto_repair_enabled")
        delay_min = data.get("auto_repair_delay_min")

        if auto_enabled is not None:
            entity_id = f"input_boolean.{self._checker_id}_health_auto_repair"
            current = str(self.get_state(entity_id))
            desired = "on" if auto_enabled else "off"
            if current != desired:
                service = "input_boolean/turn_on" if auto_enabled else "input_boolean/turn_off"
                try:
                    self.call_service(service, entity_id=entity_id)
                    self.log(
                        f"Auto-repair {'enabled' if auto_enabled else 'disabled'}",
                        level="INFO",
                    )
                except Exception as exc:
                    self.log(f"Failed to update auto-repair toggle: {exc!r}", level="ERROR")

        if delay_min is not None:
            entity_id = f"input_number.{self._checker_id}_health_auto_repair_delay"
            try:
                current = int(float(self.get_state(entity_id)))
            except (TypeError, ValueError):
                current = None
            if current != int(delay_min):
                try:
                    self.call_service(
                        "input_number/set_value",
                        entity_id=entity_id,
                        value=int(delay_min),
                    )
                    self.log(f"Auto-repair delay set to {delay_min}m", level="INFO")
                except Exception as exc:
                    self.log(f"Failed to update auto-repair delay: {exc!r}", level="ERROR")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _aggregate_repair_status(self) -> str:
        """Derive top-level repair status from per-fan states."""
        statuses = [s["status"] for s in self._fan_repair_states.values()]
        if REPAIR_IN_PROGRESS in statuses:
            return REPAIR_IN_PROGRESS
        if REPAIR_PENDING in statuses:
            return REPAIR_PENDING
        if REPAIR_FAILED in statuses:
            return REPAIR_FAILED
        if REPAIR_SUCCESS in statuses:
            return REPAIR_SUCCESS
        return REPAIR_IDLE

    def _build_repair_state(self) -> Dict[str, Any]:
        enabled, delay_min = self._read_auto_repair_config()

        # Find latest repair attempt across all fans
        last_attempt = None
        for fr in self._fan_repair_states.values():
            if fr["last_repair_attempt"]:
                if last_attempt is None or fr["last_repair_attempt"] > last_attempt:
                    last_attempt = fr["last_repair_attempt"]

        # Build detail from current activity
        active_fan = None
        for name, fr in self._fan_repair_states.items():
            if fr["status"] == REPAIR_IN_PROGRESS:
                active_fan = name
                break

        if active_fan:
            detail = f"Repairing {active_fan}"
        elif self._repair_status == REPAIR_PENDING:
            detail = "Auto-repair pending"
        else:
            failed_count = sum(
                1 for fr in self._fan_repair_states.values()
                if fr["status"] == REPAIR_FAILED
            )
            if failed_count:
                detail = f"{failed_count} fan(s) repair failed"
            else:
                detail = ""

        return {
            "status": self._aggregate_repair_status(),
            "detail": detail,
            "auto_repair_enabled": enabled,
            "auto_repair_delay_min": delay_min,
            "auto_repair_deadline": (
                self._auto_repair_deadline.isoformat(timespec="seconds")
                if self._auto_repair_deadline
                else None
            ),
            "last_repair_attempt": last_attempt,
            "device_repairs": {
                name: {
                    "status": fr["status"],
                    "detail": fr["detail"],
                    "last_repair_attempt": fr["last_repair_attempt"],
                }
                for name, fr in self._fan_repair_states.items()
            },
        }
