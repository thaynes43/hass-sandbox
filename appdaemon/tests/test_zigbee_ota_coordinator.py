"""Unit tests for the pure OTA coordinator state machine."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo_root / "apps"))

from zigbee_ota.ota_coordinator import (  # noqa: E402
    OtaCoordinator,
    StartUpdate,
)


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.ts = start

    def __call__(self) -> float:
        return self.ts

    def advance(self, seconds: float) -> None:
        self.ts += seconds


def make_coordinator(clock: FakeClock | None = None, **overrides: Any) -> OtaCoordinator:
    clock = clock or FakeClock()
    defaults = dict(
        include_globs=["update.*hue*"],
        retry_base_s=900,
        retry_max_s=21600,
        busy_backoff_s=300,
        online_retry_grace_s=60,
        progress_stall_s=2700,
        update_timeout_s=14400,
        now=clock,
        make_transaction=lambda name: f"t-{name}",
    )
    defaults.update(overrides)
    return OtaCoordinator(**defaults)


def entity(
    name: str,
    state: str = "on",
    in_progress: bool = False,
    installed: str = "100",
    latest: str = "200",
) -> dict[str, Any]:
    return {
        "state": state,
        "attributes": {
            "friendly_name": name,
            "installed_version": installed,
            "latest_version": latest,
            "in_progress": in_progress,
        },
    }


def snapshot(*names: str, **kw: Any) -> dict[str, dict[str, Any]]:
    return {f"update.{name}": entity(name, **kw) for name in names}


def refresh(
    coord: OtaCoordinator,
    snap: dict[str, dict[str, Any]],
    z2m: Any = None,
) -> None:
    """Feed a snapshot, vouching for every entity in it as a Z2M device.

    Home Assistant is the identity source in production, so tests that aren't
    about identity supply it here; pass ``z2m`` to narrow it.
    """
    coord.set_z2m_entities(set(snap) if z2m is None else set(z2m))
    coord.refresh_entities(snap)


# ---------------------------------------------------------------------------
# Queue derivation
# ---------------------------------------------------------------------------


def test_refresh_filters_globs_state_and_known_devices() -> None:
    coord = make_coordinator()
    coord.set_known_devices({"hue_a", "hue_b"})
    snap = snapshot("hue_a", "hue_b")
    snap["update.hue_c"] = entity("hue_c")  # matches glob, unknown to Z2M
    snap["update.inovelli_x"] = entity("inovelli_x")  # fails glob
    snap["update.hue_off"] = entity("hue_off", state="off")  # nothing pending
    coord.refresh_entities(snap)  # bridge/devices is the only identity source
    status = coord.status()
    assert status["pending"] == ["hue_a", "hue_b"]
    assert status["remaining"] == 2
    assert status["identity_source"] == "zigbee2mqtt bridge"


def test_no_identity_source_queues_nothing() -> None:
    """Fail closed: without a Z2M device list the queue stays empty."""
    coord = make_coordinator()
    coord.refresh_entities(snapshot("hue_a"))
    status = coord.status()
    assert status["pending"] == []
    assert status["remaining"] == 0
    assert status["identity_source"] == "none"
    assert coord.decide() is None


def test_ha_identity_filters_non_z2m_entities() -> None:
    """A broad glob must not reach an entity Home Assistant says isn't Z2M."""
    coord = make_coordinator(include_globs=["update.*"])
    snap = snapshot("hue_a")
    snap["update.tom_haynes_version"] = entity("Immich - Tom Version")
    refresh(coord, snap, z2m=["update.hue_a"])
    assert coord.status()["pending"] == ["hue_a"]
    decision = coord.decide()
    assert decision is not None and decision.friendly_name == "hue_a"


def test_identity_lookup_failure_blocks_new_starts() -> None:
    coord = make_coordinator()
    refresh(coord, snapshot("hue_a"))
    coord.mark_identity_unavailable("HA unreachable")
    assert coord.decide() is None
    status = coord.status()
    assert status["identity_source"] == "stale (HA unreachable)"
    assert "HA unreachable" in status["last_event"]
    # The queue survives; a fresh lookup releases the hold.
    refresh(coord, snapshot("hue_a"))
    assert coord.decide() is not None


def test_exclude_globs() -> None:
    coord = make_coordinator(exclude_globs=["update.*_b"])
    refresh(coord, snapshot("hue_a", "hue_b"))
    assert coord.status()["pending"] == ["hue_a"]


def test_vanished_entities_drop_from_queue() -> None:
    coord = make_coordinator()
    refresh(coord, snapshot("hue_a", "hue_b"))
    refresh(coord, snapshot("hue_b"))
    assert coord.status()["pending"] == ["hue_b"]


# ---------------------------------------------------------------------------
# Sequential decisions
# ---------------------------------------------------------------------------


def test_decide_starts_one_update_alphabetically() -> None:
    coord = make_coordinator()
    refresh(coord, snapshot("hue_b", "hue_a"))
    decision = coord.decide()
    assert decision == StartUpdate(friendly_name="hue_a", transaction="t-hue_a")
    assert coord.decide() is None  # one at a time
    assert coord.status()["in_flight"]["device"] == "hue_a"


