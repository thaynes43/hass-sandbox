"""Spa Health Checker — monitors a Gecko-integrated hot tub.

Performs four checks on a configurable interval:

1. **Gateway Ping** — ICMP ping the in.touch gateway IP
2. **Overall Connection** — verify the Gecko overall_connection binary sensor
3. **Transport Connection** — verify the Gecko transport_connection binary sensor
4. **Thermostat Staleness** — detect zombie state by checking how recently
   the thermostat entity was updated

Supports a repair action (power-cycle via a smart switch) with auto-repair
capability.  Repair config is persisted in self-provisioned HA helpers so it
survives AppDaemon restarts and can be adjusted from the Lovelace card.

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
from shared.check_utils import apply_cross_check, ping_check

logger = logging.getLogger(__name__)

# Repair state constants
REPAIR_IDLE = "idle"
REPAIR_PENDING = "pending"
REPAIR_IN_PROGRESS = "in_progress"
REPAIR_SUCCESS = "success"
REPAIR_FAILED = "failed"

REPAIR_POLL_INTERVAL_S = 5


class SpaHealthChecker(hass.Hass):
    """Health checker for a Gecko-integrated spa with repair support."""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        args = self.args or {}

        # Identity
        self._checker_id: str = args.get("checker_id", "spa")
        self._checker_name: str = args.get("checker_name", self._checker_id)

        # Gateway ping
        self._gateway_host: str = args.get("gateway_host", "")

        # Entity connectivity checks
        self._connection_entities: List[str] = args.get("connection_entities", [])

        # Staleness detection
        self._staleness_entity: str = args.get("staleness_entity", "")
        self._staleness_threshold_s: int = int(
            args.get("staleness_threshold_s", 300)
        )

        # Repair
        self._repair_switch: str = args.get("repair_switch", "")
        self._repair_recovery_wait_s: int = int(
            args.get("repair_recovery_wait_s", 300)
        )
        self._auto_repair_enabled_default: bool = bool(
            args.get("auto_repair_enabled_default", False)
        )
        self._auto_repair_delay_min_default: int = int(
            args.get("auto_repair_delay_min_default", 15)
        )

        # "health_dependencies" avoids collision with AppDaemon's built-in "dependencies"
        self._dependencies: List[dict] = args.get("health_dependencies", [])

        # Timing
        self._check_interval_s: int = int(args.get("check_interval_s", 120))

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

        self.log(
            f"SpaHealthChecker initialising: id={self._checker_id}, "
            f"gateway={self._gateway_host}, "
            f"staleness_entity={self._staleness_entity}, "
            f"repair_switch={self._repair_switch}",
            level="INFO",
        )

        self.run_in(self._on_startup, 0)

    def _on_startup(self, kwargs: Any) -> None:
        self.create_task(self._async_startup())

    async def _async_startup(self) -> None:
        await self._provision_entities()
        await self._refresh_auto_repair_config()
        self._register()

        # Listen for controller ready (re-register if controller restarts)
        self.listen_event(
            self._on_controller_ready, "health_check_controller_ready"
        )
        # Listen for force-recheck requests
        self.listen_event(self._on_recheck, "health_check_recheck")
        # Listen for repair commands from controller
        self.listen_event(
            self._on_repair_command,
            f"health_check_repair_{self._checker_id}",
        )

        # Run first check after short delay, then start periodic timer
        self.run_in(self._first_check, 5)

        self.log(
            f"SpaHealthChecker '{self._checker_name}' started", level="INFO"
        )

    async def _provision_entities(self) -> None:
        """Create auto-repair helper entities if they don't exist."""
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
                # Set default value
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
            self.log(
                f"Failed to provision auto-repair delay helper: {exc!r}",
                level="ERROR",
            )

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def _register(self) -> None:
        check_names = self._build_check_names()
        payload: dict = {
            "checker_id": self._checker_id,
            "checker_name": self._checker_name,
            "check_names": check_names,
            "supports_repair": True,
            "repair_state": self._build_repair_state(),
        }
        if self._dependencies:
            payload["dependencies"] = self._dependencies
        self.fire_event(
            "health_check_command",
            command="register_checker",
            payload=json.dumps(payload),
        )
        self.log(
            f"Registered '{self._checker_name}' with checks: {check_names}",
            level="INFO",
        )

    def _build_check_names(self) -> List[str]:
        names = []
        if self._gateway_host:
            names.append("Gateway Ping")
        for entity_id in self._connection_entities:
            # Derive a friendly name from the entity ID
            # e.g. binary_sensor.westford_spa_overall_connection → Overall Connection
            short = entity_id.split(".")[-1]
            # Strip prefix up to and including "spa_"
            if "_spa_" in short:
                short = short.split("_spa_", 1)[1]
            friendly = short.replace("_", " ").title()
            names.append(friendly)
        if self._staleness_entity:
            names.append("Thermostat Staleness")
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
            self.log("Manual repair requested", level="INFO")
            self._start_repair()
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
        """Execute all configured checks and report results."""
        await self._refresh_auto_repair_config()
        results: List[Dict[str, str]] = []

        # 1. Gateway ping
        if self._gateway_host:
            result = await self._check_gateway_ping()
            results.append(result)

        # 2. Connection entity checks
        for entity_id in self._connection_entities:
            result = await self._check_connection_entity(entity_id)
            results.append(result)

        # 3. Staleness detection
        if self._staleness_entity:
            result = await self._check_staleness()
            results.append(result)

        # Evaluate auto-repair logic (skip if repair is already in progress)
        if self._repair_status not in (REPAIR_IN_PROGRESS,):
            self._evaluate_auto_repair(results)

        # Cross-check: downgrade critical→warning for partial failures
        # (after auto-repair eval so repair triggers see raw statuses)
        apply_cross_check(results)

        # Report to controller
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

    async def _check_gateway_ping(self) -> Dict[str, str]:
        try:
            result = await ping_check(self._gateway_host)
            return {
                "name": "Gateway Ping",
                "status": result["status"],
                "detail": result["detail"],
            }
        except Exception as exc:
            self.log(f"Gateway ping failed: {exc!r}", level="ERROR")
            return {
                "name": "Gateway Ping",
                "status": "critical",
                "detail": f"Error: {exc}",
            }

    async def _check_connection_entity(
        self, entity_id: str
    ) -> Dict[str, str]:
        # Derive friendly name
        short = entity_id.split(".")[-1]
        if "_spa_" in short:
            short = short.split("_spa_", 1)[1]
        friendly = short.replace("_", " ").title()

        try:
            state = await self.get_state(entity_id)
            if state is None:
                return {
                    "name": friendly,
                    "status": "critical",
                    "detail": "Entity not found",
                }
            if str(state) == "on":
                return {"name": friendly, "status": "ok", "detail": "connected"}
            return {
                "name": friendly,
                "status": "critical",
                "detail": f"State: {state}",
            }
        except Exception as exc:
            self.log(f"Connection entity check failed for {entity_id}: {exc!r}", level="ERROR")
            return {
                "name": friendly,
                "status": "critical",
                "detail": f"Error: {exc}",
            }

    async def _check_staleness(self) -> Dict[str, str]:
        try:
            attrs = await self.get_state(self._staleness_entity, attribute="all")
            if attrs is None:
                return {
                    "name": "Thermostat Staleness",
                    "status": "critical",
                    "detail": "Entity not found",
                }

            last_updated = attrs.get("last_updated", "")
            if not last_updated:
                return {
                    "name": "Thermostat Staleness",
                    "status": "critical",
                    "detail": "No last_updated",
                }

            # Parse ISO timestamp from HA
            if isinstance(last_updated, str):
                # HA returns ISO format, may have +00:00 timezone
                lu_dt = datetime.datetime.fromisoformat(last_updated)
                if lu_dt.tzinfo is not None:
                    lu_dt = lu_dt.replace(tzinfo=None)
            else:
                lu_dt = last_updated

            age_s = (datetime.datetime.utcnow() - lu_dt).total_seconds()

            if age_s <= self._staleness_threshold_s:
                return {
                    "name": "Thermostat Staleness",
                    "status": "ok",
                    "detail": f"Updated {int(age_s)}s ago",
                }
            return {
                "name": "Thermostat Staleness",
                "status": "critical",
                "detail": f"Stale: {int(age_s)}s (threshold {self._staleness_threshold_s}s)",
            }
        except Exception as exc:
            self.log(f"Staleness check failed: {exc!r}", level="ERROR")
            return {
                "name": "Thermostat Staleness",
                "status": "critical",
                "detail": f"Error: {exc}",
            }

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
        """Evaluate whether to start, continue, or cancel auto-repair."""
        all_ok = all(r["status"] == "ok" for r in results)
        any_bad = any(r["status"] in ("critical", "degraded") for r in results)

        # If all checks pass, cancel any pending repair and reset
        if all_ok:
            if self._repair_status == REPAIR_PENDING:
                self.log("All checks ok — cancelling pending auto-repair", level="INFO")
            if self._repair_status in (REPAIR_PENDING, REPAIR_SUCCESS):
                self._repair_status = REPAIR_IDLE
                self._repair_detail = ""
                self._auto_repair_deadline = None
                self._unhealthy_since = None
            return

        # Don't trigger auto-repair from "failed" state (no auto-retry)
        if self._repair_status == REPAIR_FAILED:
            return

        # Don't trigger auto-repair from "success" state (waiting for checks to clear)
        if self._repair_status == REPAIR_SUCCESS:
            return

        # Only trigger on actual critical/degraded, not unknown
        if not any_bad:
            return

        enabled, delay_min = self._read_auto_repair_config()
        if not enabled:
            # Track unhealthy time but don't act
            if self._unhealthy_since is None:
                self._unhealthy_since = datetime.datetime.now()
            return

        now = datetime.datetime.now()

        if self._repair_status == REPAIR_IDLE:
            # Start tracking unhealthy duration
            if self._unhealthy_since is None:
                self._unhealthy_since = now
            deadline = self._unhealthy_since + datetime.timedelta(minutes=delay_min)
            if now >= deadline:
                self.log(
                    f"Unhealthy for >{delay_min}m — starting auto-repair",
                    level="INFO",
                )
                self._start_repair()
            else:
                self._repair_status = REPAIR_PENDING
                self._auto_repair_deadline = deadline
                self._repair_detail = f"Auto-repair at {deadline.isoformat(timespec='seconds')}"
                self.log(
                    f"Repair pending — deadline {deadline.isoformat(timespec='seconds')}",
                    level="INFO",
                )

        elif self._repair_status == REPAIR_PENDING:
            # Check if deadline has been reached
            if self._auto_repair_deadline and now >= self._auto_repair_deadline:
                self.log("Auto-repair deadline reached — starting repair", level="INFO")
                self._start_repair()

    # ------------------------------------------------------------------
    # Repair execution
    # ------------------------------------------------------------------

    def _start_repair(self) -> None:
        """Initiate a repair (power cycle)."""
        if self._repair_status == REPAIR_IN_PROGRESS:
            self.log("Repair already in progress — ignoring", level="WARNING")
            return

        if not self._repair_switch:
            self.log("No repair_switch configured — cannot repair", level="WARNING")
            self._repair_status = REPAIR_FAILED
            self._repair_detail = "No repair switch configured"
            return

        self._repair_status = REPAIR_IN_PROGRESS
        self._repair_detail = "Power cycling..."
        self._auto_repair_deadline = None
        self._last_repair_attempt = datetime.datetime.now().isoformat(
            timespec="seconds"
        )

        # Report status immediately so the card shows in_progress
        self._report_repair_status_only()

        self._repair_task = self.create_task(self._execute_repair())

    async def _execute_repair(self) -> None:
        """Power cycle the spa and poll for recovery."""
        try:
            # Turn off
            self.log(f"Turning off {self._repair_switch}", level="INFO")
            self.call_service(
                "switch/turn_off",
                entity_id=self._repair_switch,
            )

            await asyncio.sleep(10)

            # Turn on
            self.log(f"Turning on {self._repair_switch}", level="INFO")
            self.call_service(
                "switch/turn_on",
                entity_id=self._repair_switch,
            )

            self._repair_detail = "Waiting for recovery..."
            self._report_repair_status_only()

            # Poll for recovery
            elapsed = 0
            while elapsed < self._repair_recovery_wait_s:
                await asyncio.sleep(REPAIR_POLL_INTERVAL_S)
                elapsed += REPAIR_POLL_INTERVAL_S

                results = await self._run_health_checks_only()
                all_ok = all(r["status"] == "ok" for r in results)

                if all_ok:
                    self._repair_status = REPAIR_SUCCESS
                    self._repair_detail = (
                        f"Recovered after {elapsed}s"
                    )
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

            # Timed out — repair failed
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
            self.log(f"Repair execution error: {exc!r}", level="ERROR")
            self._report_repair_status_only()

    async def _run_health_checks_only(self) -> List[Dict[str, str]]:
        """Run all health checks and return results (without reporting)."""
        results: List[Dict[str, str]] = []
        if self._gateway_host:
            results.append(await self._check_gateway_ping())
        for entity_id in self._connection_entities:
            results.append(await self._check_connection_entity(entity_id))
        if self._staleness_entity:
            results.append(await self._check_staleness())
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
    # Repair config updates (from card via controller)
    # ------------------------------------------------------------------

    def _update_repair_config(self, data: dict) -> None:
        """Update auto-repair HA helpers from card settings."""
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
                    self.log(f"Auto-repair {'enabled' if auto_enabled else 'disabled'}", level="INFO")
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

    def _build_repair_state(self) -> Dict[str, Any]:
        """Build the repair state dict for inclusion in status reports."""
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
