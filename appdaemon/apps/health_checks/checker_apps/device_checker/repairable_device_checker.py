"""Repairable Device Checker — extends BasicDeviceChecker with repair support.

Adds a repair state machine and auto-repair via power-cycling a smart switch.
The repair action is: turn off the switch, wait, turn on and confirm the
switch reports on (``shared/switch_power_cycle.py``), then poll checks for
recovery.  A switch that will not come back on ends the repair as failed
straight away, without the recovery wait.

Reusable for any device that can be recovered by toggling a smart switch.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import sys
import time
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
from shared.switch_power_cycle import (
    power_cycle_switch,
    switch_not_on_detail,
    with_note,
)

logger = logging.getLogger(__name__)

REPAIR_IDLE = "idle"
REPAIR_PENDING = "pending"
REPAIR_IN_PROGRESS = "in_progress"
REPAIR_SUCCESS = "success"
REPAIR_FAILED = "failed"

REPAIR_POLL_INTERVAL_S = 5

#: Check statuses that count as recovered while polling after a power cycle.
#: `warning` is included for one reason only: BasicDeviceChecker's checks
#: produce it solely when ping_host does not resolve and ping_fallback_host
#: answers — the device is up, DNS is not — and a DNS outage must not turn a
#: power cycle that worked into "did not recover".
_RECOVERED_STATUSES = ("ok", "warning")


class RepairableDeviceChecker(AutoRepairConfigMixin, BasicDeviceChecker):
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
        # "Healthy" uses the same set as the recovery wait: a `warning` here can
        # only be the DNS fallback answering (the device is up, its name is
        # not). Treating it as healthy also stands down a `pending` repair, so
        # a countdown cannot outlive a recovery that happened during a DNS
        # outage and fire with no dwell on the next single bad cycle.
        all_ok = all(r["status"] in _RECOVERED_STATUSES for r in results)
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

        enabled, delay_min = self._read_auto_repair_config()
        if not enabled:
            # Stand the ladder down BEFORE the early returns below, not after
            # them: a checker parked at `success`, or one whose results are
            # all warnings, takes one of those returns on every cycle and
            # would otherwise hold the critical page on a repair state
            # auto-repair is no longer allowed to reach.
            self._stand_down_pending_repair("Auto-repair disabled")

        if self._repair_status == REPAIR_SUCCESS and any_bad:
            # Gated on any_bad (critical/degraded), not on "not all ok": an
            # `unknown` (no data) or a `warning` (the DNS-fallback case: the
            # name did not resolve but the device answered on its IP) says
            # nothing bad about the device, so it must not turn a good repair
            # into a failure. Such a cycle falls through to the `not any_bad`
            # return below and arms nothing. (An unresolvable name without a
            # ping_fallback_host is critical, and does count.)
            #
            # A relapse: the device is bad again before a fully healthy cycle
            # (that returned above). `success` was judged moments after the
            # power cycle, and left standing it is one of the bridge's
            # repair-hold states — the page would be withheld for a device
            # that is down again. `failed`, not `idle`: this outage has had
            # its one repair, `idle` would re-arm another, and `failed` is
            # what releases the page. It clears on the next all-ok cycle.
            # Mirrors RepairableDeviceGroupChecker._demote_stale_success.
            self._repair_status = REPAIR_FAILED
            self._repair_detail = (
                "Relapsed after a successful repair — recovery did not stick"
            )
            self.log(
                f"'{self._checker_name}' relapsed after a successful repair — "
                f"marking the repair failed (no second auto-repair this outage)",
                level="WARNING",
            )
            return

        if not any_bad:
            return

        if not enabled:
            # Keep the outage clock running: the dwell is measured from when
            # the outage started, not from when auto-repair was re-enabled.
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
            cycle = await power_cycle_switch(
                self, self._repair_switch, self._repair_off_duration_s
            )
            if not cycle.switch_on:
                # The helper has logged the ERROR. Waiting for recovery would
                # only burn the recovery window on an unpowered device.
                self._repair_status = REPAIR_FAILED
                self._repair_detail = switch_not_on_detail(self._repair_switch, cycle.note)
                # No duration_s: the recovery wait never started.
                self._pending_repair_events.append({"result": "failed"})
                self._report_repair_status_only()
                return

            # The recovery clock starts only now, with the switch confirmed on.
            self._repair_detail = "Waiting for recovery..."
            self._report_repair_status_only()

            # Wall-clock time, not just the sleeps: each iteration's checks
            # take time too (a timed-out ping is seconds), and counting only
            # the sleeps let a "300 s" wait run for many minutes. The sleep
            # term keeps it advancing when sleeps are patched out in tests.
            started = time.monotonic()
            elapsed = 0
            while elapsed < self._repair_recovery_wait_s:
                await asyncio.sleep(REPAIR_POLL_INTERVAL_S)
                elapsed = max(
                    elapsed + REPAIR_POLL_INTERVAL_S,
                    int(time.monotonic() - started),
                )

                results = await self._run_checks_only()
                if all(r["status"] in _RECOVERED_STATUSES for r in results):
                    self._repair_status = REPAIR_SUCCESS
                    self._repair_detail = f"Recovered after {elapsed}s"
                    self._unhealthy_since = None
                    # A note is not worth the card on success: log only.
                    self.log(
                        with_note(
                            f"Repair successful — recovered after {elapsed}s",
                            cycle.note,
                        ),
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
            # The note (e.g. the outlet never reported off) goes into the
            # detail, so the card and the Alertmanager description say it.
            self._repair_detail = with_note(
                f"Did not recover after {self._repair_recovery_wait_s}s",
                cycle.note,
            )
            self.log(
                with_note(
                    f"Repair failed — no recovery after "
                    f"{self._repair_recovery_wait_s}s",
                    cycle.note,
                ),
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
