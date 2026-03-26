"""Repairable Device Checker — extends BasicDeviceChecker with repair support.

Adds a repair state machine and auto-repair via power-cycling a smart switch.
The repair action is: turn off the switch, wait, turn on, then poll checks
for recovery.

Reusable for any device that can be recovered by toggling a smart switch.
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
from health_checks.checker_apps.device_checker.device_checker import (
    BasicDeviceChecker,
)
from shared.check_utils import apply_cross_check

logger = logging.getLogger(__name__)

REPAIR_IDLE = "idle"
REPAIR_PENDING = "pending"
REPAIR_IN_PROGRESS = "in_progress"
REPAIR_SUCCESS = "success"
REPAIR_FAILED = "failed"

REPAIR_POLL_INTERVAL_S = 5


class RepairableDeviceChecker(BasicDeviceChecker):
    """BasicDeviceChecker with smart-switch power-cycle repair support."""

    # ------------------------------------------------------------------
    # Lifecycle (extends parent)
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        super().initialize()

        args = self.args or {}

        # Repair config
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

        # Repair state machine
        self._repair_status: str = REPAIR_IDLE
        self._repair_detail: str = ""
        self._auto_repair_deadline: Optional[datetime.datetime] = None
        self._last_repair_attempt: Optional[str] = None
        self._unhealthy_since: Optional[datetime.datetime] = None
        self._repair_task: Optional[asyncio.Task] = None

        # Cached auto-repair config (updated each async check cycle)
        self._cached_auto_repair_enabled: bool = self._auto_repair_enabled_default
        self._cached_auto_repair_delay_min: int = self._auto_repair_delay_min_default

    async def _async_startup(self) -> None:
        await self._provision_repair_helpers()
        await self._refresh_auto_repair_config()

        # Register with supports_repair (override parent registration)
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
            f"RepairableDeviceChecker '{self._checker_name}' started",
            level="INFO",
        )

    async def _provision_repair_helpers(self) -> None:
        ha_url = self.args.get("ha_url")
        ha_token_env = self.args.get("ha_token_env")
        if not ha_url or not ha_token_env:
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
                min=1, max=60, step=1,
                unit_of_measurement="min", mode="box",
            )
            if created:
                entity_id = f"input_number.{self._checker_id}_health_auto_repair_delay"
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
            self.log(f"Failed to provision auto-repair delay: {exc!r}", level="ERROR")

    # ------------------------------------------------------------------
    # Registration (override parent to add supports_repair)
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
            f"Registered '{self._checker_name}' with checks: {check_names}",
            level="INFO",
        )

    # ------------------------------------------------------------------
    # Check execution (override to add repair logic)
    # ------------------------------------------------------------------

    async def _run_checks(self) -> None:
        await self._refresh_auto_repair_config()
        results = await self._run_checks_only()

        # Evaluate auto-repair (skip if repair in progress)
        if self._repair_status != REPAIR_IN_PROGRESS:
            self._evaluate_auto_repair(results)

        # Cross-check: downgrade critical→warning for partial failures
        # (after auto-repair eval so repair triggers see raw statuses)
        apply_cross_check(results)

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
        return {
            "checker_id": self._checker_id,
            "results": results,
            "repair_state": self._build_repair_state(),
        }

    # ------------------------------------------------------------------
    # Repair event handler
    # ------------------------------------------------------------------

    def _on_repair_command(
        self, event_name: str, data: dict, kwargs: Any
    ) -> None:
        action = data.get("action", "")
        if action == "start_repair":
            self.log("Manual repair requested", level="INFO")
            self._start_repair()
        elif action == "update_repair_config":
            self._update_repair_config(data)

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
        all_ok = all(r["status"] == "ok" for r in results)
        any_bad = any(r["status"] in ("critical", "degraded") for r in results)

        if all_ok:
            if self._repair_status == REPAIR_PENDING:
                self.log("All checks ok — cancelling pending auto-repair", level="INFO")
            if self._repair_status in (REPAIR_PENDING, REPAIR_SUCCESS):
                self._repair_status = REPAIR_IDLE
                self._repair_detail = ""
                self._auto_repair_deadline = None
                self._unhealthy_since = None
            return

        if self._repair_status == REPAIR_FAILED:
            return

        if self._repair_status == REPAIR_SUCCESS:
            return

        if not any_bad:
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

        if self._repair_status == REPAIR_IDLE:
            if now >= deadline:
                self.log(
                    f"Unhealthy for >{delay_min}m — starting auto-repair",
                    level="INFO",
                )
                self._start_repair()
            else:
                self._repair_status = REPAIR_PENDING
                self._auto_repair_deadline = deadline
                self._repair_detail = (
                    f"Auto-repair at {deadline.isoformat(timespec='seconds')}"
                )
        elif self._repair_status == REPAIR_PENDING:
            if self._auto_repair_deadline and now >= self._auto_repair_deadline:
                self.log("Auto-repair deadline reached", level="INFO")
                self._start_repair()

    # ------------------------------------------------------------------
    # Repair execution
    # ------------------------------------------------------------------

    def _start_repair(self) -> None:
        if self._repair_status == REPAIR_IN_PROGRESS:
            self.log("Repair already in progress — ignoring", level="WARNING")
            return

        if not self._repair_switch:
            self._repair_status = REPAIR_FAILED
            self._repair_detail = "No repair switch configured"
            return

        self._repair_status = REPAIR_IN_PROGRESS
        self._repair_detail = "Power cycling..."
        self._auto_repair_deadline = None
        self._last_repair_attempt = datetime.datetime.now().isoformat(
            timespec="seconds"
        )

        self._report_repair_status_only()
        self._repair_task = self.create_task(self._execute_repair())

    async def _execute_repair(self) -> None:
        try:
            self.log(f"Turning off {self._repair_switch}", level="INFO")
            self.call_service(
                "switch/turn_off", entity_id=self._repair_switch
            )

            await asyncio.sleep(self._repair_off_duration_s)

            self.log(f"Turning on {self._repair_switch}", level="INFO")
            self.call_service(
                "switch/turn_on", entity_id=self._repair_switch
            )

            self._repair_detail = "Waiting for recovery..."
            self._report_repair_status_only()

            elapsed = 0
            while elapsed < self._repair_recovery_wait_s:
                await asyncio.sleep(REPAIR_POLL_INTERVAL_S)
                elapsed += REPAIR_POLL_INTERVAL_S

                results = await self._run_checks_only()
                if all(r["status"] == "ok" for r in results):
                    self._repair_status = REPAIR_SUCCESS
                    self._repair_detail = f"Recovered after {elapsed}s"
                    self._unhealthy_since = None
                    self.log(
                        f"Repair successful — recovered after {elapsed}s",
                        level="INFO",
                    )
                    self._report_repair_status_only()
                    return

                self._repair_detail = (
                    f"Waiting for recovery... {elapsed}s/{self._repair_recovery_wait_s}s"
                )

            self._repair_status = REPAIR_FAILED
            self._repair_detail = (
                f"Did not recover after {self._repair_recovery_wait_s}s"
            )
            self.log(
                f"Repair failed — no recovery after {self._repair_recovery_wait_s}s",
                level="WARNING",
            )
            self._report_repair_status_only()

        except Exception as exc:
            self._repair_status = REPAIR_FAILED
            self._repair_detail = f"Repair error: {exc}"
            self.log(f"Repair error: {exc!r}", level="ERROR")
            self._report_repair_status_only()

    def _report_repair_status_only(self) -> None:
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
                        "input_number/set_value", entity_id=entity_id, value=int(delay_min)
                    )
                except Exception as exc:
                    self.log(f"Failed to update auto-repair delay: {exc!r}", level="ERROR")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_repair_state(self) -> Dict[str, Any]:
        enabled, delay_min = self._read_auto_repair_config()
        return {
            "status": self._repair_status,
            "detail": self._repair_detail,
            "auto_repair_enabled": enabled,
            "auto_repair_delay_min": delay_min,
            "auto_repair_deadline": (
                self._auto_repair_deadline.isoformat(timespec="seconds")
                if self._auto_repair_deadline
                else None
            ),
            "last_repair_attempt": self._last_repair_attempt,
        }
