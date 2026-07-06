"""Shade Gateway Checker — distinguishes PowerView RF disconnects from real low batteries.

**Incident context.** PowerView G3 shades report ``0%`` battery when they lose
their RF link to the gateway — not just when the battery is actually dead.
When the gateway hiccups, every shade on it can flap ``100% <-> 0%`` many
times over several hours as it repeatedly loses and regains its link. The
generic :class:`~health_checks.checker_apps.battery_checker.battery_checker.BatteryChecker`
has no way to tell that apart from a genuine dying battery, so it pages on
every 0% reading — even though the shades self-heal with no intervention.

This checker owns the disconnect concern **gateway-wide** (all shades, not
per-entity): it watches every shade battery sensor for the implausible-drop
signature (a healthy baseline collapsing straight to ~0%), tracks a single
gateway-level "disconnect episode" that survives mid-episode flap-backs to
100%, and after a grace period auto-repairs by power-cycling the PoE port
that feeds the primary gateway. One auto-restart per episode — if that
doesn't restore the shades, it escalates to a critical page instead of
retrying forever.

Communication with the controller is event-only (never ``get_app``), same
as every other checker in this package.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import re
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
from shared.check_utils import is_implausible_battery_drop

logger = logging.getLogger(__name__)

# Suffixes to strip from friendly_name for cleaner display names (case-insensitive)
_BATTERY_SUFFIXES = [
    " battery level",
    " battery",
]

# Repair state constants (same vocabulary as SpaHealthChecker / ProtectHealthChecker)
REPAIR_IDLE = "idle"
REPAIR_PENDING = "pending"
REPAIR_IN_PROGRESS = "in_progress"
REPAIR_SUCCESS = "success"
REPAIR_FAILED = "failed"

REPAIR_POLL_INTERVAL_S = 5


class ShadeGatewayChecker(hass.Hass):
    """Health checker that owns gateway-disconnect detection for all shade batteries."""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        args = self.args or {}

        # Identity
        self._checker_id: str = args.get("checker_id", "shade_gateway")
        self._checker_name: str = args.get("checker_name", "Shade Gateway")
        self._check_interval_s: int = int(args.get("check_interval_s", 300))

        # Entity discovery patterns (same include/exclude approach as BatteryChecker)
        self._include_patterns: List[re.Pattern] = []
        self._exclude_patterns: List[re.Pattern] = []
        for pattern_cfg in args.get("entity_patterns", []):
            if "include" in pattern_cfg:
                self._include_patterns.append(re.compile(pattern_cfg["include"]))
            if "exclude" in pattern_cfg:
                self._exclude_patterns.append(re.compile(pattern_cfg["exclude"]))

        # Discovered entities: entity_id -> display_name
        self._entities: Dict[str, str] = {}

        # Disconnect detection thresholds
        self._disconnect_low_threshold: float = float(
            args.get("disconnect_low_threshold", 5)
        )
        self._healthy_floor: float = float(args.get("healthy_floor", 40))
        self._recovery_settle_s: int = int(args.get("recovery_settle_s", 900))

        # Repair (power-cycle the PoE port feeding the primary gateway)
        self._repair_button: str = args.get("repair_button", "")
        self._repair_settle_s: int = int(args.get("repair_settle_s", 180))
        self._repair_recovery_wait_s: int = int(
            args.get("repair_recovery_wait_s", 900)
        )
        self._auto_repair_enabled_default: bool = bool(
            args.get("auto_repair_enabled_default", True)
        )
        self._auto_repair_delay_min_default: int = int(
            args.get("auto_repair_delay_min_default", 120)
        )

        # Per-entity tracking
        self._last_good_value: Dict[str, float] = {}
        # None means "currently unavailable/unknown" (explicit, not stale).
        self._current_value: Dict[str, Optional[float]] = {}
        # entity_id -> timestamp of its most recent qualifying (implausible) drop,
        # kept only while the episode they contributed to is active.
        self._episode_affected: Dict[str, datetime.datetime] = {}

        # Gateway-level disconnect episode state
        self._disconnect_since: Optional[datetime.datetime] = None
        self._last_zero_time: Optional[datetime.datetime] = None
        # One auto-restart per episode — set True the moment a repair (auto
        # or manual) is started, cleared only when the episode fully clears.
        self._repair_attempted_this_episode: bool = False

        # Repair state machine
        self._repair_status: str = REPAIR_IDLE
        self._repair_detail: str = ""
        self._auto_repair_deadline: Optional[datetime.datetime] = None
        self._last_repair_attempt: Optional[str] = None
        self._repair_task: Optional[asyncio.Task] = None

        # Cached auto-repair config (updated each async check cycle)
        self._cached_auto_repair_enabled: bool = self._auto_repair_enabled_default
        self._cached_auto_repair_delay_min: int = self._auto_repair_delay_min_default

        self.log(
            f"ShadeGatewayChecker initializing: id={self._checker_id}, "
            f"disconnect_low_threshold={self._disconnect_low_threshold:.0f}%, "
            f"healthy_floor={self._healthy_floor:.0f}%, "
            f"recovery_settle_s={self._recovery_settle_s}, "
            f"repair_button={self._repair_button}",
            level="INFO",
        )

        self.run_in(self._on_startup, 0)

    def _on_startup(self, kwargs: Any) -> None:
        self.create_task(self._async_startup())

    async def _async_startup(self) -> None:
        await self._discover_entities()

        if not self._entities:
            self.log(
                f"No entities matched patterns for checker '{self._checker_id}'",
                level="WARNING",
            )

        await self._seed_baselines()
        await self._provision_entities()
        await self._refresh_auto_repair_config()
        self._register()

        # Catch every transition immediately — a 300s poll can miss a shade
        # that flaps 0%->100% and back between polls. This is the mechanism
        # that lets us see flaps a poll-only approach would silently miss.
        for entity_id in self._entities:
            self.listen_state(self._on_shade_state_change, entity_id)

        # Listen for controller ready (re-register if controller restarts)
        self.listen_event(self._on_controller_ready, "health_check_controller_ready")
        # Listen for force-recheck requests
        self.listen_event(self._on_recheck, "health_check_recheck")
        # Listen for repair commands from controller
        self.listen_event(
            self._on_repair_command, f"health_check_repair_{self._checker_id}"
        )

        # Run first check after short delay, then start periodic timer
        self.run_in(self._first_check, 5)

        self.log(
            f"ShadeGatewayChecker '{self._checker_name}' started with "
            f"{len(self._entities)} entities",
            level="INFO",
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
            # Grace defaults to 2h and must allow up to 6h — much higher than
            # the other checkers' 60m cap — since a shade RF disconnect is
            # expected to often self-heal well within a couple of hours.
            created = await prov.ensure_helper(
                "input_number",
                f"{self._checker_id} Health Auto Repair Delay",
                min=15,
                max=360,
                step=15,
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

    # ------------------------------------------------------------------
    # Entity discovery
    # ------------------------------------------------------------------

    async def _discover_entities(self) -> None:
        """Discover shade battery entities matching configured regex patterns.

        Same include/exclude approach as BatteryChecker: filter to
        ``device_class=battery`` + ``unit_of_measurement=%``, then apply
        include/exclude regexes against the entity_id.
        """
        all_states = await self.get_state() or {}
        matched: Dict[str, str] = {}

        for entity_id, state_obj in all_states.items():
            if not isinstance(state_obj, dict):
                continue

            attrs = state_obj.get("attributes", {})
            if not isinstance(attrs, dict):
                attrs = {}

            if attrs.get("device_class") != "battery":
                continue
            if attrs.get("unit_of_measurement") != "%":
                continue

            included = any(p.search(entity_id) for p in self._include_patterns)
            if not included:
                continue

            excluded = any(p.search(entity_id) for p in self._exclude_patterns)
            if excluded:
                continue

            friendly_name = attrs.get("friendly_name", entity_id)
            display_name = friendly_name
            lower = display_name.lower()
            for suffix in _BATTERY_SUFFIXES:
                if lower.endswith(suffix):
                    display_name = display_name[: len(display_name) - len(suffix)]
                    break

            matched[entity_id] = display_name

        self._entities = matched

        self.log(
            f"Discovered {len(self._entities)} shade battery entities for checker "
            f"'{self._checker_id}':",
            level="INFO",
        )
        for entity_id, display_name in sorted(self._entities.items()):
            self.log(f"  - {entity_id} ({display_name})", level="INFO")

    async def _seed_baselines(self) -> None:
        """Seed per-entity baselines from current state at startup (cold start).

        Only a *healthy* current reading seeds ``last_good_value`` — a shade
        that happens to be reading low at AppDaemon startup does NOT
        fabricate an episode; we require an observed healthy->low
        *transition* (via listen_state) before ever flagging a disconnect.
        """
        seeded = 0
        for entity_id in self._entities:
            try:
                state = await self.get_state(entity_id)
            except Exception as exc:
                self.log(f"Failed to seed baseline for {entity_id}: {exc!r}", level="WARNING")
                continue

            if state is None or str(state) in ("unavailable", "unknown"):
                continue

            try:
                value = float(state)
            except (TypeError, ValueError):
                continue

            self._current_value[entity_id] = value
            if value > self._disconnect_low_threshold:
                self._last_good_value[entity_id] = value
                seeded += 1

        self.log(
            f"Seeded {seeded}/{len(self._entities)} shade battery baselines from "
            f"current healthy state (no episode fabricated from cold-start low readings)",
            level="INFO",
        )

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def _register(self) -> None:
        payload: Dict[str, Any] = {
            "checker_id": self._checker_id,
            "checker_name": self._checker_name,
            "check_names": ["Gateway Link"],
            "supports_repair": True,
            "repair_state": self._build_repair_state(),
            "alerting": {"alertname": "ShadeGatewayDisconnected"},
        }
        self.fire_event(
            "health_check_command",
            command="register_checker",
            payload=json.dumps(payload),
        )
        self.log(
            f"Registered '{self._checker_name}' monitoring {len(self._entities)} shades",
            level="INFO",
        )

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_controller_ready(self, event_name: str, data: dict, kwargs: Any) -> None:
        self.log(
            f"Controller ready — re-registering '{self._checker_name}'", level="INFO"
        )
        self._register()

    def _on_recheck(self, event_name: str, data: dict, kwargs: Any) -> None:
        self.log(f"Force recheck requested for '{self._checker_name}'", level="INFO")
        self.create_task(self._run_checks())

    def _on_repair_command(self, event_name: str, data: dict, kwargs: Any) -> None:
        action = data.get("action", "")
        if action == "start_repair":
            self.log("Manual repair requested", level="INFO")
            self._start_repair()
        elif action == "cancel_repair":
            self._cancel_repair()
        elif action == "update_repair_config":
            self._update_repair_config(data)

    def _on_shade_state_change(
        self, entity: str, attribute: str, old: Any, new: Any, kwargs: Any
    ) -> None:
        self._process_reading(entity, new)

    def _first_check(self, kwargs: Any) -> None:
        self.create_task(self._run_checks())
        self.run_every(
            self._check_tick, f"now+{self._check_interval_s}", self._check_interval_s
        )

    def _check_tick(self, kwargs: Any) -> None:
        self.create_task(self._run_checks())

    # ------------------------------------------------------------------
    # Disconnect detection
    # ------------------------------------------------------------------

    def _process_reading(self, entity_id: str, raw_value: Any) -> None:
        """Classify a single sensor reading and update episode state.

        An "impossible drop" (healthy baseline >= healthy_floor collapsing
        straight to <= disconnect_low_threshold) starts or extends the
        gateway-wide disconnect episode. A gradual decline (baseline already
        below healthy_floor) is left alone — that's a genuine dying battery,
        still the BatteryChecker's concern.

        A non-numeric reading (``unavailable``/``unknown``/``None``, e.g.
        during an HA restart) records the entity as *currently unknown*
        rather than leaving a stale cached value, but never starts or feeds
        an episode — detection stays keyed only on the numeric
        drop-to-low-from-healthy signature, so transient unavailability at
        startup or during a restart can never fabricate a disconnect.
        """
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            self._current_value[entity_id] = None
            return

        prev_good = self._last_good_value.get(entity_id)
        now = datetime.datetime.now()

        if is_implausible_battery_drop(
            prev_good, value, self._healthy_floor, self._disconnect_low_threshold
        ):
            self._last_zero_time = now
            self._episode_affected[entity_id] = now
            display_name = self._entities.get(entity_id, entity_id)

            if self._disconnect_since is None:
                self._disconnect_since = now
                self.log(
                    f"Shade gateway disconnect episode started — {display_name} "
                    f"dropped {prev_good:.0f}%→{value:.0f}%",
                    level="WARNING",
                )
            else:
                self.log(
                    f"Shade gateway disconnect continues — {display_name} "
                    f"dropped {prev_good:.0f}%→{value:.0f}%",
                    level="WARNING",
                )

        if value > self._disconnect_low_threshold:
            self._last_good_value[entity_id] = value

        self._current_value[entity_id] = value

    def _affected_shades_healthy(self) -> bool:
        """True if every shade that actually flapped THIS episode is healthy now.

        Recovery is judged only against ``_episode_affected`` — the shades
        that contributed a qualifying (implausible) drop during the current
        episode — not against every discovered shade. An unrelated shade
        that happens to be ``unavailable``/``unknown`` (e.g. a battery
        change, or any other entity hiccup with nothing to do with the
        gateway) never contributed to this episode, so it must never block
        its recovery.

        For each affected shade, ``_current_value`` must be a KNOWN numeric
        reading above ``disconnect_low_threshold``: ``None`` (explicitly
        unavailable/unknown, per ``_process_reading``) or a reading still
        at/below the threshold both mean "can't confirm this one came back"
        and block recovery. No affected shades (``_episode_affected`` empty)
        trivially counts as healthy.
        """
        if not self._episode_affected:
            return True
        for entity_id in self._episode_affected:
            value = self._current_value.get(entity_id)
            if value is None or value <= self._disconnect_low_threshold:
                return False
        return True

    def _recompute_recovery(self) -> None:
        """Clear the episode once flap-free for recovery_settle_s.

        A mid-episode 100% reading must NOT clear the episode by itself —
        only sustained flap-free health for the full settle window does,
        which is exactly what defeats the overnight flapping this checker
        exists to survive.
        """
        if self._disconnect_since is None:
            return

        if self._last_zero_time is None:
            # Defensive: episode marked active with no recorded zero time.
            if self._affected_shades_healthy():
                self._clear_episode()
            return

        now = datetime.datetime.now()
        flap_free_s = (now - self._last_zero_time).total_seconds()
        if flap_free_s >= self._recovery_settle_s and self._affected_shades_healthy():
            self.log(
                f"Shade gateway episode recovered — flap-free for {flap_free_s:.0f}s "
                f"(settle threshold {self._recovery_settle_s}s)",
                level="INFO",
            )
            self._clear_episode()

    def _clear_episode(self) -> None:
        self._disconnect_since = None
        self._last_zero_time = None
        self._episode_affected = {}
        self._repair_attempted_this_episode = False

    # ------------------------------------------------------------------
    # Check execution
    # ------------------------------------------------------------------

    async def _run_checks(self) -> None:
        """Recompute recovery, build results, drive auto-repair, and report."""
        await self._refresh_auto_repair_config()
        self._recompute_recovery()

        results = self._build_results()

        # Evaluate auto-repair logic (skip if repair is already in progress)
        if self._repair_status != REPAIR_IN_PROGRESS:
            self._evaluate_auto_repair()

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

        self.log(
            f"Check cycle complete for '{self._checker_name}': {results[0]['status']}",
            level="INFO" if results[0]["status"] == "ok" else "WARNING",
        )

    def _build_results(self) -> List[Dict[str, str]]:
        if self._disconnect_since is None:
            return [
                {
                    "name": "Gateway Link",
                    "status": "ok",
                    "detail": f"{len(self._entities)} shade batteries reporting normally",
                }
            ]

        now = datetime.datetime.now()
        minutes = int((now - self._disconnect_since).total_seconds() // 60)
        enabled, delay_min = self._read_auto_repair_config()
        deadline = self._disconnect_since + datetime.timedelta(minutes=delay_min)
        affected_names = sorted(
            self._entities.get(eid, eid) for eid in self._episode_affected
        )
        affected_str = ", ".join(affected_names) if affected_names else "unknown"

        if self._repair_status == REPAIR_IN_PROGRESS:
            status = "critical"
            detail = (
                f"Disconnected {minutes}m; {self._repair_detail}; "
                f"affected: {affected_str}"
            )
        elif self._repair_status == REPAIR_FAILED:
            status = "critical"
            detail = f"{self._repair_detail}; affected: {affected_str}"
        elif now >= deadline:
            # Past the grace deadline: page critical either way, but word it
            # honestly — an auto-restart only actually fires when auto-repair
            # is enabled; otherwise a human must power-cycle the gateway.
            status = "critical"
            if enabled:
                detail = (
                    f"Disconnected {minutes}m (past {delay_min}m auto-restart "
                    f"deadline); affected: {affected_str}"
                )
            else:
                detail = (
                    f"Disconnected {minutes}m — auto-restart disabled, manual "
                    f"gateway power-cycle needed; affected: {affected_str}"
                )
        elif enabled:
            status = "warning"
            detail = (
                f"Disconnected {minutes}m; auto-restart at "
                f"{deadline.isoformat(timespec='seconds')}; affected: {affected_str}"
            )
        else:
            status = "warning"
            detail = (
                f"Disconnected {minutes}m; auto-restart disabled "
                f"(monitoring only); affected: {affected_str}"
            )

        return [{"name": "Gateway Link", "status": status, "detail": detail}]

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

    def _evaluate_auto_repair(self) -> None:
        """Evaluate whether to start, continue, or cancel auto-repair.

        Driven directly by the gateway-level disconnect episode
        (``_disconnect_since``) rather than by check results, since the
        episode already IS the "unhealthy since" clock. One auto-restart
        per episode: ``_repair_attempted_this_episode`` blocks further auto
        attempts until the episode fully clears (manual repair via the card
        is unaffected — it always calls ``_start_repair`` directly).
        """
        episode_active = self._disconnect_since is not None

        if not episode_active:
            if self._repair_status == REPAIR_PENDING:
                self.log("Episode cleared — cancelling pending auto-repair", level="INFO")
            if self._repair_status in (REPAIR_PENDING, REPAIR_SUCCESS, REPAIR_FAILED):
                self._repair_status = REPAIR_IDLE
                self._repair_detail = ""
                self._auto_repair_deadline = None
            return

        # Don't trigger auto-repair from "success" state (waiting for the
        # episode to clear via the normal recovery path).
        if self._repair_status == REPAIR_SUCCESS:
            return

        # One restart per episode — already attempted (this also covers the
        # REPAIR_FAILED case: no branch below matches it, so it stays FAILED
        # — the human escalation page — until the episode clears).
        if self._repair_attempted_this_episode:
            return

        enabled, delay_min = self._read_auto_repair_config()
        if not enabled:
            return

        now = datetime.datetime.now()
        deadline = self._disconnect_since + datetime.timedelta(minutes=delay_min)

        if self._repair_status == REPAIR_IDLE:
            if now >= deadline:
                self.log(
                    f"Shade gateway disconnected >{delay_min}m — starting auto-repair",
                    level="WARNING",
                )
                self._start_repair()
            else:
                self._repair_status = REPAIR_PENDING
                self._auto_repair_deadline = deadline
                self._repair_detail = (
                    f"Auto-restart at {deadline.isoformat(timespec='seconds')}"
                )
        elif self._repair_status == REPAIR_PENDING:
            self._auto_repair_deadline = deadline
            if now >= deadline:
                self.log("Auto-repair deadline reached — starting repair", level="WARNING")
                self._start_repair()

    # ------------------------------------------------------------------
    # Repair execution
    # ------------------------------------------------------------------

    def _start_repair(self) -> None:
        """Initiate a repair (power cycle the PoE port feeding the gateway)."""
        if self._repair_status == REPAIR_IN_PROGRESS:
            self.log("Repair already in progress — ignoring", level="WARNING")
            return

        if not self._repair_button:
            self.log("No repair_button configured — cannot repair", level="WARNING")
            self._repair_status = REPAIR_FAILED
            self._repair_detail = "No repair button configured"
            return

        self._repair_status = REPAIR_IN_PROGRESS
        self._repair_detail = "Power-cycling gateway (PoE port)..."
        self._auto_repair_deadline = None
        self._repair_attempted_this_episode = True
        self._last_repair_attempt = datetime.datetime.now().isoformat(timespec="seconds")

        # Report status immediately so the card shows in_progress
        self._report_repair_status_only()

        self._repair_task = self.create_task(self._execute_repair())

    def _cancel_repair(self) -> None:
        """Cancel a pending auto-repair.

        Only cancels the *scheduled repair*, not the underlying disconnect
        episode — ``_disconnect_since`` is left untouched so the checker
        keeps reporting the outage even if the human cancels the countdown.
        """
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
        self._report_repair_status_only()

    async def _execute_repair(self) -> None:
        """Press the repair button, wait for gateway reboot, then poll for recovery."""
        try:
            self.log(f"Pressing {self._repair_button}", level="INFO")
            self.call_service("button/press", entity_id=self._repair_button)

            self._repair_detail = (
                f"Waiting {self._repair_settle_s}s for gateway reboot..."
            )
            self._report_repair_status_only()

            await asyncio.sleep(self._repair_settle_s)

            elapsed = self._repair_settle_s
            while elapsed <= self._repair_recovery_wait_s:
                if self._provisional_recovery_confirmed():
                    self._repair_status = REPAIR_SUCCESS
                    self._repair_detail = f"Gateway recovered after {elapsed}s"
                    self._disconnect_since = None
                    self._last_zero_time = None
                    self._episode_affected = {}
                    self._repair_attempted_this_episode = False
                    self.log(
                        f"Shade gateway repair succeeded — recovered after {elapsed}s",
                        level="INFO",
                    )
                    self._report_repair_status_only()
                    return

                self._repair_detail = (
                    f"Waiting for recovery... {elapsed}s/{self._repair_recovery_wait_s}s"
                )
                await asyncio.sleep(REPAIR_POLL_INTERVAL_S)
                elapsed += REPAIR_POLL_INTERVAL_S

            # Timed out — one restart per episode; escalate to a human page.
            self._repair_status = REPAIR_FAILED
            self._repair_detail = (
                f"Gateway power-cycle did not restore shades after "
                f"{self._repair_recovery_wait_s}s — manual intervention needed"
            )
            self.log(
                f"Shade gateway repair failed — no recovery after "
                f"{self._repair_recovery_wait_s}s",
                level="ERROR",
            )
            self._report_repair_status_only()

        except Exception as exc:
            self._repair_status = REPAIR_FAILED
            self._repair_detail = f"Repair error: {exc}"
            self.log(f"Repair execution error: {exc!r}", level="ERROR")
            self._report_repair_status_only()

    def _provisional_recovery_confirmed(self) -> bool:
        """Affected shades healthy AND flap-free since the button press for repair_settle_s.

        Judged against ``_episode_affected`` (via ``_affected_shades_healthy``),
        not every discovered shade — an unrelated shade being unavailable
        must never stall a repair that has, in fact, already succeeded.

        Deliberately a shorter bar than the ordinary ``recovery_settle_s``
        (used when nothing was repaired) — a fresh reboot that's stayed
        clean for ``repair_settle_s`` is good evidence the fix worked; we
        don't need to wait out the full ambient flap-suppression window.
        """
        if not self._affected_shades_healthy():
            return False
        if self._last_zero_time is None:
            return True
        flap_free_s = (datetime.datetime.now() - self._last_zero_time).total_seconds()
        return flap_free_s >= self._repair_settle_s

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