def test_success_response_advances_to_next_device() -> None:
    coord = make_coordinator()
    refresh(coord, snapshot("hue_a", "hue_b"))
    coord.decide()
    coord.on_update_response(
        {"status": "ok", "transaction": "t-hue_a", "data": {"id": "hue_a"}}
    )
    status = coord.status()
    assert status["completed_count_this_run"] == 1
    assert status["in_flight"] == {}
    decision = coord.decide()
    assert decision is not None and decision.friendly_name == "hue_b"


def test_entity_flipping_off_with_new_version_counts_as_done() -> None:
    coord = make_coordinator()
    refresh(coord, snapshot("hue_a", "hue_b"))
    refresh(
        coord,
        {
            **snapshot("hue_b"),
            "update.hue_a": entity("hue_a", state="off", installed="200"),
        },
    )
    status = coord.status()
    assert status["completed_count_this_run"] == 1
    assert status["completed_this_run"][0]["version"] == "200"
    assert status["pending"] == ["hue_b"]


def test_entity_flipping_off_without_a_new_version_is_not_a_completion() -> None:
    """Z2M withdrawing a pulled release is not a successful update."""
    coord = make_coordinator()
    refresh(coord, snapshot("hue_a", "hue_b"))
    refresh(coord, {**snapshot("hue_b"), "update.hue_a": entity("hue_a", state="off")})
    status = coord.status()
    assert status["completed_count_this_run"] == 0
    assert status["completed_this_run"] == []
    assert [e["device"] for e in status["cleared_without_update"]] == ["hue_a"]
    assert status["cleared_without_update_count"] == 1
    assert status["pending"] == ["hue_b"]


def test_completed_device_not_requeued_from_stale_snapshot() -> None:
    clock = FakeClock()
    coord = make_coordinator(clock)
    refresh(coord, snapshot("hue_a", "hue_b"))
    coord.decide()
    coord.on_update_response(
        {"status": "ok", "transaction": "t-hue_a", "data": {"id": "hue_a"}}
    )
    # HA snapshot still says "on" for a few seconds after Z2M success.
    refresh(coord, snapshot("hue_a", "hue_b"))
    decision = coord.decide()
    assert decision is not None and decision.friendly_name == "hue_b"
    # After the suppression window a genuinely still-pending entity requeues.
    clock.advance(601)
    coord.on_update_response(
        {"status": "ok", "transaction": "t-hue_b", "data": {"id": "hue_b"}}
    )
    refresh(coord, snapshot("hue_a"))
    retry = coord.decide()
    assert retry is not None and retry.friendly_name == "hue_a"


def test_fresh_devices_run_before_retried_ones() -> None:
    clock = FakeClock()
    coord = make_coordinator(clock)
    refresh(coord, snapshot("hue_a", "hue_b"))
    coord.decide()  # hue_a in flight
    coord.on_update_response(
        {"status": "error", "error": "boom", "transaction": "t-hue_a", "data": {"id": "hue_a"}}
    )
    clock.advance(2000)  # past hue_a's 900s backoff
    decision = coord.decide()
    assert decision is not None and decision.friendly_name == "hue_b"


# ---------------------------------------------------------------------------
# Failures, backoff, offline retry
# ---------------------------------------------------------------------------


def test_generic_error_backs_off_exponentially_with_cap() -> None:
    clock = FakeClock()
    coord = make_coordinator(clock, retry_base_s=100, retry_max_s=350)
    refresh(coord, snapshot("hue_a"))
    expected_backoffs = [100, 200, 350, 350]
    for expected in expected_backoffs:
        decision = coord.decide()
        assert decision is not None and decision.friendly_name == "hue_a"
        coord.on_update_response(
            {
                "status": "error",
                "error": "some failure",
                "transaction": decision.transaction,
                "data": {"id": "hue_a"},
            }
        )
        assert coord.decide() is None  # still cooling down
        clock.advance(expected - 1)
        assert coord.decide() is None
        clock.advance(1)


def test_offline_error_marks_offline_and_online_event_fast_tracks_retry() -> None:
    clock = FakeClock()
    coord = make_coordinator(clock)
    refresh(coord, snapshot("hue_a"))
    decision = coord.decide()
    coord.on_update_response(
        {
            "status": "error",
            "error": "Device didn't respond to OTA request (timeout)",
            "transaction": decision.transaction,
            "data": {"id": "hue_a"},
        }
    )
    assert coord.decide() is None  # 900s cooldown
    refresh(coord, snapshot("hue_a", state="unavailable"))
    # Bulb regains power: the retained MQTT message beats Home Assistant's
    # entity state, and the retry collapses to the online grace window.
    assert coord.set_availability("hue_a", True) is True
    clock.advance(61)
    retry = coord.decide()
    assert retry is not None and retry.friendly_name == "hue_a"


def test_offline_devices_are_skipped_until_online() -> None:
    coord = make_coordinator()
    refresh(coord, snapshot("hue_a", "hue_b"))
    coord.set_availability("hue_a", False)
    decision = coord.decide()
    assert decision is not None and decision.friendly_name == "hue_b"


