"""Pure decision logic for the Zigbee OTA orchestrator.

No AppDaemon imports here — the coordinator is a state machine fed by the
adapter app (``zigbee_ota_app.py``) and returns decisions for it to execute.
All external state (which devices need updates, which are online, what is
currently updating) is re-derived from Home Assistant update entities and
retained Zigbee2MQTT topics on every refresh, so a restart never loses the
queue — only per-attempt retry counters reset, which is safe (the worst case
is retrying a failed device sooner than its backoff would have).

The coordinator fails closed on device identity: it manages an ``update.*``
entity only when an identity source has vouched for it being a Zigbee2MQTT
device, so the include glob can be as broad as ``update.*`` without ever
touching an Immich, HACS, ESPHome or Z-Wave update entity.
"""

from __future__ import annotations

import fnmatch
import time
from dataclasses import dataclass, field
from datetime import datetime
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

# Attribute lists are capped: Home Assistant writes a recorder row every time
# the sensor's attributes change, and an uncapped list on a 163-device fleet
# makes that row multi-kB.
STATUS_LIST_CAP = 25
ERROR_TEXT_CAP = 120

# How long a "this device is transferring" mark is carried while its entity
# says nothing — absent from the state dump, or unavailable. Silence is not
# evidence a transfer stopped, but an unbounded mark would let one device that
# never comes back (dead battery, switched off at the wall) stall the fleet
# for good. Deliberately not update_timeout_s: that clock starts when the
# attempt starts, so the two would expire together and the guard would be
# useless for the very case it exists for.
TRANSFER_MARK_TTL_S = 3600.0

_OFFLINE_ERROR_MARKERS = ("respond", "timeout", "timed out", "offline", "unreachable")
_BUSY_ERROR_MARKERS = ("already in progress",)
# Z2M answers "No image currently available" (and, older, "No image available")
# when the device advertises an update but the OTA index has no file for it —
# typically a release the maintainers pulled. Nothing is transferred, so it is
# neither a completed update nor a failure worth retrying.
_NO_IMAGE_ERROR_MARKERS = ("no image",)
# Z2M answers "Device 'X' does not exist" when the id it was sent names no
# device — which happens when the Home Assistant friendly_name has been
# renamed away from the Zigbee2MQTT device name. Retrying can only repeat it.
_UNKNOWN_DEVICE_MARKERS = ("does not exist",)

# Why a device is parked (no attempt burned, no backoff scheduled).
PARK_NO_IMAGE = "no image"
PARK_UNKNOWN = "unknown to zigbee2mqtt"

# How the Z2M device list was learned, for the status sensor.
IDENTITY_NONE = "none"
IDENTITY_HA = "home assistant"
IDENTITY_BRIDGE = "zigbee2mqtt bridge"


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
        status: dict[str, Any] = {
            "device": self.friendly_name,
            "adopted": self.adopted,
            "started_at": _at(self.started_ts),
            "stalled": self.stalled,
        }
        # Omit rather than report None: Home Assistant drops null attributes.
        if self.progress is not None:
            status["progress_pct"] = self.progress
        if self.remaining_s is not None:
            status["remaining_s"] = self.remaining_s
        return status


def _at(ts: float) -> str:
    """Render an absolute moment for the status sensor.

    Absolute, never a countdown: Home Assistant writes a recorder row every
    time an attribute changes, and a countdown changes on every tick by
    definition. "Retrying at 14:32" is also easier to read than "in 871s".

    Timezone-aware, because a naive string reads as UTC in a Home Assistant
    template and can't be compared with ``now()``.
    """
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


