"""Protect Health Checker — detects and auto-heals the silent UniFi Protect
websocket freeze.

The failure mode: the Protect integration's websocket dies silently — camera
entity attributes keep updating but motion/smart-detection binary sensors
stop changing state entirely, with zero log errors.  All affected sensors
keep an identical ``last_changed`` (the moment they were last re-registered).
The proven fix is reloading the Protect config entry.

Two checks on a configurable interval:

1. **Sensor Discovery** — find all Protect event sensors (motion
   device_class + ``*_detected`` smart detections) via the entity registry
   (``integration_entities`` template), with a config-list override.
2. **Event Stream** — the newest ``last_changed`` across all event sensors,
   measured in *active-hours seconds* (overnight quiet doesn't count toward
   staleness), must be younger than ``stale_after_s``.

Freeze handling is a small state machine: once frozen, the check stays
critical until a *genuine* event arrives — one strictly newer than the
baseline (the newest event at detection time, or the post-reload
re-registration settle window).  This is what lets the checker verify that
a reload actually revived the event stream instead of trusting the
re-registration timestamps the reload itself produces.

Repair (config entry reload) integrates with the standard auto-repair
framework (HA helper toggle + delay), with an additional reload cooldown
(default 1/hour).  The loaded config entry is discovered at runtime — never
hardcoded — via the config-entries REST API.

Communication with the controller is event-only (never ``get_app``).
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Add health_checks package root so we can import shared utilities
_health_checks_root = str(Path(__file__).resolve().parents[2])
if _health_checks_root not in sys.path:
    sys.path.insert(0, _health_checks_root)

# Add appdaemon root for providers
_appdaemon_root = str(Path(__file__).resolve().parents[4])
if _appdaemon_root not in sys.path:
    sys.path.insert(0, _appdaemon_root)

import hassapi as hass

from providers.ha_provisioner import HAProvisioner, HaAdminClient

logger = logging.getLogger(__name__)

UTC = datetime.timezone.utc

# Repair state constants (match spa_health_checker semantics)
REPAIR_IDLE = "idle"
REPAIR_PENDING = "pending"
REPAIR_IN_PROGRESS = "in_progress"
REPAIR_SUCCESS = "success"
REPAIR_FAILED = "failed"

DISCOVERY_CHECK = "Sensor Discovery"
EVENT_STREAM_CHECK = "Event Stream"

REPAIR_POLL_INTERVAL_S = 30

# Renders [["entity_id", "device_class", "last_changed_iso"], ...] for every
# binary_sensor owned by the integration.  Validated against live HA.
_DISCOVERY_TEMPLATE = (
    "{{% set ents = integration_entities('{domain}')"
    " | select('match', 'binary_sensor') | list %}}"
    '[{{% for e in ents %}}["{{{{ e }}}}", "{{{{ state_attr(e, \'device_class\') }}}}", '
    '"{{{{ states[e].last_changed.isoformat() if states[e] else \'\' }}}}"]'
    '{{{{ "," if not loop.last }}}}{{% endfor %}}]'
)


def _parse_iso_utc(value: Any) -> Optional[datetime.datetime]:
    """Parse an ISO timestamp (str or datetime) into an aware UTC datetime."""
    if isinstance(value, datetime.datetime):
        dt = value
    elif value:
        try:
            dt = datetime.datetime.fromisoformat(str(value))
        except (ValueError, TypeError):
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _parse_hhmm(value: str, fallback: datetime.time) -> datetime.time:
    try:
        parts = str(value).split(":")
        return datetime.time(int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
    except (ValueError, IndexError):
        return fallback


def active_seconds_between(
    start: datetime.datetime,
    end: datetime.datetime,
    window_start: datetime.time,
    window_end: datetime.time,
) -> float:
    """Seconds of overlap between [start, end] and the daily active window.

    Both datetimes must share a timezone (wall-clock comparison).  The
    window must not cross midnight (e.g. 08:00–23:00).
    """
    if end <= start or window_end <= window_start:
        return 0.0
    total = 0.0
    day = start.date()
    while day <= end.date():
        ws = datetime.datetime.combine(day, window_start).replace(tzinfo=start.tzinfo)
        we = datetime.datetime.combine(day, window_end).replace(tzinfo=start.tzinfo)
        lo = max(start, ws)
        hi = min(end, we)
        if hi > lo:
            total += (hi - lo).total_seconds()
        day += datetime.timedelta(days=1)
    return total


def _fmt_age(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds / 60)}m"
    return f"{seconds / 3600:.1f}h"


class ProtectHealthChecker(hass.Hass):
    """Health checker for the UniFi Protect event stream with reload auto-heal."""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        args = self.args or {}

        # Identity
        self._checker_id: str = args.get("checker_id", "protect")
        self._checker_name: str = args.get("checker_name", "UniFi Protect")

        # Integration / discovery
        self._integration_domain: str = args.get(
            "integration_domain", "unifiprotect"
        )
        self._motion_entities: List[str] = list(args.get("motion_entities", []))

        # Staleness
        self._stale_after_s: int = int(args.get("stale_after_s", 10800))
        self._active_start: datetime.time = _parse_hhmm(
            args.get("active_start", "08:00"), datetime.time(8, 0)
        )
        self._active_end: datetime.time = _parse_hhmm(
            args.get("active_end", "23:00"), datetime.time(23, 0)
        )
        if self._active_end <= self._active_start:
            self.log(
                f"Invalid active window {self._active_start}-{self._active_end} "
                "(must not cross midnight) — falling back to 08:00-23:00; "
                "staleness could otherwise never accumulate",
                level="WARNING",
            )
            self._active_start = datetime.time(8, 0)
            self._active_end = datetime.time(23, 0)
        self._tz = self._resolve_tz(args.get("active_tz", "America/New_York"))

        # Repair
        self._reload_cooldown_s: int = int(args.get("reload_cooldown_s", 3600))
        self._repair_settle_s: int = int(args.get("repair_settle_s", 60))
        self._repair_recovery_wait_s: int = int(
            args.get("repair_recovery_wait_s", 600)
        )
        self._auto_repair_enabled_default: bool = bool(
            args.get("auto_repair_enabled_default", True)
        )
        self._auto_repair_delay_min_default: int = int(
            args.get("auto_repair_delay_min_default", 1)
        )

        # Alerting passthrough (forwarded to the controller at registration)
        self._alerting: Dict[str, Any] = dict(args.get("alerting") or {})

        # Timing
        self._check_interval_s: int = int(args.get("check_interval_s", 300))

        # Freeze state machine
        self._frozen: bool = False
        self._frozen_since: Optional[datetime.datetime] = None
        # Events must be strictly newer than this (aware UTC) to count as
        # genuine; set to newest-at-detection, then to reload-time + settle.
        self._event_baseline: Optional[datetime.datetime] = None

        # Repair state machine
        self._repair_status: str = REPAIR_IDLE
        self._repair_detail: str = ""
        self._auto_repair_deadline: Optional[datetime.datetime] = None
        self._last_repair_attempt: Optional[str] = None
        self._last_reload_at: Optional[datetime.datetime] = None
        self._unhealthy_since: Optional[datetime.datetime] = None
        self._repair_task: Optional[asyncio.Task] = None

        # Cached auto-repair config (refreshed each check cycle)
        self._cached_auto_repair_enabled: bool = self._auto_repair_enabled_default
        self._cached_auto_repair_delay_min: int = self._auto_repair_delay_min_default

        # Discovery cache: entity IDs from the last successful discovery
        self._sensors: List[str] = []

        self._admin: Optional[HaAdminClient] = None

        self.log(
            f"ProtectHealthChecker initialising: id={self._checker_id}, "
            f"domain={self._integration_domain!r}, "
            f"stale_after={self._stale_after_s}s, "
            f"active={self._active_start}-{self._active_end} ({self._tz}), "
            f"reload_cooldown={self._reload_cooldown_s}s, "
            f"override_entities={len(self._motion_entities)}",
            level="INFO",
        )

        self.run_in(self._on_startup, 0)

    def _resolve_tz(self, tz_name: str):
        """Resolve a timezone name, falling back to the system local zone."""
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(str(tz_name))
        except Exception as exc:
            self.log(
                f"Could not load timezone {tz_name!r} ({exc!r}) — "
                "falling back to system local time",
                level="WARNING",
            )
            return None

    def _on_startup(self, kwargs: Any) -> None:
        self.create_task(self._async_startup())

    async def _async_startup(self) -> None:
        ha_url = self.args.get("ha_url")
        ha_token_env = self.args.get("ha_token_env")
        if ha_url and ha_token_env:
            try:
                self._admin = HaAdminClient(
                    ha_url=ha_url, ha_token_env=ha_token_env
                )
            except Exception as exc:
                # Missing token env var must not kill startup — the checker
                # still registers and reports discovery as critical.
                self.log(
                    f"Failed to initialise HA admin client: {exc!r}",
                    level="ERROR",
                )
        else:
            self.log(
                "ha_url / ha_token_env not configured — discovery and "
                "config-entry reload are unavailable",
                level="WARNING",
            )

        await self._provision_entities()
        await self._refresh_auto_repair_config()
        self._register()

        self.listen_event(self._on_controller_ready, "health_check_controller_ready")
        self.listen_event(self._on_recheck, "health_check_recheck")
        self.listen_event(
            self._on_repair_command,
            f"health_check_repair_{self._checker_id}",
        )

        self.run_in(self._first_check, 5)

        self.log(
            f"ProtectHealthChecker '{self._checker_name}' started", level="INFO"
        )

    async def _provision_entities(self) -> None:
        """Create auto-repair helper entities if they don't exist."""
        ha_url = self.args.get("ha_url")
        ha_token_env = self.args.get("ha_token_env")
        if not ha_url or not ha_token_env:
            return

        try:
            prov = HAProvisioner(ha_url=ha_url, ha_token_env=ha_token_env)
        except Exception as exc:
            self.log(
                f"Failed to initialise provisioner: {exc!r}", level="ERROR"
            )
            return

        try:
            created = await prov.ensure_helper(
                "input_boolean",
                f"{self._checker_id} Health Auto Repair",
            )
            if created:
                entity_id = f"input_boolean.{self._checker_id}_health_auto_repair"
                self.log(f"Provisioned {entity_id}", level="INFO")
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
                            f"Failed to default-enable auto-repair: {exc!r}",
                            level="WARNING",
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
                entity_id = (
                    f"input_number.{self._checker_id}_health_auto_repair_delay"
                )
                try:
                    self.call_service(
                        "input_number/set_value",
                        entity_id=entity_id,
                        value=self._auto_repair_delay_min_default,
                    )
                except Exception as exc:
                    self.log(
                        f"Failed to set default for {entity_id}: {exc!r}",
                        level="DEBUG",
                    )
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
        check_names = [DISCOVERY_CHECK, EVENT_STREAM_CHECK]
        payload: Dict[str, Any] = {
            "checker_id": self._checker_id,
            "checker_name": self._checker_name,
            "check_names": check_names,
            "supports_repair": True,
            "repair_state": self._build_repair_state(),
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

    def _on_repair_command(self, event_name: str, data: dict, kwargs: Any) -> None:
        action = data.get("action", "")
        if action == "start_repair":
            # Manual repair from the card bypasses the reload cooldown.
            self.log("Manual repair requested", level="INFO")
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
        await self._refresh_auto_repair_config()

        sensors, discovery_result = await self._discover_sensors()
        event_result = self._check_event_stream(sensors)
        results = [discovery_result, event_result]

        # No cross-check: the two checks are independent failure domains.

        if self._repair_status not in (REPAIR_IN_PROGRESS,):
            self._evaluate_auto_repair(results)

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

    async def _discover_sensors(
        self,
    ) -> Tuple[List[Tuple[str, Optional[datetime.datetime]]], Dict[str, str]]:
        """Return [(entity_id, last_changed)] plus the discovery check result."""
        if self._motion_entities:
            sensors: List[Tuple[str, Optional[datetime.datetime]]] = []
            for entity_id in self._motion_entities:
                try:
                    attrs = await self.get_state(entity_id, attribute="all")
                except Exception as exc:
                    self.log(
                        f"get_state failed for {entity_id}: {exc!r}",
                        level="WARNING",
                    )
                    attrs = None
                last_changed = (
                    _parse_iso_utc(attrs.get("last_changed")) if attrs else None
                )
                sensors.append((entity_id, last_changed))
            self._sensors = [e for e, _ in sensors]
            found = [s for s in sensors if s[1] is not None]
            if not found:
                return sensors, {
                    "name": DISCOVERY_CHECK,
                    "status": "critical",
                    "detail": (
                        f"None of the {len(sensors)} configured entities found"
                    ),
                }
            return sensors, {
                "name": DISCOVERY_CHECK,
                "status": "ok",
                "detail": f"{len(found)} configured event sensors",
            }

        if self._admin is None:
            return [], {
                "name": DISCOVERY_CHECK,
                "status": "critical",
                "detail": "No ha_url/ha_token_env and no motion_entities override",
            }

        try:
            template = _DISCOVERY_TEMPLATE.format(domain=self._integration_domain)
            rendered = await self._admin.render_template(template)
            rows = json.loads(rendered)
            sensors = []
            for row in rows:
                entity_id, device_class, last_changed = (
                    str(row[0]),
                    str(row[1]),
                    row[2],
                )
                if device_class != "motion" and not entity_id.endswith("_detected"):
                    continue
                sensors.append((entity_id, _parse_iso_utc(last_changed)))
            self._sensors = [e for e, _ in sensors]
            if not sensors:
                return sensors, {
                    "name": DISCOVERY_CHECK,
                    "status": "critical",
                    "detail": (
                        f"No event sensors found for integration "
                        f"{self._integration_domain!r} — is the config entry loaded?"
                    ),
                }
            return sensors, {
                "name": DISCOVERY_CHECK,
                "status": "ok",
                "detail": f"{len(sensors)} event sensors discovered",
            }
        except Exception as exc:
            self.log(f"Sensor discovery failed: {exc!r}", level="ERROR")
            if self._sensors:
                # Fall back to the cached entity list with fresh state reads.
                sensors = []
                for entity_id in self._sensors:
                    try:
                        attrs = await self.get_state(entity_id, attribute="all")
                    except Exception:
                        attrs = None
                    sensors.append(
                        (
                            entity_id,
                            _parse_iso_utc(attrs.get("last_changed"))
                            if attrs
                            else None,
                        )
                    )
                # Detail must not embed the exception — aiohttp errors
                # stringify with the (secret) ha_url and the published
                # sensor reaches the frontend (security rule S3).  The full
                # exception is in the ERROR log above.
                return sensors, {
                    "name": DISCOVERY_CHECK,
                    "status": "warning",
                    "detail": (
                        f"Discovery failed ({type(exc).__name__}); using "
                        f"{len(sensors)} cached sensors"
                    ),
                }
            return [], {
                "name": DISCOVERY_CHECK,
                "status": "critical",
                "detail": f"Discovery failed: {type(exc).__name__}",
            }

    def _check_event_stream(
        self, sensors: List[Tuple[str, Optional[datetime.datetime]]]
    ) -> Dict[str, str]:
        timestamped = [(e, ts) for e, ts in sensors if ts is not None]
        if not timestamped:
            return {
                "name": EVENT_STREAM_CHECK,
                "status": "unknown",
                "detail": "No event sensors with timestamps available",
            }

        newest_entity, newest_ts = max(timestamped, key=lambda item: item[1])
        now_utc = datetime.datetime.now(UTC)
        wall_age_s = max(0.0, (now_utc - newest_ts).total_seconds())

        if self._frozen:
            if self._repair_status == REPAIR_IN_PROGRESS:
                # The reload re-registers every entity with fresh
                # last_changed mid-await — a concurrent check cycle must not
                # mistake that for recovery.  _execute_repair owns recovery
                # detection (its own settle baseline) while in progress.
                return {
                    "name": EVENT_STREAM_CHECK,
                    "status": "critical",
                    "detail": (
                        "Event stream frozen — reload in progress, "
                        "awaiting post-reload events"
                    ),
                }
            if self._event_baseline is None:
                # Defensive: should be set at detection time.
                self._event_baseline = newest_ts
            resumed = [
                e for e, ts in timestamped if ts > self._event_baseline
            ]
            if resumed:
                self.log(
                    f"Event stream resumed — first fresh event from {resumed[0]}",
                    level="INFO",
                )
                self._unfreeze()
                return {
                    "name": EVENT_STREAM_CHECK,
                    "status": "ok",
                    "detail": f"Events resumed ({resumed[0]})",
                }
            frozen_for = (
                (now_utc - self._frozen_since).total_seconds()
                if self._frozen_since
                else 0.0
            )
            detail = (
                f"Event stream frozen for {_fmt_age(frozen_for)} — "
                f"no genuine event since detection"
            )
            if self._repair_status == REPAIR_FAILED:
                detail += " (auto-heal failed)"
            return {
                "name": EVENT_STREAM_CHECK,
                "status": "critical",
                "detail": detail,
            }

        # Not frozen: evaluate staleness in active-hours seconds so overnight
        # quiet (camera-dependent, up to ~12h) doesn't false-positive.
        now_local = (
            now_utc.astimezone(self._tz) if self._tz else now_utc.astimezone()
        )
        newest_local = (
            newest_ts.astimezone(self._tz) if self._tz else newest_ts.astimezone()
        )
        effective_age_s = active_seconds_between(
            newest_local, now_local, self._active_start, self._active_end
        )

        in_window = self._active_start <= now_local.time() <= self._active_end
        if effective_age_s > self._stale_after_s and in_window:
            self._frozen = True
            self._frozen_since = now_utc
            self._event_baseline = newest_ts
            self.log(
                f"Protect event stream FROZEN: newest event "
                f"{newest_entity} at {newest_ts.isoformat()} — "
                f"{_fmt_age(effective_age_s)} of active hours ago "
                f"(threshold {_fmt_age(self._stale_after_s)})",
                level="WARNING",
            )
            return {
                "name": EVENT_STREAM_CHECK,
                "status": "critical",
                "detail": (
                    f"No events for {_fmt_age(effective_age_s)} of active "
                    f"hours (threshold {_fmt_age(self._stale_after_s)}); "
                    f"newest: {newest_entity}"
                ),
            }

        return {
            "name": EVENT_STREAM_CHECK,
            "status": "ok",
            "detail": (
                f"Newest event {_fmt_age(wall_age_s)} ago ({newest_entity})"
            ),
        }

    def _unfreeze(self) -> None:
        self._frozen = False
        self._frozen_since = None
        self._event_baseline = None
        self._unhealthy_since = None

    # ------------------------------------------------------------------
    # Auto-repair logic (mirrors spa_health_checker + reload cooldown)
    # ------------------------------------------------------------------

    async def _refresh_auto_repair_config(self) -> None:
        try:
            entity_id = f"input_boolean.{self._checker_id}_health_auto_repair"
            enabled_state = await self.get_state(entity_id)
            if enabled_state is not None:
                self._cached_auto_repair_enabled = str(enabled_state) == "on"
        except Exception as exc:
            self.log(f"Failed to read auto-repair toggle: {exc!r}", level="WARNING")

        try:
            entity_id = f"input_number.{self._checker_id}_health_auto_repair_delay"
            delay_state = await self.get_state(entity_id)
            if delay_state is not None and str(delay_state) not in (
                "unavailable",
                "unknown",
            ):
                self._cached_auto_repair_delay_min = int(float(delay_state))
        except Exception as exc:
            self.log(f"Failed to read auto-repair delay: {exc!r}", level="WARNING")

    def _read_auto_repair_config(self) -> tuple[bool, int]:
        return self._cached_auto_repair_enabled, self._cached_auto_repair_delay_min

    def _reload_cooldown_remaining_s(self) -> float:
        if self._last_reload_at is None:
            return 0.0
        elapsed = (
            datetime.datetime.now() - self._last_reload_at
        ).total_seconds()
        return max(0.0, self._reload_cooldown_s - elapsed)

    def _evaluate_auto_repair(self, results: List[Dict[str, str]]) -> None:
        all_ok = all(r["status"] == "ok" for r in results)
        any_critical = any(r["status"] == "critical" for r in results)

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

        if not any_critical:
            return

        enabled, delay_min = self._read_auto_repair_config()
        if not enabled:
            if self._unhealthy_since is None:
                self._unhealthy_since = datetime.datetime.now()
            return

        now = datetime.datetime.now()

        if self._repair_status == REPAIR_IDLE:
            if self._unhealthy_since is None:
                self._unhealthy_since = now
            deadline = self._unhealthy_since + datetime.timedelta(minutes=delay_min)
            deadline = self._apply_cooldown(deadline, now)
            if now >= deadline:
                self.log(
                    f"Unhealthy past auto-repair delay ({delay_min}m) — "
                    "starting config entry reload",
                    level="INFO",
                )
                self._start_repair()
            else:
                self._repair_status = REPAIR_PENDING
                self._auto_repair_deadline = deadline
                self._repair_detail = (
                    f"Auto-repair at {deadline.isoformat(timespec='seconds')}"
                )
                self.log(
                    f"Repair pending — deadline "
                    f"{deadline.isoformat(timespec='seconds')}",
                    level="INFO",
                )

        elif self._repair_status in (REPAIR_PENDING, REPAIR_FAILED):
            # FAILED retries once the reload cooldown allows another attempt.
            deadline = self._auto_repair_deadline or now
            deadline = self._apply_cooldown(deadline, now)
            if now >= deadline:
                self.log("Auto-repair deadline reached — starting repair", level="INFO")
                self._start_repair()
            else:
                # Keep the reported deadline honest when the cooldown
                # pushed it out.
                self._auto_repair_deadline = deadline
                if self._repair_status == REPAIR_FAILED:
                    self._repair_detail = (
                        f"Auto-heal failed; retry at "
                        f"{deadline.isoformat(timespec='seconds')}"
                    )
                else:
                    self._repair_detail = (
                        f"Auto-repair at {deadline.isoformat(timespec='seconds')}"
                    )

    def _apply_cooldown(
        self, deadline: datetime.datetime, now: datetime.datetime
    ) -> datetime.datetime:
        """Push a repair deadline out past the reload cooldown if needed."""
        remaining = self._reload_cooldown_remaining_s()
        if remaining > 0:
            cooldown_until = now + datetime.timedelta(seconds=remaining)
            if cooldown_until > deadline:
                return cooldown_until
        return deadline

    # ------------------------------------------------------------------
    # Repair execution
    # ------------------------------------------------------------------

    def _start_repair(self) -> None:
        if self._repair_status == REPAIR_IN_PROGRESS:
            self.log("Repair already in progress — ignoring", level="WARNING")
            return

        if self._admin is None:
            self.log(
                "No ha_url/ha_token_env configured — cannot reload config entry",
                level="WARNING",
            )
            self._repair_status = REPAIR_FAILED
            self._repair_detail = "No HA admin access configured"
            return

        self._repair_status = REPAIR_IN_PROGRESS
        self._repair_detail = "Reloading Protect config entry..."
        self._auto_repair_deadline = None
        self._last_repair_attempt = datetime.datetime.now().isoformat(
            timespec="seconds"
        )

        self._report_repair_status_only()
        self._repair_task = self.create_task(self._execute_repair())

    def _cancel_repair(self) -> None:
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
        """Reload the Protect config entry and verify events resume."""
        try:
            entry_id, entry_title = await self._find_loaded_entry()
            if not entry_id:
                self._repair_status = REPAIR_FAILED
                self._repair_detail = (
                    f"No loaded {self._integration_domain!r} config entry found"
                )
                self.log(self._repair_detail, level="ERROR")
                self._report_repair_status_only()
                return

            self.log(
                f"Reloading config entry {entry_id} ({entry_title!r})",
                level="INFO",
            )
            self._last_reload_at = datetime.datetime.now()
            # Raise the baseline BEFORE the reload so re-registration
            # timestamps produced during the await can never read as
            # recovery, even if a check cycle races the in-progress guard.
            if self._frozen:
                self._event_baseline = datetime.datetime.now(UTC) + datetime.timedelta(
                    seconds=self._repair_settle_s
                )
            await self._admin.reload_config_entry(entry_id)
            reload_done = datetime.datetime.now(UTC)

            # Reload re-registers every entity with a fresh last_changed —
            # only events after the settle window prove the stream is alive.
            baseline = reload_done + datetime.timedelta(
                seconds=self._repair_settle_s
            )
            if self._frozen:
                self._event_baseline = baseline

            self.log(
                f"Config entry reloaded; waiting up to "
                f"{self._repair_recovery_wait_s}s for events newer than "
                f"{baseline.isoformat()}",
                level="INFO",
            )
            self._repair_detail = "Reloaded; waiting for events to resume..."
            self._report_repair_status_only()

            elapsed = 0
            while elapsed < self._repair_recovery_wait_s:
                await asyncio.sleep(REPAIR_POLL_INTERVAL_S)
                elapsed += REPAIR_POLL_INTERVAL_S

                fresh_entity = await self._any_event_after(baseline)
                if fresh_entity:
                    self._repair_status = REPAIR_SUCCESS
                    self._repair_detail = (
                        f"Events resumed {elapsed}s after reload "
                        f"({fresh_entity})"
                    )
                    self._unfreeze()
                    self.log(
                        f"Repair successful — {self._repair_detail}",
                        level="INFO",
                    )
                    self._report_repair_status_only()
                    return

                self._repair_detail = (
                    f"Waiting for events... "
                    f"{elapsed}s/{self._repair_recovery_wait_s}s"
                )

            self._repair_status = REPAIR_FAILED
            self._repair_detail = (
                f"No events within {self._repair_recovery_wait_s}s of reload"
            )
            self.log(
                f"Repair failed — {self._repair_detail}; alert stays firing",
                level="WARNING",
            )
            self._report_repair_status_only()

        except Exception as exc:
            self._repair_status = REPAIR_FAILED
            # Class name only — aiohttp errors stringify with the (secret)
            # ha_url and repair_detail reaches the published sensor (S3).
            self._repair_detail = f"Repair error: {type(exc).__name__}"
            self.log(f"Repair execution error: {exc!r}", level="ERROR")
            self._report_repair_status_only()

    async def _find_loaded_entry(self) -> Tuple[Optional[str], Optional[str]]:
        """Discover the loaded config entry for the integration at runtime.

        There may be additional non-loaded entries (e.g. an ignored UDM
        discovery) — only ``state == "loaded"`` qualifies.
        """
        entries = await self._admin.list_config_entries(self._integration_domain)
        loaded = [e for e in entries if e.get("state") == "loaded"]
        if not loaded:
            return None, None
        if len(loaded) > 1:
            titles = [e.get("title") for e in loaded]
            self.log(
                f"Multiple loaded {self._integration_domain!r} entries "
                f"({titles}) — using the first",
                level="WARNING",
            )
        entry = loaded[0]
        return entry.get("entry_id"), entry.get("title")

    async def _any_event_after(
        self, baseline: datetime.datetime
    ) -> Optional[str]:
        """Return the first sensor with a state change newer than baseline."""
        for entity_id in self._sensors:
            try:
                attrs = await self.get_state(entity_id, attribute="all")
            except Exception:
                continue
            if not attrs:
                continue
            last_changed = _parse_iso_utc(attrs.get("last_changed"))
            if last_changed and last_changed > baseline:
                return entity_id
        return None

    def _report_repair_status_only(self) -> None:
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
        auto_enabled = data.get("auto_repair_enabled")
        delay_min = data.get("auto_repair_delay_min")

        if auto_enabled is not None:
            entity_id = f"input_boolean.{self._checker_id}_health_auto_repair"
            state = self.get_state(entity_id)
            desired = "on" if auto_enabled else "off"
            if state is None:
                # Helper not provisioned (yet) — don't fire a service call
                # that can only fail.
                self.log(
                    f"Auto-repair toggle {entity_id} not available — "
                    "skipping update",
                    level="WARNING",
                )
            elif str(state) != desired:
                service = (
                    "input_boolean/turn_on"
                    if auto_enabled
                    else "input_boolean/turn_off"
                )
                try:
                    self.call_service(service, entity_id=entity_id)
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