def test_all_offline_means_no_decision() -> None:
    coord = make_coordinator()
    refresh(coord, snapshot("hue_a"))
    coord.set_availability("hue_a", False)
    assert coord.decide() is None
    assert coord.status()["offline"] == ["hue_a"]


def test_unknown_availability_is_eligible() -> None:
    coord = make_coordinator()
    refresh(coord, snapshot("hue_a"))
    assert coord.decide() is not None


# ---------------------------------------------------------------------------
# Busy handling
# ---------------------------------------------------------------------------


def test_busy_error_requeues_without_burning_attempt() -> None:
    clock = FakeClock()
    coord = make_coordinator(clock)
    refresh(coord, snapshot("hue_a"))
    decision = coord.decide()
    coord.on_update_response(
        {
            "status": "error",
            "error": "Update or check already in progress",
            "transaction": decision.transaction,
        }
    )
    status = coord.status()
    assert status["in_flight"] == {}
    assert status["busy_until"] != ""
    assert coord.decide() is None  # busy window
    clock.advance(301)
    # The device is also held past the window so the fleet doesn't spin on it.
    assert coord.decide() is None
    clock.advance(901)
    retry = coord.decide()
    assert retry is not None and retry.friendly_name == "hue_a"
    # No attempt was recorded for the busy bounce.
    assert coord.status()["failed_attempts_this_run"] == 0


# ---------------------------------------------------------------------------
# Adoption of externally started updates
# ---------------------------------------------------------------------------


def test_adopts_external_in_progress_update() -> None:
    coord = make_coordinator()
    refresh(coord, snapshot("hue_a", "hue_b") | {
        "update.hue_c": entity("hue_c", in_progress=True)
    })
    status = coord.status()
    assert status["in_flight"]["device"] == "hue_c"
    assert status["in_flight"]["adopted"] is True
    assert coord.decide() is None


def test_adopted_update_finishes_via_device_update_obj() -> None:
    coord = make_coordinator()
    refresh(coord, {"update.hue_c": entity("hue_c", in_progress=True)})
    coord.on_device_update_obj("hue_c", {"state": "updating", "progress": 50})
    coord.on_device_update_obj("hue_c", {"state": "idle"})
    status = coord.status()
    assert status["in_flight"] == {}
    assert status["completed_count_this_run"] == 1


# ---------------------------------------------------------------------------
# Progress, stall, absolute timeout
# ---------------------------------------------------------------------------


def test_progress_tracking_and_stall_flag() -> None:
    clock = FakeClock()
    coord = make_coordinator(clock, progress_stall_s=100)
    refresh(coord, snapshot("hue_a"))
    coord.decide()
    coord.on_device_update_obj("hue_a", {"state": "updating", "progress": 5, "remaining": 900})
    fl = coord.status()["in_flight"]
    assert fl["progress_pct"] == 5 and fl["remaining_s"] == 900
    clock.advance(101)
    coord.decide()
    assert coord.status()["in_flight"]["stalled"] is True
    # Progress resumes: stall clears.
    coord.on_device_update_obj("hue_a", {"state": "updating", "progress": 6})
    coord.decide()
    assert coord.status()["in_flight"]["stalled"] is False


def test_absolute_timeout_fails_attempt_and_late_ok_still_completes() -> None:
    clock = FakeClock()
    coord = make_coordinator(clock, update_timeout_s=1000)
    refresh(coord, snapshot("hue_a", "hue_b"))
    decision = coord.decide()
    clock.advance(1001)
    assert coord.decide() is None  # times out hue_a, staggers
    assert coord.status()["failed_attempts_this_run"] == 1
    clock.advance(301)
    next_decision = coord.decide()
    assert next_decision is not None and next_decision.friendly_name == "hue_b"
    # Z2M eventually reports the original update finished fine.
    coord.on_update_response(
        {"status": "ok", "transaction": decision.transaction, "data": {"id": "hue_a"}}
    )
    status = coord.status()
    assert status["completed_count_this_run"] == 1
    assert "hue_a" not in status["pending"]


# ---------------------------------------------------------------------------
# "No image currently available"
# ---------------------------------------------------------------------------


NO_IMAGE = "Update of 'hue_a' failed (No image currently available)"


def test_no_image_is_not_a_failure_and_burns_no_retry() -> None:
    coord = make_coordinator()
    refresh(coord, snapshot("hue_a", "hue_b"))
    decision = coord.decide()
    assert decision is not None and decision.friendly_name == "hue_a"
    coord.on_update_response(
        {
            "status": "error",
            "error": NO_IMAGE,
            "transaction": decision.transaction,
            "data": {"id": "hue_a"},
        }
    )
    status = coord.status()
    assert status["failed_attempts_this_run"] == 0
    assert status["cooldown"] == []
    assert status["skipped_no_image"] == ["hue_a"]
    assert status["in_flight"] == {}
    # The queue moves straight on to the next device.
    nxt = coord.decide()
    assert nxt is not None and nxt.friendly_name == "hue_b"


