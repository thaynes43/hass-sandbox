"""Basic Device Health Checker — generic, config-driven device monitor.

A simple checker for devices that need entity state monitoring and an
optional IP ping.  Each instance monitors one device with configurable
checks:

1. **Entity checks** — verify one or more HA entities match expected states
2. **IP ping** — ICMP ping the device (optional). A ``ping_host`` given as a
   name can also take a ``ping_fallback_host`` (its IP), pinged only when the
   name does not resolve — see ``_check_ping``.

No repair support — this is a lightweight monitor for devices that
cannot be auto-repaired from AppDaemon.

Communication with the controller is event-only (never ``get_app``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Add health_checks package root so we can import shared utilities
_health_checks_root = str(Path(__file__).resolve().parents[2])
if _health_checks_root not in sys.path:
    sys.path.insert(0, _health_checks_root)

import hassapi as hass

from shared.check_utils import apply_cross_check, ping_check

logger = logging.getLogger(__name__)

#: How long ``_check_ping`` waits for ``ping_host`` to resolve before treating
#: the name path as broken (a hung resolver). Only used with a
#: ``ping_fallback_host``.
PING_RESOLVE_TIMEOUT_S = 3


async def _resolve_ipv4(host: str) -> Tuple[bool, str]:
    """Whether *host* resolves to an IPv4 address; ``(False, why)`` if not.

    ``socket.gaierror`` (NXDOMAIN and friends) and a resolver that does not
    answer within ``PING_RESOLVE_TIMEOUT_S`` both mean "no". Anything else is
    re-raised: the caller cannot tell, so it must not blame DNS.
    """
    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(
            loop.getaddrinfo(host, None, family=socket.AF_INET),
            timeout=PING_RESOLVE_TIMEOUT_S,
        )
        return True, ""
    except socket.gaierror as exc:
        return False, f"resolver error: {exc}"
    except asyncio.TimeoutError:
        return False, f"resolver timed out after {PING_RESOLVE_TIMEOUT_S}s"


class BasicDeviceChecker(hass.Hass):
    """Config-driven health checker for a single device."""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        self._configure()
        # Schedule startup only once the WHOLE _configure() chain has run.
        # AppDaemon can fire this run_in(0) callback, and so start
        # _async_startup, while initialize() is still executing. When the
        # schedule sat at the end of the base initialize(), a subclass that
        # set its own state after super().initialize() raced it: on
        # 2026-10-03 the Z-Wave checker's startup ran ~200 ms before
        # RepairableNetworkProtocolChecker had set _repair_attempts, died on
        # an AttributeError, never registered, and every checker depending
        # on "zwave" read unknown until the app was restarted. Subclasses
        # override _configure(), never initialize().
        self.run_in(self._on_startup, 0)

    def _configure(self) -> None:
        """Read args and build state. Must not schedule anything."""
        args = self.args or {}

        # Identity
        self._checker_id: str = args.get("checker_id", "device")
        self._checker_name: str = args.get("checker_name", self._checker_id)

        # IP ping (optional)
        self._ping_host: str = args.get("ping_host", "")
        self._ping_check_name: str = args.get("ping_check_name", "Ping")
        # Wi-Fi devices in power-save routinely drop a lone ping; retry before
        # calling it a miss (ok on the first success).
        self._ping_attempts: int = max(1, int(args.get("ping_attempts", 1)))
        # Optional IP for a ping_host given as a name: pinged only when the
        # name does not resolve, so the device answers for itself during a
        # DNS outage (up → warning, down → critical) instead of the outage
        # reading as a dead device.
        self._ping_fallback_host: str = args.get("ping_fallback_host", "") or ""
        # Whether the last ping went via the fallback (for transition logs).
        self._ping_using_fallback: bool = False

        # Entity checks (list of dicts with entity_id, healthy_state, name)
        # healthy_state can be:
        #   - a specific value (e.g. "active", "ok") — exact match
        #   - omitted or empty — any state except unavailable/unknown is ok
        raw_entities = args.get("entities", [])
        self._entities: List[Dict[str, str]] = []
        for e in raw_entities:
            raw_healthy = e.get("healthy_state")
            if raw_healthy is None or raw_healthy == "":
                healthy = ""  # empty means "not unavailable/unknown"
            elif isinstance(raw_healthy, bool):
                # YAML coerces "on"/"off" to bool — reverse it
                healthy = "on" if raw_healthy else "off"
            else:
                healthy = str(raw_healthy)
            self._entities.append({
                "entity_id": e.get("entity_id", ""),
                "healthy_state": healthy,
                "name": e.get("name", e.get("entity_id", "Entity")),
            })

        # Timing
        self._check_interval_s: int = int(args.get("check_interval_s", 180))

        # Repair events pending delivery to the controller — drained into the
        # next report_status payload by _build_report_payload (edge events,
        # delivered once). Only populated by repair-capable subclasses.
        self._pending_repair_events: List[Dict[str, Any]] = []

        self.log(
            f"BasicDeviceChecker initialising: id={self._checker_id}, "
            f"name={self._checker_name}, ping={self._ping_host}, "
            f"entities={len(self._entities)}",
            level="INFO",
        )

    def _on_startup(self, kwargs: Any) -> None:
        self.create_task(self._async_startup())

    async def _async_startup(self) -> None:
        self._register()

        self.listen_event(
            self._on_controller_ready, "health_check_controller_ready"
        )
        self.listen_event(self._on_recheck, "health_check_recheck")

        self.run_in(self._first_check, 5)
        self.log(
            f"BasicDeviceChecker '{self._checker_name}' started", level="INFO"
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
            }),
        )
        self.log(
            f"Registered '{self._checker_name}' with checks: {check_names}",
            level="INFO",
        )

    def _build_check_names(self) -> List[str]:
        names = []
        if self._ping_host:
            names.append(self._ping_check_name)
        for e in self._entities:
            names.append(e["name"])
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
        results = await self._run_checks_only()
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

    async def _run_checks_only(self) -> List[Dict[str, str]]:
        """Run all checks and return results without reporting."""
        results: List[Dict[str, str]] = []
        if self._ping_host:
            results.append(await self._check_ping())
        for entity_conf in self._entities:
            results.append(await self._check_entity_state(entity_conf))
        return results

    def _build_report_payload(self, results: List[Dict[str, str]]) -> Dict[str, Any]:
        """Build the report_status payload. Subclasses can extend to add repair_state.

        Drains any pending repair_events buffered by a repair-capable
        subclass so they ride along on the very next report_status call —
        these are one-shot edge events and must never be sent twice.
        """
        payload: Dict[str, Any] = {
            "checker_id": self._checker_id,
            "results": results,
        }
        if self._pending_repair_events:
            payload["repair_events"] = self._pending_repair_events
            self._pending_repair_events = []
        return payload

    async def _check_ping(self) -> Dict[str, str]:
        try:
            if self._ping_fallback_host:
                result = await self._ping_with_fallback()
            else:
                # No fallback: exactly the plain ping by name (no pre-resolve).
                result = await ping_check(
                    self._ping_host, attempts=self._ping_attempts
                )
            return {
                "name": self._ping_check_name,
                "status": result["status"],
                "detail": result["detail"],
            }
        except Exception as exc:
            self.log(f"Ping check failed: {exc!r}", level="ERROR")
            return {
                "name": self._ping_check_name,
                "status": "critical",
                "detail": f"Error: {exc}",
            }

    async def _ping_with_fallback(self) -> Dict[str, str]:
        """Resolve ``ping_host`` first, then decide which address to ping.

        Resolve first, because only the resolver can say whether the *name*
        is the problem. If it resolves, the ping by name is authoritative and
        its result stands — no fallback, so a device that merely drops ICMP
        keeps the configured miss tolerance (``ping_attempts``) and is not
        blamed on DNS. If it does not (``socket.gaierror``, or no answer
        within ``PING_RESOLVE_TIMEOUT_S`` — a hung resolver), the fallback IP
        answers instead. A name that resolves here but not a moment later in
        ``ping`` (``cannot resolve``, a race) takes the fallback too.
        """
        resolves, why = await _resolve_ipv4(self._ping_host)
        if not resolves:
            return await self._ping_fallback(why)

        result = await ping_check(self._ping_host, attempts=self._ping_attempts)
        if str(result.get("detail", "")).startswith("cannot resolve"):
            return await self._ping_fallback("ping could not resolve it")
        if self._ping_using_fallback:
            self._ping_using_fallback = False
            self.log(
                f"{self._ping_host} resolves again — back to pinging it by name",
                level="INFO",
            )
        return result

    async def _ping_fallback(self, why: str) -> Dict[str, str]:
        """Ping ``ping_fallback_host`` because ``ping_host`` does not resolve.

        For a repairable checker an unresolvable name would otherwise read as
        a dead device (``ping_check``: critical, ``cannot resolve <host>``)
        and power-cycle a healthy one. With an IP to fall back on, the device
        answers for itself; the detail always says the name did not resolve:

        * fallback answers → ``warning``,
          ``"<ms> via <fallback> — cannot resolve <host>"``: the device is up
          and DNS is not. Warning is UI-only (``alertmanager_bridge`` maps it
          to ``severity=warning``, and only critical reaches the phone) and
          never arms a repair (``any_bad`` is critical/degraded only).
        * fallback does not answer → its own status (``critical``), same
          detail shape, so a dead device still repairs and pages during a DNS
          outage.

        *why* is for the log only (resolver error, resolver timeout, race).
        """
        fallback = self._ping_fallback_host
        if not self._ping_using_fallback:
            self._ping_using_fallback = True
            self.log(
                f"{self._ping_host} does not resolve ({why}) — pinging "
                f"{fallback} instead until it does",
                level="WARNING",
            )
        result = await ping_check(fallback, attempts=self._ping_attempts)
        detail = (
            f"{result['detail']} via {fallback} — "
            f"cannot resolve {self._ping_host}"
        )
        if result["status"] == "ok":
            return {"status": "warning", "detail": detail}
        return {"status": result["status"], "detail": detail}

    async def _check_entity_state(self, entity_conf: dict) -> Dict[str, str]:
        entity_id = entity_conf["entity_id"]
        healthy_state = entity_conf["healthy_state"]
        name = entity_conf["name"]

        try:
            state = await self.get_state(entity_id)
            if state is None or str(state) in ("unavailable", "unknown"):
                return {
                    "name": name,
                    "status": "critical",
                    "detail": f"State: {state}",
                }
            if not healthy_state:
                # No specific state required — just not unavailable/unknown
                return {"name": name, "status": "ok", "detail": str(state)}
            if str(state) == healthy_state:
                return {"name": name, "status": "ok", "detail": str(state)}
            return {
                "name": name,
                "status": "critical",
                "detail": f"Expected '{healthy_state}', got '{state}'",
            }
        except Exception as exc:
            self.log(
                f"Entity check failed for {entity_id}: {exc!r}", level="ERROR"
            )
            return {
                "name": name,
                "status": "critical",
                "detail": f"Error: {exc}",
            }
