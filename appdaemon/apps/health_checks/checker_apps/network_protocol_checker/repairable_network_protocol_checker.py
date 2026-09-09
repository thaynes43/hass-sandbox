"""Repairable Network Protocol Checker — NetworkProtocolChecker + software restart.

Adds rate-limited auto-repair to the generic protocol checker for radios
reached over a **TCP serial bridge** (TubesZB ESP32 boards running ESPHome).

Why this exists (2026-09-09 outage)
-----------------------------------
Z-Wave runs as ``zwave-js-ui -> tcp://tubeszb-zwave01:6638``.  The ESP32
stream server accepts exactly one client and never notices when that client
dies.  When a switch reboot dropped the TCP session, the ESP32 kept the dead
socket, refused the reconnect, and zwave-js logged "Failed to open the serial
port" every 26 s for 4 h 20 m.  Every Z-Wave entity in HA sat ``unavailable``
and the door/motion automations silently did nothing.  Restarting the *pod*
does not help — our side of the old socket sits in FIN_WAIT2 and the ESP32
never acks the close.

The verified remedy is an **ESPHome software restart** of the ESP32
(``button.restart_the_esp32_device_2``), which drops the stale client so the
waiting zwave-js reconnect loop succeeds within seconds.

SAFETY — read before changing anything here
-------------------------------------------
The dongle must **never** be power-cycled (PoE port cycling or any hard power
path): Tom's previous TubesZB board was destroyed by repeated PoE restarts.
The only action this class ever takes is a single ``button/press`` on the
ESPHome software-restart button.  On top of that, every restart passes these
hard gates, all enforced in code:

1. **Stale-client signature only.**  The monitored integration entity must be
   unhealthy *while the radio still answers ICMP*.  If ping is down the board
   is offline and a software restart cannot possibly help — we do nothing and
   let normal alerting page.  An optional ESPHome "serial connected" sensor
   adds a second confirmation that the board thinks it still has a client.
2. **Dwell** — the integration must have been unhealthy for the full
   auto-repair delay (default 5 min) before the first action.  The dwell
   clock restarts from scratch on every AppDaemon start, so an image deploy
   landing mid-outage can never trigger an immediate restart.
3. **Minimum interval** between restarts (default 15 min).
4. **Rolling 24 h cap** on restarts (default 3; ``0`` disables restarts
   entirely, behaving exactly like omitting ``repair_button``).  These two survive an
   AppDaemon restart: attempt timestamps are persisted to
   ``input_text.<checker_id>_health_repair_attempts`` and re-seeded on
   startup, so a deploy mid-outage resumes the ladder instead of resetting
   it.  They are deliberately *not* read back out of
   ``sensor.health_check_status`` — see the Persistence section below for
   why that races the controller and loses.
5. **Quiet period** after every action (default 3 min) before the checker is
   allowed to evaluate a repair again.
6. **Exactly one action per evaluation** — the state machine moves to
   ``in_progress`` before anything is pressed.

When the 24 h cap is exhausted the checker stops repairing and escalates:
the integration check is forced back to ``critical`` (bypassing the
partial-failure cross-check that would otherwise mask it as ``warning``) so
Alertmanager raises a critical alert and pages.  That escalation is the
second half of the fix — during the outage the cross-check downgrade meant
the checker only ever reported ``warning``, which never pages.

Reusable for the Zigbee TubesZB board later: point ``repair_button`` at its
ESPHome restart button.  Enabled for Z-Wave only for now.
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
from health_checks.checker_apps.network_protocol_checker.network_protocol_checker import (
    NetworkProtocolChecker,
)
from shared.check_utils import apply_cross_check

logger = logging.getLogger(__name__)

REPAIR_IDLE = "idle"
REPAIR_PENDING = "pending"
REPAIR_IN_PROGRESS = "in_progress"
REPAIR_SUCCESS = "success"
REPAIR_FAILED = "failed"

REPAIR_POLL_INTERVAL_S = 10

CONTROLLER_SENSOR = "sensor.health_check_status"

#: Rolling window the restart cap is measured over.
ATTEMPT_WINDOW_S = 24 * 60 * 60

#: States that mean "no usable reading", not a real value.
UNAVAILABLE_STATES = ("unavailable", "unknown", "none", "")

#: Bounds of the auto-repair delay helper. Every path that can set the cached
#: delay clamps to these, including the card command: HA silently rejects a
#: set_value outside the helper's range without AppDaemon raising, so an
#: unclamped cache would keep a value the helper never accepted — and a delay
#: of 0 collapses the dwell gate and restarts the board on the first
#: unhealthy cycle.
DELAY_MIN_MIN = 1
DELAY_MIN_MAX = 60


class RepairableNetworkProtocolChecker(NetworkProtocolChecker):
    """NetworkProtocolChecker with rate-limited ESPHome software-restart repair."""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        super().initialize()

        args = self.args or {}

        # Repair action
        self._repair_button: str = args.get("repair_button", "")
        self._repair_recovery_wait_s: int = int(
            args.get("repair_recovery_wait_s", 300)
        )

        # Stale-client signature guards
        self._repair_requires_radio_ping: bool = bool(
            args.get("repair_requires_radio_ping", True)
        )
        self._repair_serial_entity: str = args.get(
            "repair_serial_connected_entity", ""
        )
        # YAML coerces a bare on/off to bool — reverse it exactly like the
        # parent does for entity_healthy_state. Without this, a config saying
        # `repair_serial_connected_state: on` becomes "True", never matches a
        # binary_sensor state, and silently disables every restart forever.
        raw_serial_state = args.get("repair_serial_connected_state", "on")
        if isinstance(raw_serial_state, bool):
            self._repair_serial_state = "on" if raw_serial_state else "off"
        else:
            self._repair_serial_state = str(raw_serial_state)

        # Hard rate limits
        self._repair_min_interval_s: int = int(
            args.get("repair_min_interval_s", 900)
        )
        self._repair_max_per_24h: int = int(args.get("repair_max_per_24h", 3))
        self._repair_quiet_period_s: int = int(
            args.get("repair_quiet_period_s", 180)
        )

        # Auto-repair helper defaults
        self._auto_repair_enabled_default: bool = bool(
            args.get("auto_repair_enabled_default", False)
        )
        #: Latches the out-of-range warning so _clamp_delay says it once per
        #: episode instead of every check cycle. Must exist before the first
        #: _clamp_delay call below.
        self._delay_clamped_logged: bool = False
        # Clamped here so every path that can set the cached delay obeys the
        # helper's bounds. Without this, auto_repair_delay_min_default: 0
        # collapses the dwell for the whole first run — the same failure the
        # card clamp prevents, through a different door.
        self._auto_repair_delay_min_default: int = self._clamp_delay(
            int(args.get("auto_repair_delay_min_default", 5))
        )

        # Repair state machine
        self._repair_status: str = REPAIR_IDLE
        self._repair_detail: str = ""
        self._auto_repair_deadline: Optional[datetime.datetime] = None
        self._last_repair_attempt: Optional[str] = None
        self._unhealthy_since: Optional[datetime.datetime] = None
        self._quiet_until: Optional[datetime.datetime] = None
        self._repair_task: Optional[asyncio.Task] = None

        # Rolling attempt log (datetimes), persisted via repair_state
        self._repair_attempts: List[datetime.datetime] = []
        #: Why the partial-failure downgrade must be reversed this cycle
        #: (see _apply_escalation). Empty means no escalation.
        self._escalate_detail: str = ""

        # Cached auto-repair config (refreshed each check cycle)
        self._cached_auto_repair_enabled: bool = self._auto_repair_enabled_default
        #: None until the first read attempt; then whether it succeeded.
        self._toggle_readable: Optional[bool] = None
        self._delay_readable: Optional[bool] = None
        self._cached_auto_repair_delay_min: int = self._auto_repair_delay_min_default

        self.log(
            f"RepairableNetworkProtocolChecker repair config: "
            f"button={self._repair_button}, "
            f"min_interval={self._repair_min_interval_s}s, "
            f"max_per_24h={self._repair_max_per_24h}, "
            f"quiet={self._repair_quiet_period_s}s, "
            f"requires_ping={self._repair_requires_radio_ping}, "
            f"serial_entity={self._repair_serial_entity or 'none'}",
            level="INFO",
        )

    async def _async_startup(self) -> None:
        # Seed before anything else that awaits: the controller's own startup
        # publish overwrites the sensor's `checkers` attribute, so the
        # fallback read is only good for the first few hundred ms.
        await self._seed_attempts()
        await self._provision_repair_helpers()
        # Write the seeded log back only now that the helper is guaranteed to
        # exist. A seed from the sensor fallback (or a persist that silently
        # failed last time) would otherwise leave the durable copy empty, and
        # the next restart would come up with a fresh budget — the same
        # failure, one restart later.
        if self._repair_attempts:
            self._persist_attempts()
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
            f"RepairableNetworkProtocolChecker '{self._checker_name}' started",
            level="INFO",
        )

    # ------------------------------------------------------------------
    # Provisioning
    # ------------------------------------------------------------------

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
                # A freshly created input_boolean is off, so without this the
                # repair would be provisioned and then never run. Only applied
                # on creation — a later manual "off" is never overridden.
                if self._auto_repair_enabled_default:
                    try:
                        self.call_service(
                            "input_boolean/turn_on", entity_id=entity_id
                        )
                    except Exception as exc:
                        self.log(
                            f"Failed to enable {entity_id}: {exc!r}", level="DEBUG"
                        )
                self.log(f"Provisioned {entity_id}", level="INFO")
        except Exception as exc:
            self.log(f"Failed to provision auto-repair toggle: {exc!r}", level="ERROR")

        try:
            created = await prov.ensure_helper(
                "input_text",
                f"{self._checker_id} Health Repair Attempts",
                max=self._ATTEMPTS_HELPER_MAX_LEN,
            )
            if created:
                self.log(
                    f"Provisioned {self._attempts_helper_entity()}", level="INFO"
                )
        except Exception as exc:
            self.log(
                f"Failed to provision repair-attempts helper: {exc!r}", level="ERROR"
            )

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
    # Persistence — the restart ladder must survive an AppDaemon restart
    # ------------------------------------------------------------------
    #
    # The attempt log lives in its own input_text helper rather than being
    # read back out of sensor.health_check_status. The controller publishes
    # that sensor once at its own startup with `checkers: {}` (see
    # health_check_controller._async_startup), and set_state replaces the
    # attribute wholesale — so on a whole-pod restart the controller usually
    # wins the race and the persisted attempts are gone before any checker
    # can read them. That would hand the ladder a fresh budget of restarts
    # mid-outage, which is exactly what the cap exists to prevent. The
    # helper is owned by this app alone and is immune to that race; the
    # sensor is still read as a fallback for the first deploy, before the
    # helper exists.

    _ATTEMPTS_HELPER_MAX_LEN = 255

    def _attempts_helper_entity(self) -> str:
        return f"input_text.{self._checker_id}_health_repair_attempts"

    def _persist_attempts(self) -> None:
        """Write the rolling attempt log to its helper. Best-effort."""
        value = json.dumps(
            [a.isoformat(timespec="seconds") for a in self._repair_attempts],
            separators=(",", ":"),
        )
        if len(value) > self._ATTEMPTS_HELPER_MAX_LEN:
            # Cannot happen at realistic caps (3 attempts ~= 66 chars), but a
            # truncated write must never look like "no attempts" — keep the
            # newest, which are the ones the interval gate needs.
            keep = list(self._repair_attempts)
            while keep and len(value) > self._ATTEMPTS_HELPER_MAX_LEN:
                keep.pop(0)
                value = json.dumps(
                    [a.isoformat(timespec="seconds") for a in keep],
                    separators=(",", ":"),
                )
        try:
            self.call_service(
                "input_text/set_value",
                entity_id=self._attempts_helper_entity(),
                value=value,
            )
        except Exception as exc:
            self.log(f"Failed to persist repair attempts: {exc!r}", level="WARNING")

    async def _seed_attempts(self) -> None:
        """Re-seed the rolling attempt log after an AppDaemon restart.

        AppDaemon image deploys restart the pod.  Without this, a deploy
        landing mid-outage would reset the ladder and let the checker hammer
        the dongle.  ``_unhealthy_since`` is deliberately **not** seeded — the
        full dwell is always re-served after a restart before the first
        action.
        """
        # _async_startup is a fire-and-forget task: anything that escapes here
        # means _register() never runs and the checker is absent rather than
        # degraded — no listeners, no checks, no alerts, for exactly the
        # outage class this exists to make audible. Seeding is an
        # optimisation; losing it costs one ladder, losing the checker costs
        # everything.
        try:
            attempts = await self._read_attempts_helper()
            source = self._attempts_helper_entity()
            if attempts is None:
                attempts = await self._read_attempts_from_controller()
                source = CONTROLLER_SENSOR
            if not attempts:
                return

            self._repair_attempts = attempts
            self._prune_attempts(datetime.datetime.now())
            if self._repair_attempts:
                self._last_repair_attempt = self._repair_attempts[-1].isoformat(
                    timespec="seconds"
                )
            self.log(
                f"Seeded {len(self._repair_attempts)} repair attempt(s) in the "
                f"last 24h from {source} (most recent "
                f"{self._last_repair_attempt}) — rate limits carry over the "
                f"restart",
                level="INFO",
            )
        except Exception as exc:
            self._repair_attempts = []
            self.log(
                f"Could not seed repair attempts ({exc!r}) — starting with an "
                f"empty ladder; the dwell and interval gates still apply",
                level="ERROR",
            )

    async def _read_attempts_helper(
        self,
    ) -> Optional[List[datetime.datetime]]:
        """Read the attempt log helper. ``None`` means "no usable value"."""
        try:
            raw = await self.get_state(self._attempts_helper_entity())
        except Exception as exc:
            self.log(f"Could not read repair-attempts helper: {exc!r}", level="WARNING")
            return None
        if not raw or str(raw) in ("unknown", "unavailable"):
            return None
        try:
            parsed = json.loads(str(raw))
        except ValueError:
            self.log(f"Ignoring unparseable repair-attempts value: {raw!r}", level="WARNING")
            return None
        if not isinstance(parsed, list):
            return None
        return self._parse_attempts(parsed)

    async def _read_attempts_from_controller(
        self,
    ) -> List[datetime.datetime]:
        """Fallback: read the attempts published in the controller's sensor.

        Only useful before the helper exists (first deploy of this feature);
        it races the controller's own initial publish, which is why the
        helper is the primary store.
        """
        try:
            state = await self.get_state(CONTROLLER_SENSOR, attribute="all")
            repair_state = (
                (state or {})
                .get("attributes", {})
                .get("checkers", {})
                .get(self._checker_id, {})
                .get("repair_state")
            ) or {}
            return self._parse_attempts(repair_state.get("repair_attempts"))
        except Exception as exc:
            self.log(
                f"Failed to seed repair attempts from {CONTROLLER_SENSOR}: {exc!r}",
                level="WARNING",
            )
            return []

    @staticmethod
    def _parse_attempts(raw: Any) -> List[datetime.datetime]:
        """Parse persisted ISO timestamps, skipping anything unparseable.

        AppDaemon 4.5.13 strips falsy attribute values, so an empty list is
        simply absent on read-back — ``None`` is a normal input here.
        """
        parsed: List[datetime.datetime] = []
        for item in raw or []:
            try:
                value = datetime.datetime.fromisoformat(str(item))
            except (TypeError, ValueError):
                continue
            if value.tzinfo is not None:
                # An offset-aware string parses cleanly and then raises
                # TypeError on the first comparison against our naive clock —
                # in sorted() or against _prune_attempts' cutoff. The helper
                # is UI-visible and hand-editable, and an HA-style timestamp
                # is offset-aware, so normalise to local naive rather than
                # letting it reach a comparison.
                value = value.astimezone().replace(tzinfo=None)
            parsed.append(value)
        return sorted(parsed)

    def _prune_attempts(self, now: datetime.datetime) -> None:
        """Drop attempts older than the rolling window."""
        cutoff = now - datetime.timedelta(seconds=ATTEMPT_WINDOW_S)
        kept = [a for a in self._repair_attempts if a > cutoff]
        if len(kept) != len(self._repair_attempts):
            self._repair_attempts = kept
            self._persist_attempts()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def _register(self) -> None:
        check_names = self._build_check_names()
        payload: Dict[str, Any] = {
            "checker_id": self._checker_id,
            "checker_name": self._checker_name,
            "check_names": check_names,
            # Not hardcoded True, unlike the other repairable checkers: this
            # is the first one to document config that means "disable repair"
            # (`repair_button: ""` / `repair_max_per_24h: 0`), and the detail
            # card gates its Repair button purely on this flag. Advertising a
            # button that can only ever refuse would also push the refusal
            # string into the Alertmanager description as auto-repair context
            # on a checker configured never to repair.
            "supports_repair": self._restarts_disabled() == "",
            "repair_state": self._build_repair_state(),
        }
        if self._dependencies:
            payload["dependencies"] = self._dependencies
        self.fire_event(
            "health_check_command",
            command="register_checker",
            payload=json.dumps(payload),
        )
        disabled = self._restarts_disabled()
        repair_note = (
            f"repair support DISABLED — {disabled}" if disabled
            else f"repair support enabled, button={self._repair_button}"
        )
        self.log(
            f"Registered '{self._checker_name}' with checks: {check_names} "
            f"({repair_note})",
            level="INFO",
        )

    # ------------------------------------------------------------------
    # Check execution
    # ------------------------------------------------------------------

    async def _run_checks(self) -> None:
        await self._refresh_auto_repair_config()
        results = await self._run_checks_only()

        # Evaluate on RAW statuses, before the cross-check masks the
        # integration failure as a partial-failure warning.
        if self._repair_status != REPAIR_IN_PROGRESS:
            await self._evaluate_auto_repair(results)

        apply_cross_check(results)
        self._apply_escalation(results)

        self.fire_event(
            "health_check_command",
            command="report_status",
            payload=json.dumps(self._build_report_payload(results)),
        )

        status_parts = [f"{r['name']}={r['status']}" for r in results]
        self.log(
            f"Check cycle complete for '{self._checker_name}': "
            f"{', '.join(status_parts)}",
            level="INFO",
        )

    def _build_report_payload(
        self, results: List[Dict[str, str]]
    ) -> Dict[str, Any]:
        payload = super()._build_report_payload(results)
        payload["repair_state"] = self._build_repair_state()
        return payload

    def _apply_escalation(self, results: List[Dict[str, str]]) -> None:
        """Undo the partial-failure downgrade when a human is genuinely needed.

        ``apply_cross_check`` demotes the integration failure to ``warning``
        because the radio ping and the web UI still pass — and warnings never
        page.  That is precisely why the 2026-09-09 outage ran silently for
        4 h 20 m.  The downgrade is right for a blip, wrong once nothing
        automatic can help, so it is reversed in exactly two cases (set by
        :meth:`_evaluate_auto_repair`):

        * the rolling restart budget is spent — auto-repair has given up;
        * the integration is down *and* the radio is unreachable — the board
          is off the network, which no software restart can fix.

        The second case is not a regression introduced here: it was always
        masked the same way (zwave-js-ui's own web UI answers happily while
        the radio is gone, so two of three checks pass). Alertmanager's
        5-minute for-duration still applies, so a transient miss cannot page.
        """
        if not self._escalate_detail:
            return
        entity_result = self._find_result(results, self._entity_check_name)
        if entity_result is None or entity_result["status"] == "ok":
            return
        entity_result["status"] = "critical"
        entity_result["detail"] += f" — {self._escalate_detail}"

    @staticmethod
    def _find_result(
        results: List[Dict[str, str]], name: str
    ) -> Optional[Dict[str, str]]:
        for r in results:
            if r.get("name") == name:
                return r
        return None

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_repair_command(
        self, event_name: str, data: dict, kwargs: Any
    ) -> None:
        action = data.get("action", "")
        if action == "start_repair":
            self.log("Manual repair requested", level="INFO")
            # Manual repair skips the dwell but NOT the rate limits: the
            # board is fragile and the caps exist to protect the hardware.
            allowed, reason = self._rate_limit_check(datetime.datetime.now())
            if not allowed:
                self._repair_status = REPAIR_FAILED
                self._repair_detail = reason
                self.log(f"Manual repair refused: {reason}", level="WARNING")
                self._report_repair_status_only()
                return
            self._start_repair()
        elif action == "cancel_repair":
            self._cancel_repair()
        elif action == "update_repair_config":
            self.log(
                f"Repair config update requested: "
                f"auto_repair_enabled={data.get('auto_repair_enabled')}, "
                f"auto_repair_delay_min={data.get('auto_repair_delay_min')}",
                level="INFO",
            )
            self._update_repair_config(data)

    def _cancel_repair(self) -> None:
        """Stand down a *scheduled* restart, without ending the outage.

        Unlike the shade gateway (one restart per episode), this checker
        re-arms every cycle, so simply dropping back to ``idle`` would let the
        countdown fire again on the very next tick. Cancelling therefore
        restarts the dwell clock: the human gets a real deferral of one full
        auto-repair delay. ``_repair_attempts`` is untouched — cancelling is
        not a restart and must not buy back budget.
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
    # Auto-repair config
    # ------------------------------------------------------------------

    async def _refresh_auto_repair_config(self) -> None:
        """Refresh the cached toggle/delay from their HA helpers.

        A read that comes back ``None`` means AppDaemon does not know the
        entity — which is the normal state for the whole first run after these
        helpers are provisioned, because AppDaemon loads the entity list at
        startup and the helpers did not exist then. ``str(None) == "on"`` is
        False, so treating that as a real read silently disables auto-repair
        until the next pod restart: the feature ships inert, with nothing in
        the logs to say so (observed on the 1.17.0 deploy). An unknown value
        is not evidence, so the previous cached value is kept — which on the
        first run is ``auto_repair_enabled_default``.
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
        return self._cached_auto_repair_enabled, self._cached_auto_repair_delay_min

    # ------------------------------------------------------------------
    # Rate limiting
    # ------------------------------------------------------------------

    def _rate_limit_check(self, now: datetime.datetime) -> tuple[bool, str]:
        """Return ``(allowed, reason)`` for a restart at *now*.

        Both limits are hard: the 24 h cap protects the board from the
        repeated-restart failure mode that destroyed the previous dongle, and
        the minimum interval keeps a flapping integration from turning into a
        restart loop.
        """
        disabled = self._restarts_disabled()
        if disabled:
            return False, disabled

        self._prune_attempts(now)

        if self._cap_is_spent(now):
            return False, self._cap_detail()

        if self._repair_attempts:
            last = self._repair_attempts[-1]
            earliest = last + datetime.timedelta(seconds=self._repair_min_interval_s)
            if now < earliest:
                return False, (
                    f"Rate limited: next restart allowed "
                    f"{earliest.isoformat(timespec='seconds')}"
                )

        return True, ""

    def _cap_detail(self) -> str:
        """Human-readable reason the restart budget is spent."""
        # _cap_is_spent only reports True with a positive cap and at least one
        # attempt, but never index blind.
        if not self._repair_attempts:
            return "Cap reached: no restarts permitted"
        retry_at = self._repair_attempts[0] + datetime.timedelta(
            seconds=ATTEMPT_WINDOW_S
        )
        return (
            f"Cap reached: {len(self._repair_attempts)}/"
            f"{self._repair_max_per_24h} restarts in 24h "
            f"(next allowed {retry_at.isoformat(timespec='seconds')})"
        )

    def _cap_is_spent(self, now: datetime.datetime) -> bool:
        """True when a real restart budget has actually been used up.

        A configured budget of ``<= 0`` is *not* a spent cap — nothing was
        ever consumed — so it returns False here and is handled by
        :meth:`_restarts_disabled` instead. Reporting it as "cap reached"
        would page on the first cycle of any outage with a fabricated
        diagnosis, the same shape as treating a missing ping check as an
        unreachable radio.
        """
        if self._repair_max_per_24h <= 0:
            return False
        self._prune_attempts(now)
        return len(self._repair_attempts) >= self._repair_max_per_24h

    def _restarts_disabled(self) -> str:
        """Reason restarts are switched off by config, or "" if they are on.

        Both forms behave identically — hold, no action, and keep the normal
        partial-failure ``warning`` downgrade — because neither is evidence
        about the outage itself.
        """
        if not self._repair_button:
            return "No repair button configured"
        if self._repair_max_per_24h <= 0:
            return "Restarts disabled (repair_max_per_24h = 0)"
        return ""

    # ------------------------------------------------------------------
    # Auto-repair evaluation
    # ------------------------------------------------------------------

    async def _evaluate_auto_repair(self, results: List[Dict[str, str]]) -> None:
        """Decide whether to press the restart button. At most one action.

        Guard order matters.  The ping/serial guards run first so a genuinely
        offline board is never mis-diagnosed as a spent repair budget, and the
        cap verdict is settled before the quiet-period guard so the escalation
        to critical cannot flicker on and off between cycles.
        """
        now = datetime.datetime.now()
        self._escalate_detail = ""

        entity_result = self._find_result(results, self._entity_check_name)
        ping_result = self._find_result(results, self._radio_check_name)

        integration_bad = (
            entity_result is not None and entity_result["status"] != "ok"
        )

        if not integration_bad:
            self._clear_ladder()
            return

        # The integration is down. From here on we take at most ONE action.
        if self._unhealthy_since is None:
            self._unhealthy_since = now

        # Guard 1: the radio must still answer ping. If it does not, the
        # board is offline/unreachable and a software restart cannot help.
        if self._repair_requires_radio_ping:
            if ping_result is None:
                # No radio_host configured: the stale-client signature cannot
                # be confirmed, so never act — and never escalate either, since
                # nothing here observed the radio to be down.
                self._hold("No radio ping configured — cannot confirm signature")
                return
            if ping_result["status"] != "ok":
                self._hold("Radio unreachable — software restart cannot help")
                # The board is off the network entirely. Nothing here can fix
                # that, so page rather than let the cross-check mask it.
                self._escalate_detail = (
                    "radio unreachable, board is offline — manual action needed"
                )
                return

        # Guard 2: the rolling 24 h cap. Deliberately settled BEFORE the
        # serial-sensor and enabled guards. After three restarts that didn't
        # take, the ESPHome serial sensor may well read `off` — but that is
        # not evidence the outage got smaller, and letting it (or a toggle
        # someone flipped) short-circuit the escalation would drop Z-Wave
        # back to a non-paging `warning` while it is still fully down and
        # auto-repair has already given up. Once the budget is spent a human
        # is needed no matter what the other signals say.
        if self._cap_is_spent(now):
            detail = self._cap_detail()
            if self._repair_status != REPAIR_FAILED:
                self.log(
                    f"Auto-repair cap reached ({self._repair_max_per_24h}/24h) "
                    f"— giving up and escalating to critical",
                    level="WARNING",
                )
            self._repair_status = REPAIR_FAILED
            self._repair_detail = detail
            self._auto_repair_deadline = None
            self._escalate_detail = (
                f"auto-repair cap reached "
                f"({self._repair_max_per_24h} restarts/24h), manual action needed"
            )
            return

        # Guard 3 (optional): the ESPHome board still believes it has a
        # serial client — the stale-client fingerprint.
        if self._repair_serial_entity:
            try:
                serial_state = await self.get_state(self._repair_serial_entity)
            except Exception as exc:
                self.log(
                    f"Failed to read {self._repair_serial_entity}: {exc!r}",
                    level="WARNING",
                )
                serial_state = None
            if str(serial_state) != self._repair_serial_state:
                self._hold(
                    f"Not the stale-client signature "
                    f"({self._repair_serial_entity}={serial_state})"
                )
                return

        enabled, delay_min = self._read_auto_repair_config()
        if not enabled:
            self._hold("Auto-repair disabled")
            return

        disabled = self._restarts_disabled()
        if disabled:
            self._hold(disabled)
            return

        # Guard 4: post-action quiet period.
        if self._quiet_until and now < self._quiet_until:
            self._hold(
                f"Quiet period until "
                f"{self._quiet_until.isoformat(timespec='seconds')}"
            )
            return

        # Guard 5: dwell and minimum interval, whichever lands later. The
        # dwell clock always restarts from scratch after an AppDaemon start,
        # so a deploy landing mid-outage never acts immediately.
        deadline = self._unhealthy_since + datetime.timedelta(minutes=delay_min)
        if self._repair_attempts:
            earliest = self._repair_attempts[-1] + datetime.timedelta(
                seconds=self._repair_min_interval_s
            )
            deadline = max(deadline, earliest)

        if now < deadline:
            self._repair_status = REPAIR_PENDING
            self._auto_repair_deadline = deadline
            self._repair_detail = (
                f"Auto-repair at {deadline.isoformat(timespec='seconds')}"
            )
            return

        self.log(
            f"Integration unhealthy for >{delay_min}m with the radio still "
            f"pingable — restarting the ESP32 via {self._repair_button}",
            level="INFO",
        )
        self._start_repair()

    def _clear_ladder(self) -> None:
        """Integration is healthy — clear the dwell/pending state.

        The rolling attempt log is deliberately **kept**: the 24 h cap counts
        restarts, not outages, so a flapping radio cannot buy fresh restarts
        by briefly recovering between them.
        """
        if self._repair_status in (REPAIR_PENDING, REPAIR_SUCCESS, REPAIR_FAILED):
            self._repair_status = REPAIR_IDLE
            self._repair_detail = ""
        self._auto_repair_deadline = None
        self._unhealthy_since = None
        self._escalate_detail = ""

    def _hold(self, detail: str) -> None:
        """Record why no action was taken this cycle, without acting.

        ``_hold`` is only ever reached with the integration *down*, which makes
        a lingering ``success`` stale by definition — and ``success`` is one of
        the bridge's repair-hold states, so leaving it up would withhold the
        critical promotion for up to ``repair_hold_cap_s`` (1800 s) while the
        outage is back. ``failed`` is kept: the bridge releases on it, and it
        carries the reason a human needs. Both clear once the integration
        actually recovers (see :meth:`_clear_ladder`).
        """
        if self._repair_status in (REPAIR_PENDING, REPAIR_SUCCESS):
            self._repair_status = REPAIR_IDLE
        self._auto_repair_deadline = None
        self._repair_detail = detail

    # ------------------------------------------------------------------
    # Repair execution
    # ------------------------------------------------------------------

    def _start_repair(self) -> None:
        if self._repair_status == REPAIR_IN_PROGRESS:
            self.log("Repair already in progress — ignoring", level="WARNING")
            return

        if not self._repair_button:
            self._repair_status = REPAIR_FAILED
            self._repair_detail = "No repair button configured"
            return

        self._repair_status = REPAIR_IN_PROGRESS
        self._repair_detail = "Restarting ESP32 (ESPHome software restart)..."
        self._auto_repair_deadline = None
        # Auto-repair evaluation is skipped while in progress, but
        # _apply_escalation still runs each cycle — so tie the escalation to a
        # live evaluation rather than letting a stale reason force critical
        # through the whole recovery window.
        self._escalate_detail = ""

        self._report_repair_status_only()
        self._repair_task = self.create_task(self._execute_repair())

    async def _execute_repair(self) -> None:
        try:
            # Record the attempt BEFORE pressing. If anything below throws or
            # the pod dies mid-repair, the attempt still counts against the
            # rate limits — the caps must never be bypassed by a crash.
            now = datetime.datetime.now()
            self._repair_attempts.append(now)
            self._prune_attempts(now)
            self._last_repair_attempt = now.isoformat(timespec="seconds")
            self._persist_attempts()

            self.log(
                f"Pressing {self._repair_button} "
                f"(attempt {len(self._repair_attempts)}/{self._repair_max_per_24h} "
                f"in the last 24h)",
                level="INFO",
            )
            self.call_service("button/press", entity_id=self._repair_button)

            self._repair_detail = "Waiting for the integration to recover..."
            self._report_repair_status_only()

            elapsed = 0
            while elapsed < self._repair_recovery_wait_s:
                await asyncio.sleep(REPAIR_POLL_INTERVAL_S)
                elapsed += REPAIR_POLL_INTERVAL_S

                result = await self._check_entity_state()
                if result["status"] == "ok":
                    self._repair_status = REPAIR_SUCCESS
                    self._repair_detail = f"Recovered after {elapsed}s"
                    self._unhealthy_since = None
                    self._escalate_detail = ""
                    self.log(
                        f"Repair successful — {self._entity_check_name} back to "
                        f"'{self._entity_healthy_state}' after {elapsed}s",
                        level="INFO",
                    )
                    self._pending_repair_events.append({
                        "result": "success",
                        "duration_s": elapsed,
                    })
                    self._finish_repair()
                    return

                self._repair_detail = (
                    f"Waiting for the integration to recover... "
                    f"{elapsed}s/{self._repair_recovery_wait_s}s"
                )

            self._repair_status = REPAIR_FAILED
            self._repair_detail = (
                f"Did not recover after {self._repair_recovery_wait_s}s"
            )
            self.log(
                f"Repair failed — no recovery after "
                f"{self._repair_recovery_wait_s}s",
                level="WARNING",
            )
            self._pending_repair_events.append({
                "result": "failed",
                "duration_s": self._repair_recovery_wait_s,
            })
            self._finish_repair()

        except Exception as exc:
            self._repair_status = REPAIR_FAILED
            self._repair_detail = f"Repair error: {exc}"
            self.log(f"Repair error: {exc!r}", level="ERROR")
            self._pending_repair_events.append({"result": "failed"})
            self._finish_repair()

    def _finish_repair(self) -> None:
        """Start the post-action quiet period and publish the outcome."""
        self._quiet_until = datetime.datetime.now() + datetime.timedelta(
            seconds=self._repair_quiet_period_s
        )
        self._report_repair_status_only()

    def _report_repair_status_only(self) -> None:
        self.fire_event(
            "health_check_command",
            command="report_status",
            payload=json.dumps(self._build_report_payload([])),
        )

    # ------------------------------------------------------------------
    # Repair config updates (from the health dashboard card)
    # ------------------------------------------------------------------

    def _update_repair_config(self, data: dict) -> None:
        auto_enabled = data.get("auto_repair_enabled")
        delay_min = data.get("auto_repair_delay_min")

        if auto_enabled is not None:
            entity_id = f"input_boolean.{self._checker_id}_health_auto_repair"
            current = str(self.get_state(entity_id))
            desired = "on" if auto_enabled else "off"
            # Mirror the command into the cache, but only once the helper
            # actually holds it. While the helper is unreadable (see
            # _refresh_auto_repair_config) the next get_state stays None for
            # the rest of the run, so waiting for the read-back would leave an
            # explicit "off" unhonoured — the card would show off, HA would
            # show off, and the board would still get restarted. Caching a
            # write that FAILED is the mirror-image bug, so it goes in the
            # success path.
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
            # so a card sending "17.0" is handled rather than fatal.
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
            # Published for the dashboard only. The durable copy that the
            # rate limits are re-seeded from is the input_text helper — see
            # _persist_attempts / _seed_attempts.
            "repair_attempts": [
                a.isoformat(timespec="seconds") for a in self._repair_attempts
            ],
            "repair_attempts_24h": len(self._repair_attempts),
            "repair_max_per_24h": self._repair_max_per_24h,
        }