def test_no_image_device_is_not_requeued_for_the_same_version() -> None:
    clock = FakeClock()
    coord = make_coordinator(clock)
    refresh(coord, snapshot("hue_a"))
    decision = coord.decide()
    coord.on_update_response(
        {
            "status": "error",
            "error": NO_IMAGE,
            "transaction": decision.transaction,
            "data": {"id": "hue_a"},
        }
    )
    # The entity still reads "on" until Z2M resets it: don't start it again,
    # however long the exponential backoff would otherwise have waited.
    clock.advance(21600)
    refresh(coord, snapshot("hue_a"))
    assert coord.decide() is None
    assert coord.status()["pending"] == []


def test_no_image_device_retries_when_a_new_version_is_offered() -> None:
    coord = make_coordinator()
    refresh(coord, snapshot("hue_a"))
    decision = coord.decide()
    coord.on_update_response(
        {
            "status": "error",
            "error": NO_IMAGE,
            "transaction": decision.transaction,
            "data": {"id": "hue_a"},
        }
    )
    refresh(coord, {"update.hue_a": entity("hue_a", latest="300")})
    retry = coord.decide()
    assert retry is not None and retry.friendly_name == "hue_a"


def test_no_image_clearing_is_not_reported_as_completed() -> None:
    coord = make_coordinator()
    refresh(coord, snapshot("hue_a"))
    decision = coord.decide()
    coord.on_update_response(
        {
            "status": "error",
            "error": NO_IMAGE,
            "transaction": decision.transaction,
            "data": {"id": "hue_a"},
        }
    )
    assert coord.status()["skipped_no_image"] == ["hue_a"]
    # Z2M resets the device's update state; HA's entity follows.
    refresh(coord, {"update.hue_a": entity("hue_a", state="off")})
    status = coord.status()
    assert status["completed_count_this_run"] == 0
    assert status["completed_this_run"] == []
    # Nothing is on offer any more, so nothing is being skipped either.
    assert status["skipped_no_image"] == []
    assert status["cleared_without_update"] == []
    assert status["remaining"] == 0


def test_no_image_for_an_untracked_device_is_recorded() -> None:
    """A manual install from the Z2M frontend hitting the same dead end."""
    coord = make_coordinator()
    refresh(coord, snapshot("hue_a"))
    coord.on_update_response(
        {
            "status": "error",
            "error": "Update of 'hue_z' failed (No image currently available)",
            "data": {"id": "hue_z"},
        }
    )
    assert coord.status()["skipped_no_image"] == ["hue_z"]
    assert coord.status()["failed_attempts_this_run"] == 0


def test_empty_ha_answer_does_not_wipe_an_established_queue() -> None:
    """An mqtt config entry still starting up renders [] — not an empty fleet."""
    clock = FakeClock()
    coord = make_coordinator(clock)
    refresh(coord, snapshot("hue_a"))
    decision = coord.decide()
    coord.on_update_response(
        {
            "status": "error",
            "error": "some failure",
            "transaction": decision.transaction,
            "data": {"id": "hue_a"},
        }
    )
    assert coord.status()["cooldown"][0]["attempts"] == 1

    coord.set_z2m_entities(set())
    status = coord.status()
    assert status["identity_source"].startswith("stale")
    assert coord.decide() is None
    # The queue and its backoff survived the bad answer.
    coord.refresh_entities(snapshot("hue_a"))
    assert coord.status()["cooldown"][0]["attempts"] == 1


def test_an_empty_device_list_never_reads_as_healthy() -> None:
    """An empty list manages nothing, so it must not look like a working one —
    otherwise a template that stopped matching sits there silently forever."""
    coord = make_coordinator()
    assert coord.set_z2m_entities(set()) is True  # nothing known yet
    coord.refresh_entities(snapshot("hue_a"))
    status = coord.status()
    assert status["identity_source"] == "none"
    assert status["z2m_devices_known"] == 0
    assert status["pending"] == []
    assert coord.decide() is None
    # And a later empty answer still can't quietly empty an established queue.
    refresh(coord, snapshot("hue_a"))
    assert coord.set_z2m_entities(set()) is False
    assert coord.status()["remaining"] == 1


def test_no_image_park_expires_and_retries_the_same_version() -> None:
    """Upstream often republishes a pulled release under the same version."""
    clock = FakeClock()
    coord = make_coordinator(clock, park_recheck_s=3600)
    refresh(coord, snapshot("hue_a"))
    decision = coord.decide()
    coord.on_update_response(
        {
            "status": "error",
            "error": NO_IMAGE,
            "transaction": decision.transaction,
            "data": {"id": "hue_a"},
        }
    )
    clock.advance(3599)
    refresh(coord, snapshot("hue_a"))
    assert coord.decide() is None
    clock.advance(2)
    refresh(coord, snapshot("hue_a"))
    retry = coord.decide()
    assert retry is not None and retry.friendly_name == "hue_a"


def test_no_image_park_is_dropped_when_the_device_leaves_the_fleet() -> None:
    coord = make_coordinator()
    refresh(coord, snapshot("hue_a", "hue_b"))
    decision = coord.decide()
    coord.on_update_response(
        {
            "status": "error",
            "error": NO_IMAGE,
            "transaction": decision.transaction,
            "data": {"id": "hue_a"},
        }
    )
    assert coord.status()["skipped_no_image"] == ["hue_a"]
    refresh(coord, snapshot("hue_b"))  # hue_a removed from Z2M
    assert coord.status()["skipped_no_image"] == []
    refresh(coord, snapshot("hue_a", "hue_b"))
    assert "hue_a" in coord.status()["pending"]


