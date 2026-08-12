"""Pure decision logic for the Zigbee OTA orchestrator.

No AppDaemon imports here — the coordinator is a state machine fed by the
adapter app (``zigbee_ota_app.py``) and returns decisions for it to execute.
All external state (which devices need updates, which are online, what is
currently updating) is re-derived from Home Assistant update entities and
retained Zigbee2MQTT topics on every refresh, so a restart never loses the
queue — only per-attempt retry counters reset, which is safe (the worst case
is retrying a failed device sooner than its backoff would have).
"""

from __future__ import annotations

import fnmatch
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


# Device queue states (derived, not persisted)
STATE_PENDING = "pending"
STATE_IN_FLIGHT = "in_flight"
STATE_COOLDOWN = "cooldown"

# Result classification for a finished attempt
RESULT_SUCCESS = "success"
RESULT_OFFLINE = "offline"
RESULT_ERROR = "error"
RESULT_BUSY = "busy"

_OFFLINE_ERROR_MARKERS = ("respond", "timeout", "timed out", "offline", "unreachable")
_BUSY_ERROR_MARKERS = ("already in progress",)


@dataclass
class StartUpdate:
    """Decision: publish an OTA update request for this device."""

    friendly_name: str
    transaction: str


@dataclass
class InFlight:
    friendly_name: str
    transaction: Optional[str]  # None when adopted from an externally started update
    started_ts: float
    adopted: bool = False
    progress: Optional[float] = None
    remaining_s: Optional[float] = None
    last_progress_ts: float = 0.0
    stalled: bool = False

    def as_status(self) -> dict[str, Any]:
        return {
            "device": self.friendly_name,
            "adopted": self.adopted,
            "progress_pct": self.progress,
            "remaining_s": self.remaining_s,
            "started_ts": self.started_ts,
            "stalled": self.stalled,
        }


@dataclass
class DeviceRecord:
    entity_id: str
    friendly_name: str
    installed_version: Optional[str] = None
    latest_version: Optional[str] = None
    attempts: int = 0
    next_attempt_ts: float = 0.0
    offline_failure: bool = False
    last_error: Optional[str] = None


