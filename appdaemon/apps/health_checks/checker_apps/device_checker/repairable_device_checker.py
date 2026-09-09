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
from shared.auto_repair_config import AutoRepairConfigMixin
from shared.check_utils import apply_cross_check

logger = logging.getLogger(__name__)

REPAIR_IDLE = "idle"
REPAIR_PENDING = "pending"
REPAIR_IN_PROGRESS = "in_progress"
REPAIR_SUCCESS = "success"
REPAIR_FAILED = "failed"

REPAIR_POLL_INTERVAL_S = 5


class RepairableDeviceChecker(AutoRepairConfigMixin, BasicDeviceChecker):
    """BasicDeviceChecker with smart-switch power-cycle repair support."""

    DELAY_MIN_MIN = 1
    DELAY_MIN_MAX = 60
    DELAY_MIN_DEFAULT = 5

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
        self._init_auto_repair_config(args)

        # Repair state machine
        self._repair_status: str = REPAIR_IDLE
        self._repair_detail: str = ""
        self._auto_repair_deadline: Optional[datetime.datetime] = None
        self._last_repair_attempt: Optional[str] = None
        self._unhealthy_since: Optional[datetime.datetime] = None
        self._repair_task: Optional[asyncio.Task] = None

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
        await self._provision_auto_repair_helpers(prov)

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
        # Extend the base payload (which also drains any pending
        # repair_events) with repair_state.
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
            self._start_repair()
        elif action == "cancel_repair":
            self._cancel_repair()
        elif action == "update_repair_config":
            self._handle_repair_config_command(data)
        else:
            self.log(f"Unknown repair action {action!r}", level="WARNING")

    def _cancel_repair(self) -> None:
        """Stand down a scheduled repair, without ending the outage.

        The card offers Cancel for any checker sitting at ``pending``, so
        without this arm the tap was accepted by the controller and then
        silently dropped here. This checker re-arms every cycle, so dropping
        back to ``idle`` alone would let the countdown fire on the very next
        tick — the dwell clock is restarted instead, giving a real deferral of
        one full auto-repair delay.
        """
        if self._repair_status != REPAIR_PENDING:
            self.log(
                f"Cannot cancel repair — status is {self._repair_status}",
                level="WARNING",
            )
            return
        self._repair_status = REPAIR_IDLE
        self._auto_repair_deadline = None
        self._unhealthy_since = datetime.datetime.now()
        _, delay_min = self._read_auto_repair_config()
        self._repair_detail = f"Cancelled by user — deferred {delay_min}m"
        self.log(
            f"Auto-repair cancelled by user — dwell restarted ({delay_min}m)",
            level="INFO",
        )
        self._report_repair_status_only()

    # ------------------------------------------------------------------
    # Auto-repair logic
    # ------------------------------------------------------------------

    def _evaluate_auto_repair(self, results: List[Dict[str, str]]) -> None:
        all_ok = all(r["status"] == "ok" for r in results)
        any_bad = any(r["status"] in ("critical", "degraded") for r in results)

        if all_ok:
            if self._repair_status == REPAIR_PENDING:
                self.log("All checks ok — cancelling pending auto-repair", level="INFO")
            if self._repair_status == REPAIR_FAILED:
                self.log("All checks ok — clearing failed repair state", level="INFO")
            if self._repair_status in (REPAIR_PENDING, REPAIR_SUCCESS, REPAIR_FAILED):
                self._repair_status = REPAIR_IDLE
                self._repair_detail = ""
                self._auto_repair_deadline = None
                self._unhealthy_since = None
            return

        if self._repair_status == REPAIR_SUCCESS:
            return

        if not any_bad:
            return

        enabled, delay_min = self._read_auto_repair_config()
        if not enabled:
            # Keep the outage clock running, but stand any countdown down.
            # A PENDING left up here is not cosmetic: the card counts down to
            # a repair that can never start, and alertmanager_bridge holds
            # the critical page for up to repair_hold_cap_s on `pending` —
            # withholding the page for an outage the operator has just said
            # will not self-heal.
            if self._repair_status == REPAIR_PENDING:
                self.log(
                    "Auto-repair disabled — cancelling pending auto-repair",
                    level="INFO",
                )
                self._repair_status = REPAIR_IDLE
                self._repair_detail = "Auto-repair disabled"
                self._auto_repair_deadline = None
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
                    self._pending_repair_events.append({
                        "result": "success",
                        "duration_s": elapsed,
                    })
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
            self._pending_repair_events.append({
                "result": "failed",
                "duration_s": self._repair_recovery_wait_s,
            })
            self._report_repair_status_only()

        except Exception as exc:
            self._repair_status = REPAIR_FAILED
            self._repair_detail = f"Repair error: {exc}"
            self.log(f"Repair error: {exc!r}", level="ERROR")
            # Duration is genuinely unknown here — the failure can occur at
            # any point in the sequence, so duration_s is omitted.
            self._pending_repair_events.append({"result": "failed"})
            self._report_repair_status_only()

    def _report_repair_status_only(self) -> None:
        # Route through _build_report_payload so any repair_event queued for
        # this conclusion (see _execute_repair) is delivered immediately.
        self.fire_event(
            "health_check_command",
            command="report_status",
            payload=json.dumps(self._build_report_payload([])),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_repair_state(self) -> Dict[str, Any]:
        return {
            "status": self._repair_status,
            "detail": self._repair_detail,
            **self._auto_repair_state_fields(),
            "auto_repair_deadline": (
                self._auto_repair_deadline.isoformat(timespec="seconds")
                if self._auto_repair_deadline
                else None
            ),
            "last_repair_attempt": self._last_repair_attempt,
        }