def test_status_lists_are_capped_with_counts() -> None:
    coord = make_coordinator(include_globs=["update.*"])
    names = [f"dev_{i:03d}" for i in range(40)]
    refresh(coord, snapshot(*names))
    status = coord.status()
    assert len(status["pending"]) == 25
    assert status["pending_count"] == 40
    assert status["remaining"] == 40


def test_schedules_are_absolute_so_ticking_does_not_change_attributes() -> None:
    """Every attribute change is a Home Assistant recorder row, so a countdown
    would write one on every tick just by counting down."""
    clock = FakeClock()
    coord = make_coordinator(clock)
    refresh(coord, snapshot("hue_a"))
    decision = coord.decide()
    coord.on_update_response(
        {
            "status": "error",
            "error": "some failure",
            "transaction": decision.transaction,
            "data": {"id": "hue_a"},
        }
    )
    before = coord.status()
    clock.advance(240)  # two ticks later
    refresh(coord, snapshot("hue_a"))
    assert coord.status() == before


# ---------------------------------------------------------------------------
# unavailable / unknown are not "the update went away"
# ---------------------------------------------------------------------------


def test_unavailable_entity_keeps_its_queue_entry_and_backoff() -> None:
    """Z2M marks the update entity unavailable when a device loses power."""
    clock = FakeClock()
    coord = make_coordinator(clock)
    refresh(coord, snapshot("hue_a"))
    decision = coord.decide()
    coord.on_update_response(
        {
            "status": "error",
            "error": "Device didn't respond to OTA request (timeout)",
            "transaction": decision.transaction,
            "data": {"id": "hue_a"},
        }
    )
    assert coord.status()["cooldown"][0]["attempts"] == 1

    # No MQTT availability message needed: the entity state is the feed.
    refresh(coord, snapshot("hue_a", state="unavailable"))
    status = coord.status()
    assert status["cooldown"][0]["attempts"] == 1  # backoff survived
    assert status["offline"] == ["hue_a"]  # visible as offline
    assert status["completed_this_run"] == []
    assert status["cleared_without_update"] == []
    assert coord.decide() is None


def test_unavailable_entity_does_not_release_a_no_image_park() -> None:
    coord = make_coordinator()
    refresh(coord, snapshot("hue_a"))
    decision = coord.decide()
    coord.on_update_response(
        {
            "status": "error",
            "error": NO_IMAGE,
            "transaction": decision.transaction,
            "data": {"id": "hue_a"},
        }
    )
    refresh(coord, snapshot("hue_a", state="unavailable"))
    assert coord.status()["skipped_no_image"] == ["hue_a"]
    refresh(coord, snapshot("hue_a"))
    assert coord.decide() is None  # still parked


def test_whole_fleet_unavailable_keeps_the_queue() -> None:
    """A Home Assistant restart flips every entity to unavailable at once."""
    coord = make_coordinator()
    refresh(coord, snapshot("hue_a", "hue_b"))
    assert coord.status()["remaining"] == 2
    refresh(coord, snapshot("hue_a", "hue_b", state="unavailable"))
    status = coord.status()
    assert status["remaining"] == 2
    assert status["cleared_without_update"] == []


def test_unavailable_entity_is_never_selected_without_an_mqtt_message() -> None:
    """The retained availability topics reach the MQTT plugin before this app
    has a listener and are never replayed, so a device that goes offline
    unseen must still be gated — Home Assistant's entity state is the feed."""
    coord = make_coordinator()
    refresh(coord, snapshot("hue_a", "hue_b"))
    snap = snapshot("hue_b")
    snap["update.hue_a"] = entity("hue_a", state="unavailable")
    refresh(coord, snap)
    decision = coord.decide()
    assert decision is not None and decision.friendly_name == "hue_b"
    assert coord.status()["offline"] == ["hue_a"]


def test_entity_becoming_available_again_fast_tracks_an_offline_retry() -> None:
    clock = FakeClock()
    coord = make_coordinator(clock)
    refresh(coord, snapshot("hue_a"))
    decision = coord.decide()
    coord.on_update_response(
        {
            "status": "error",
            "error": "Device didn't respond to OTA request (timeout)",
            "transaction": decision.transaction,
            "data": {"id": "hue_a"},
        }
    )
    refresh(coord, snapshot("hue_a", state="unavailable"))
    assert coord.decide() is None
    refresh(coord, snapshot("hue_a"))  # powered back on
    clock.advance(61)
    retry = coord.decide()
    assert retry is not None and retry.friendly_name == "hue_a"


def test_absolute_times_carry_a_utc_offset() -> None:
    """A naive string reads as UTC in a Home Assistant template."""
    clock = FakeClock()
    coord = make_coordinator(clock)
    refresh(coord, snapshot("hue_a"))
    coord.decide()
    started = coord.status()["in_flight"]["started_at"]
    assert started[-6] in "+-" or started.endswith("Z")


