"""Fan Health Checker — monitors Modern Forms ceiling fans.

Performs two checks per fan on a configurable interval:

1. **Entity State** — verify the fan entity is not unavailable/unknown
2. **IP Ping** — ICMP ping the fan's IP address (retried; ESP fans in
   Wi-Fi power-save routinely drop a single ping)

All fans are reported as a single checker to avoid dashboard clutter.
Supports per-fan repair via a configurable HA script (e.g. zen32_hard_reset).

Auto-repair fires only for an entity-down fan (State check critical) and each
fan accrues its own grace period — one long-failed fan never fast-tracks a
power-cycle of another fan that merely blipped.

Failed repairs retry with CrashLoopBackOff semantics: never stop, delay
doubling per failure (delay × 2^(n-1)) capped at ``repair_backoff_max_min``.
Like k8s CrashLoopBackOff, the counter only resets after *sustained*
recovery (``repair_backoff_reset_min``, default 30 min) or a manual
repair — a fan that pops back up for a minute and drops again resumes
the ladder where it left off instead of earning a fresh instant
power-cycle (the 2026-08-31 page storm: ~11 power-cycles in 5 h because
every false recovery reset the ladder to attempt 1).  The ladder is
persisted in ``input_text.<checker_id>_health_repair_ladder`` so an
AppDaemon app reload mid-incident cannot reset it either.

Each fan may declare the UniFi access point it associates with
(``ap_status_entity`` — the HA UniFi integration's AP state sensor).
These are **Wi-Fi fans** (Modern Forms); when the fan's AP is down the
fan being unreachable is expected, so the power-cycle is withheld until
the AP recovers and the alert text says which AP is at fault.  (The
repair script power-cycles the fan via its ZEN32 relay — the ZEN32 is
Z-Wave, the fan is not.)

Communication with the controller is event-only (never ``get_app``).
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

# Delay between fan restore commands so the just-rebooted fan accepts each one
RESTORE_STEP_DELAY_S = 1

# Ping attempts per check cycle — ESP fans in Wi-Fi power-save often drop a
# single ping, so one miss must never count as a failure
PING_ATTEMPTS = 3

# Max wait for the repair script (mode: single, with a long cooldown tail) to
# be free before giving up — a turn_on while it runs is silently dropped, and
# the fan would burn its one repair attempt without any power-cycle happening
SCRIPT_BUSY_WAIT_S = 660


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
        # Re-apply the fan's pre-repair on/off + speed + direction after a
        # successful repair (the power-cycle reboots the fan to its hardware
        # default). Enabled by default; set false to disable.
        self._restore_state_enabled: bool = bool(
            args.get("restore_state_enabled", True)
        )

        # Last-known-good fan state cache (entity_id -> {state, percentage,
        # direction}), kept fresh by a state listener and seeded from HA on
        # startup. Used to restore a fan after its repair power-cycle.
        self._fan_state_cache: Dict[str, Dict[str, Any]] = {}
        self._entity_to_fan: Dict[str, str] = {
            fan["entity_id"]: fan["name"] for fan in self._fans
        }

        # CrashLoopBackOff cap for per-fan repair retries: the n-th failure
        # schedules retry n+1 after delay × 2^(n-1) minutes, capped here
        # (default 6h). Attempts reset on sustained recovery or manual repair.
        self._repair_backoff_max_min: int = int(
            args.get("repair_backoff_max_min", 360)
        )
        # How long a fan must stay fully healthy before its backoff ladder
        # resets (k8s CrashLoopBackOff resets after 10 min of clean running;
        # ESP fans get longer). 0 = reset on the first clean cycle (the old
        # behaviour, which let a flapping fan earn an instant attempt-1
        # power-cycle every few minutes).
        self._repair_backoff_reset_min: int = int(
            args.get("repair_backoff_reset_min", 30)
        )

        # Last-observed state of each fan's access point status entity
        # (fan name → lowercased state string or None when unknown).
        self._ap_state_by_fan: Dict[str, Optional[str]] = {}

        # Per-fan repair state
        self._fan_repair_states: Dict[str, Dict[str, Any]] = {
            fan["name"]: {
                "status": REPAIR_IDLE,
                "detail": "",
                "last_repair_attempt": None,
                "attempts": 0,
                "next_retry_at": None,
                # When the fan was first observed fully healthy while its
                # backoff ladder is non-idle; the ladder resets only once
                # this streak reaches repair_backoff_reset_min.
                "recovered_at": None,
            }
            for fan in self._fans
        }

        # Per-fan unhealthy tracking: when each fan's entity first went down
        # (None = healthy). Each fan accrues its own auto-repair grace period
        # so one long-failed fan can never fast-track a repair of another fan
        # that only just blipped.
        self._fan_unhealthy_since: Dict[str, Optional[datetime.datetime]] = {
            fan["name"]: None for fan in self._fans
        }

        # True while ALL fans are entity-down at once (systemic outage
        # signature) — auto-repair is suspended for the duration
        self._systemic_outage: bool = False

        # Global repair tracking
        self._repair_status: str = REPAIR_IDLE
        self._auto_repair_deadline: Optional[datetime.datetime] = None
        self._repair_task: Optional[asyncio.Task] = None
        # Set synchronously when a manual repair-all is scheduled so the
        # auto-repair evaluator can't race in before the task starts
        self._manual_repair_starting: bool = False

        # Cached auto-repair config (updated each async check cycle)
        self._cached_auto_repair_enabled: bool = self._auto_repair_enabled_default
        self._cached_auto_repair_delay_min: int = self._auto_repair_delay_min_default

        # Repair completion events awaiting delivery to the controller.
        # Drained into the next report_status payload (once-only delivery).
        self._pending_repair_events: List[Dict[str, Any]] = []

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
        await self._seed_repair_ladder()
        await self._seed_state_cache()
        self._register_state_listeners()
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
                except Exception as exc:
                    self.log(f"Failed to set default for {entity_id}: {exc!r}", level="DEBUG")
                self.log(f"Provisioned {entity_id}", level="INFO")
        except Exception as exc:
            self.log(
                f"Failed to provision auto-repair delay helper: {exc!r}",
                level="ERROR",
            )

        try:
            created = await prov.ensure_helper(
                "input_text",
                f"{self._checker_id} Health Repair Ladder",
                max=255,
            )
            if created:
                self.log(
                    f"Provisioned input_text.{self._checker_id}_health_repair_ladder",
                    level="INFO",
                )
        except Exception as exc:
            self.log(
                f"Failed to provision repair-ladder helper: {exc!r}",
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

        # Per-fan unhealthy timers must track every cycle — even while a
        # repair is active — otherwise a fan that recovers mid-repair keeps a
        # stale timer and a later blip would get an instant grace-less
        # power-cycle.
        self._update_fan_unhealthy_timers(results)

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
        if self._pending_repair_events:
            payload["repair_events"] = self._pending_repair_events
            self._pending_repair_events = []
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
        await self._refresh_ap_state(fan)
        try:
            state = await self.get_state(fan["entity_id"])
            if state is None or str(state) in ("unavailable", "unknown"):
                return {
                    "name": name,
                    "status": "critical",
                    "detail": f"State: {state}{self._ap_note(fan)}",
                }
            return {"name": name, "status": "ok", "detail": str(state)}
        except Exception as exc:
            self.log(
                f"Entity check failed for {fan['entity_id']}: {exc!r}",
                level="ERROR",
            )
            return {"name": name, "status": "critical", "detail": f"Error: {exc}"}

    # ------------------------------------------------------------------
    # Access-point awareness
    # ------------------------------------------------------------------
    #
    # Modern Forms fans are Wi-Fi devices, each associating with one UniFi
    # access point (configured per fan as ``ap_status_entity`` — the UniFi
    # integration's AP state sensor, e.g. sensor.kitchen_pantry_u7_pro_state).
    # When that AP is down, the fan being unreachable is a *network* fault:
    # power-cycling the fan cannot help, so repair is withheld until the AP
    # recovers, and the alert text names the AP instead of implying a fan
    # (or Z-Wave) fault.

    # AP sensor states that positively mean "this AP is down". Anything
    # else — including unavailable/unknown (the UniFi integration itself
    # being broken) — is treated as AP-state-unknown and does not gate
    # repair.
    _AP_DOWN_STATES = ("disconnected", "not_home", "off")

    async def _refresh_ap_state(self, fan: dict) -> None:
        """Cache the fan's AP status-sensor state (lowercased) for this cycle."""
        entity = fan.get("ap_status_entity")
        if not entity:
            return
        name = fan["name"]
        previous = self._ap_state_by_fan.get(name)
        try:
            state = await self.get_state(entity)
            current: Optional[str] = (
                str(state).lower() if state is not None else None
            )
        except Exception as exc:
            self.log(
                f"AP state check failed for {entity}: {exc!r}", level="WARNING"
            )
            current = None
        self._ap_state_by_fan[name] = current

        was_down = previous in self._AP_DOWN_STATES
        is_down = current in self._AP_DOWN_STATES
        if is_down and not was_down:
            self.log(
                f"{self._ap_label(fan)} is {current} — {name} offline is "
                "expected; holding power-cycle repairs until the AP recovers",
                level="WARNING",
            )
        elif was_down and not is_down:
            self.log(
                f"{self._ap_label(fan)} recovered ({current}) — {name} "
                "repairs re-enabled",
                level="INFO",
            )

    def _ap_is_down(self, fan: dict) -> bool:
        return (
            self._ap_state_by_fan.get(fan["name"]) in self._AP_DOWN_STATES
        )

    def _ap_label(self, fan: dict) -> str:
        ap_name = fan.get("ap_name")
        if not ap_name:
            entity = fan.get("ap_status_entity", "")
            obj = entity.split(".", 1)[-1]
            for suffix in ("_state",):
                if obj.endswith(suffix):
                    obj = obj[: -len(suffix)]
            ap_name = obj.replace("_", " ").title() or "AP"
        return f"AP {ap_name}"

    def _ap_note(self, fan: dict) -> str:
        """Alert-detail suffix describing the Wi-Fi fan's AP status."""
        if not fan.get("ap_status_entity"):
            return " (Wi-Fi fan)"
        state = self._ap_state_by_fan.get(fan["name"])
        label = self._ap_label(fan)
        if state in self._AP_DOWN_STATES:
            return (
                f" (Wi-Fi fan; {label} is {state} — fan offline expected, "
                "power-cycle held until the AP recovers)"
            )
        if state is None:
            return f" (Wi-Fi fan; {label}: state unknown)"
        return f" (Wi-Fi fan; {label}: {state} — fan itself unreachable)"

    async def _check_fan_ping(self, fan: dict) -> Dict[str, str]:
        name = f"{fan['name']} Ping"
        try:
            # Modern Forms fans are ESP devices in Wi-Fi power-save; a single
            # 2s ping routinely misses. Retry before calling it a failure.
            result = await ping_check(fan["ip"], attempts=PING_ATTEMPTS)
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

    def _check_single_fan_results(
        self, fan: dict, results: List[Dict[str, str]]
    ) -> bool:
        """Return True if all checks for a single fan are ok."""
        fan_results = [
            r for r in results if r["name"].startswith(fan["name"])
        ]
        return all(r["status"] == "ok" for r in fan_results)

    def _is_any_repair_active(self) -> bool:
        return self._manual_repair_starting or any(
            s["status"] == REPAIR_IN_PROGRESS
            for s in self._fan_repair_states.values()
        )

    def _reset_recovered_fans(self, results: List[Dict[str, str]]) -> None:
        """Reset repair state for fans that have *stayed* recovered.

        CrashLoopBackOff semantics: a recovery only resets the backoff
        ladder after it has been sustained for ``repair_backoff_reset_min``
        minutes. Until then the attempt count (and therefore the next
        retry's delay) survives, so a fan that flaps back down minutes
        after a "successful" repair resumes the ladder instead of earning
        a fresh instant power-cycle.
        """
        now = datetime.datetime.now()
        for fan in self._fans:
            if not self._check_single_fan_results(fan, results):
                continue
            fr = self._fan_repair_states[fan["name"]]
            if fr["status"] not in (REPAIR_FAILED, REPAIR_SUCCESS):
                continue
            if fr["recovered_at"] is None:
                fr["recovered_at"] = now
                if self._repair_backoff_reset_min > 0:
                    self.log(
                        f"{fan['name']} recovered — backoff ladder "
                        f"(attempts={fr['attempts']}) resets after "
                        f"{self._repair_backoff_reset_min}m of sustained "
                        "health",
                        level="INFO",
                    )
            held = (
                now - fr["recovered_at"]
            ).total_seconds() < self._repair_backoff_reset_min * 60
            if held:
                continue
            self.log(
                f"{fan['name']} recovered — resetting repair state",
                level="INFO",
            )
            fr["status"] = REPAIR_IDLE
            fr["detail"] = ""
            fr["attempts"] = 0
            fr["next_retry_at"] = None
            fr["recovered_at"] = None
            self._persist_ladder()

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

    def _is_fan_repair_worthy(
        self, fan: dict, results: List[Dict[str, str]]
    ) -> bool:
        """Return True if *fan* warrants a power-cycle repair.

        Only an entity-down fan (State check critical — unavailable/unknown)
        justifies cutting power. A ping-only miss while HA can still reach
        the fan is a transient warning, never a reason to power-cycle a
        possibly-running fan.

        A fan whose access point is down is likewise never repair-worthy:
        the outage is the network's, and power-cycling the fan cannot fix
        it — the repair (and its grace/backoff clocks) hold until the AP
        recovers.
        """
        state_name = f"{fan['name']} State"
        entity_down = any(
            r["name"] == state_name and r["status"] == "critical"
            for r in results
        )
        if entity_down and self._ap_is_down(fan):
            return False
        return entity_down

    def _update_fan_unhealthy_timers(
        self, results: List[Dict[str, str]]
    ) -> None:
        """Update per-fan unhealthy timers from raw check results.

        A fan accrues time toward its own auto-repair deadline only while its
        entity is down. This keeps one long-failed fan from fast-tracking
        repairs of other fans that blip. Runs every check cycle, including
        while a repair is active.
        """
        now = datetime.datetime.now()
        repair_worthy = [
            f for f in self._fans if self._is_fan_repair_worthy(f, results)
        ]

        # Systemic-outage signature: every fan entity-down at once points at
        # HA, the integration, or the Wi-Fi network — not at individual fans.
        # Power-cycling them all one by one would be wrong (and useless), so
        # suspend the timers until the signature clears.
        if len(self._fans) > 1 and len(repair_worthy) == len(self._fans):
            if not self._systemic_outage:
                self._systemic_outage = True
                self.log(
                    f"All {len(self._fans)} fans entity-down — systemic "
                    "outage signature, auto-repair suspended",
                    level="WARNING",
                )
            for name in self._fan_unhealthy_since:
                self._fan_unhealthy_since[name] = None
                self._floor_stale_backoff(name, now)
            return
        if self._systemic_outage:
            self._systemic_outage = False
            self.log(
                "Systemic outage signature cleared — auto-repair resumed",
                level="INFO",
            )

        for fan in self._fans:
            name = fan["name"]
            if self._is_fan_repair_worthy(fan, results):
                fr = self._fan_repair_states[name]
                fr["recovered_at"] = None
                if fr["status"] == REPAIR_SUCCESS:
                    # The repair's "success" did not stick — the fan is down
                    # again before its recovery was sustained. Count the
                    # false success as a failed attempt and resume the
                    # backoff ladder instead of restarting it at attempt 1
                    # with a stale grace deadline (which fired an instant
                    # power-cycle every few minutes on 2026-08-31).
                    self._register_fan_relapse(name)
                    self._fan_unhealthy_since[name] = now
                elif self._fan_unhealthy_since[name] is None:
                    self._fan_unhealthy_since[name] = now
                    self.log(
                        f"{name} entity down — auto-repair grace timer started",
                        level="INFO",
                    )
            else:
                self._fan_unhealthy_since[name] = None
                self._floor_stale_backoff(name, now)

    def _floor_stale_backoff(self, name: str, now: datetime.datetime) -> None:
        """Slide a FAILED fan's retry forward while it is not repair-worthy.

        While a fan's entity is reachable (or a systemic outage suspends
        timers), its scheduled backoff retry keeps sliding to at least
        delay_min from now. This way a stale retry time can never fire the
        instant the entity blips down again — the fan always gets at least
        one full delay of sustained entity-down first — while the attempt
        ladder is preserved (only full recovery resets it).
        """
        fr = self._fan_repair_states[name]
        if fr["status"] != REPAIR_FAILED or not fr["next_retry_at"]:
            return
        _, delay_min = self._read_auto_repair_config()
        floor = now + datetime.timedelta(minutes=delay_min)
        if fr["next_retry_at"] < floor:
            fr["next_retry_at"] = floor

    def _evaluate_auto_repair(self, results: List[Dict[str, str]]) -> None:
        now = datetime.datetime.now()
        enabled, delay_min = self._read_auto_repair_config()

        # Candidates with their due time: entity-down fans that are either
        # awaiting a first attempt (due = their grace deadline) or in a
        # CrashLoopBackOff retry window after a failed attempt (due = the
        # scheduled retry). A failed repair never ends the episode.
        candidates: List[tuple] = []
        for f in self._fans:
            name = f["name"]
            if self._fan_unhealthy_since[name] is None:
                continue
            fr = self._fan_repair_states[name]
            if fr["status"] == REPAIR_IDLE:
                due = self._fan_unhealthy_since[name] + datetime.timedelta(
                    minutes=delay_min
                )
            elif fr["status"] == REPAIR_FAILED and fr["next_retry_at"]:
                due = fr["next_retry_at"]
            else:
                continue
            candidates.append((due, f))

        if not candidates:
            # Nothing repair-worthy (healthy, ping-only blips, or repair
            # already running) — clear any pending countdown
            if self._repair_status == REPAIR_PENDING:
                self.log(
                    "No repair-worthy fans — cancelling pending auto-repair",
                    level="INFO",
                )
                self._repair_status = REPAIR_IDLE
                self._auto_repair_deadline = None
            return

        if not enabled:
            # Clear any countdown started before the toggle was switched off —
            # otherwise reports keep advertising a pending repair (with a
            # stale deadline) that can never fire.
            if self._repair_status == REPAIR_PENDING:
                self.log(
                    "Auto-repair disabled — cancelling pending auto-repair",
                    level="INFO",
                )
                self._repair_status = REPAIR_IDLE
                self._auto_repair_deadline = None
            return

        # Earliest-due fan first (first attempts and backoff retries compete
        # on equal terms)
        candidates.sort(key=lambda t: t[0])
        due, fan = candidates[0]
        fr = self._fan_repair_states[fan["name"]]

        if now >= due:
            attempt_no = fr["attempts"] + 1
            self.log(
                f"Auto-repair triggered — starting with {fan['name']} "
                f"(attempt {attempt_no}, entity down since "
                f"{self._fan_unhealthy_since[fan['name']].isoformat(timespec='seconds')})",
                level="INFO",
            )
            self._start_fan_repair(fan)
        elif fr["status"] == REPAIR_IDLE:
            # Pre-first-attempt grace countdown — cancellable pending
            self._repair_status = REPAIR_PENDING
            self._auto_repair_deadline = due
            self.log(
                f"Repair pending for {fan['name']} — deadline "
                f"{due.isoformat(timespec='seconds')}",
                level="INFO",
            )
        elif self._repair_status == REPAIR_PENDING:
            # Waiting on a failed fan's backoff — not a cancellable
            # pre-attempt countdown (the per-fan detail carries the retry
            # time), so don't advertise pending
            self._repair_status = REPAIR_IDLE
            self._auto_repair_deadline = None

    # ------------------------------------------------------------------
    # State cache / restore
    # ------------------------------------------------------------------

    async def _seed_state_cache(self) -> None:
        """Seed the last-known-good cache from current HA state on startup.

        Reading fresh from HA covers the case where a fan changed state while
        AppDaemon was down. Fans that are ``unavailable`` at startup are left
        unseeded; the state listener populates them once they report a good
        state.
        """
        for fan in self._fans:
            entity_id = fan["entity_id"]
            try:
                full = await self.get_state(entity_id, attribute="all")
            except Exception as exc:
                self.log(
                    f"Failed to seed state cache for {fan['name']}: {exc!r}",
                    level="WARNING",
                )
                continue

            cached = self._extract_good_state(full)
            if cached is None:
                state = full.get("state") if isinstance(full, dict) else full
                self.log(
                    f"{fan['name']} is '{state}' at startup — cache will seed "
                    f"once it reports a good state",
                    level="INFO",
                )
                continue

            self._fan_state_cache[entity_id] = cached
            self.log(
                f"Seeded state cache for {fan['name']}: {cached}", level="INFO"
            )

    def _register_state_listeners(self) -> None:
        for fan in self._fans:
            self.listen_state(
                self._on_fan_state_change, fan["entity_id"], attribute="all"
            )
        self.log(
            f"Registered state listeners for {len(self._fans)} fans",
            level="INFO",
        )

    @staticmethod
    def _extract_good_state(full: Any) -> Optional[Dict[str, Any]]:
        """Return {state, percentage, direction} from a full state dict, or
        None if the fan is not in a good (on/off) state."""
        if not isinstance(full, dict):
            return None
        state = full.get("state")
        if state not in ("on", "off"):
            return None
        attrs = full.get("attributes") or {}
        return {
            "state": state,
            "percentage": attrs.get("percentage"),
            "direction": attrs.get("direction"),
        }

    def _on_fan_state_change(
        self, entity: str, attribute: str, old: Any, new: Any, kwargs: Any
    ) -> None:
        """Cache a fan's last-known-good on/off + speed + direction.

        Skipped while that fan is being repaired so the power-cycle's transient
        states never overwrite the pre-repair value we need to restore.
        """
        fan_name = self._entity_to_fan.get(entity)
        if not fan_name:
            return
        if (
            self._fan_repair_states.get(fan_name, {}).get("status")
            == REPAIR_IN_PROGRESS
        ):
            return
        cached = self._extract_good_state(new)
        if cached is None:
            return
        self._fan_state_cache[entity] = cached

    async def _restore_fan_state(self, fan: dict) -> None:
        """Re-apply the cached on/off + speed + direction after a repair.

        The power-cycle reboots the physical fan to its hardware default even
        when HA still shows stale state, so we push the last-known-good values
        back to the device.
        """
        if not self._restore_state_enabled:
            return

        entity_id = fan["entity_id"]
        cached = self._fan_state_cache.get(entity_id)
        if not cached or cached.get("state") not in ("on", "off"):
            self.log(
                f"No cached state for {fan['name']} — skipping restore",
                level="INFO",
            )
            return

        desired = cached["state"]
        try:
            if desired == "off":
                self.call_service("fan/turn_off", entity_id=entity_id)
                self.log(f"Restored {fan['name']} to off", level="INFO")
                return

            pct = cached.get("percentage")
            direction = cached.get("direction")
            self.call_service("fan/turn_on", entity_id=entity_id)
            if isinstance(pct, (int, float)) and pct > 0:
                await asyncio.sleep(RESTORE_STEP_DELAY_S)
                self.call_service(
                    "fan/set_percentage",
                    entity_id=entity_id,
                    percentage=int(pct),
                )
            if direction in ("forward", "reverse"):
                await asyncio.sleep(RESTORE_STEP_DELAY_S)
                self.call_service(
                    "fan/set_direction",
                    entity_id=entity_id,
                    direction=direction,
                )
            self.log(
                f"Restored {fan['name']} to on "
                f"(speed={pct}%, direction={direction})",
                level="INFO",
            )
        except Exception as exc:
            self.log(
                f"Failed to restore state for {fan['name']}: {exc!r}",
                level="ERROR",
            )

    # ------------------------------------------------------------------
    # Repair execution
    # ------------------------------------------------------------------

    def _start_manual_repair(self) -> None:
        """Start manual repair for all failing fans."""
        if self._is_any_repair_active():
            self.log("Repair already in progress — ignoring", level="WARNING")
            return

        # Reset backoff ladders so fans can be retried immediately (a manual
        # repair is a human-declared fresh start)
        for fr in self._fan_repair_states.values():
            if fr["status"] in (REPAIR_FAILED, REPAIR_SUCCESS) or fr["attempts"]:
                fr["status"] = REPAIR_IDLE
                fr["detail"] = ""
                fr["attempts"] = 0
                fr["next_retry_at"] = None
                fr["recovered_at"] = None
        self._persist_ladder()

        # Block the auto-repair evaluator synchronously until the task runs
        self._manual_repair_starting = True
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
        """Repair all currently entity-down fans sequentially.

        Same repair-worthiness rule as auto-repair: only a fan whose entity is
        down gets power-cycled — a ping-only blip never does, even manually.
        """
        try:
            # Run a fresh check to get current state
            results = await self._run_health_checks_only()
            repairable = [
                f for f in self._fans
                if self._is_fan_repair_worthy(f, results)
                and self._fan_repair_states[f["name"]]["status"] == REPAIR_IDLE
            ]

            if not repairable:
                self.log("No fans to repair", level="INFO")
                return

            for fan in repairable:
                await self._execute_fan_repair(fan)
        finally:
            self._manual_repair_starting = False

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
            # The repair script is mode: single with a long cooldown tail — a
            # turn_on while it is still running is silently dropped and the
            # fan would burn the attempt without any power-cycle. Wait for
            # the script to be free first.
            if not await self._wait_for_repair_script_free(fr):
                self._register_fan_repair_failure(
                    fan_name, "Repair script busy — no power-cycle attempted"
                )
                self._repair_status = self._aggregate_repair_status()
                self.log(
                    f"{fan_name} repair aborted — {self._repair_script} still "
                    f"busy after {SCRIPT_BUSY_WAIT_S}s",
                    level="WARNING",
                )
                self._pending_repair_events.append({
                    "result": "failed",
                    "duration_s": 0,
                    "device": fan_name,
                })
                self._report_repair_status_only()
                return

            self.log(
                f"Calling {self._repair_script} for {fan_name}", level="INFO"
            )
            # script/turn_on is fire-and-forget: calling the script as its own
            # service (script/<name>) blocks until the script finishes, and
            # zen32_hard_reset ends with a long lockout delay — the WS request
            # would always hit AppDaemon's 60s timeout and log a warning.
            self.call_service(
                "script/turn_on",
                entity_id=self._repair_script,
                variables={
                    "power_switch_entity": fan["power_switch"],
                    "relay_control_select_entity": fan["relay_control"],
                    "scene_control_select_entity": fan["scene_control"],
                    "unavailable_fan_entity": fan["entity_id"],
                },
            )

            fr["detail"] = "Waiting for recovery..."
            self._report_repair_status_only()

            # Poll for recovery. Elapsed is measured on the monotonic clock —
            # each poll also runs the health checks (with ping retries), so
            # summing sleep intervals would badly under-count wall time.
            start_mono = time.monotonic()
            elapsed = 0
            while elapsed < self._repair_recovery_wait_s:
                await asyncio.sleep(REPAIR_POLL_INTERVAL_S)
                elapsed = int(time.monotonic() - start_mono)

                results = await self._run_health_checks_only()
                if self._check_single_fan_results(fan, results):
                    # Restore state while still IN_PROGRESS so the listener stays
                    # frozen and our restore commands are the ones that seed the
                    # cache afterward.
                    fr["detail"] = f"Recovered after {elapsed}s — restoring state"
                    self._report_repair_status_only()
                    await self._restore_fan_state(fan)

                    # Attempts are NOT reset here — only a *sustained*
                    # recovery (repair_backoff_reset_min in
                    # _reset_recovered_fans) resets the backoff ladder, so a
                    # relapse minutes from now resumes it (crashloop
                    # semantics).
                    fr["status"] = REPAIR_SUCCESS
                    fr["detail"] = f"Recovered after {elapsed}s"
                    fr["next_retry_at"] = None
                    self._persist_ladder()
                    self._repair_status = self._aggregate_repair_status()
                    self.log(
                        f"{fan_name} repair successful — recovered after {elapsed}s",
                        level="INFO",
                    )
                    self._pending_repair_events.append({
                        "result": "success",
                        "duration_s": elapsed,
                        "device": fan_name,
                    })
                    self._report_repair_status_only()
                    return

                fr["detail"] = (
                    f"Waiting for recovery... {elapsed}s/{self._repair_recovery_wait_s}s"
                )

            # Timeout — schedule the next backoff retry
            self._register_fan_repair_failure(
                fan_name,
                f"Did not recover after {self._repair_recovery_wait_s}s",
            )
            self._repair_status = self._aggregate_repair_status()
            self.log(
                f"{fan_name} repair failed — no recovery after "
                f"{self._repair_recovery_wait_s}s (attempt {fr['attempts']}; "
                f"next retry at "
                f"{fr['next_retry_at'].isoformat(timespec='seconds')})",
                level="WARNING",
            )
            self._pending_repair_events.append({
                "result": "failed",
                "duration_s": self._repair_recovery_wait_s,
                "device": fan_name,
            })
            self._report_repair_status_only()

        except Exception as exc:
            self._register_fan_repair_failure(fan_name, f"Repair error: {exc}")
            self._repair_status = self._aggregate_repair_status()
            self.log(f"Repair error for {fan_name}: {exc!r}", level="ERROR")
            self._report_repair_status_only()

    def _register_fan_repair_failure(self, fan_name: str, detail: str) -> None:
        """Mark a fan's repair failed and schedule its next backoff retry.

        CrashLoopBackOff semantics: the episode never ends on failure. The
        n-th failure schedules retry n+1 after delay × 2^(n-1) minutes,
        capped at repair_backoff_max_min. Reset on *sustained* recovery or
        manual repair.
        """
        self._schedule_backoff_retry(fan_name, detail)

    def _register_fan_relapse(self, fan_name: str) -> None:
        """Resume the backoff ladder after a repair "success" failed to stick.

        The false success counts as a failed attempt: the ladder climbs and
        the next power-cycle waits out the corresponding backoff instead of
        firing on the next check cycle.
        """
        fr = self._fan_repair_states[fan_name]
        self._schedule_backoff_retry(
            fan_name, "Relapsed after repair — recovery did not stick"
        )
        self.log(
            f"{fan_name} relapsed after repair — resuming backoff ladder "
            f"(attempt {fr['attempts']}; next retry at "
            f"{fr['next_retry_at'].isoformat(timespec='seconds')})",
            level="WARNING",
        )

    def _schedule_backoff_retry(self, fan_name: str, detail: str) -> None:
        fr = self._fan_repair_states[fan_name]
        fr["attempts"] += 1
        _, delay_min = self._read_auto_repair_config()
        backoff_min = min(
            delay_min * (2 ** (fr["attempts"] - 1)),
            self._repair_backoff_max_min,
        )
        fr["next_retry_at"] = datetime.datetime.now() + datetime.timedelta(
            minutes=backoff_min
        )
        fr["status"] = REPAIR_FAILED
        fr["recovered_at"] = None
        fr["detail"] = (
            f"{detail} (attempt {fr['attempts']}; retry at "
            f"{fr['next_retry_at'].strftime('%H:%M')})"
        )
        self._persist_ladder()

    # ------------------------------------------------------------------
    # Backoff-ladder persistence
    # ------------------------------------------------------------------
    #
    # The ladder lives in input_text.<checker_id>_health_repair_ladder
    # (lazily provisioned) as compact JSON {fan: [attempts, next_retry]}
    # so an AppDaemon app reload mid-incident — an HA restart or plugin
    # reconnect re-initialises every app (observed 2026-08-31 07:24) —
    # cannot reset a climbing ladder back to instant attempt-1 power-cycles.

    _LADDER_HELPER_MAX_LEN = 255

    def _ladder_helper_entity(self) -> str:
        return f"input_text.{self._checker_id}_health_repair_ladder"

    def _persist_ladder(self) -> None:
        """Write the per-fan backoff ladder to its input_text helper.

        Best-effort: failures are logged and never block repair logic. Only
        fans with a non-zero attempt count are stored; if the payload would
        exceed input_text's length limit, the lowest ladders are dropped
        first (they lose the least backoff).
        """
        entries: Dict[str, Any] = {
            name: [
                fr["attempts"],
                fr["next_retry_at"].isoformat(timespec="seconds")
                if fr["next_retry_at"]
                else None,
                # The status disambiguates a success-awaiting-sustained-
                # recovery ladder from a failed one, so a reload does not
                # misreport a currently-healthy fan as "failed".
                "success" if fr["status"] == REPAIR_SUCCESS else "failed",
            ]
            for name, fr in self._fan_repair_states.items()
            if fr["attempts"] > 0
        }
        value = json.dumps(entries, separators=(",", ":"))
        while len(value) > self._LADDER_HELPER_MAX_LEN and entries:
            drop = min(entries, key=lambda n: entries[n][0])
            del entries[drop]
            value = json.dumps(entries, separators=(",", ":"))
        try:
            self.call_service(
                "input_text/set_value",
                entity_id=self._ladder_helper_entity(),
                value=value,
            )
        except Exception as exc:
            self.log(
                f"Failed to persist repair ladder: {exc!r}", level="WARNING"
            )

    async def _seed_repair_ladder(self) -> None:
        """Restore the backoff ladder from its helper after an app reload."""
        try:
            raw = await self.get_state(self._ladder_helper_entity())
        except Exception as exc:
            self.log(
                f"Could not read repair-ladder helper: {exc!r}",
                level="WARNING",
            )
            return
        if not raw or str(raw) in ("unknown", "unavailable"):
            return
        try:
            entries = json.loads(str(raw))
        except ValueError:
            entries = None
        if not isinstance(entries, dict):
            self.log(
                f"Ignoring unparseable repair-ladder value: {raw!r}",
                level="WARNING",
            )
            return
        _, delay_min = self._read_auto_repair_config()
        # Never retry before one grace delay after a restart — a stale past
        # retry time must not fire the instant the app comes back.
        floor = datetime.datetime.now() + datetime.timedelta(minutes=delay_min)
        restored = []
        for name, entry in entries.items():
            fr = self._fan_repair_states.get(name)
            if fr is None or not isinstance(entry, (list, tuple)) or not entry:
                continue
            try:
                attempts = int(entry[0])
            except (TypeError, ValueError):
                continue
            if attempts <= 0:
                continue
            next_retry = None
            if len(entry) > 1 and entry[1]:
                try:
                    next_retry = datetime.datetime.fromisoformat(str(entry[1]))
                except ValueError:
                    next_retry = None
            was_success = len(entry) > 2 and entry[2] == "success"
            fr["attempts"] = attempts
            if was_success:
                # The ladder was persisted right after a successful repair
                # whose recovery had not yet been sustained. The fan may
                # well be healthy — restore as SUCCESS so the dashboard
                # does not misreport it as failed; _reset_recovered_fans
                # (sustained health) or a relapse takes it from here.
                fr["status"] = REPAIR_SUCCESS
                fr["next_retry_at"] = None
                fr["detail"] = (
                    f"Backoff ladder restored after restart "
                    f"(attempt {attempts}; awaiting sustained recovery)"
                )
            else:
                fr["status"] = REPAIR_FAILED
                fr["next_retry_at"] = (
                    max(next_retry, floor) if next_retry else floor
                )
                fr["detail"] = (
                    f"Backoff ladder restored after restart (attempt {attempts}; "
                    f"retry at {fr['next_retry_at'].strftime('%H:%M')})"
                )
            restored.append(f"{name} (attempt {attempts})")
        if restored:
            self.log(
                "Restored repair backoff ladder from "
                f"{self._ladder_helper_entity()}: {', '.join(restored)}",
                level="INFO",
            )

    async def _wait_for_repair_script_free(self, fr: Dict[str, Any]) -> bool:
        """Wait until the repair script entity is not running ('on').

        Returns True when free, False if still busy after SCRIPT_BUSY_WAIT_S.
        Errors reading the script state are treated as free — a transient WS
        hiccup must not block a repair.
        """
        elapsed = 0
        while True:
            try:
                state = await self.get_state(self._repair_script)
            except Exception as exc:
                self.log(
                    f"Could not read {self._repair_script} state: {exc!r} — "
                    "assuming free",
                    level="WARNING",
                )
                return True
            if str(state) != "on":
                return True
            if elapsed >= SCRIPT_BUSY_WAIT_S:
                return False
            fr["detail"] = (
                f"Waiting for repair script to be free... "
                f"{elapsed}s/{SCRIPT_BUSY_WAIT_S}s"
            )
            await asyncio.sleep(REPAIR_POLL_INTERVAL_S)
            elapsed += REPAIR_POLL_INTERVAL_S

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
        payload: Dict[str, Any] = {
            "checker_id": self._checker_id,
            "results": [],
            "repair_state": self._build_repair_state(),
        }
        if self._pending_repair_events:
            payload["repair_events"] = self._pending_repair_events
            self._pending_repair_events = []
        self.fire_event(
            "health_check_command",
            command="report_status",
            payload=json.dumps(payload),
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
        """Derive top-level repair status from per-fan states.

        Per-fan states never hold PENDING (the grace countdown is global), so
        the global flag must be consulted or the card countdown and the
        controller's pending paging-hold would never see it.
        """
        statuses = [s["status"] for s in self._fan_repair_states.values()]
        if REPAIR_IN_PROGRESS in statuses:
            return REPAIR_IN_PROGRESS
        if REPAIR_PENDING in statuses or self._repair_status == REPAIR_PENDING:
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
            # Name the failed fans with their ladder position — this string
            # rides into the Alertmanager description, so the Pushover page
            # itself should say which fan, which attempt, and when the next
            # power-cycle is due.
            failed = [
                f"{name}: {fr['detail']}" if fr["detail"] else name
                for name, fr in self._fan_repair_states.items()
                if fr["status"] == REPAIR_FAILED
            ]
            detail = "; ".join(failed)

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
                    "attempts": fr["attempts"],
                    "next_retry_at": (
                        fr["next_retry_at"].isoformat(timespec="seconds")
                        if fr["next_retry_at"]
                        else None
                    ),
                }
                for name, fr in self._fan_repair_states.items()
            },
        }
