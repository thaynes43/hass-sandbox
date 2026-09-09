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

#: States that mean "no usable reading", not a real value.
UNAVAILABLE_STATES = ("unavailable", "unknown", "none", "")

#: Bounds of the auto-repair delay helper. Every path that can set the cached
#: delay clamps to these, and the ensure_helper call uses them too, so the
#: clamp and the helper can never drift apart. HA silently rejects a set_value
#: outside the helper's range without AppDaemon raising, so an unclamped cache
#: would hold a value the helper never accepted.
DELAY_MIN_MIN = 1
DELAY_MIN_MAX = 60


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
        #: None until the first read attempt; then whether it succeeded.
        #: Drives the transition-only logging in _refresh_auto_repair_config.
        self._toggle_readable: Optional[bool] = None
        self._delay_readable: Optional[bool] = None
        #: Latches the out-of-range warning so _clamp_delay says it once per
        #: episode instead of every check cycle. Must exist before the first
        #: _clamp_delay call below.
        self._delay_clamped_logged: bool = False
        # Clamped here so every path that can set the cached delay obeys
        # the helper's bounds. Without this, an out-of-range
        # auto_repair_delay_min_default would be honoured verbatim for the
        # whole first run (a 0 collapses the dwell gate entirely) — the
        # same failure the card clamp prevents, through a different door.
        self._auto_repair_delay_min_default: int = self._clamp_delay(
            int(args.get("auto_repair_delay_min_default", 5))
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
                entity_id = f"input_boolean.{self._checker_id}_health_auto_repair"
                self.log(f"Provisioned {entity_id}", level="INFO")
                # A freshly created input_boolean is off. The unreadable-helper
                # guard in _refresh_auto_repair_config honours
                # auto_repair_enabled_default for the whole first run, so
                # without this the toggle would silently flip itself off the
                # moment the helper became readable after the next restart —
                # auto-repair disabled one deploy later, with nothing to say
                # so. Only applied on creation: a later manual "off" is never
                # overridden.
                if self._auto_repair_enabled_default:
                    try:
                        self.call_service(
                            "input_boolean/turn_on", entity_id=entity_id
                        )
                        self.log(
                            f"Auto-repair default-enabled via {entity_id}",
                            level="INFO",
                        )
                    except Exception as exc:
                        self.log(
                            f"Failed to default-enable auto-repair on "
                            f"{entity_id}: {exc!r}",
                            level="WARNING",
                        )
        except Exception as exc:
            self.log(f"Failed to provision auto-repair toggle: {exc!r}", level="ERROR")

        try:
            created = await prov.ensure_helper(
                "input_number",
                f"{self._checker_id} Health Auto Repair Delay",
                min=DELAY_MIN_MIN, max=DELAY_MIN_MAX, step=1,
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
        elif action == "update_repair_config":
            self.log(
                f"Repair config update requested: "
                f"auto_repair_enabled={data.get('auto_repair_enabled')}, "
                f"auto_repair_delay_min={data.get('auto_repair_delay_min')}",
                level="INFO",
            )
            self._update_repair_config(data)

    # ------------------------------------------------------------------
    # Auto-repair logic
    # ------------------------------------------------------------------

    async def _refresh_auto_repair_config(self) -> None:
        """Refresh the cached toggle/delay from their HA helpers.

        A read that comes back ``None`` means AppDaemon does not know the
        entity — the normal state for the whole first run after these helpers
        are provisioned, because AppDaemon loads its entity list at startup
        and the helpers did not exist then. ``str(None) == "on"`` is False, so
        treating that as a real read silently disables auto-repair until the
        next pod restart, with nothing in the logs to say so (the 1.17.0
        Z-Wave failure). An unknown value is not evidence, so the previous
        cached value is kept — which on the first run is
        ``auto_repair_enabled_default``.
        """
        try:
            entity_id = f"input_boolean.{self._checker_id}_health_auto_repair"
            enabled_state = await self.get_state(entity_id)
            readable = (
                enabled_state is not None
                and str(enabled_state).lower() not in UNAVAILABLE_STATES
            )
            if readable:
                self._cached_auto_repair_enabled = str(enabled_state) == "on"
            # Log the transitions, not every cycle: an operator needs the
            # window to have a visible open and close, without a message
            # every check_interval_s for as long as it lasts.
            if readable != self._toggle_readable:
                # A clean start with a readable helper is not the *close* of
                # an unreadable window — only announce that if one was open.
                if readable and self._toggle_readable is None:
                    pass
                elif readable:
                    self.log(
                        f"{entity_id} is readable again — auto-repair "
                        f"{'enabled' if self._cached_auto_repair_enabled else 'disabled'} "
                        f"from the helper",
                        level="INFO",
                    )
                else:
                    self.log(
                        f"{entity_id} not readable (state={enabled_state!r}) — "
                        f"running on the cached default, auto-repair "
                        f"{'enabled' if self._cached_auto_repair_enabled else 'disabled'}",
                        level="WARNING",
                    )
                self._toggle_readable = readable
        except Exception as exc:
            self.log(f"Failed to read auto-repair toggle: {exc!r}", level="WARNING")

        try:
            entity_id = f"input_number.{self._checker_id}_health_auto_repair_delay"
            delay_state = await self.get_state(entity_id)
            delay_readable = (
                delay_state is not None
                and str(delay_state).lower() not in UNAVAILABLE_STATES
            )
            if delay_readable:
                self._cached_auto_repair_delay_min = self._clamp_delay(
                    int(float(delay_state))
                )
            if delay_readable != self._delay_readable:
                if delay_readable and self._delay_readable is None:
                    pass
                elif delay_readable:
                    self.log(
                        f"{entity_id} is readable again — auto-repair delay "
                        f"{self._cached_auto_repair_delay_min}m from the helper",
                        level="INFO",
                    )
                else:
                    self.log(
                        f"{entity_id} not readable (state={delay_state!r}) — "
                        f"using the cached default of "
                        f"{self._cached_auto_repair_delay_min}m",
                        level="WARNING",
                    )
                self._delay_readable = delay_readable
        except Exception as exc:
            self.log(f"Failed to read auto-repair delay: {exc!r}", level="WARNING")

    def _clamp_delay(self, value: int) -> int:
        """Clamp a delay to the helper's bounds, saying so when it bites.

        logging-standards puts "validation failure with fallback" at WARNING,
        and overriding what an operator asked for must not be silent. But the
        helper read runs every check cycle, so an out-of-range value sitting
        in the helper would otherwise warn every ``check_interval_s`` for as
        long as it sat there. The warning is therefore emitted once per
        out-of-range episode: ``_delay_clamped_logged`` latches it, and is
        cleared again the moment an in-range value is seen.
        """
        clamped = max(DELAY_MIN_MIN, min(DELAY_MIN_MAX, value))
        if clamped != value:
            if not self._delay_clamped_logged:
                self.log(
                    f"Auto-repair delay {value}m is outside the permitted "
                    f"{DELAY_MIN_MIN}-{DELAY_MIN_MAX}m range — using {clamped}m",
                    level="WARNING",
                )
                self._delay_clamped_logged = True
        else:
            self._delay_clamped_logged = False
        return clamped

    def _read_auto_repair_config(self) -> tuple[bool, int]:
        """Return cached auto-repair config (sync-safe)."""
        return self._cached_auto_repair_enabled, self._cached_auto_repair_delay_min

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
    # Repair config updates
    # ------------------------------------------------------------------

    def _update_repair_config(self, data: dict) -> None:
        """Apply a repair-config command from the health dashboard card.

        The command is authoritative for the cache, but only once the write
        has landed. While the helper is unreadable (see
        _refresh_auto_repair_config) the next get_state stays None for the
        rest of the run, so waiting for the read-back would leave an explicit
        choice unhonoured — the card would show off, HA would show off, and
        the repair would still fire. Caching a write that FAILED is the
        mirror-image bug, so every cache update sits in a success path.
        """
        auto_enabled = data.get("auto_repair_enabled")
        delay_min = data.get("auto_repair_delay_min")

        if auto_enabled is not None:
            entity_id = f"input_boolean.{self._checker_id}_health_auto_repair"
            current = str(self.get_state(entity_id))
            desired = "on" if auto_enabled else "off"
            if current == desired:
                self._cached_auto_repair_enabled = bool(auto_enabled)
            else:
                service = (
                    "input_boolean/turn_on" if auto_enabled
                    else "input_boolean/turn_off"
                )
                try:
                    self.call_service(service, entity_id=entity_id)
                    self._cached_auto_repair_enabled = bool(auto_enabled)
                except Exception as exc:
                    self.log(
                        f"Failed to update auto-repair toggle: {exc!r}",
                        level="ERROR",
                    )

        if delay_min is not None:
            entity_id = f"input_number.{self._checker_id}_health_auto_repair_delay"
            # Parse once, the same way the helper read does (int(float(...))),
            # so a card sending "17.0" is handled rather than fatal. An
            # unparseable value skips only the delay half of the command.
            try:
                desired_delay = self._clamp_delay(int(float(delay_min)))
            except (TypeError, ValueError):
                self.log(
                    f"Ignoring unparseable auto_repair_delay_min: {delay_min!r}",
                    level="WARNING",
                )
                desired_delay = None
            try:
                current_val = int(float(self.get_state(entity_id)))
            except (TypeError, ValueError):
                current_val = None
            if desired_delay is None:
                pass
            elif current_val == desired_delay:
                self._cached_auto_repair_delay_min = desired_delay
            else:
                try:
                    self.call_service(
                        "input_number/set_value",
                        entity_id=entity_id,
                        value=desired_delay,
                    )
                    self._cached_auto_repair_delay_min = desired_delay
                except Exception as exc:
                    self.log(
                        f"Failed to update auto-repair delay: {exc!r}",
                        level="ERROR",
                    )

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