def test_pending_lists_only_what_the_picker_would_start() -> None:
    """An offline device counted as pending claims work that never starts."""
    coord = make_coordinator()
    refresh(coord, snapshot("hue_a", "hue_b"))
    snap = snapshot("hue_b")
    snap["update.hue_a"] = entity("hue_a", state="unavailable")
    refresh(coord, snap)
    status = coord.status()
    assert status["pending"] == ["hue_b"]
    assert status["pending_count"] == 1
    assert status["offline"] == ["hue_a"]
    assert status["remaining"] == 2  # still needs firmware, just not now


def test_pending_is_empty_while_zigbee2mqtt_is_busy() -> None:
    clock = FakeClock()
    coord = make_coordinator(clock)
    refresh(coord, snapshot("hue_a"))
    decision = coord.decide()
    coord.on_update_response(
        {
            "status": "error",
            "error": "Update or check already in progress",
            "transaction": decision.transaction,
        }
    )
    status = coord.status()
    assert status["pending"] == []  # decide() would refuse to start it
    assert status["busy_until"] != ""  # and the sensor says why
    clock.advance(301)
    assert coord.status()["pending"] == []  # still held on its own schedule
    clock.advance(901)
    assert coord.status()["pending"] == ["hue_a"]


def test_bridge_devices_are_counted_when_home_assistant_answers_empty() -> None:
    coord = make_coordinator()
    coord.set_known_devices({"hue_a", "hue_b"})
    coord.set_z2m_entities(set())
    coord.refresh_entities(snapshot("hue_a"))
    status = coord.status()
    assert status["identity_source"] == "zigbee2mqtt bridge"
    assert status["z2m_devices_known"] == 2
    assert status["pending"] == ["hue_a"]


def test_unknown_device_is_parked_not_retried_forever() -> None:
    """A Home Assistant rename makes the id we send name no Z2M device."""
    clock = FakeClock()
    coord = make_coordinator(clock)
    refresh(coord, snapshot("hue_a", "hue_b"))
    decision = coord.decide()
    assert decision is not None and decision.friendly_name == "hue_a"
    coord.on_update_response(
        {
            "status": "error",
            "error": "Device 'hue_a' does not exist",
            "transaction": decision.transaction,
            "data": {"id": "hue_a"},
        }
    )
    status = coord.status()
    assert status["unknown_to_z2m"] == ["hue_a"]
    assert status["unknown_to_z2m_count"] == 1
    assert status["skipped_no_image"] == []
    assert status["failed_attempts_this_run"] == 0
    assert status["cooldown"] == []
    nxt = coord.decide()
    assert nxt is not None and nxt.friendly_name == "hue_b"
    # Still parked a day of backoff cycles later.
    clock.advance(21600)
    refresh(coord, snapshot("hue_a", "hue_b"))
    assert "hue_a" not in coord.status()["pending"]


def test_renaming_the_device_back_recovers_immediately() -> None:
    coord = make_coordinator()
    refresh(coord, snapshot("hue_a"))
    decision = coord.decide()
    coord.on_update_response(
        {
            "status": "error",
            "error": "Device 'hue_a' does not exist",
            "transaction": decision.transaction,
            "data": {"id": "hue_a"},
        }
    )
    refresh(coord, snapshot("hue_real_name"))
    retry = coord.decide()
    assert retry is not None and retry.friendly_name == "hue_real_name"


def test_cleared_without_update_keeps_the_most_recent_entries() -> None:
    clock = FakeClock()
    coord = make_coordinator(clock)
    for _ in range(2):
        refresh(coord, snapshot("hue_a"))
        clock.advance(10)
        refresh(coord, {"update.hue_a": entity("hue_a", state="off")})
        clock.advance(10)
    status = coord.status()
    assert status["cleared_without_update_count"] == 2  # not deduplicated
    assert status["cleared_without_update"][-1]["at"] > (
        status["cleared_without_update"][0]["at"]
    )


def test_a_failed_lookup_reports_its_reason_on_the_first_tick() -> None:
    coord = make_coordinator()
    coord.mark_identity_unavailable("HA unreachable")
    coord.refresh_entities(snapshot("hue_a"))
    assert "HA unreachable" in coord.status()["last_event"]


def test_an_update_that_never_starts_gives_the_slot_back() -> None:
    """A sleeping battery device must not hold the fleet's only slot for the
    full absolute timeout — Z2M counts it online for 25h."""
    clock = FakeClock()
    coord = make_coordinator(clock, progress_stall_s=100, update_timeout_s=14400)
    refresh(coord, snapshot("hue_a", "hue_b"))
    decision = coord.decide()
    assert decision is not None and decision.friendly_name == "hue_a"
    clock.advance(101)
    # Staggered by the busy window rather than started in the same tick.
    assert coord.decide() is None
    assert coord.status()["busy_until"] != ""
    clock.advance(301)
    nxt = coord.decide()
    assert nxt is not None and nxt.friendly_name == "hue_b"
    status = coord.status()
    assert status["cooldown"][0]["device"] == "hue_a"
    assert status["cooldown"][0]["offline_failure"] is True
    # Offline-classified, so checking in again fast-tracks the retry instead
    # of serving out the full backoff (see the fast-track test above).
    assert status["in_flight"]["device"] == "hue_b"


