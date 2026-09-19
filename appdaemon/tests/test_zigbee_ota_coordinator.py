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
    assert status["cleared_without_update"] == ["hue_a"]
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
    # Bulb regains power: retry collapses to the online grace window.
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
    assert status["busy_wait_s"] > 0
    assert coord.decide() is None  # busy window
    clock.advance(301)
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
    next_decision = coord.decide()  # times out hue_a, starts hue_b
    assert coord.status()["failed_attempts_this_run"] == 1
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
    # Z2M resets the device's update state; HA's entity follows.
    refresh(coord, {"update.hue_a": entity("hue_a", state="off")})
    status = coord.status()
    assert status["completed_count_this_run"] == 0
    assert status["completed_this_run"] == []
    assert status["skipped_no_image"] == ["hue_a"]
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


def test_first_empty_ha_answer_is_accepted() -> None:
    """With nothing known yet, an empty fleet is a legitimate answer."""
    coord = make_coordinator()
    coord.set_z2m_entities(set())
    coord.refresh_entities(snapshot("hue_a"))
    status = coord.status()
    assert status["identity_source"] == "home assistant"
    assert status["pending"] == []
    assert coord.decide() is None


def test_no_image_park_expires_and_retries_the_same_version() -> None:
    """Upstream often republishes a pulled release under the same version."""
    clock = FakeClock()
    coord = make_coordinator(clock, no_image_recheck_s=3600)
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
    refresh(coord, snapshot("hue_b"))  # hue_a removed from Z2M
    # The run record stays, the gate does not.
    assert coord.status()["skipped_no_image"] == ["hue_a"]
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


def test_countdowns_are_rounded_to_the_minute() -> None:
    clock = FakeClock()
    coord = make_coordinator(clock, retry_base_s=905)
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
    clock.advance(7)
    assert coord.status()["cooldown"][0]["retry_in_s"] % 60 == 0