@dataclass
class ParkedDevice:
    """A device Z2M cannot install right now, for a reason retrying won't fix."""

    reason: str
    latest_version: Optional[str]  # None until the next refresh learns it
    ts: float


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
    park_recheck_s: float = 86400.0
    # How long a device must stay absent before it is retired. Set from the
    # app's scan_interval_s: refreshes are not evenly spaced (an MQTT response
    # drives one immediately), so counting them would let two land
    # milliseconds apart on the same partial dump.
    retire_grace_s: float = 120.0
    now: Callable[[], float] = time.time
    make_transaction: Callable[[str], str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.make_transaction is None:
            self.make_transaction = lambda name: f"zota-{int(self.now())}-{abs(hash(name)) % 10000}"
        self._devices: dict[str, DeviceRecord] = {}  # friendly_name -> record
        self._known_z2m_devices: set[str] = set()
        self._z2m_entity_ids: Optional[set[str]] = None
        self._identity_stale: Optional[str] = None
        self._availability: dict[str, bool] = {}
        self._in_flight: Optional[InFlight] = None
        self._global_busy_until: float = 0.0
        self._completed: list[dict[str, Any]] = []  # this process lifetime only
        self._recently_completed: dict[str, float] = {}
        # Devices whose attempt has settled while Home Assistant may still be
        # showing in_progress. Anchored to device state rather than a clock:
        # a wall-clock window would have to outlast the tick interval to be
        # any use, and outlasting it is exactly what makes a genuine external
        # install invisible.
        self._abandoned: set[str] = set()
        # friendly_name -> when it was first absent from a usable refresh
        self._absent: dict[str, float] = {}
        # friendly_name -> when its entity last reported a transfer under way.
        # Carried across ticks where the entity says nothing (absent from the
        # dump, or unavailable) and expired after TRANSFER_MARK_TTL_S.
        self._transferring: dict[str, float] = {}
        self._parked: dict[str, ParkedDevice] = {}  # not installable right now
        self._cleared: list[dict[str, Any]] = []
        self._failed_attempts: int = 0
        self._last_event: str = "startup"

    # ------------------------------------------------------------------
    # Inputs
    # ------------------------------------------------------------------

    def set_known_devices(self, friendly_names: set[str]) -> None:
        """Friendly names from the retained zigbee2mqtt/bridge/devices doc.

        A secondary identity source: AppDaemon's MQTT plugin subscribes once at
        plugin start, so the retained document is usually delivered seconds
        before this app registers its listener and is never replayed. When it
        does arrive it is trusted; the Home Assistant lookup below is what the
        app actually relies on.
        """
        self._known_z2m_devices = set(friendly_names)

    def set_z2m_entities(self, entity_ids: set[str]) -> bool:
        """The authoritative set of Zigbee2MQTT ``update.*`` entity ids, read
        from Home Assistant on every tick. Returns False when the answer was
        rejected and the previous list is still in force.

        An empty answer where devices were known before is treated as a failed
        lookup, not as an empty fleet: the mqtt config entry still setting up
        after a Home Assistant restart renders exactly that, and accepting it
        would drop every queued device along with its retry state.
        """
        fresh = set(entity_ids)
        if not fresh and self._z2m_entity_ids:
            self.mark_identity_unavailable(
                "Home Assistant reported no Zigbee2MQTT update entities"
            )
            return False
        self._z2m_entity_ids = fresh
        self._identity_stale = None
        return True

    def mark_identity_unavailable(self, reason: str) -> None:
        """The Home Assistant lookup failed this tick. Keep the last known-good
        set for reference but start nothing until a fresh one arrives."""
        self._identity_stale = reason
        # A tick we could not read Home Assistant on is not evidence that a
        # device has gone; don't let it age one towards retirement.
        self._absent.clear()
        self._last_event = f"holding: {reason}"

    @property
    def identity_ready(self) -> bool:
        """True when some trustworthy source names at least one Z2M device.

        An empty list is deliberately not ready: it manages nothing, so
        treating it as a working state would let the app sit there looking
        healthy while silently doing nothing — and would re-derive (and so
        empty) the queue on every tick.
        """
        return bool(self._z2m_entity_ids) or bool(self._known_z2m_devices)

    def _is_z2m(self, entity_id: str, friendly: str) -> bool:
        """Fail closed: an entity is managed only when a source vouches for it."""
        if self._z2m_entity_ids is not None and entity_id in self._z2m_entity_ids:
            return True
        return friendly in self._known_z2m_devices

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
        An entity is only managed when an identity source vouches for it being a
        Zigbee2MQTT device — the Home Assistant lookup (:meth:`set_z2m_entities`)
        or the retained bridge document (:meth:`set_known_devices`). With no
        source at all the queue is left untouched and stays empty, so a broad
        include glob can never publish an OTA request for, say, an Immich or
        HACS update entity.
        """
        if self._identity_stale is not None:
            # The device list could not be refreshed this tick. Re-deriving the
            # queue from a snapshot we can't match against a trustworthy list
            # would drop devices that are still there. Checked first so
            # last_event keeps the reason mark_identity_unavailable just set.
            return
        if not self.identity_ready:
            self._last_event = (
                "no Zigbee2MQTT device list yet — nothing queued"
            )
            self._absent.clear()
            return
        present: set[str] = set()
        # Carried forward: silence is not evidence a transfer stopped.
        transferring: dict[str, float] = dict(self._transferring)
        adopted_candidate: Optional[str] = None
        for entity_id, payload in snapshot.items():
            if not self._entity_matches(entity_id):
                continue
            attrs = payload.get("attributes") or {}
            friendly = attrs.get("friendly_name") or entity_id.split(".", 1)[1]
            if not self._is_z2m(entity_id, friendly):
                continue
            present.add(friendly)
            state = payload.get("state")
            if state not in ("on", "off"):
                # unavailable/unknown: Z2M discovery marks the update entity
                # unavailable whenever the device is out of touch (a bulb
                # switched off at the wall, a Home Assistant restart). That
                # says nothing about the firmware, so keep everything we know
                # — the queue entry, its backoff, its no-image park — and wait.
                # Home Assistant is also the reliable feed for the offline
                # gate: the retained availability topics are delivered to the
                # MQTT plugin before this app has a listener, so a device that
                # was already offline at startup has no MQTT record at all.
                # Deliberately NOT discarding from _abandoned here: an
                # unavailable entity is an absence of information, and the
                # device dropping off the mesh is exactly what makes a
                # transfer go silent, so this flap is the expected condition
                # around the timeout this suppression exists for. The
                # transferring mark is carried by default for the same
                # reason, so there is nothing to do for it here.
                self.set_availability(friendly, False)
                continue
            self.set_availability(friendly, True)
            if state != "on":
                # Nothing is on offer any more, so nothing is being skipped
                # and nothing is being transferred.
                transferring.pop(friendly, None)
                self._abandoned.discard(friendly)
                parked = self._parked.pop(friendly, None)
                # Work out whether anything actually installed.
                existing = self._devices.pop(friendly, None)
                if existing is not None:
                    self._settle_cleared(friendly, existing, attrs)
                elif (
                    self._in_flight is not None
                    and self._in_flight.friendly_name == friendly
                ):
                    # An update can be in flight with no record of its own —
                    # an external install on a parked device, or our own
                    # attempt whose record an earlier refresh dropped. The
                    # entity going off is its terminal signal either way, and
                    # without this the slot is held until update_timeout_s.
                    adopted = self._in_flight.adopted
                    self._in_flight = None
                    installed = attrs.get("installed_version")
                    if (
                        parked is not None
                        and parked.latest_version is not None
                        and installed is not None
                        and str(installed) == parked.latest_version
                    ):
                        # The version it was parked on is now installed: proof
                        # enough to count it, which is the only way an external
                        # install on a parked device is ever recorded.
                        self._record_completion(
                            friendly,
                            DeviceRecord(
                                entity_id=entity_id,
                                friendly_name=friendly,
                                latest_version=parked.latest_version,
                            ),
                            attrs,
                        )
                    else:
                        self._last_event = (
                            f"{friendly}: "
                            + ("external update" if adopted else "attempt")
                            + " ended; no version change to confirm"
                        )
                continue
            completed_ts = self._recently_completed.get(friendly)
            just_finished = (
                completed_ts is not None
                and self.now() - completed_ts < self.completed_suppress_s
            )
            in_progress = bool(attrs.get("in_progress"))
            if in_progress:
                # Recorded whatever else we decide about this device. Z2M's
                # in-progress guard is per device, so a transfer we are not
                # tracking is still a transfer on the mesh.
                transferring[friendly] = self.now()
            else:
                transferring.pop(friendly, None)
            if not in_progress:
                # Home Assistant has caught up: whatever we settled is no
                # longer showing, so a future in_progress is a real install.
                self._abandoned.discard(friendly)
            if (
                in_progress
                and friendly not in self._abandoned
                and (
                    self._in_flight is None
                    or self._in_flight.friendly_name != friendly
                )
            ):
                # Checked before the park skip below: an externally started
                # update must be adopted even for a device we would otherwise
                # pass over, or decide() starts a second one alongside it.
                # Z2M's in-progress guard is per device and would not reject
                # the second request. Suppressed for a device we just settled,
                # whose flag Home Assistant has not cleared yet — adopting
                # that is a phantom flight only the four-hour timeout ends,
                # and the timeout would re-adopt it, forever.
                adopted_candidate = friendly
            if friendly in self._parked and self._still_parked(friendly, attrs):
                continue
            if just_finished:
                # Don't re-queue a device we just finished off a stale snapshot.
                continue
            rec = self._devices.get(friendly)
            if rec is None:
                rec = DeviceRecord(entity_id=entity_id, friendly_name=friendly)
                self._devices[friendly] = rec
            rec.installed_version = str(attrs.get("installed_version"))
            rec.latest_version = str(attrs.get("latest_version"))

        # Retire devices that disappeared (renamed, removed from Z2M, or no
        # longer matching) — the queue is derived state. One absent snapshot
        # is not proof of that: entity restore is not atomic, and
        # integration_entities() tracks the state machine rather than the
        # registry (verified on this install: 6591 entities, none outside the
        # state machine), so mid-restart the identity set and the dump shrink
        # together and nothing distinguishes a device that is gone from one
        # whose state has not come back yet. Staying absent for a whole scan
        # interval does: a restore catches up well inside one, and a real
        # removal is retired a tick later than it used to be, which costs
        # nothing.
        # A mark that has gone unconfirmed for TRANSFER_MARK_TTL_S has
        # outlived any plausible silent transfer; honouring it further would
        # let one device that never comes back stall the whole fleet.
        cutoff = self.now() - TRANSFER_MARK_TTL_S
        self._transferring = {
            name: ts for name, ts in transferring.items() if ts > cutoff
        }
        tracked = set(self._devices) | set(self._parked) | self._abandoned
        # Rebuilt, not updated in place: a record popped outside a refresh (a
        # success, or a late ok) would otherwise keep a stale mark and be
        # retired on its first absence the next time it is queued.
        self._absent = {
            name: ts
            for name, ts in self._absent.items()
            if name in tracked and name not in present
        }
        for friendly in tracked - present:
            self._absent.setdefault(friendly, self.now())
        gone = {
            name
            for name, ts in self._absent.items()
            if self.now() - ts >= self.retire_grace_s
        }
        for friendly in gone:
            self._devices.pop(friendly, None)
            self._parked.pop(friendly, None)
            self._absent.pop(friendly, None)
            self._transferring.pop(friendly, None)
        self._abandoned -= gone

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
        if transaction is not None:
            # A transaction identifies exactly one attempt. Never fall back to
            # the name when one is present: Z2M's late answer for an attempt we
            # already abandoned would otherwise be applied to the device's
            # *next* attempt, burning it and freeing the slot while Z2M is
            # still working.
            matches_flight = fl is not None and transaction == fl.transaction
        else:
            matches_flight = (
                fl is not None
                and friendly is not None
                and friendly == fl.friendly_name
            )
        # An answer about a device that is in flight again under a different
        # transaction concerns an attempt we already settled. It can be
        # reported, but it must not touch the live attempt's record, its park
        # or its slot — doing so is how a settled answer sabotages the retry
        # it has nothing to do with.
        stale_for_live_device = (
            not matches_flight
            and fl is not None
            and friendly is not None
            and friendly == fl.friendly_name
        )
        if status == "ok":
            if matches_flight:
                self._finish_in_flight(RESULT_SUCCESS)
            elif stale_for_live_device:
                self._last_event = f"{friendly}: late success for a settled attempt"
            elif friendly in self._devices:
                # A late success for an attempt we already gave up on, for a
                # device that is not running one right now.
                rec = self._devices.pop(friendly)
                self._record_completion(friendly, rec, {})
                self._last_event = f"{friendly} completed outside tracked attempt"
            return
        error = str(payload.get("error") or "unknown error")
        lowered = error.lower()
        if any(marker in lowered for marker in _NO_IMAGE_ERROR_MARKERS):
            # Nothing was transferred and nothing will be until the OTA index
            # carries a file again: not a completion, and not worth retrying.
            if stale_for_live_device:
                self._last_event = f"{friendly}: late no-image for a settled attempt"
                return
            target = fl.friendly_name if (matches_flight and fl is not None) else friendly
            if target:
                self._record_skip(target, PARK_NO_IMAGE, error, matches_flight)
            return
        if any(marker in lowered for marker in _UNKNOWN_DEVICE_MARKERS):
            # The id we sent names no Z2M device — almost always a Home
            # Assistant rename. The availability and progress topics for this
            # name can never match either, so retrying is pointless.
            if stale_for_live_device:
                self._last_event = f"{friendly}: late unknown-device for a settled attempt"
                return
            target = fl.friendly_name if (matches_flight and fl is not None) else friendly
            if target:
                self._record_skip(target, PARK_UNKNOWN, error, matches_flight)
            return
        if any(marker in lowered for marker in _BUSY_ERROR_MARKERS):
            # Another OTA (ours after a local timeout, or manual) is running.
            self._global_busy_until = self.now() + self.busy_backoff_s
            if (
                transaction is not None
                and matches_flight
                and fl is not None
                and not fl.adopted
            ):
                # Only ever ours: pressing Install in the Home Assistant UI
                # publishes the same request without a transaction, and Z2M's
                # per-device guard answers it with exactly this error. Matching
                # that by name would release a transfer that is running.
                # Our request never started; requeue without burning an attempt.
                self._in_flight = None
                rec = self._devices.get(fl.friendly_name)
                if rec is not None:
                    # Strictly past the global window, not level with it:
                    # _is_eligible clears both gates in the same tick, and a
                    # busy bounce burns no attempt, so a device rescheduled to
                    # the window's own instant still wins the pick and the
                    # fleet spins on it. Z2M's still-open operation is often
                    # one we abandoned on a stall, so give the others a turn.
                    rec.next_attempt_ts = max(
                        rec.next_attempt_ts,
                        self._global_busy_until + self.retry_base_s,
                    )
                    # Z2M answered, so whatever earlier failure marked this
                    # device offline is over. Leaving the flag set would let
                    # the next availability flap collapse the hold above to
                    # online_retry_grace_s and restart the spin.
                    rec.offline_failure = False
                self._last_event = f"Z2M busy; {fl.friendly_name} requeued"
            return
        if matches_flight:
            offline = any(marker in lowered for marker in _OFFLINE_ERROR_MARKERS)
            self._finish_in_flight(RESULT_OFFLINE if offline else RESULT_ERROR, error=error)
            return
        if stale_for_live_device:
            self._last_event = f"{friendly}: late failure for a settled attempt"
            return
        rec = self._devices.get(friendly) if friendly else None
        if rec is not None:
            # Z2M's late answer for an attempt we already gave up on. Keep the
            # reason visible without touching the backoff already scheduled —
            # the attempt was counted when we abandoned it.
            rec.last_error = error
            self._last_event = f"{friendly}, after we gave up: {error}"

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
                # Same optimism as the never-started branch below, with more
                # reason for it: this attempt may have been transferring right
                # up until it went silent. Stagger the next device.
                self._global_busy_until = ts + self.busy_backoff_s
            elif (
                not fl.adopted
                and fl.progress is None
                and ts - fl.started_ts > self.progress_stall_s
            ):
                # Our request never sent a single byte. A sleeping battery
                # device that Z2M still counts as online (its passive
                # availability timeout is 25h) would otherwise hold the
                # fleet's only slot for the full update_timeout_s.
                #
                # Never for an adopted update: Z2M's in-progress guard is per
                # device, so abandoning one and starting another in the same
                # tick would put two transfers on the mesh — the exact thing
                # adoption exists to prevent. update_timeout_s is the backstop
                # there, because an adopted update's terminal signal is the
                # device's update object rather than a response we can match.
                #
                # Offline-type so the device is fast-tracked if its entity
                # goes unavailable and comes back; otherwise it simply rejoins
                # the queue on the normal backoff ladder.
                self._finish_in_flight(
                    RESULT_OFFLINE,
                    error=(
                        "no transfer started within "
                        f"{int(self.progress_stall_s)}s"
                    ),
                )
                # Releasing the slot is optimistic: Z2M has no cancel API, so
                # its operation for that device may still be open until its
                # own timeout fires. Nothing is on the air (no byte was ever
                # transferred), but stagger the next device by the busy window
                # rather than starting one in the very same tick.
                self._global_busy_until = ts + self.busy_backoff_s
            else:
                if (
                    fl.last_progress_ts
                    and ts - fl.last_progress_ts > self.progress_stall_s
                ):
                    fl.stalled = True
                return None
        if not self.identity_ready:
            return None
        if self._identity_stale is not None:
            # The device list could not be refreshed this tick; don't guess.
            return None
        others_transferring = self._others_transferring()
        if others_transferring:
            # Something on the mesh is mid-transfer that we are not tracking:
            # an external install, or an attempt we timed out on that Z2M is
            # still running. Starting another would put two transfers on the
            # air, which is the one thing this app must never do. A stale flag
            # only delays the next start until Home Assistant clears it, and
            # a stalled queue is visible on the sensor where a doubled
            # transfer would not be.
            self._last_event = f"holding: {others_transferring[0]} still transferring"
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

    def _others_transferring(self) -> list[str]:
        """Managed devices reporting a transfer that is not the in-flight one.

        Z2M's in-progress guard is per device, so it would not reject a second
        request; while this is non-empty nothing may start.
        """
        in_flight = self._in_flight.friendly_name if self._in_flight else None
        return sorted(
            name for name in self._transferring if name != in_flight
        )

    def _is_eligible(self, rec: DeviceRecord, ts: float) -> bool:
        """Can this device be started right now?

        The status sensor's ``pending`` list uses this too — a device listed as
        ready that the picker would never choose is just confusing.
        """
        return (
            rec.next_attempt_ts <= ts
            and ts >= self._global_busy_until
            and not self._others_transferring()
            # Unknown availability counts as online: with nothing said either
            # way, the request itself is the probe (failure lands in cooldown).
            and self._availability.get(rec.friendly_name, True)
            and (
                self._in_flight is None
                or self._in_flight.friendly_name != rec.friendly_name
            )
        )

    def _next_candidate(self, ts: float) -> Optional[DeviceRecord]:
        eligible = [
            rec for rec in self._devices.values() if self._is_eligible(rec, ts)
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
        self._abandoned.add(fl.friendly_name)
        rec = self._devices.get(fl.friendly_name)
        if result == RESULT_SUCCESS:
            if rec is not None:
                self._devices.pop(fl.friendly_name, None)
                self._record_completion(fl.friendly_name, rec, {})
            else:
                self._recently_completed[fl.friendly_name] = self.now()
                # Same shape as every other entry; the version is simply not
                # known here, the record having already been dropped.
                self._completed.append(
                    {
                        "device": fl.friendly_name,
                        "at": _at(self.now()),
                        "version": "",
                    }
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

    def _still_parked(self, friendly: str, attrs: dict[str, Any]) -> bool:
        """Should a no-image device stay parked this tick?

        It is released when a different version is offered, and re-checked
        every ``park_recheck_s`` — upstream often republishes a pulled
        release under the *same* version number, which a version comparison
        alone would never notice. Both releases exist for ``PARK_NO_IMAGE``;
        a ``PARK_UNKNOWN`` park recovers instead when the device is renamed
        back, because the corrected name was never parked. Re-checking one
        costs a single immediate "does not exist" and burns no attempt.
        """
        skip = self._parked[friendly]
        if self.now() - skip.ts >= self.park_recheck_s:
            self._parked.pop(friendly, None)
            self._last_event = f"{friendly}: re-checking (parked: {skip.reason})"
            return False
        offered = str(attrs.get("latest_version"))
        if skip.latest_version is None:
            skip.latest_version = offered  # learn it now, keep skipping
            return True
        if offered == skip.latest_version:
            return True
        self._parked.pop(friendly, None)  # a different release is on offer
        return False

    def _settle_cleared(
        self, friendly: str, rec: DeviceRecord, attrs: dict[str, Any]
    ) -> None:
        """A tracked device's update entity went back to ``off``.

        That only means new firmware is installed when the installed version
        actually moved. Z2M also clears the flag when it withdraws an update it
        cannot deliver (a pulled release), and that must not be reported as a
        successful update.
        """
        installed = attrs.get("installed_version")
        if installed is not None and str(installed) != str(rec.installed_version):
            self._record_completion(friendly, rec, attrs)
            self._clear_in_flight_for(friendly)
            return
        self._clear_in_flight_for(friendly)
        self._cleared.append({"device": friendly, "at": _at(self.now())})
        self._last_event = f"{friendly}: update withdrawn without installing"

    def _clear_in_flight_for(self, friendly: str) -> None:
        if self._in_flight is not None and self._in_flight.friendly_name == friendly:
            self._in_flight = None

    def _record_skip(
        self, friendly: str, reason: str, error: str, end_flight: bool
    ) -> None:
        """Park a device Z2M cannot install right now. No attempt is burned and
        no backoff is scheduled — a retry would only get the same answer."""
        rec = self._devices.pop(friendly, None)
        self._abandoned.add(friendly)
        # latest_version None = not known yet (a response for a device we
        # weren't tracking); the next refresh learns it and keeps skipping.
        self._parked[friendly] = ParkedDevice(
            reason=reason,
            latest_version=rec.latest_version if rec is not None else None,
            ts=self.now(),
        )
        if end_flight:
            self._clear_in_flight_for(friendly)
        self._last_event = f"{friendly} skipped: {error}"

    def _record_completion(
        self, friendly: str, rec: DeviceRecord, attrs: dict[str, Any]
    ) -> None:
        self._recently_completed[friendly] = self.now()
        # Also a settle: its three callers outside _finish_in_flight would
        # otherwise leave the gate open on a device whose in_progress flag is
        # about to be stale.
        self._abandoned.add(friendly)
        self._completed.append(
            {
                "device": friendly,
                "at": _at(self.now()),
                "version": str(
                    attrs.get("installed_version") or rec.latest_version or ""
                ),
            }
        )

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """The status-sensor payload.

        Every attribute change writes a Home Assistant recorder row, so the
        lists are capped (with a count beside them) and every schedule is an
        absolute time rather than a countdown — otherwise a 163-device fleet
        writes a multi-kB row on every tick just because a timer ticked down.

        ``cooldown`` holds every device waiting on a schedule, whether it
        failed or was only bounced by a busy Z2M; ``attempts`` says which.
        A device appears in exactly one of ``in_flight``, ``pending`` and
        ``cooldown``, with three gaps: during a global busy window a device
        whose own schedule has elapsed is in none of them (``busy_until``
        explains it); a cooling-down device adopted from an external install
        shows only under ``in_flight``; and a device whose backoff has
        elapsed while it is still unavailable shows only under ``offline``,
        which is the overnight steady state on a fleet with battery sensors.
        """
        ts = self.now()
        in_flight_name = self._in_flight.friendly_name if self._in_flight else None
        cooldown = sorted(
            (
                {
                    "device": rec.friendly_name,
                    "attempts": rec.attempts,
                    "retry_at": _at(rec.next_attempt_ts),
                    "offline_failure": rec.offline_failure,
                    "last_error": (rec.last_error or "")[:ERROR_TEXT_CAP],
                }
                for rec in self._devices.values()
                if rec.next_attempt_ts > ts
                and rec.friendly_name != in_flight_name
            ),
            key=lambda item: item["device"],
        )
        offline = sorted(
            rec.friendly_name
            for rec in self._devices.values()
            if self._availability.get(rec.friendly_name) is False
        )
        no_image = sorted(
            name
            for name, park in self._parked.items()
            if park.reason == PARK_NO_IMAGE
        )
        unknown = sorted(
            name
            for name, park in self._parked.items()
            if park.reason == PARK_UNKNOWN
        )
        pending = sorted(
            rec.friendly_name
            for rec in self._devices.values()
            if self._is_eligible(rec, ts)
        )
        remaining = len(self._devices)
        if in_flight_name is not None and in_flight_name not in self._devices:
            remaining += 1
        return {
            "remaining": remaining,
            "pending": pending[:STATUS_LIST_CAP],
            "pending_count": len(pending),
            "in_flight": self._in_flight.as_status() if self._in_flight else {},
            "cooldown": cooldown[:STATUS_LIST_CAP],
            "cooldown_count": len(cooldown),
            "offline": offline[:STATUS_LIST_CAP],
            "offline_count": len(offline),
            "completed_this_run": self._completed[-STATUS_LIST_CAP:],
            "completed_count_this_run": len(self._completed),
            "skipped_no_image": no_image[:STATUS_LIST_CAP],
            "skipped_no_image_count": len(no_image),
            "unknown_to_z2m": unknown[:STATUS_LIST_CAP],
            "unknown_to_z2m_count": len(unknown),
            "cleared_without_update": self._cleared[-STATUS_LIST_CAP:],
            "cleared_without_update_count": len(self._cleared),
            "failed_attempts_this_run": self._failed_attempts,
            "busy_until": _at(self._global_busy_until) if ts < self._global_busy_until else "",
            "transferring": sorted(self._transferring)[:STATUS_LIST_CAP],
            "transferring_count": len(self._transferring),
            "z2m_devices_known": self.z2m_device_count,
            "identity_source": self._identity_source(),
            "last_event": self._last_event,
        }

    @property
    def z2m_device_count(self) -> int:
        # Truthiness, matching _identity_source: an empty HA answer must not
        # report 0 devices while the bridge document is what's being used.
        if self._z2m_entity_ids:
            return len(self._z2m_entity_ids)
        return len(self._known_z2m_devices)

    def _identity_source(self) -> str:
        if self._identity_stale is not None:
            return f"stale ({self._identity_stale})"
        if self._z2m_entity_ids:
            return IDENTITY_HA
        if self._known_z2m_devices:
            return IDENTITY_BRIDGE
        return IDENTITY_NONE