def test_a_transfer_in_progress_keeps_the_slot_until_the_absolute_timeout() -> None:
    clock = FakeClock()
    coord = make_coordinator(clock, progress_stall_s=100, update_timeout_s=1000)
    refresh(coord, snapshot("hue_a", "hue_b"))
    coord.decide()
    coord.on_device_update_obj("hue_a", {"state": "updating", "progress": 60})
    clock.advance(101)
    assert coord.decide() is None  # stalled, but patient
    assert coord.status()["in_flight"]["stalled"] is True
    clock.advance(900)
    assert coord.decide() is None  # absolute timeout fires, staggered
    clock.advance(301)
    assert coord.decide() is not None


def test_an_adopted_update_is_never_abandoned_early() -> None:
    """Z2M's in-progress guard is per device: dropping an adopted update and
    starting another would put two transfers on the mesh at once."""
    clock = FakeClock()
    coord = make_coordinator(clock, progress_stall_s=100, update_timeout_s=1000)
    snap = snapshot("hue_b")
    snap["update.hue_c"] = entity("hue_c", in_progress=True)
    refresh(coord, snap)
    assert coord.status()["in_flight"]["adopted"] is True
    clock.advance(101)  # no progress published yet
    assert coord.decide() is None  # hue_b must NOT start
    assert coord.status()["in_flight"]["device"] == "hue_c"
    # The absolute timeout is still the backstop (plus the busy stagger).
    clock.advance(900)
    assert coord.decide() is None
    clock.advance(301)
    nxt = coord.decide()
    assert nxt is not None and nxt.friendly_name == "hue_b"


def test_a_late_answer_for_an_abandoned_attempt_is_recorded() -> None:
    """Z2M can answer long after we gave up; the reason shouldn't vanish."""
    clock = FakeClock()
    coord = make_coordinator(clock, progress_stall_s=100)
    refresh(coord, snapshot("hue_a", "hue_b"))
    decision = coord.decide()
    clock.advance(101)
    coord.decide()  # abandons hue_a
    attempts_before = coord.status()["cooldown"][0]["attempts"]
    coord.on_update_response(
        {
            "status": "error",
            "error": "Device didn't respond to OTA request",
            "transaction": decision.transaction,
            "data": {"id": "hue_a"},
        }
    )
    status = coord.status()
    assert "hue_a" in status["last_event"]
    assert status["cooldown"][0]["last_error"] == "Device didn't respond to OTA request"
    assert status["cooldown"][0]["attempts"] == attempts_before  # not double-counted
    assert status["failed_attempts_this_run"] == 1


def test_a_late_answer_never_kills_the_next_attempt() -> None:
    """Z2M's answer for an abandoned transaction must not land on the retry."""
    clock = FakeClock()
    coord = make_coordinator(
        clock,
        progress_stall_s=100,
        retry_base_s=100,
        make_transaction=lambda name: f"t-{name}-{int(clock.ts)}",
    )
    refresh(coord, snapshot("hue_a"))
    first = coord.decide()
    clock.advance(101)
    coord.decide()  # abandons hue_a, sets the busy window
    clock.advance(301)
    second = coord.decide()
    assert second is not None and second.transaction != first.transaction

    coord.on_update_response(
        {
            "status": "error",
            "error": "Device didn't respond to OTA request",
            "transaction": first.transaction,
            "data": {"id": "hue_a"},
        }
    )
    status = coord.status()
    assert status["in_flight"]["device"] == "hue_a"  # the live attempt survives
    assert status["failed_attempts_this_run"] == 1  # not double-counted


def test_the_absolute_timeout_also_staggers_the_next_device() -> None:
    clock = FakeClock()
    coord = make_coordinator(clock, update_timeout_s=1000, progress_stall_s=2000)
    refresh(coord, snapshot("hue_a", "hue_b"))
    coord.decide()
    coord.on_device_update_obj("hue_a", {"state": "updating", "progress": 40})
    clock.advance(1001)
    assert coord.decide() is None  # timed out, but staggered
    assert coord.status()["busy_until"] != ""
    clock.advance(301)
    nxt = coord.decide()
    assert nxt is not None and nxt.friendly_name == "hue_b"


def test_a_late_success_never_ends_the_live_attempt() -> None:
    """Z2M's ok for an abandoned transaction must not free the slot while the
    same device is in flight again."""
    clock = FakeClock()
    coord = make_coordinator(
        clock,
        progress_stall_s=100,
        retry_base_s=100,
        make_transaction=lambda name: f"t-{name}-{int(clock.ts)}",
    )
    refresh(coord, snapshot("hue_a"))
    first = coord.decide()
    clock.advance(101)
    coord.decide()  # abandons hue_a
    clock.advance(301)
    second = coord.decide()
    assert second is not None and second.friendly_name == "hue_a"

    coord.on_update_response(
        {"status": "ok", "transaction": first.transaction, "data": {"id": "hue_a"}}
    )
    status = coord.status()
    assert status["in_flight"]["device"] == "hue_a"  # live attempt untouched
    assert "settled attempt" in status["last_event"]  # reported, not applied
    assert coord.decide() is None  # and nothing else starts

    # The live attempt still settles normally, with its version recorded.
    coord.on_update_response(
        {"status": "ok", "transaction": second.transaction, "data": {"id": "hue_a"}}
    )
    status = coord.status()
    assert status["in_flight"] == {}
    assert status["completed_count_this_run"] == 1
    assert status["completed_this_run"][0]["version"] == "200"


