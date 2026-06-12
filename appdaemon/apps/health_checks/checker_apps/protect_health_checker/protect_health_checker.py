"""Protect Health Checker — detects and auto-heals the silent UniFi Protect
websocket freeze.

The failure mode: the Protect integration's websocket dies silently — camera
entity attributes keep updating but motion/smart-detection binary sensors
stop changing state entirely, with zero log errors.  All affected sensors
keep an identical ``last_changed`` (the moment they were last re-registered).
The proven fix is reloading the Protect config entry.

Four checks on a configurable interval:

1. **Sensor Discovery** — find all Protect event sensors (motion
   device_class + ``*_detected`` smart detections + entry-sensor contact
   channels) via the entity registry (``integration_entities`` template),
   with a config-list override.  Devices that expose a door/moisture/tamper
   channel are classified as entry sensors (USL); everything else is a
   camera.
2. **Sensor Availability** — the fast path for a hard integration outage
   (auth failure, UNVR down): when essentially every event sensor is
   ``unavailable`` for longer than a grace period, that is unambiguous —
   no need to wait out the staleness threshold.  Partial unavailability
   (e.g. the entry sensors dropping off overnight) is a warning.
3. **Camera Events** — the newest ``last_changed`` across *available*
   camera-group sensors, measured in *active-hours seconds* (overnight
   quiet doesn't count toward staleness), must be younger than
   ``stale_after_s``.  ``unavailable`` transitions are NOT events and are
   excluded from freshness.
4. **Entry Sensors** — availability of the USL entry-sensor group with a
   last-event detail.  No freshness threshold: a door legitimately not
   opening for hours must not page.

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
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

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
AVAILABILITY_CHECK = "Sensor Availability"
CAMERA_EVENTS_CHECK = "Camera Events"
ENTRY_SENSORS_CHECK = "Entry Sensors"

REPAIR_POLL_INTERVAL_S = 30

# A device exposing any of these channels is an entry sensor (USL), not a
# camera — used to split event sensors into the two check groups.
ENTRY_MARKER_CLASSES = ("door", "moisture", "tamper")

UNAVAILABLE_STATES = ("unavailable", "unknown", "none", "")

GROUP_CAMERA = "camera"
GROUP_ENTRY = "entry"

# Renders [["entity_id", "device_class", "state", "last_changed_iso",
# "device_id"], ...] for every binary_sensor owned by the integration.
# Validated against live HA.
_DISCOVERY_TEMPLATE = (
    "{{% set ents = integration_entities('{domain}')"
    " | select('match', 'binary_sensor') | list %}}"
    '[{{% for e in ents %}}["{{{{ e }}}}", "{{{{ state_attr(e, \'device_class\') }}}}", '
    '"{{{{ states(e) }}}}", '
    '"{{{{ states[e].last_changed.isoformat() if states[e] else \'\' }}}}", '
    '"{{{{ device_id(e) or \'\' }}}}"]'
    '{{{{ "," if not loop.last }}}}{{% endfor %}}]'
)


class EventSensor(NamedTuple):
    """One tracked Protect event sensor with its current state snapshot."""

    entity_id: str
    state: str
    last_changed: Optional[datetime.datetime]
    group: str  # GROUP_CAMERA or GROUP_ENTRY
    device_id: str = ""  # registry device; empty → entity is its own device


def _sensor_device(s: EventSensor) -> str:
    """Device key for grouping — entities without a device stand alone."""
    return s.device_id or s.entity_id


def _is_available(state: str) -> bool:
    return str(state).lower() not in UNAVAILABLE_STATES


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

        # Availability fast path (hard integration outage)
        self._availability_grace_s: int = int(
            args.get("availability_grace_s", 900)
        )
        self._availability_critical_pct: int = int(
            args.get("availability_critical_pct", 90)
        )
        # How long a freshly-offline device stays warning-worthy.  Past the
        # window the downtime is accepted as expected (the wireless G6
        # cameras live unplugged most of the year) and the warning clears.
        self._availability_warn_window_s: int = int(
            args.get("availability_warn_window_s", 86400)
        )

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

        # Discovery caches from the last successful discovery:
        # camera-group entity IDs (used for repair verification) and the
        # entity_id → group map (used when discovery fails transiently).
        self._sensors: List[str] = []
        self._sensor_groups: Dict[str, str] = {}

        # Availability outage latch (see _check_availability) and
        # first-observed-missing times for entities absent from the state
        # machine (no last_changed to derive a dwell from).
        self._availability_down: bool = False
        self._missing_since: Dict[str, datetime.datetime] = {}

        # Device-level view (rebuilt each discovery): liveness, when a dark
        # device went dark, tracked entities per device, plus the set of
        # devices this app instance has actually witnessed alive — only
        # those may raise an offline warning (chronically-dark devices and
        # disabled channels are expected and stay quiet).
        self._device_live: Dict[str, bool] = {}
        self._device_dark_since: Dict[str, Optional[datetime.datetime]] = {}
        self._device_entities: Dict[str, List[str]] = {}
        self._devices_seen_alive: set = set()
        self._sensor_devices: Dict[str, str] = {}

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
        check_names = [
            DISCOVERY_CHECK,
            AVAILABILITY_CHECK,
            CAMERA_EVENTS_CHECK,
            ENTRY_SENSORS_CHECK,
        ]
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
        availability_result = self._check_availability(sensors)
        # Freshness only ever looks at AVAILABLE camera sensors: an
        # unavailable transition refreshes last_changed without being an
        # event, and entry-sensor activity must not mask a camera freeze.
        camera_events = [
            (s.entity_id, s.last_changed)
            for s in sensors
            if s.group == GROUP_CAMERA
            and _is_available(s.state)
            and s.last_changed is not None
        ]
        event_result = self._check_event_stream(camera_events)
        entry_result = self._check_entry_sensors(sensors)
        results = [
            discovery_result,
            availability_result,
            event_result,
            entry_result,
        ]

        # No cross-check: the checks are independent failure domains.

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

    async def _sensor_from_state(
        self, entity_id: str, group: str, device_id: str = ""
    ) -> EventSensor:
        """Build an EventSensor from AppDaemon's state cache."""
        try:
            attrs = await self.get_state(entity_id, attribute="all")
        except Exception as exc:
            self.log(
                f"get_state failed for {entity_id}: {exc!r}", level="WARNING"
            )
            attrs = None
        if not attrs:
            return EventSensor(entity_id, "unavailable", None, group, device_id)
        return EventSensor(
            entity_id,
            str(attrs.get("state", "unavailable")),
            _parse_iso_utc(attrs.get("last_changed")),
            group,
            device_id,
        )

    async def _discover_sensors(
        self,
    ) -> Tuple[List[EventSensor], Dict[str, str]]:
        """Return the tracked event sensors plus the discovery check result.

        Classification: any Protect device exposing a door/moisture/tamper
        channel is an entry sensor (USL); its motion and contact channels go
        to the entry group.  Remaining motion + ``*_detected`` sensors are
        cameras.
        """
        if self._motion_entities:
            sensors = [
                await self._sensor_from_state(entity_id, GROUP_CAMERA)
                for entity_id in self._motion_entities
            ]
            self._remember_groups(sensors)
            self._update_device_maps_from_sensors(sensors)
            found = [s for s in sensors if s.last_changed is not None]
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

            # First pass: devices with an entry-marker channel are USL
            # entry sensors, not cameras.
            entry_devices = {
                str(row[4])
                for row in rows
                if str(row[1]) in ENTRY_MARKER_CLASSES and str(row[4])
            }

            sensors = []
            for row in rows:
                entity_id, device_class, state, last_changed, device_id = (
                    str(row[0]),
                    str(row[1]),
                    str(row[2]),
                    row[3],
                    str(row[4]),
                )
                is_entry_device = bool(device_id) and device_id in entry_devices
                if is_entry_device:
                    # Entry sensors: motion + contact channels are events.
                    if device_class not in ("motion", "door"):
                        continue
                    group = GROUP_ENTRY
                elif device_class == "motion" or entity_id.endswith("_detected"):
                    group = GROUP_CAMERA
                else:
                    continue
                sensors.append(
                    EventSensor(
                        entity_id,
                        state,
                        _parse_iso_utc(last_changed),
                        group,
                        device_id,
                    )
                )

            self._remember_groups(sensors)
            # Device liveness from ALL channels of the integration — a
            # camera whose every event channel is disabled still proves it
            # is online via e.g. its is_dark channel.
            self._set_device_maps(
                [
                    (str(r[4]) or str(r[0]), str(r[2]), _parse_iso_utc(r[3]))
                    for r in rows
                ],
                sensors,
            )
            if not sensors:
                return sensors, {
                    "name": DISCOVERY_CHECK,
                    "status": "critical",
                    "detail": (
                        f"No event sensors found for integration "
                        f"{self._integration_domain!r} — is the config entry loaded?"
                    ),
                }
            cameras = sum(1 for s in sensors if s.group == GROUP_CAMERA)
            return sensors, {
                "name": DISCOVERY_CHECK,
                "status": "ok",
                "detail": (
                    f"{len(sensors)} event sensors discovered "
                    f"({cameras} camera, {len(sensors) - cameras} entry)"
                ),
            }
        except Exception as exc:
            self.log(f"Sensor discovery failed: {exc!r}", level="ERROR")
            if self._sensor_groups:
                # Fall back to the cached entity list with fresh state reads.
                sensors = [
                    await self._sensor_from_state(
                        entity_id,
                        group,
                        self._sensor_devices.get(entity_id, ""),
                    )
                    for entity_id, group in self._sensor_groups.items()
                ]
                self._update_device_maps_from_sensors(sensors)
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

    def _remember_groups(self, sensors: List[EventSensor]) -> None:
        """Cache discovery results for fallback and repair verification."""
        self._sensor_groups = {s.entity_id: s.group for s in sensors}
        self._sensor_devices = {s.entity_id: s.device_id for s in sensors}
        self._sensors = [
            s.entity_id for s in sensors if s.group == GROUP_CAMERA
        ]

    # ------------------------------------------------------------------
    # Device-level view
    # ------------------------------------------------------------------

    def _update_device_maps_from_sensors(
        self, sensors: List[EventSensor]
    ) -> None:
        """Device maps from tracked sensors only (override/fallback paths)."""
        self._set_device_maps(
            [
                (_sensor_device(s), s.state, s.last_changed)
                for s in sensors
            ],
            sensors,
        )

    def _set_device_maps(
        self,
        channel_rows: List[Tuple[str, str, Optional[datetime.datetime]]],
        sensors: List[EventSensor],
    ) -> None:
        """Rebuild device liveness/dark-since maps.

        ``channel_rows`` is (device_key, state, last_changed) for every
        known channel — template discovery feeds ALL integration channels
        so a device with only disabled event sensors still reads as live
        via e.g. is_dark.  A dark device's ``dark_since`` is the newest
        transition among its channels (≈ the moment it went offline).
        """
        tracked = {_sensor_device(s) for s in sensors}
        live: Dict[str, bool] = {d: False for d in tracked}
        dark_since: Dict[str, Optional[datetime.datetime]] = {}
        for device_key, state, last_changed in channel_rows:
            if device_key not in live:
                continue
            if _is_available(state):
                live[device_key] = True
            elif last_changed is not None:
                prev = dark_since.get(device_key)
                if prev is None or last_changed > prev:
                    dark_since[device_key] = last_changed

        self._device_live = live
        self._device_dark_since = {
            d: dark_since.get(d) for d, is_live in live.items() if not is_live
        }
        entities: Dict[str, List[str]] = {}
        for s in sensors:
            entities.setdefault(_sensor_device(s), []).append(s.entity_id)
        self._device_entities = entities
        self._devices_seen_alive |= {
            d for d, is_live in live.items() if is_live
        }

    def _witnessed_offline_devices(
        self, now_utc: datetime.datetime, min_age_s: float
    ) -> List[str]:
        """Fully-dark devices that this app instance witnessed alive.

        The witnessed gate keeps expected downtime quiet: chronically-dark
        devices (disabled features, wireless cameras unplugged since before
        the last restart) never qualify.  ``min_age_s`` is the debounce —
        the grace for the standalone warning, 0 for the recovery latch
        (post-reload re-registration makes dark timestamps fresh, which
        must not read as recovery).  Past ``availability_warn_window_s``
        the downtime is accepted as expected and the device goes quiet.
        """
        out: List[str] = []
        for device_key, is_live in self._device_live.items():
            if is_live or device_key not in self._devices_seen_alive:
                continue
            since = self._device_dark_since.get(device_key)
            if since is None:
                # Dark with no timestamp (entities gone entirely) — treat
                # as just past the debounce.
                out.append(device_key)
                continue
            age = (now_utc - since).total_seconds()
            if min_age_s < age <= self._availability_warn_window_s:
                out.append(device_key)
        return sorted(out)

    def _eligible_offline_devices(
        self, now_utc: datetime.datetime
    ) -> List[str]:
        """Warning-worthy offline devices (grace-debounced)."""
        return self._witnessed_offline_devices(
            now_utc, self._availability_grace_s
        )

    def _device_label(self, device_key: str) -> str:
        """Human-readable handle for a device — its first tracked entity."""
        entities = self._device_entities.get(device_key)
        return sorted(entities)[0] if entities else device_key

    # ------------------------------------------------------------------
    # Checks
    # ------------------------------------------------------------------

    def _down_past_grace(
        self, s: EventSensor, now_utc: datetime.datetime
    ) -> bool:
        """True when an unavailable sensor has been down past the grace.

        ``last_changed`` is the transition moment.  Entities missing from
        the state machine entirely (no timestamp) are timed from when we
        first observed them missing — instant-down would bypass the grace
        and false-page during a reload's unload window.
        """
        if _is_available(s.state):
            self._missing_since.pop(s.entity_id, None)
            return False
        if s.last_changed is not None:
            self._missing_since.pop(s.entity_id, None)
            dwell = (now_utc - s.last_changed).total_seconds()
        else:
            first_seen = self._missing_since.setdefault(s.entity_id, now_utc)
            dwell = (now_utc - first_seen).total_seconds()
        return dwell > self._availability_grace_s

    def _check_availability(
        self, sensors: List[EventSensor]
    ) -> Dict[str, str]:
        """Fast path for a hard integration outage.

        CRITICAL is percentage-based over ALL tracked sensors, dwell-gated
        (the grace absorbs a reload's unavailable blip) and LATCHED on
        sensors actually returning — a reload that re-registers everything
        (resetting every ``last_changed``) must never read as recovery.

        WARNING is device-based and deliberately narrow: only a fully-dark
        device that this app instance witnessed alive, within the warn
        window of going dark.  Channels dead on a live device are disabled
        features, and devices dark since before the app started (or past
        the window) are expected downtime — both stay quiet, by design.
        """
        if not sensors:
            return {
                "name": AVAILABILITY_CHECK,
                "status": "unknown",
                "detail": "No event sensors to track",
            }

        now_utc = datetime.datetime.now(UTC)
        unavailable_now = [s for s in sensors if not _is_available(s.state)]
        down = [
            s.entity_id for s in sensors if self._down_past_grace(s, now_utc)
        ]
        eligible = self._eligible_offline_devices(now_utc)

        if self._availability_down:
            unavail_pct = 100.0 * len(unavailable_now) / len(sensors)
            if unavail_pct >= self._availability_critical_pct:
                return {
                    "name": AVAILABILITY_CHECK,
                    "status": "critical",
                    "detail": (
                        f"{len(unavailable_now)}/{len(sensors)} event sensors "
                        "unavailable — integration connection lost"
                    ),
                }
            # No grace floor here: still-down devices carry fresh
            # post-reload timestamps, which must not read as recovery —
            # and a relapse during recovery must re-page instantly.
            # Devices dark since before the outage (never witnessed
            # alive) don't hold the latch open.
            recovering = self._witnessed_offline_devices(now_utc, 0.0)
            if recovering:
                labels = ", ".join(
                    self._device_label(d) for d in recovering[:3]
                )
                return {
                    "name": AVAILABILITY_CHECK,
                    "status": "warning",
                    "detail": (
                        f"{len(recovering)} device(s) still offline "
                        f"(recovering): {labels}"
                        f"{', …' if len(recovering) > 3 else ''}"
                    ),
                }
            self._availability_down = False
            self.log("Availability outage cleared", level="INFO")

        down_pct = 100.0 * len(down) / len(sensors)
        if down_pct >= self._availability_critical_pct:
            self._availability_down = True
            self.log(
                f"Availability outage CONFIRMED: {len(down)}/{len(sensors)} "
                f"event sensors unavailable for "
                f">{_fmt_age(self._availability_grace_s)}",
                level="WARNING",
            )
            return {
                "name": AVAILABILITY_CHECK,
                "status": "critical",
                "detail": (
                    f"{len(down)}/{len(sensors)} event sensors unavailable "
                    f"for >{_fmt_age(self._availability_grace_s)} — "
                    "integration connection lost"
                ),
            }

        if eligible:
            labels = ", ".join(
                self._device_label(d) for d in eligible[:3]
            )
            return {
                "name": AVAILABILITY_CHECK,
                "status": "warning",
                "detail": (
                    f"{len(eligible)} device(s) recently went offline: "
                    f"{labels}{', …' if len(eligible) > 3 else ''}"
                ),
            }

        available = len(sensors) - len(unavailable_now)
        detail = f"{available}/{len(sensors)} event sensors available"
        if unavailable_now:
            detail += (
                f" ({len(unavailable_now)} on disabled channels or "
                "expected-offline devices)"
            )
        return {
            "name": AVAILABILITY_CHECK,
            "status": "ok",
            "detail": detail,
        }

    def _check_entry_sensors(
        self, sensors: List[EventSensor]
    ) -> Dict[str, str]:
        """Entry-sensor (USL) group: device availability + last-event detail.

        No freshness threshold — a door legitimately not opening for hours
        must not page.  A fully-dark entry DEVICE is a persistent warning
        (these are security sensors, never expected offline — no witnessed
        gate, no warn window).  Channels dead on a live device are disabled
        features (e.g. motion sensing off) and stay quiet.
        """
        entry = [s for s in sensors if s.group == GROUP_ENTRY]
        if not entry:
            return {
                "name": ENTRY_SENSORS_CHECK,
                "status": "ok",
                "detail": "No entry sensors discovered",
            }

        now_utc = datetime.datetime.now(UTC)
        devices = sorted({_sensor_device(s) for s in entry})
        dark = []
        for device_key in devices:
            if self._device_live.get(device_key, True):
                continue
            since = self._device_dark_since.get(device_key)
            age = (
                (now_utc - since).total_seconds()
                if since is not None
                else self._availability_grace_s + 1
            )
            if age > self._availability_grace_s:
                dark.append(device_key)

        if dark:
            labels = ", ".join(self._device_label(d) for d in dark[:3])
            return {
                "name": ENTRY_SENSORS_CHECK,
                "status": "warning",
                "detail": (
                    f"{len(dark)}/{len(devices)} entry sensors offline "
                    f"({labels}{', …' if len(dark) > 3 else ''})"
                ),
            }

        live_channels = [s for s in entry if _is_available(s.state)]
        disabled = len(entry) - len(live_channels)
        base = f"{len(devices)} entry sensors online"
        if disabled:
            base += f" ({disabled} disabled channels)"
        events = [
            (s.entity_id, s.last_changed)
            for s in live_channels
            if s.last_changed is not None
        ]
        if not events:
            return {
                "name": ENTRY_SENSORS_CHECK,
                "status": "ok",
                "detail": base,
            }
        newest_entity, newest_ts = max(events, key=lambda item: item[1])
        age = max(0.0, (now_utc - newest_ts).total_seconds())
        return {
            "name": ENTRY_SENSORS_CHECK,
            "status": "ok",
            "detail": (
                f"{base}; newest event {_fmt_age(age)} ago ({newest_entity})"
            ),
        }

    def _check_event_stream(
        self, sensors: List[Tuple[str, Optional[datetime.datetime]]]
    ) -> Dict[str, str]:
        """Camera-event freshness — expects available camera sensors only."""
        timestamped = [(e, ts) for e, ts in sensors if ts is not None]
        if not timestamped:
            if self._frozen:
                # Don't drop a firing freeze alert just because the camera
                # sensors went unavailable on top of it.
                return {
                    "name": CAMERA_EVENTS_CHECK,
                    "status": "critical",
                    "detail": (
                        "Event stream frozen — camera sensors currently "
                        "unavailable (see Sensor Availability)"
                    ),
                }
            return {
                "name": CAMERA_EVENTS_CHECK,
                "status": "unknown",
                "detail": "No available camera sensors with timestamps",
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
                    "name": CAMERA_EVENTS_CHECK,
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
                    "name": CAMERA_EVENTS_CHECK,
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
                "name": CAMERA_EVENTS_CHECK,
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
                "name": CAMERA_EVENTS_CHECK,
                "status": "critical",
                "detail": (
                    f"No events for {_fmt_age(effective_age_s)} of active "
                    f"hours (threshold {_fmt_age(self._stale_after_s)}); "
                    f"newest: {newest_entity}"
                ),
            }

        return {
            "name": CAMERA_EVENTS_CHECK,
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
        any_critical = any(r["status"] == "critical" for r in results)

        if not any_critical:
            # Stand down once nothing is critical — warnings (e.g. an entry
            # sensor offline for days) must not pin a stale PENDING/SUCCESS/
            # FAILED state that would block auto-heal for the NEXT incident.
            if self._repair_status == REPAIR_PENDING:
                self.log(
                    "No critical checks — cancelling pending auto-repair",
                    level="INFO",
                )
            if self._repair_status == REPAIR_FAILED:
                self.log(
                    "No critical checks — clearing failed repair state",
                    level="INFO",
                )
            if self._repair_status in (REPAIR_PENDING, REPAIR_SUCCESS, REPAIR_FAILED):
                self._repair_status = REPAIR_IDLE
                self._repair_detail = ""
                self._auto_repair_deadline = None
                self._unhealthy_since = None
            return

        # Still critical after a "successful" reload: the success was
        # illusory (results here are computed post-repair).  Fall through to
        # re-arm like FAILED — the reload cooldown paces the retry.

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

        elif self._repair_status in (REPAIR_PENDING, REPAIR_FAILED, REPAIR_SUCCESS):
            # FAILED (and illusory SUCCESS) retries once the reload cooldown
            # allows another attempt.
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
                    # An illusory SUCCESS (still critical) re-arms as a
                    # plain pending repair.
                    self._repair_status = REPAIR_PENDING
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
        """Return the first camera sensor with a genuine event after baseline.

        Unavailable sensors are skipped: a transition INTO unavailable
        updates ``last_changed`` without being an event, so counting it
        would declare a failed reload successful.
        """
        for entity_id in self._sensors:
            try:
                attrs = await self.get_state(entity_id, attribute="all")
            except Exception:
                continue
            if not attrs:
                continue
            if not _is_available(attrs.get("state", "unavailable")):
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
