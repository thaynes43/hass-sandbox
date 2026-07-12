"""ImageGen Health Checker — detects a wedged or dead ComfyUI instance.

ComfyUI going down (or its queue freezing) is the canonical symptom of the
GPU falling off the PCI bus on the Proxmox host — a failure only a host
reboot fixes, so this checker is **page-only**: no auto-repair.

Two checks on a configurable interval, polling ``GET /prompt``
(``exec_info.queue_remaining``) via
:class:`providers.ai_providers.comfyui.comfyui_status_client.ComfyUIStatusClient`:

1. **API Reachable** — warning while the endpoint is unreachable, escalating
   to critical after ``unreachable_after_s`` (default 15m).
2. **Queue Progress** — critical when ``queue_remaining`` stays > 0 without
   *any* value change for ``queue_stuck_after_s`` (default 30m).  The first
   generation after a ComfyUI restart takes ~8.5 minutes (model load) but
   completes — the queue counter moves — so the 30m threshold cannot
   false-positive on cold starts.  ComfyUI's queue is in-memory: a restart
   resets it to 0, which simply reads as healthy.

Communication with the controller is event-only (never ``get_app``).
"""

from __future__ import annotations

import datetime
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Add appdaemon root for providers
_appdaemon_root = str(Path(__file__).resolve().parents[4])
if _appdaemon_root not in sys.path:
    sys.path.insert(0, _appdaemon_root)

import hassapi as hass

from providers.ai_providers.comfyui.comfyui_status_client import (
    ComfyUIStatusClient,
    ComfyUIStatusError,
)

logger = logging.getLogger(__name__)

API_CHECK = "API Reachable"
QUEUE_CHECK = "Queue Progress"

CONTROLLER_SENSOR = "sensor.health_check_status"


def _fmt_minutes(seconds: float) -> str:
    return f"{int(max(0.0, seconds) / 60)}m"