def test_a_late_no_image_never_ends_the_live_attempt() -> None:
    clock = FakeClock()
    coord = make_coordinator(
        clock,
        progress_stall_s=100,
        retry_base_s=100,
        make_transaction=lambda name: f"t-{name}-{int(clock.ts)}",
    )
    refresh(coord, snapshot("hue_a"))
    first = coord.decide()
    clock.advance(101)
    coord.decide()
    clock.advance(301)
    second = coord.decide()
    assert second is not None and second.friendly_name == "hue_a"

    coord.on_update_response(
        {
            "status": "error",
            "error": NO_IMAGE,
            "transaction": first.transaction,
            "data": {"id": "hue_a"},
        }
    )
    status = coord.status()
    assert status["in_flight"]["device"] == "hue_a"
    assert status["skipped_no_image"] == []  # the live attempt is not parked
    assert "settled attempt" in status["last_event"]
    assert coord.decide() is None

    # The live attempt still settles, and parks the device properly.
    coord.on_update_response(
        {
            "status": "error",
            "error": NO_IMAGE,
            "transaction": second.transaction,
            "data": {"id": "hue_a"},
        }
    )
    status = coord.status()
    assert status["in_flight"] == {}
    assert status["skipped_no_image"] == ["hue_a"]


def test_a_busy_bounce_does_not_make_the_device_the_next_pick() -> None:
    """Z2M's still-open operation is often one we abandoned on a stall; the
    fleet must not spin on that device every time the window lifts."""
    clock = FakeClock()
    coord = make_coordinator(clock, retry_base_s=900, busy_backoff_s=300)
    refresh(coord, snapshot("hue_a", "hue_b"))
    decision = coord.decide()
    assert decision is not None and decision.friendly_name == "hue_a"
    coord.on_update_response(
        {
            "status": "error",
            "error": "Update or check already in progress",
            "transaction": decision.transaction,
        }
    )
    clock.advance(299)
    assert coord.decide() is None  # global window
    clock.advance(2)
    # hue_b gets the turn, even though the bounce burned no attempt on hue_a.
    nxt = coord.decide()
    assert nxt is not None and nxt.friendly_name == "hue_b"
    assert coord.status()["failed_attempts_this_run"] == 0
    # hue_a comes back on its own schedule, not the window's.
    coord.on_update_response(
        {"status": "ok", "transaction": nxt.transaction, "data": {"id": "hue_b"}}
    )
    assert coord.decide() is None
    clock.advance(900)
    later = coord.decide()
    assert later is not None and later.friendly_name == "hue_a"


def test_every_completion_entry_has_the_same_shape() -> None:
    """The success path where the record is already gone must not publish a
    differently shaped entry — a card reading .version would get undefined."""
    coord = make_coordinator()
    refresh(coord, snapshot("hue_a", "hue_b"))
    first = coord.decide()
    assert first is not None and first.friendly_name == "hue_a"
    # hue_a leaves the fleet mid-attempt, so its record is dropped.
    refresh(coord, snapshot("hue_b"))
    coord.on_update_response(
        {"status": "ok", "transaction": first.transaction, "data": {"id": "hue_a"}}
    )
    second = coord.decide()
    assert second is not None and second.friendly_name == "hue_b"
    coord.on_update_response(
        {"status": "ok", "transaction": second.transaction, "data": {"id": "hue_b"}}
    )
    entries = coord.status()["completed_this_run"]
    assert len(entries) == 2  # one from each path
    assert all(set(entry) == {"device", "at", "version"} for entry in entries)


def test_a_busy_bounced_device_is_visible_while_it_waits() -> None:
    """No device should sit in the queue without appearing on the sensor."""
    clock = FakeClock()
    coord = make_coordinator(clock, retry_base_s=900, busy_backoff_s=300)
    refresh(coord, snapshot("hue_a"))
    decision = coord.decide()
    coord.on_update_response(
        {
            "status": "error",
            "error": "Update or check already in progress",
            "transaction": decision.transaction,
        }
    )
    clock.advance(301)  # global window gone, device still held
    status = coord.status()
    assert status["remaining"] == 1
    assert status["pending"] == []
    assert status["busy_until"] == ""
    assert status["cooldown"][0]["device"] == "hue_a"
    assert status["cooldown"][0]["attempts"] == 0  # a bounce, not a failure


def test_the_busy_reschedule_survives_a_long_busy_backoff() -> None:
    """The hold must clear the window structurally, not by a ratio of
    retry_base_s to busy_backoff_s that nothing enforces."""
    clock = FakeClock()
    coord = make_coordinator(clock, retry_base_s=100, busy_backoff_s=900)
    refresh(coord, snapshot("hue_a", "hue_b"))
    decision = coord.decide()
    coord.on_update_response(
        {
            "status": "error",
            "error": "Update or check already in progress",
            "transaction": decision.transaction,
        }
    )
    clock.advance(901)  # the long global window has lifted
    nxt = coord.decide()
    assert nxt is not None and nxt.friendly_name == "hue_b"