@dataclass
class OtaCoordinator:
    include_globs: list[str] = field(default_factory=lambda: ["update.*"])
    exclude_globs: list[str] = field(default_factory=list)
    retry_base_s: float = 900.0
    retry_max_s: float = 21600.0
    busy_backoff_s: float = 300.0
    online_retry_grace_s: float = 60.0
    progress_stall_s: float = 2700.0
    update_timeout_s: float = 14400.0
    completed_suppress_s: float = 600.0
    now: Callable[[], float] = time.time
    make_transaction: Callable[[str], str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.make_transaction is None:
            self.make_transaction = lambda name: f"zota-{int(self.now())}-{abs(hash(name)) % 10000}"
        self._devices: dict[str, DeviceRecord] = {}  # friendly_name -> record
        self._known_z2m_devices: set[str] = set()
        self._availability: dict[str, bool] = {}
        self._in_flight: Optional[InFlight] = None
        self._global_busy_until: float = 0.0
        self._completed: list[dict[str, Any]] = []  # this process lifetime only
        self._recently_completed: dict[str, float] = {}
        self._failed_attempts: int = 0
        self._last_event: str = "startup"

    # ------------------------------------------------------------------
    # Inputs
    # ------------------------------------------------------------------

    def set_known_devices(self, friendly_names: set[str]) -> None:
        """Friendly names from the retained zigbee2mqtt/bridge/devices doc."""
        self._known_z2m_devices = set(friendly_names)

    def set_availability(self, friendly_name: str, online: bool) -> bool:
        """Track availability. Returns True when an offline-failed device came
        back online (the adapter should schedule a prompt tick)."""
        was_online = self._availability.get(friendly_name)
        self._availability[friendly_name] = online
        if online and was_online is not True:
            rec = self._devices.get(friendly_name)
            if rec is not None and rec.offline_failure:
                # Device regained power: collapse the remaining backoff to a
                # short grace so it retries soon, as long as it stays online.
                rec.next_attempt_ts = min(
                    rec.next_attempt_ts, self.now() + self.online_retry_grace_s
                )
                self._last_event = f"{friendly_name} back online; retry rescheduled"
                return True
        return False

    def refresh_entities(self, snapshot: dict[str, dict[str, Any]]) -> None:
        """Re-derive the queue from a Home Assistant update-domain snapshot.

        ``snapshot`` maps entity_id -> {"state": "on"/"off", "attributes": {...}}.
        Devices are matched to Zigbee2MQTT via the entity friendly_name, which
        Z2M discovery sets to the device friendly name. Entities that don't
        correspond to a known Z2M device are ignored even if they match the
        include globs (protects against non-Z2M update entities).
        """
        seen: set[str] = set()
        adopted_candidate: Optional[str] = None
        for entity_id, payload in snapshot.items():
            if not self._entity_matches(entity_id):
                continue
            attrs = payload.get("attributes") or {}
            friendly = attrs.get("friendly_name") or entity_id.split(".", 1)[1]
            if self._known_z2m_devices and friendly not in self._known_z2m_devices:
                continue
            if payload.get("state") != "on":
                # Update no longer pending: if we were tracking it, it finished.
                existing = self._devices.pop(friendly, None)
                if existing is not None:
                    self._record_completion(friendly, existing, attrs)
                continue
            completed_ts = self._recently_completed.get(friendly)
            if (
                completed_ts is not None
                and self.now() - completed_ts < self.completed_suppress_s
            ):
                # HA's update entity can lag Z2M's success by a few seconds;
                # don't re-queue a device we just finished off a stale snapshot.
                continue
            seen.add(friendly)
            rec = self._devices.get(friendly)
            if rec is None:
                rec = DeviceRecord(entity_id=entity_id, friendly_name=friendly)
                self._devices[friendly] = rec
            rec.installed_version = str(attrs.get("installed_version"))
            rec.latest_version = str(attrs.get("latest_version"))
            if attrs.get("in_progress") and (
                self._in_flight is None or self._in_flight.friendly_name != friendly
            ):
                adopted_candidate = friendly

        # Drop devices that disappeared from the snapshot entirely (renamed,
        # removed from Z2M, or no longer matching) — the queue is derived state.
        for friendly in list(self._devices):
            if friendly not in seen:
                self._devices.pop(friendly)

        if adopted_candidate is not None and self._in_flight is None:
            self._in_flight = InFlight(
                friendly_name=adopted_candidate,
                transaction=None,
                started_ts=self.now(),
                adopted=True,
                last_progress_ts=self.now(),
            )
            self._last_event = f"adopted in-progress update for {adopted_candidate}"

    def on_device_update_obj(self, friendly_name: str, update: dict[str, Any]) -> None:
        """Progress from the device state topic's ``update`` object."""
        fl = self._in_flight
        if fl is None or fl.friendly_name != friendly_name:
            return
        state = update.get("state")
        if state == "updating":
            progress = update.get("progress")
            if progress is not None and progress != fl.progress:
                fl.progress = float(progress)
                fl.last_progress_ts = self.now()
                fl.stalled = False
            remaining = update.get("remaining")
            if remaining is not None:
                fl.remaining_s = float(remaining)
        elif fl.adopted and state in ("idle", "available"):
            # Adopted updates have no transaction to correlate a response with;
            # the update object leaving "updating" is their terminal signal.
            # installed == latest (state idle) means success.
            success = state == "idle"
            self._finish_in_flight(
                RESULT_SUCCESS if success else RESULT_ERROR,
                error=None if success else "adopted update ended without installing",
            )

    def on_update_response(self, payload: dict[str, Any]) -> None:
        """Terminal response on zigbee2mqtt/bridge/response/device/ota_update/update."""
        status = payload.get("status")
        data = payload.get("data") or {}
        friendly = data.get("id")
        transaction = payload.get("transaction")
        fl = self._in_flight
        matches_flight = fl is not None and (
            (transaction is not None and transaction == fl.transaction)
            or (friendly is not None and friendly == fl.friendly_name)
        )
        if status == "ok":
            if matches_flight:
                self._finish_in_flight(RESULT_SUCCESS)
            elif friendly in self._devices:
                # e.g. we timed the attempt out locally but Z2M finished it.
                rec = self._devices.pop(friendly)
                self._record_completion(friendly, rec, {})
                self._last_event = f"{friendly} completed outside tracked attempt"
            return
        error = str(payload.get("error") or "unknown error")
        lowered = error.lower()
        if any(marker in lowered for marker in _BUSY_ERROR_MARKERS):
            # Another OTA (ours after a local timeout, or manual) is running.
            self._global_busy_until = self.now() + self.busy_backoff_s
            if matches_flight and fl is not None and not fl.adopted:
                # Our request never started; requeue without burning an attempt.
                self._in_flight = None
                self._last_event = f"Z2M busy; {fl.friendly_name} requeued"
            return
        if matches_flight:
            offline = any(marker in lowered for marker in _OFFLINE_ERROR_MARKERS)
            self._finish_in_flight(RESULT_OFFLINE if offline else RESULT_ERROR, error=error)

    # ------------------------------------------------------------------
    # Decisions
    # ------------------------------------------------------------------

    def decide(self) -> Optional[StartUpdate]:
        """Called on every tick. Handles in-flight housekeeping and, when idle,
        picks the next eligible device and returns a StartUpdate decision."""
        ts = self.now()
        fl = self._in_flight
        if fl is not None:
            if ts - fl.started_ts > self.update_timeout_s:
                self._finish_in_flight(
                    RESULT_ERROR,
                    error=f"no terminal response after {int(self.update_timeout_s)}s",
                )
            else:
                if (
                    fl.last_progress_ts
                    and ts - fl.last_progress_ts > self.progress_stall_s
                ):
                    fl.stalled = True
                return None
        if ts < self._global_busy_until:
            return None
        candidate = self._next_candidate(ts)
        if candidate is None:
            return None
        transaction = self.make_transaction(candidate.friendly_name)
        self._in_flight = InFlight(
            friendly_name=candidate.friendly_name,
            transaction=transaction,
            started_ts=ts,
            last_progress_ts=ts,
        )
        self._last_event = f"starting update for {candidate.friendly_name}"
        return StartUpdate(friendly_name=candidate.friendly_name, transaction=transaction)

    def _next_candidate(self, ts: float) -> Optional[DeviceRecord]:
        eligible = [
            rec
            for rec in self._devices.values()
            if rec.next_attempt_ts <= ts
            # Unknown availability counts as online: with no retained message
            # yet the request itself is the probe (failure lands in cooldown).
            and self._availability.get(rec.friendly_name, True)
            and (
                self._in_flight is None
                or self._in_flight.friendly_name != rec.friendly_name
            )
        ]
        if not eligible:
            return None
        # Fresh devices first so one flaky bulb can't starve the fleet; then
        # alphabetical for a deterministic, resumable order.
        return min(eligible, key=lambda rec: (rec.attempts, rec.friendly_name))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _entity_matches(self, entity_id: str) -> bool:
        if not any(fnmatch.fnmatch(entity_id, glob) for glob in self.include_globs):
            return False
        return not any(fnmatch.fnmatch(entity_id, glob) for glob in self.exclude_globs)

    def _finish_in_flight(self, result: str, error: Optional[str] = None) -> None:
        fl = self._in_flight
        if fl is None:
            return
        self._in_flight = None
        rec = self._devices.get(fl.friendly_name)
        if result == RESULT_SUCCESS:
            if rec is not None:
                self._devices.pop(fl.friendly_name, None)
                self._record_completion(fl.friendly_name, rec, {})
            else:
                self._recently_completed[fl.friendly_name] = self.now()
                self._completed.append(
                    {"device": fl.friendly_name, "ts": self.now()}
                )
            self._last_event = f"{fl.friendly_name} updated successfully"
            return
        self._failed_attempts += 1
        if rec is None:
            self._last_event = f"{fl.friendly_name} failed: {error}"
            return
        rec.attempts += 1
        rec.last_error = error
        rec.offline_failure = result == RESULT_OFFLINE
        backoff = min(self.retry_base_s * (2 ** (rec.attempts - 1)), self.retry_max_s)
        rec.next_attempt_ts = self.now() + backoff
        self._last_event = (
            f"{fl.friendly_name} attempt {rec.attempts} failed ({error}); "
            f"retry in {int(backoff)}s"
        )

    def _record_completion(
        self, friendly: str, rec: DeviceRecord, attrs: dict[str, Any]
    ) -> None:
        self._recently_completed[friendly] = self.now()
        self._completed.append(
            {
                "device": friendly,
                "ts": self.now(),
                "version": str(
                    attrs.get("installed_version") or rec.latest_version or ""
                ),
            }
        )
        if self._in_flight is not None and self._in_flight.friendly_name == friendly:
            self._in_flight = None

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        ts = self.now()
        cooldown = sorted(
            (
                {
                    "device": rec.friendly_name,
                    "attempts": rec.attempts,
                    "retry_in_s": max(0, int(rec.next_attempt_ts - ts)),
                    "offline_failure": rec.offline_failure,
                    "last_error": rec.last_error,
                }
                for rec in self._devices.values()
                if rec.attempts > 0 and rec.next_attempt_ts > ts
            ),
            key=lambda item: item["device"],
        )
        offline = sorted(
            rec.friendly_name
            for rec in self._devices.values()
            if self._availability.get(rec.friendly_name) is False
        )
        in_flight_name = self._in_flight.friendly_name if self._in_flight else None
        remaining = len(self._devices)
        if in_flight_name is not None and in_flight_name not in self._devices:
            remaining += 1
        return {
            "remaining": remaining,
            "pending": sorted(
                rec.friendly_name
                for rec in self._devices.values()
                if rec.next_attempt_ts <= ts and rec.friendly_name != in_flight_name
            ),
            "in_flight": self._in_flight.as_status() if self._in_flight else None,
            "cooldown": cooldown,
            "offline": offline,
            "completed_this_run": self._completed[-25:],
            "completed_count_this_run": len(self._completed),
            "failed_attempts_this_run": self._failed_attempts,
            "busy_wait_s": max(0, int(self._global_busy_until - ts)),
            "last_event": self._last_event,
        }