class ImageGenHealthChecker(hass.Hass):
    """Health checker for the ComfyUI image-generation service (page only)."""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        args = self.args or {}

        # Identity
        self._checker_id: str = args.get("checker_id", "imagegen")
        self._checker_name: str = args.get("checker_name", "Image Gen")

        # Polling
        self._comfyui_url: str = args.get("comfyui_url", "")
        self._check_interval_s: int = int(args.get("check_interval_s", 120))
        self._request_timeout_s: int = int(args.get("request_timeout_s", 10))

        # Thresholds
        self._queue_stuck_after_s: int = int(args.get("queue_stuck_after_s", 1800))
        self._unreachable_after_s: int = int(args.get("unreachable_after_s", 900))

        # Alerting passthrough (forwarded to the controller at registration)
        self._alerting: Dict[str, Any] = dict(args.get("alerting") or {})

        # Poll state
        self._last_queue_value: Optional[int] = None
        self._queue_unchanged_since: Optional[datetime.datetime] = None
        self._unreachable_since: Optional[datetime.datetime] = None

        # Metrics: the queue depth freshly read on the current poll cycle
        # only — cleared whenever it can't be determined (unreachable or
        # unconfigured) so a stale reading is never reported as current.
        self._metric_queue_remaining: Optional[int] = None

        self._client: Optional[ComfyUIStatusClient] = None
        if self._comfyui_url:
            self._client = ComfyUIStatusClient(
                self._comfyui_url, timeout_s=self._request_timeout_s
            )

        self.log(
            f"ImageGenHealthChecker initialising: id={self._checker_id}, "
            f"url={self._comfyui_url!r}, interval={self._check_interval_s}s, "
            f"queue_stuck_after={self._queue_stuck_after_s}s, "
            f"unreachable_after={self._unreachable_after_s}s",
            level="INFO",
        )

        self.run_in(self._on_startup, 0)

    def _on_startup(self, kwargs: Any) -> None:
        self.create_task(self._async_startup())

    async def _async_startup(self) -> None:
        await self._seed_from_published_status()
        self._register()

        self.listen_event(self._on_controller_ready, "health_check_controller_ready")
        self.listen_event(self._on_recheck, "health_check_recheck")

        self.run_in(self._first_check, 5)

        self.log(
            f"ImageGenHealthChecker '{self._checker_name}' started", level="INFO"
        )

    async def _seed_from_published_status(self) -> None:
        """Carry detection state across AppDaemon restarts.

        The detection clocks live in memory, but the controller's published
        sensor survives a pod restart (image deploys are hands-off).  Without
        seeding, a restart mid-outage would stop re-posting the firing alert
        (false ``[RESOLVED]`` after Alertmanager's resolve_timeout) and the
        15/30-minute clocks would restart from zero.  Seeding re-detects the
        outage within one check cycle, well inside the resolve_timeout.
        """
        try:
            state = await self.get_state(CONTROLLER_SENSOR, attribute="all")
            checker = (
                (state or {})
                .get("attributes", {})
                .get("checkers", {})
                .get(self._checker_id)
            )
            if not checker:
                return

            now = datetime.datetime.now()
            for check in checker.get("checks", []):
                name = check.get("name")
                status = check.get("status")
                since = self._parse_seed_ts(check.get("last_changed"), now)

                if name == API_CHECK and status in ("warning", "critical"):
                    if status == "critical":
                        # Already past the threshold before the restart.
                        since = min(
                            since,
                            now
                            - datetime.timedelta(
                                seconds=self._unreachable_after_s + 1
                            ),
                        )
                    self._unreachable_since = since
                    self.log(
                        f"Seeded unreachable-since from published status: "
                        f"{since.isoformat(timespec='seconds')} ({status})",
                        level="INFO",
                    )
                elif name == QUEUE_CHECK and status == "critical":
                    self._queue_unchanged_since = min(
                        since,
                        now
                        - datetime.timedelta(
                            seconds=self._queue_stuck_after_s + 1
                        ),
                    )
                    # Pre-restart queue depth is unknown: -1 means only a
                    # genuine decrease (or empty queue) clears the stuck state.
                    self._last_queue_value = -1
                    self.log(
                        "Seeded stuck-queue state from published status "
                        f"(since {self._queue_unchanged_since.isoformat(timespec='seconds')})",
                        level="INFO",
                    )
        except Exception as exc:
            self.log(
                f"Could not seed state from published status: {exc!r}",
                level="DEBUG",
            )

    @staticmethod
    def _parse_seed_ts(
        value: Any, fallback: datetime.datetime
    ) -> datetime.datetime:
        """Parse a controller timestamp (naive local ISO) with a fallback."""
        if value:
            try:
                return datetime.datetime.fromisoformat(str(value))
            except (ValueError, TypeError):
                pass
        return fallback

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def _register(self) -> None:
        check_names = [API_CHECK, QUEUE_CHECK]
        payload: Dict[str, Any] = {
            "checker_id": self._checker_id,
            "checker_name": self._checker_name,
            "check_names": check_names,
            "supports_repair": False,
        }
        if self._alerting:
            payload["alerting"] = self._alerting
        self.fire_event(
            "health_check_command",
            command="register_checker",
            payload=json.dumps(payload),
        )
        self.log(
            f"Registered '{self._checker_name}' with checks: {check_names}",
            level="INFO",
        )

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_controller_ready(self, event_name: str, data: dict, kwargs: Any) -> None:
        self.log(
            f"Controller ready — re-registering '{self._checker_name}'",
            level="INFO",
        )
        self._register()

    def _on_recheck(self, event_name: str, data: dict, kwargs: Any) -> None:
        self.log(
            f"Force recheck requested for '{self._checker_name}'", level="INFO"
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
        results = await self._poll_comfyui()

        payload: Dict[str, Any] = {
            "checker_id": self._checker_id,
            "results": results,
        }

        # Domain metric: ComfyUI queue depth, when freshly read this cycle.
        if self._metric_queue_remaining is not None:
            payload["metrics"] = [
                {
                    "name": "queue_remaining",
                    "value": self._metric_queue_remaining,
                    "type": "gauge",
                }
            ]

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

    async def _poll_comfyui(self) -> List[Dict[str, str]]:
        now = datetime.datetime.now()
        # Reset each cycle — only a fresh, successful read below sets it.
        self._metric_queue_remaining = None

        if self._client is None:
            return [
                {
                    "name": API_CHECK,
                    "status": "critical",
                    "detail": "comfyui_url not configured",
                },
                {
                    "name": QUEUE_CHECK,
                    "status": "unknown",
                    "detail": "comfyui_url not configured",
                },
            ]

        try:
            remaining = await self._client.fetch_queue_remaining()
        except ComfyUIStatusError as exc:
            return self._unreachable_results(now, exc)

        if self._unreachable_since is not None:
            self.log(
                f"ComfyUI reachable again (queue_remaining={remaining})",
                level="INFO",
            )
        self._unreachable_since = None
        self._metric_queue_remaining = remaining

        # Only a DECREASE (a job finished) or an empty queue counts as
        # progress.  Increases are producer activity — during a GPU wedge
        # the detection pipeline keeps submitting jobs, so the counter
        # climbs while nothing executes; that must not reset the stuck
        # clock or the canonical failure would never page.
        if (
            self._last_queue_value is None
            or remaining < self._last_queue_value
            or remaining == 0
        ):
            self._queue_unchanged_since = now
        self._last_queue_value = remaining

        api_result = {
            "name": API_CHECK,
            "status": "ok",
            "detail": f"queue_remaining={remaining}",
        }

        if remaining == 0:
            return [
                api_result,
                {"name": QUEUE_CHECK, "status": "ok", "detail": "Queue empty"},
            ]

        unchanged_for_s = (
            (now - self._queue_unchanged_since).total_seconds()
            if self._queue_unchanged_since
            else 0.0
        )
        if unchanged_for_s > self._queue_stuck_after_s:
            self.log(
                f"ComfyUI queue STUCK at {remaining} for "
                f"{_fmt_minutes(unchanged_for_s)} "
                f"(threshold {_fmt_minutes(self._queue_stuck_after_s)})",
                level="WARNING",
            )
            return [
                api_result,
                {
                    "name": QUEUE_CHECK,
                    "status": "critical",
                    "detail": (
                        f"Queue at {remaining} with no job completed for "
                        f"{_fmt_minutes(unchanged_for_s)} (threshold "
                        f"{_fmt_minutes(self._queue_stuck_after_s)}) — "
                        "likely GPU/host failure; needs human attention"
                    ),
                },
            ]

        return [
            api_result,
            {
                "name": QUEUE_CHECK,
                "status": "ok",
                "detail": (
                    f"Queue at {remaining}, last progress "
                    f"{_fmt_minutes(unchanged_for_s)} ago"
                ),
            },
        ]

    def _unreachable_results(
        self, now: datetime.datetime, exc: ComfyUIStatusError
    ) -> List[Dict[str, str]]:
        if self._unreachable_since is None:
            self._unreachable_since = now
            self.log(f"ComfyUI unreachable: {exc}", level="WARNING")

        # Detail must not embed the exception — connection errors stringify
        # with the internal service URL and the published sensor reaches the
        # frontend (security rule S3).  The full exception is in the log.
        down_for_s = (now - self._unreachable_since).total_seconds()
        if down_for_s > self._unreachable_after_s:
            status = "critical"
            detail = (
                f"Unreachable for {_fmt_minutes(down_for_s)} (threshold "
                f"{_fmt_minutes(self._unreachable_after_s)}) — "
                "likely GPU/host failure; needs human attention"
            )
        else:
            status = "warning"
            detail = (
                f"Unreachable for {_fmt_minutes(down_for_s)} "
                f"(critical after "
                f"{_fmt_minutes(self._unreachable_after_s)})"
            )

        return [
            {"name": API_CHECK, "status": status, "detail": detail},
            {
                "name": QUEUE_CHECK,
                "status": "unknown",
                "detail": "Endpoint unreachable",
            },
        ]
