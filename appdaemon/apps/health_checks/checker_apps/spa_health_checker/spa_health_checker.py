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

#: States that mean "no usable reading", not a real value.
UNAVAILABLE_STATES = ("unavailable", "unknown", "none", "")

#: Bounds of the auto-repair delay helper. Every path that can set the cached
#: delay clamps to these, and the ensure_helper call uses them too, so the
#: clamp and the helper can never drift apart. HA silently rejects a set_value
#: outside the helper's range without AppDaemon raising, so an unclamped cache
#: would hold a value the helper never accepted.
DELAY_MIN_MIN = 1
DELAY_MIN_MAX = 60


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

        # Staleness detection — support list (staleness_entities) or single (staleness_entity)
        staleness_list = args.get("staleness_entities", [])
        if not staleness_list and args.get("staleness_entity"):
            staleness_list = [args.get("staleness_entity")]
        self._staleness_entities: List[str] = staleness_list
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
            int(args.get("auto_repair_delay_min_default", 15))
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

        # How long to hold power off during a repair cycle. 10s proved too
        # short for a wedged in.touch3 (2026-08-26: 10s cut failed, 60s cut
        # recovered it), so default to a full minute.
        self._repair_power_off_s: int = int(args.get("repair_power_off_s", 60))

        # CrashLoopBackOff-style retry: a failed repair never ends the
        # episode. Each failure schedules the next attempt at
        # delay × 2^(n-1) minutes, capped here (default 6h). Attempts reset
        # on full recovery or manual repair.
        self._repair_backoff_max_min: int = int(
            args.get("repair_backoff_max_min", 360)
        )
        self._repair_attempts: int = 0
        self._next_retry_at: Optional[datetime.datetime] = None

        # Repair-conclusion events awaiting delivery to the controller
        # (drained into the next report_status payload — see
        # _record_repair_event / _drain_pending_repair_events).
        self._pending_repair_events: List[Dict[str, Any]] = []

        # Cached auto-repair config (updated each async check cycle)
        self._cached_auto_repair_enabled: bool = self._auto_repair_enabled_default
        self._cached_auto_repair_delay_min: int = self._auto_repair_delay_min_default

        self.log(
            f"SpaHealthChecker initialising: id={self._checker_id}, "
            f"gateway={self._gateway_host}, "
            f"staleness_entities={self._staleness_entities}, "
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
                min=DELAY_MIN_MIN,
                max=DELAY_MIN_MAX,
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
        if self._staleness_entities:
            names.append("Staleness")
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
            if self._repair_status == REPAIR_IN_PROGRESS:
                # _start_repair would ignore the request — don't let an
                # ignored tap wipe the backoff ladder mid-repair
                self.log("Repair already in progress — ignoring", level="WARNING")
                return
            # Manual repair starts a fresh backoff episode
            self._repair_attempts = 0
            self._next_retry_at = None
            self._start_repair()
        elif action == "cancel_repair":
            self._cancel_repair()
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
        if self._staleness_entities:
            result = await self._check_staleness()
            results.append(result)

        # Cross-check: downgrade critical→warning for partial failures
        # Must run BEFORE auto-repair eval so partial failures (warning)
        # do not trigger auto-repair — only genuine overall critical does.
        apply_cross_check(results)

        # Evaluate auto-repair logic (skip if repair is already in progress)
        if self._repair_status not in (REPAIR_IN_PROGRESS,):
            self._evaluate_auto_repair(results)

        # Report to controller
        payload: Dict[str, Any] = {
            "checker_id": self._checker_id,
            "results": results,
            "repair_state": self._build_repair_state(),
        }
        self._drain_pending_repair_events(payload)
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
        """Check staleness across all configured entities.

        Uses OR logic — if ANY entity is fresh, the check passes.
        Tracks the minimum (freshest) age across all entities.
        """
        min_age_s: Optional[float] = None
        freshest_name: str = ""

        for entity_id in self._staleness_entities:
            entity_name = entity_id.split(".")[-1]
            try:
                attrs = await self.get_state(entity_id, attribute="all")
                if attrs is None:
                    self.log(
                        f"Staleness: entity {entity_id} not found", level="WARNING"
                    )
                    continue

                last_updated = attrs.get("last_updated", "")
                if not last_updated:
                    self.log(
                        f"Staleness: entity {entity_id} has no last_updated",
                        level="WARNING",
                    )
                    continue

                # Parse ISO timestamp from HA
                if isinstance(last_updated, str):
                    lu_dt = datetime.datetime.fromisoformat(last_updated)
                    if lu_dt.tzinfo is not None:
                        lu_dt = lu_dt.replace(tzinfo=None)
                else:
                    lu_dt = last_updated

                age_s = (datetime.datetime.utcnow() - lu_dt).total_seconds()

                if min_age_s is None or age_s < min_age_s:
                    min_age_s = age_s
                    freshest_name = entity_name

            except Exception as exc:
                self.log(
                    f"Staleness check failed for {entity_id}: {exc!r}", level="ERROR"
                )

        if min_age_s is None:
            # All entities were missing or errored
            return {
                "name": "Staleness",
                "status": "critical",
                "detail": "Entity not found",
            }

        if min_age_s <= self._staleness_threshold_s:
            return {
                "name": "Staleness",
                "status": "ok",
                "detail": f"Freshest: {freshest_name} updated {int(min_age_s)}s ago",
            }

        n = len(self._staleness_entities)
        return {
            "name": "Staleness",
            "status": "critical",
            "detail": (
                f"All {n} entities stale "
                f"(freshest: {int(min_age_s)}s, threshold: {self._staleness_threshold_s}s)"
            ),
        }

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
        """Evaluate whether to start, continue, or cancel auto-repair.

        Called after apply_cross_check, so partial failures are already
        downgraded to 'warning'. Only a genuinely overall-critical situation
        (all checks failing → no cross-check downgrade) should trigger repair.
        """
        all_ok = all(r["status"] == "ok" for r in results)
        any_critical = any(r["status"] == "critical" for r in results)

        # If all checks pass, cancel any pending repair and reset
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
                self._repair_attempts = 0
                self._next_retry_at = None
            return

        # A success relapse (critical again before an all-ok cycle) starts a
        # fresh episode instead of trapping in SUCCESS forever (attempts were
        # already reset on success); fall through to the normal grace path.
        if self._repair_status == REPAIR_SUCCESS:
            self._repair_status = REPAIR_IDLE
            self._repair_detail = ""

        # Only trigger on actual critical (after cross-check, partial failures are warning)
        if not any_critical:
            # A non-critical interlude suspends the FAILED backoff clock:
            # keep the scheduled retry at least delay_min out so a later
            # return to critical must be sustained before a stale retry can
            # fire — never an instant power-cycle off an hours-old schedule.
            if self._repair_status == REPAIR_FAILED and self._next_retry_at:
                _, delay_min = self._read_auto_repair_config()
                floor = datetime.datetime.now() + datetime.timedelta(
                    minutes=delay_min
                )
                if self._next_retry_at < floor:
                    self._next_retry_at = floor
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

        elif self._repair_status == REPAIR_FAILED:
            # CrashLoopBackOff retry: a failed repair never ends the episode —
            # retry once the scheduled backoff expires. (No _next_retry_at
            # means the failure was unrepairable, e.g. no switch configured.)
            if self._next_retry_at and now >= self._next_retry_at:
                self.log(
                    f"Repair backoff expired (attempt {self._repair_attempts} "
                    "failed) — retrying repair",
                    level="INFO",
                )
                self._start_repair()

    # ------------------------------------------------------------------
    # Repair execution
    # ------------------------------------------------------------------

    def _record_repair_event(
        self,
        result: str,
        duration_s: Optional[float] = None,
        device: Optional[str] = None,
    ) -> None:
        """Buffer a repair-conclusion event for delivery on the next
        report_status payload (see _drain_pending_repair_events)."""
        event: Dict[str, Any] = {"result": result}
        if duration_s is not None:
            event["duration_s"] = duration_s
        if device:
            event["device"] = device
        self._pending_repair_events.append(event)

    def _drain_pending_repair_events(self, payload: Dict[str, Any]) -> None:
        """Attach any buffered repair_events to a report_status payload and
        clear the buffer so each event is delivered exactly once."""
        if self._pending_repair_events:
            payload["repair_events"] = self._pending_repair_events
            self._pending_repair_events = []

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

    def _cancel_repair(self) -> None:
        """Cancel a pending auto-repair."""
        if self._repair_status != REPAIR_PENDING:
            self.log(
                f"Cannot cancel repair — status is {self._repair_status}",
                level="WARNING",
            )
            return
        self.log("Auto-repair cancelled by user", level="INFO")
        self._repair_status = REPAIR_IDLE
        self._repair_detail = ""
        self._auto_repair_deadline = None
        self._unhealthy_since = None
        self._report_repair_status_only()

    async def _execute_repair(self) -> None:
        """Power cycle the spa and poll for recovery."""
        try:
            # Turn off
            self.log(f"Turning off {self._repair_switch}", level="INFO")
            self.call_service(
                "switch/turn_off",
                entity_id=self._repair_switch,
            )

            await asyncio.sleep(self._repair_power_off_s)

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
                    self._repair_attempts = 0
                    self._next_retry_at = None
                    self._record_repair_event("success", duration_s=elapsed)
                    self.log(
                        f"Repair successful — recovered after {elapsed}s",
                        level="INFO",
                    )
                    self._report_repair_status_only()
                    return

                self._repair_detail = (
                    f"Waiting for recovery... {elapsed}s/{self._repair_recovery_wait_s}s"
                )

            # Timed out — repair failed; schedule the next backoff retry
            self._register_repair_failure(
                f"Did not recover after {self._repair_recovery_wait_s}s"
            )
            self._record_repair_event(
                "failed", duration_s=self._repair_recovery_wait_s
            )
            self.log(
                f"Repair failed — no recovery after "
                f"{self._repair_recovery_wait_s}s (attempt "
                f"{self._repair_attempts}; next retry at "
                f"{self._next_retry_at.isoformat(timespec='seconds')})",
                level="WARNING",
            )
            self._report_repair_status_only()

        except Exception as exc:
            self._register_repair_failure(f"Repair error: {exc}")
            self._record_repair_event("failed")
            self.log(f"Repair execution error: {exc!r}", level="ERROR")
            self._report_repair_status_only()

    def _register_repair_failure(self, detail: str) -> None:
        """Mark the repair failed and schedule the next retry.

        CrashLoopBackOff semantics: the episode never ends on failure. The
        n-th failure schedules retry n+1 after delay × 2^(n-1) minutes,
        capped at repair_backoff_max_min. The counter resets only on full
        recovery or a manual repair.
        """
        self._repair_attempts += 1
        _, delay_min = self._read_auto_repair_config()
        backoff_min = min(
            delay_min * (2 ** (self._repair_attempts - 1)),
            self._repair_backoff_max_min,
        )
        self._next_retry_at = datetime.datetime.now() + datetime.timedelta(
            minutes=backoff_min
        )
        self._repair_status = REPAIR_FAILED
        self._repair_detail = (
            f"{detail} (attempt {self._repair_attempts}; retry at "
            f"{self._next_retry_at.strftime('%H:%M')})"
        )

    async def _run_health_checks_only(self) -> List[Dict[str, str]]:
        """Run all health checks and return results (without reporting)."""
        results: List[Dict[str, str]] = []
        if self._gateway_host:
            results.append(await self._check_gateway_ping())
        for entity_id in self._connection_entities:
            results.append(await self._check_connection_entity(entity_id))
        if self._staleness_entities:
            results.append(await self._check_staleness())
        return results

    def _report_repair_status_only(self) -> None:
        """Fire a status report with current repair state (no new check results)."""
        payload: Dict[str, Any] = {
            "checker_id": self._checker_id,
            "results": [],
            "repair_state": self._build_repair_state(),
        }
        self._drain_pending_repair_events(payload)
        self.fire_event(
            "health_check_command",
            command="report_status",
            payload=json.dumps(payload),
        )

    # ------------------------------------------------------------------
    # Repair config updates (from card via controller)
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
                    self.log(
                        f"Auto-repair delay {desired_delay}m set",
                        level="INFO",
                    )
                except Exception as exc:
                    self.log(
                        f"Failed to update auto-repair delay: {exc!r}",
                        level="ERROR",
                    )

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
            "repair_attempts": self._repair_attempts,
            "next_retry_at": (
                self._next_retry_at.isoformat(timespec="seconds")
                if self._next_retry_at
                else None
            ),
        }
