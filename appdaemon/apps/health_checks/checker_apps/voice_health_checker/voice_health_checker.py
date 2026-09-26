"""Voice Health Checker — is the voice assistant stack actually working?

Home Assistant cannot tell: the Wyoming ``stt``/``tts`` entities and the
``conversation`` agent stay "available" while the speech servers behind them
are down (they only go unavailable for a moment when an entry reloads).  This
checker probes each piece directly, one named check per configured entry:

* ``type: wyoming`` — a Wyoming ``describe`` handshake against ``host``:``port``
  that must list an installed ``service`` program (``asr`` = speech-to-text,
  ``tts`` = text-to-speech).  The same handshake HA's Wyoming integration uses.
* ``type: agent`` — the conversation agent ``entity_id`` must exist and not be
  unavailable (its integration is loaded), and ``reachability_url`` (the LLM
  API) must answer an anonymous GET with anything below HTTP 500.  No API key
  is held or sent.  ``dependency: cloud`` masks it while the internet is down.

The checks are independent services, so there is no cross-check: any one of
them down breaks voice and is critical on its own.

Communication with the controller is event-only (never ``get_app``).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

# Add health_checks package root so we can import shared utilities
_health_checks_root = str(Path(__file__).resolve().parents[2])
if _health_checks_root not in sys.path:
    sys.path.insert(0, _health_checks_root)

import hassapi as hass

from shared.check_utils import http_reachable_check, wyoming_check

_CHECK_TYPES = ("wyoming", "agent")


class VoiceHealthChecker(hass.Hass):
    """Checks the speech servers and the LLM agent behind the Assist pipelines."""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        args = self.args or {}

        self._checker_id: str = args.get("checker_id", "voice")
        self._checker_name: str = args.get("checker_name", "Voice")
        self._check_interval_s: int = int(args.get("check_interval_s", 120))
        self._check_timeout_s: int = int(args.get("check_timeout_s", 5))

        self._checks: List[Dict[str, Any]] = []
        for entry in args.get("checks", []) or []:
            check = self._parse_check(entry)
            if check is not None:
                self._checks.append(check)

        self.log(
            f"VoiceHealthChecker initialising: id={self._checker_id}, "
            f"name={self._checker_name}, "
            f"checks={[c['name'] for c in self._checks]}",
            level="INFO",
        )

        self.run_in(self._on_startup, 0)

    def _parse_check(self, entry: Any) -> Dict[str, Any] | None:
        """Validate one ``checks`` entry; log and skip a malformed one."""
        if not isinstance(entry, dict):
            self.log(f"Skipping non-dict check entry: {entry!r}", level="WARNING")
            return None
        name = entry.get("name")
        kind = entry.get("type")
        if not name or kind not in _CHECK_TYPES:
            self.log(
                f"Skipping check {entry!r}: needs a name and type in {_CHECK_TYPES}",
                level="WARNING",
            )
            return None
        if kind == "wyoming":
            if entry.get("service") not in ("asr", "tts") or not entry.get("host") or not entry.get("port"):
                self.log(
                    f"Skipping wyoming check {name!r}: needs service asr|tts, host and port",
                    level="WARNING",
                )
                return None
        if kind == "agent" and not entry.get("entity_id"):
            self.log(f"Skipping agent check {name!r}: needs entity_id", level="WARNING")
            return None
        return dict(entry)

    def _on_startup(self, kwargs: Any) -> None:
        """run_in callback — launches the async startup coroutine."""
        self.create_task(self._async_startup())

    async def _async_startup(self) -> None:
        """Register with controller, set up listeners and timer."""
        self._register()

        self.listen_event(self._on_controller_ready, "health_check_controller_ready")
        self.listen_event(self._on_recheck, "health_check_recheck")

        self.run_in(self._first_check, 5)

        self.log("VoiceHealthChecker started", level="INFO")

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def _register(self) -> None:
        """Fire registration event to the controller."""
        check_names = [c["name"] for c in self._checks]

        dep_map: Dict[str, List[str]] = {}
        for c in self._checks:
            dep = c.get("dependency")
            if dep:
                dep_map.setdefault(dep, []).append(c["name"])

        payload: Dict[str, Any] = {
            "checker_id": self._checker_id,
            "checker_name": self._checker_name,
            "check_names": check_names,
        }
        if dep_map:
            payload["dependencies"] = [
                {"checker_id": dep_id, "affects_checks": names}
                for dep_id, names in dep_map.items()
            ]

        self.fire_event(
            "health_check_command",
            command="register_checker",
            payload=json.dumps(payload),
        )
        self.log(
            f"Registered '{self._checker_name}' with checks: {check_names}, "
            f"dependencies: {list(dep_map.keys())}",
            level="INFO",
        )

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_controller_ready(self, event_name: str, data: dict, kwargs: Any) -> None:
        """Re-register when controller (re)starts."""
        self.log(
            f"Controller ready — re-registering '{self._checker_name}'",
            level="INFO",
        )
        self._register()
        self.create_task(self._run_checks())

    def _on_recheck(self, event_name: str, data: dict, kwargs: Any) -> None:
        """Run check immediately on force-recheck request."""
        self.log(f"Force recheck requested for '{self._checker_name}'", level="INFO")
        self.create_task(self._run_checks())

    def _first_check(self, kwargs: Any) -> None:
        """Run the first check, then start periodic timer."""
        self.create_task(self._run_checks())
        self.run_every(
            self._check_tick,
            f"now+{self._check_interval_s}",
            self._check_interval_s,
        )

    def _check_tick(self, kwargs: Any) -> None:
        """Periodic timer callback."""
        self.create_task(self._run_checks())

    # ------------------------------------------------------------------
    # Check execution
    # ------------------------------------------------------------------

    async def _run_one(self, check: Dict[str, Any]) -> Dict[str, str]:
        """Run a single configured check and return ``{status, detail}``."""
        if check["type"] == "wyoming":
            return await wyoming_check(
                check["host"],
                int(check["port"]),
                check["service"],
                timeout_s=self._check_timeout_s,
            )

        # type: agent
        entity_id = check["entity_id"]
        state = await self.get_state(entity_id)
        if state is None:
            return {"status": "critical", "detail": f"{entity_id} not found"}
        if state == "unavailable":
            return {"status": "critical", "detail": f"{entity_id} unavailable"}
        url = check.get("reachability_url")
        if not url:
            return {"status": "ok", "detail": "agent loaded"}
        label = f"{check.get('reachability_name', 'API')} reachable"
        return await http_reachable_check(url, timeout_s=self._check_timeout_s, label=label)

    async def _run_checks(self) -> None:
        """Execute all configured checks and report results."""
        results: List[Dict[str, str]] = []

        for check in self._checks:
            name = check["name"]
            try:
                result = await self._run_one(check)
            except Exception as exc:
                self.log(f"Voice check {name!r} failed: {exc!r}", level="ERROR")
                result = {"status": "critical", "detail": f"Error: {exc}"}
            results.append({
                "name": name,
                "status": result["status"],
                "detail": result["detail"],
            })

        self.fire_event(
            "health_check_command",
            command="report_status",
            payload=json.dumps({
                "checker_id": self._checker_id,
                "results": results,
            }),
        )

        status_parts = [f"{r['name']}={r['status']}" for r in results]
        self.log(
            f"Check cycle complete for '{self._checker_name}': "
            f"{', '.join(status_parts)}",
            level="INFO" if any(r["status"] != "ok" for r in results) else "DEBUG",
        )
