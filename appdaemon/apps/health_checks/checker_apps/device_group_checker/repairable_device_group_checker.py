"""Repairable Device Group Checker — extends DeviceGroupChecker with per-device repair.

Adds a per-device repair state machine and auto-repair via power-cycling smart
switches. Each device can have its own repair switch or share a top-level switch.

Repair logic:
- Each device gets one auto-repair attempt before being marked failed.
- Repairs execute sequentially — one device at a time.
- Manual repair resets all failed states and repairs all failing devices.
- A device that recovers naturally resets to idle.

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

_health_checks_root = str(Path(__file__).resolve().parents[2])
if _health_checks_root not in sys.path:
    sys.path.insert(0, _health_checks_root)

_appdaemon_root = str(Path(__file__).resolve().parents[4])
if _appdaemon_root not in sys.path:
    sys.path.insert(0, _appdaemon_root)

from providers.ha_provisioner import HAProvisioner
from health_checks.checker_apps.device_group_checker.device_group_checker import (
    DeviceGroupChecker,
)
from shared.check_utils import apply_cross_check_per_device

logger = logging.getLogger(__name__)

# Repair state constants
REPAIR_IDLE = "idle"
REPAIR_PENDING = "pending"
REPAIR_IN_PROGRESS = "in_progress"
REPAIR_SUCCESS = "success"
REPAIR_FAILED = "failed"

REPAIR_POLL_INTERVAL_S = 5


class RepairableDeviceGroupChecker(DeviceGroupChecker):
    """DeviceGroupChecker with per-device smart-switch power-cycle repair support."""

    # ------------------------------------------------------------------
    # Lifecycle (extends parent)
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        super().initialize()

        args = self.args or {}

        # Top-level shared repair switch (used if device has no per-device switch)
        self._repair_switch: str = args.get("repair_switch", "")
        self._repair_recovery_wait_s: int = int(
            args.get("repair_recovery_wait_s", 300)
        )
        self._repair_off_duration_s: int = int(
            args.get("repair_off_duration_s", 10)
        )
        self._auto_repair_enabled_default: bool = bool(
            args.get("auto_repair_enabled_default", False)
        )
        self._auto_repair_delay_min_default: int = int(
            args.get("auto_repair_delay_min_default", 5)
        )

        # Per-device repair switch lookup (keyed by device name)
        self._device_repair_switches: Dict[str, str] = {}
        for dev in self.args.get("devices", []):
            switch = dev.get("repair_switch", "")
            if switch:
                self._device_repair_switches[dev["name"]] = switch

        # Per-device repair state
        self._device_repair_states: Dict[str, Dict[str, Any]] = {
            dev["name"]: {
                "status": REPAIR_IDLE,
                "detail": "",
                "last_repair_attempt": None,
            }
            for dev in self._devices
        }

        # Global repair tracking
        self._repair_status: str = REPAIR_IDLE
        self._unhealthy_since: Optional[datetime.datetime] = None
        self._auto_repair_deadline: Optional[datetime.datetime] = None
        self._repair_task: Optional[asyncio.Task] = None

        # Cached auto-repair config (updated each async check cycle)
        self._cached_auto_repair_enabled: bool = self._auto_repair_enabled_default
        self._cached_auto_repair_delay_min: int = self._auto_repair_delay_min_default

    async def _async_startup(self) -> None:
        await self._provision_repair_helpers()
        await self._refresh_auto_repair_config()

        # Register with supports_repair (overrides parent registration)
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
            f"RepairableDeviceGroupChecker '{self._checker_name}' started",
            level="INFO",
        )

    async def _provision_repair_helpers(self) -> None:
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
            self.log(
                f"Failed to provision auto-repair toggle: {exc!r}", level="ERROR"
            )

        try:
            created = await prov.ensure_helper(
                "input_number",
                f"{self._checker_id} Health Auto Repair Delay",
                min=1, max=60, step=1,
                unit_of_measurement="min", mode="box",
            )
            if created:
                entity_id = (
                    f"input_number.{self._checker_id}_health_auto_repair_delay"
                )
                try:
                    self.call_service(
                        "input_number/set_value",
                        entity_id=entity_id,
                        value=self._auto_repair_delay_min_default,
                    )
                except Exception as exc:
                    self.log(f"Failed to set default for {entity_id}: {exc!r}", level="DEBUG")
                self.log(f"Provisioned {entity_id}", level="INFO")
        except Exception as exc:
            self.log(
                f"Failed to provision auto-repair delay: {exc!r}", level="ERROR"
            )

    # ------------------------------------------------------------------
    # Registration (override parent to add supports_repair + repair_state)
    # ------------------------------------------------------------------

    def _build_register_payload(self, check_names: List[str]) -> Dict[str, Any]:
        payload = super()._build_register_payload(check_names)
        payload["supports_repair"] = True
        payload["repair_state"] = self._build_repair_state()
        return payload

    # ------------------------------------------------------------------
    # Check execution (override to add repair logic)
    # ------------------------------------------------------------------

    async def _run_checks(self) -> None:
        await self._refresh_auto_repair_config()
        results = await self._run_checks_only()

        # Auto-reset device repair states for devices that recovered naturally
        self._reset_recovered_devices(results)

        # Evaluate auto-repair (skip if any repair is in progress)
        if not self._is_any_repair_active():
            self._evaluate_auto_repair(results)

        # Cross-check: downgrade critical→warning for partial failures per device
        # (after auto-repair eval so repair triggers see raw statuses)
        apply_cross_check_per_device(
            results, [d["name"] for d in self._devices]
        )

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

    def _build_report_payload(self, results: List[Dict[str, str]]) -> Dict[str, Any]:
        payload = super()._build_report_payload(results)
        payload["repair_state"] = self._build_repair_state()
        return payload

    # ------------------------------------------------------------------
    # Repair event handler
    # ------------------------------------------------------------------

    def _on_repair_command(
        self, event_name: str, data: dict, kwargs: Any
    ) -> None:
        action = data.get("action", "")
        if action == "start_repair":
            self.log("Manual repair requested", level="INFO")
            self._start_manual_repair()
        elif action == "update_repair_config":
            self._update_repair_config(data)

    # ------------------------------------------------------------------
    # Device failure helpers
    # ------------------------------------------------------------------

    def _get_failing_devices(
        self, results: List[Dict[str, str]]
    ) -> List[Dict[str, Any]]:
        """Return device configs whose checks are failing."""
        failing = []
        for dev in self._devices:
            dev_results = [
                r for r in results if r["name"].startswith(dev["name"])
            ]
            if any(r["status"] != "ok" for r in dev_results):
                failing.append(dev)
        return failing

    def _check_single_device_results(
        self, dev: dict, results: List[Dict[str, str]]
    ) -> bool:
        """Return True if all checks for a single device are ok."""
        dev_results = [
            r for r in results if r["name"].startswith(dev["name"])
        ]
        return all(r["status"] == "ok" for r in dev_results)

    def _is_any_repair_active(self) -> bool:
        return any(
            s["status"] == REPAIR_IN_PROGRESS
            for s in self._device_repair_states.values()
        )

    def _reset_recovered_devices(self, results: List[Dict[str, str]]) -> None:
        """Reset per-device repair state to idle for devices that recovered naturally."""
        for dev in self._devices:
            if self._check_single_device_results(dev, results):
                dr = self._device_repair_states[dev["name"]]
                if dr["status"] in (REPAIR_FAILED, REPAIR_SUCCESS):
                    self.log(
                        f"{dev['name']} recovered — resetting repair state",
                        level="INFO",
                    )
                    dr["status"] = REPAIR_IDLE
                    dr["detail"] = ""

    # ------------------------------------------------------------------
    # Auto-repair logic
    # ------------------------------------------------------------------

    async def _refresh_auto_repair_config(self) -> None:
        """Read auto-repair config from HA helpers (async). Updates cached values."""
        try:
            entity_id = f"input_boolean.{self._checker_id}_health_auto_repair"
            enabled_state = await self.get_state(entity_id)
            self._cached_auto_repair_enabled = str(enabled_state) == "on"
        except Exception as exc:
            self.log(f"Failed to read auto-repair toggle: {exc!r}", level="WARNING")

        try:
            entity_id = f"input_number.{self._checker_id}_health_auto_repair_delay"
            delay_state = await self.get_state(entity_id)
            if delay_state is not None and str(delay_state) not in ("unavailable", "unknown"):
                self._cached_auto_repair_delay_min = int(float(delay_state))
        except Exception as exc:
            self.log(f"Failed to read auto-repair delay: {exc!r}", level="WARNING")

    def _read_auto_repair_config(self) -> tuple[bool, int]:
        """Return cached auto-repair config (sync-safe)."""
        return self._cached_auto_repair_enabled, self._cached_auto_repair_delay_min

    def _evaluate_auto_repair(self, results: List[Dict[str, str]]) -> None:
        failing_devices = self._get_failing_devices(results)
        all_ok = len(failing_devices) == 0

        # If all devices ok, cancel pending and reset
        if all_ok:
            if self._repair_status == REPAIR_PENDING:
                self.log(
                    "All devices ok — cancelling pending auto-repair", level="INFO"
                )
            self._repair_status = REPAIR_IDLE
            self._auto_repair_deadline = None
            self._unhealthy_since = None
            return

        # Only consider devices that haven't already been attempted
        repairable = [
            d for d in failing_devices
            if self._device_repair_states[d["name"]]["status"] == REPAIR_IDLE
        ]

        # If no repairable devices left, stay in current state
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
            self.log(
                f"Auto-repair triggered — starting with {repairable[0]['name']}",
                level="INFO",
            )
            self._start_device_repair(repairable[0])
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
        """Start manual repair for all failing devices."""
        if self._is_any_repair_active():
            self.log("Repair already in progress — ignoring", level="WARNING")
            return

        # Reset failed states so they can be retried
        for dr in self._device_repair_states.values():
            if dr["status"] == REPAIR_FAILED:
                dr["status"] = REPAIR_IDLE
                dr["detail"] = ""

        self._repair_task = self.create_task(self._repair_all_failing())

    def _start_device_repair(self, dev: dict) -> None:
        """Start repair for a single device."""
        if self._is_any_repair_active():
            self.log("Repair already in progress — ignoring", level="WARNING")
            return

        # Set state before creating task so it's visible immediately
        dr = self._device_repair_states[dev["name"]]
        dr["status"] = REPAIR_IN_PROGRESS
        dr["detail"] = "Starting repair..."
        dr["last_repair_attempt"] = datetime.datetime.now().isoformat(
            timespec="seconds"
        )
        self._repair_status = REPAIR_IN_PROGRESS
        self._auto_repair_deadline = None

        self._repair_task = self.create_task(self._execute_device_repair(dev))

    async def _repair_all_failing(self) -> None:
        """Repair all currently-failing devices sequentially."""
        results = await self._run_checks_only()
        failing = self._get_failing_devices(results)
        repairable = [
            d for d in failing
            if self._device_repair_states[d["name"]]["status"] == REPAIR_IDLE
        ]

        if not repairable:
            self.log("No devices to repair", level="INFO")
            return

        for dev in repairable:
            await self._execute_device_repair(dev)

    def _get_repair_switch_for_device(self, dev: dict) -> str:
        """Return the repair switch for a device — per-device or shared top-level."""
        return self._device_repair_switches.get(dev["name"], self._repair_switch)

    async def _execute_device_repair(self, dev: dict) -> None:
        """Power-cycle the repair switch for a device and poll for recovery."""
        dev_name = dev["name"]
        dr = self._device_repair_states[dev_name]
        repair_switch = self._get_repair_switch_for_device(dev)

        # Ensure state is set (may already be set by _start_device_repair)
        if dr["status"] != REPAIR_IN_PROGRESS:
            dr["status"] = REPAIR_IN_PROGRESS
            dr["detail"] = "Power cycling..."
            dr["last_repair_attempt"] = datetime.datetime.now().isoformat(
                timespec="seconds"
            )
            self._repair_status = REPAIR_IN_PROGRESS
            self._auto_repair_deadline = None

        if not repair_switch:
            dr["status"] = REPAIR_FAILED
            dr["detail"] = "No repair switch configured"
            self._repair_status = self._aggregate_repair_status()
            self._report_repair_status_only()
            return

        # Report immediately
        self._report_repair_status_only()

        try:
            self.log(f"Turning off {repair_switch} for {dev_name}", level="INFO")
            self.call_service("switch/turn_off", entity_id=repair_switch)

            await asyncio.sleep(self._repair_off_duration_s)

            self.log(f"Turning on {repair_switch} for {dev_name}", level="INFO")
            self.call_service("switch/turn_on", entity_id=repair_switch)

            dr["detail"] = "Waiting for recovery..."
            self._report_repair_status_only()

            # Poll for recovery
            elapsed = 0
            while elapsed < self._repair_recovery_wait_s:
                await asyncio.sleep(REPAIR_POLL_INTERVAL_S)
                elapsed += REPAIR_POLL_INTERVAL_S

                results = await self._run_checks_only()
                if self._check_single_device_results(dev, results):
                    dr["status"] = REPAIR_SUCCESS
                    dr["detail"] = f"Recovered after {elapsed}s"
                    self._repair_status = self._aggregate_repair_status()
                    self._unhealthy_since = None
                    self.log(
                        f"{dev_name} repair successful — recovered after {elapsed}s",
                        level="INFO",
                    )
                    self._report_repair_status_only()
                    return

                dr["detail"] = (
                    f"Waiting for recovery... "
                    f"{elapsed}s/{self._repair_recovery_wait_s}s"
                )

            # Timeout
            dr["status"] = REPAIR_FAILED
            dr["detail"] = (
                f"Did not recover after {self._repair_recovery_wait_s}s"
            )
            self._repair_status = self._aggregate_repair_status()
            self.log(
                f"{dev_name} repair failed — no recovery after "
                f"{self._repair_recovery_wait_s}s",
                level="WARNING",
            )
            self._report_repair_status_only()

        except Exception as exc:
            dr["status"] = REPAIR_FAILED
            dr["detail"] = f"Repair error: {exc}"
            self._repair_status = self._aggregate_repair_status()
            self.log(f"Repair error for {dev_name}: {exc!r}", level="ERROR")
            self._report_repair_status_only()

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
                service = (
                    "input_boolean/turn_on"
                    if auto_enabled
                    else "input_boolean/turn_off"
                )
                try:
                    self.call_service(service, entity_id=entity_id)
                    self.log(
                        f"Auto-repair {'enabled' if auto_enabled else 'disabled'}",
                        level="INFO",
                    )
                except Exception as exc:
                    self.log(
                        f"Failed to update auto-repair toggle: {exc!r}",
                        level="ERROR",
                    )

        if delay_min is not None:
            entity_id = (
                f"input_number.{self._checker_id}_health_auto_repair_delay"
            )
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
                    self.log(
                        f"Auto-repair delay set to {delay_min}m", level="INFO"
                    )
                except Exception as exc:
                    self.log(
                        f"Failed to update auto-repair delay: {exc!r}",
                        level="ERROR",
                    )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _aggregate_repair_status(self) -> str:
        """Derive top-level repair status from per-device states."""
        statuses = [s["status"] for s in self._device_repair_states.values()]
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

        # Find latest repair attempt across all devices
        last_attempt = None
        for dr in self._device_repair_states.values():
            if dr["last_repair_attempt"]:
                if last_attempt is None or dr["last_repair_attempt"] > last_attempt:
                    last_attempt = dr["last_repair_attempt"]

        # Build detail from current activity
        active_device = None
        for name, dr in self._device_repair_states.items():
            if dr["status"] == REPAIR_IN_PROGRESS:
                active_device = name
                break

        if active_device:
            detail = f"Repairing {active_device}"
        elif self._repair_status == REPAIR_PENDING:
            detail = "Auto-repair pending"
        else:
            failed_count = sum(
                1
                for dr in self._device_repair_states.values()
                if dr["status"] == REPAIR_FAILED
            )
            if failed_count:
                detail = f"{failed_count} device(s) repair failed"
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
                    "status": dr["status"],
                    "detail": dr["detail"],
                    "last_repair_attempt": dr["last_repair_attempt"],
                }
                for name, dr in self._device_repair_states.items()
            },
        }
