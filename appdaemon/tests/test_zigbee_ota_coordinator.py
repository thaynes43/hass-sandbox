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


def entity(name: str, state: str = "on", in_progress: bool = False) -> dict[str, Any]:
    return {
        "state": state,
        "attributes": {
            "friendly_name": name,
            "installed_version": "100",
            "latest_version": "200",
            "in_progress": in_progress,
        },
    }


def snapshot(*names: str, **kw: Any) -> dict[str, dict[str, Any]]:
    return {f"update.{name}": entity(name, **kw) for name in names}


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
    coord.refresh_entities(snap)
    status = coord.status()
    assert status["pending"] == ["hue_a", "hue_b"]
    assert status["remaining"] == 2


def test_empty_known_devices_means_no_z2m_filter() -> None:
    coord = make_coordinator()
    coord.refresh_entities(snapshot("hue_a"))
    assert coord.status()["pending"] == ["hue_a"]


def test_exclude_globs() -> None:
    coord = make_coordinator(exclude_globs=["update.*_b"])
    coord.refresh_entities(snapshot("hue_a", "hue_b"))
    assert coord.status()["pending"] == ["hue_a"]


def test_vanished_entities_drop_from_queue() -> None:
    coord = make_coordinator()
    coord.refresh_entities(snapshot("hue_a", "hue_b"))
    coord.refresh_entities(snapshot("hue_b"))
    assert coord.status()["pending"] == ["hue_b"]


# ---------------------------------------------------------------------------
# Sequential decisions
# ---------------------------------------------------------------------------


def test_decide_starts_one_update_alphabetically() -> None:
    coord = make_coordinator()
    coord.refresh_entities(snapshot("hue_b", "hue_a"))
    decision = coord.decide()
    assert decision == StartUpdate(friendly_name="hue_a", transaction="t-hue_a")
    assert coord.decide() is None  # one at a time
    assert coord.status()["in_flight"]["device"] == "hue_a"


def test_success_response_advances_to_next_device() -> None:
    coord = make_coordinator()
    coord.refresh_entities(snapshot("hue_a", "hue_b"))
    coord.decide()
    coord.on_update_response(
        {"status": "ok", "transaction": "t-hue_a", "data": {"id": "hue_a"}}
    )
    status = coord.status()
    assert status["completed_count_this_run"] == 1
    assert status["in_flight"] is None
    decision = coord.decide()
    assert decision is not None and decision.friendly_name == "hue_b"


def test_entity_flipping_off_outside_flight_counts_as_done() -> None:
    coord = make_coordinator()
    coord.refresh_entities(snapshot("hue_a", "hue_b"))
    coord.refresh_entities({**snapshot("hue_b"), "update.hue_a": entity("hue_a", state="off")})
    status = coord.status()
    assert status["completed_count_this_run"] == 1
    assert status["pending"] == ["hue_b"]


def test_completed_device_not_requeued_from_stale_snapshot() -> None:
    clock = FakeClock()
    coord = make_coordinator(clock)
    coord.refresh_entities(snapshot("hue_a", "hue_b"))
    coord.decide()
    coord.on_update_response(
        {"status": "ok", "transaction": "t-hue_a", "data": {"id": "hue_a"}}
    )
    # HA snapshot still says "on" for a few seconds after Z2M success.
    coord.refresh_entities(snapshot("hue_a", "hue_b"))
    decision = coord.decide()
    assert decision is not None and decision.friendly_name == "hue_b"
    # After the suppression window a genuinely still-pending entity requeues.
    clock.advance(601)
    coord.on_update_response(
        {"status": "ok", "transaction": "t-hue_b", "data": {"id": "hue_b"}}
    )
    coord.refresh_entities(snapshot("hue_a"))
    retry = coord.decide()
    assert retry is not None and retry.friendly_name == "hue_a"


def test_fresh_devices_run_before_retried_ones() -> None:
    clock = FakeClock()
    coord = make_coordinator(clock)
    coord.refresh_entities(snapshot("hue_a", "hue_b"))
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
    coord.refresh_entities(snapshot("hue_a"))
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
    coord.refresh_entities(snapshot("hue_a"))
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
    coord.refresh_entities(snapshot("hue_a", "hue_b"))
    coord.set_availability("hue_a", False)
    decision = coord.decide()
    assert decision is not None and decision.friendly_name == "hue_b"


def test_all_offline_means_no_decision() -> None:
    coord = make_coordinator()
    coord.refresh_entities(snapshot("hue_a"))
    coord.set_availability("hue_a", False)
    assert coord.decide() is None
    assert coord.status()["offline"] == ["hue_a"]


def test_unknown_availability_is_eligible() -> None:
    coord = make_coordinator()
    coord.refresh_entities(snapshot("hue_a"))
    assert coord.decide() is not None


# ---------------------------------------------------------------------------
# Busy handling
# ---------------------------------------------------------------------------


def test_busy_error_requeues_without_burning_attempt() -> None:
    clock = FakeClock()
    coord = make_coordinator(clock)
    coord.refresh_entities(snapshot("hue_a"))
    decision = coord.decide()
    coord.on_update_response(
        {
            "status": "error",
            "error": "Update or check already in progress",
            "transaction": decision.transaction,
        }
    )
    status = coord.status()
    assert status["in_flight"] is None
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
    coord.refresh_entities(snapshot("hue_a", "hue_b") | {
        "update.hue_c": entity("hue_c", in_progress=True)
    })
    status = coord.status()
    assert status["in_flight"]["device"] == "hue_c"
    assert status["in_flight"]["adopted"] is True
    assert coord.decide() is None


def test_adopted_update_finishes_via_device_update_obj() -> None:
    coord = make_coordinator()
    coord.refresh_entities({"update.hue_c": entity("hue_c", in_progress=True)})
    coord.on_device_update_obj("hue_c", {"state": "updating", "progress": 50})
    coord.on_device_update_obj("hue_c", {"state": "idle"})
    status = coord.status()
    assert status["in_flight"] is None
    assert status["completed_count_this_run"] == 1


# ---------------------------------------------------------------------------
# Progress, stall, absolute timeout
# ---------------------------------------------------------------------------


def test_progress_tracking_and_stall_flag() -> None:
    clock = FakeClock()
    coord = make_coordinator(clock, progress_stall_s=100)
    coord.refresh_entities(snapshot("hue_a"))
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
    coord.refresh_entities(snapshot("hue_a", "hue_b"))
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
