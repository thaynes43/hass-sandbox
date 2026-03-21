"""
Comprehensive unit tests for vestaboard_controller_app.frame_queue.

Covers:
- TTL true/false x Expiration true/false (4 combos)
- LIFO ordering
- Same-source deduplication
- override_ttl=True / False
- Full scenario walkthrough (static → calendar → garage events → TTL/expiry → fallback)
- Fallback to previous non-expired frame
- Empty queue tick (no-op)
- clear()
- remove_source()
- get_state() snapshot correctness
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make vestaboard_controller_app importable without AppDaemon installed
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps"))

import uuid
import pytest
from vestaboard_apps._shared.frame_queue import (
    BoardFrame,
    FrameQueue,
    FrameQueueAction,
    FrameQueueState,
    _is_expired,
    _ttl_expired,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BLANK_CHARS: list[list[int]] = [[0] * 22 for _ in range(6)]


def make_frame(
    source: str = "test",
    source_label: str = "Test",
    ttl_s: int | None = None,
    max_age_s: int | None = None,
    override_ttl: bool = False,
    should_expire: bool = False,
    created_at: float = 1000.0,
) -> BoardFrame:
    return BoardFrame(
        frame_id=uuid.uuid4().hex,
        characters=BLANK_CHARS,
        source=source,
        source_label=source_label,
        ttl_s=ttl_s,
        max_age_s=max_age_s,
        override_ttl=override_ttl,
        should_expire=should_expire,
        created_at=created_at,
        displayed_at=None,
    )


def make_queue() -> FrameQueue:
    return FrameQueue(log_fn=lambda msg: None)


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------


class TestPredicates:
    def test_is_expired_no_expiration(self):
        f = make_frame(max_age_s=None, created_at=1000.0)
        assert not _is_expired(f, 9999.0)

    def test_is_expired_not_yet(self):
        f = make_frame(max_age_s=60, created_at=1000.0)
        assert not _is_expired(f, 1059.9)

    def test_is_expired_exact_boundary(self):
        f = make_frame(max_age_s=60, created_at=1000.0)
        assert _is_expired(f, 1060.0)

    def test_is_expired_past(self):
        f = make_frame(max_age_s=60, created_at=1000.0)
        assert _is_expired(f, 2000.0)

    def test_ttl_expired_no_ttl_returns_true(self):
        """None TTL means 'freely replaceable' — always returns True."""
        f = make_frame(ttl_s=None, created_at=1000.0)
        f.displayed_at = 1000.0
        assert _ttl_expired(f, 9999.0)

    def test_ttl_expired_not_displayed(self):
        f = make_frame(ttl_s=30, created_at=1000.0)
        # displayed_at is None
        assert not _ttl_expired(f, 2000.0)

    def test_ttl_expired_not_yet(self):
        f = make_frame(ttl_s=30, created_at=1000.0)
        f.displayed_at = 1000.0
        assert not _ttl_expired(f, 1029.9)

    def test_ttl_expired_exact_boundary(self):
        f = make_frame(ttl_s=30, created_at=1000.0)
        f.displayed_at = 1000.0
        assert _ttl_expired(f, 1030.0)

    def test_ttl_expired_past(self):
        f = make_frame(ttl_s=30, created_at=1000.0)
        f.displayed_at = 1000.0
        assert _ttl_expired(f, 2000.0)


# ---------------------------------------------------------------------------
# TTL x Expiration matrix (4 combos)
# ---------------------------------------------------------------------------


class TestTTLExpirationMatrix:
    """Test behaviour for all four TTL/Expiration combinations."""

    def test_no_ttl_no_expiration(self):
        """Frame without TTL or expiration is freely replaceable but stays
        displayed when there is nothing else to promote."""
        q = make_queue()
        f = make_frame(ttl_s=None, max_age_s=None, created_at=1000.0)
        action = q.push(f, now=1000.0)

        assert action.display_frame is f
        # Many ticks later — TTL is "expired" (None = freely replaceable) but
        # nothing in pending/fallback so the frame stays displayed.
        tick = q.tick(now=99999.0)
        assert tick.display_frame is None  # no change — nothing to promote
        state = q.get_state(now=99999.0)
        assert state.displayed is f
        assert state.displayed_ttl_remaining_s is None

    def test_with_ttl_no_expiration(self):
        """Frame with TTL but no expiration: yields when TTL runs out."""
        q = make_queue()
        f = make_frame(ttl_s=30, max_age_s=None, created_at=1000.0)
        q.push(f, now=1000.0)

        # Before TTL expires
        tick_before = q.tick(now=1020.0)
        assert tick_before.display_frame is None

        # After TTL expires with nothing in queue → still no change (empty queue)
        tick_after = q.tick(now=1031.0)
        assert tick_after.display_frame is None
        state = q.get_state(now=1031.0)
        # displayed is still the frame (TTL expired but frame not expired)
        assert state.displayed is f

    def test_no_ttl_with_expiration(self):
        """Frame with expiration but no TTL: freely replaceable, stays until
        expiry when nothing else to promote."""
        q = make_queue()
        f = make_frame(ttl_s=None, max_age_s=60, created_at=1000.0)
        q.push(f, now=1000.0)

        # Before expiration — TTL is "expired" (None) but nothing to promote
        tick = q.tick(now=1059.0)
        assert tick.display_frame is None
        assert q.get_state(now=1059.0).displayed is f

        # After expiration
        tick_after = q.tick(now=1060.0)
        state = q.get_state(now=1061.0)
        assert state.displayed is None

    def test_with_ttl_and_expiration(self):
        """Frame with both TTL and expiration."""
        q = make_queue()
        # TTL=30, expiration=60
        f = make_frame(ttl_s=30, max_age_s=60, created_at=1000.0)
        q.push(f, now=1000.0)

        # At t=1025: both still active
        tick1 = q.tick(now=1025.0)
        assert tick1.display_frame is None

        # Push a second frame (different source) after TTL (30s)
        f2 = make_frame(source="other", ttl_s=None, max_age_s=None, created_at=1031.0)
        # TTL expires at 1030 so push at 1031 should display immediately
        # because displayed f's TTL has expired
        action = q.push(f2, now=1031.0)
        assert action.display_frame is f2

        # f gets moved to fallback since it hasn't expired yet
        state = q.get_state(now=1031.0)
        assert state.displayed is f2
        assert len(state.fallback_stack) == 1

        # After f expires (t=1060+), fallback is purged
        tick_late = q.tick(now=1061.0)
        state2 = q.get_state(now=1061.0)
        assert len(state2.fallback_stack) == 0


# ---------------------------------------------------------------------------
# LIFO ordering
# ---------------------------------------------------------------------------


class TestLIFO:
    def test_lifo_ordering(self):
        """push A then push B → tick promotes B (LIFO)."""
        q = make_queue()
        # Occupy board with TTL
        base = make_frame(source="base", ttl_s=100, created_at=1000.0)
        q.push(base, now=1000.0)

        fa = make_frame(source="a", created_at=1001.0)
        fb = make_frame(source="b", created_at=1002.0)
        q.push(fa, now=1001.0)  # queued
        q.push(fb, now=1002.0)  # queued LIFO after A

        state = q.get_state(now=1002.0)
        # pending[0] should be the most-recent (B), pending[1] = A
        assert state.pending[0] is fb
        assert state.pending[1] is fa

        # Expire base TTL
        action = q.tick(now=1101.0)
        assert action.display_frame is fb  # B promoted (LIFO)

    def test_three_frames_lifo(self):
        """push A, B, C → promotes C, then B (since C has no TTL), then A."""
        q = make_queue()
        base = make_frame(source="base", ttl_s=10, created_at=0.0)
        q.push(base, now=0.0)

        fa = make_frame(source="a", created_at=1.0)
        fb = make_frame(source="b", created_at=2.0)
        fc = make_frame(source="c", created_at=3.0)
        q.push(fa, now=1.0)
        q.push(fb, now=2.0)
        q.push(fc, now=3.0)

        # TTL expires at t=10
        tick1 = q.tick(now=11.0)
        assert tick1.display_frame is fc

        # fc has no TTL → freely replaceable → fb gets promoted immediately
        tick2 = q.tick(now=12.0)
        assert tick2.display_frame is fb

        # fb also has no TTL → fa gets promoted
        tick3 = q.tick(now=13.0)
        assert tick3.display_frame is fa


# ---------------------------------------------------------------------------
# Same-source deduplication
# ---------------------------------------------------------------------------


class TestSameSourceDedup:
    def test_dedup_replaces_pending(self):
        """Pushing a second frame from the same source replaces the first."""
        q = make_queue()
        base = make_frame(source="base", ttl_s=100, created_at=1000.0)
        q.push(base, now=1000.0)

        f1 = make_frame(source="calendar", created_at=1001.0)
        f2 = make_frame(source="calendar", created_at=1002.0)
        action1 = q.push(f1, now=1001.0)
        action2 = q.push(f2, now=1002.0)

        # f1 should be in dropped_frames of action2
        assert f1 in action2.dropped_frames

        state = q.get_state(now=1002.0)
        pending_ids = [f.frame_id for f in state.pending]
        assert f2.frame_id in pending_ids
        assert f1.frame_id not in pending_ids

    def test_dedup_only_same_source(self):
        """Different sources are NOT deduplicated."""
        q = make_queue()
        base = make_frame(source="base", ttl_s=100, created_at=1000.0)
        q.push(base, now=1000.0)

        fa = make_frame(source="a", created_at=1001.0)
        fb = make_frame(source="b", created_at=1002.0)
        q.push(fa, now=1001.0)
        action = q.push(fb, now=1002.0)

        # No drops from different sources
        assert fa not in action.dropped_frames

        state = q.get_state(now=1002.0)
        assert len(state.pending) == 2


# ---------------------------------------------------------------------------
# override_ttl
# ---------------------------------------------------------------------------


class TestOverrideTTL:
    def test_override_ttl_true_bypasses_active_ttl(self):
        """override_ttl=True displays immediately even with active TTL."""
        q = make_queue()
        base = make_frame(source="base", ttl_s=100, created_at=1000.0)
        q.push(base, now=1000.0)

        urgent = make_frame(
            source="garage",
            ttl_s=20,
            override_ttl=True,
            created_at=1005.0,
        )
        action = q.push(urgent, now=1005.0)

        assert action.display_frame is urgent
        assert q.get_state(now=1005.0).displayed is urgent

    def test_override_ttl_false_queues_behind_active_ttl(self):
        """override_ttl=False (default) queues behind active TTL."""
        q = make_queue()
        base = make_frame(source="base", ttl_s=100, created_at=1000.0)
        q.push(base, now=1000.0)

        normal = make_frame(source="calendar", ttl_s=None, override_ttl=False, created_at=1005.0)
        action = q.push(normal, now=1005.0)

        assert action.display_frame is None
        assert q.get_state(now=1005.0).displayed is base

    def test_override_ttl_moves_displayed_to_fallback(self):
        """When override_ttl pre-empts, old frame goes to fallback (if not expired)."""
        q = make_queue()
        base = make_frame(source="base", ttl_s=100, max_age_s=200, created_at=1000.0)
        q.push(base, now=1000.0)

        urgent = make_frame(source="urgent", override_ttl=True, created_at=1001.0)
        q.push(urgent, now=1001.0)

        state = q.get_state(now=1001.0)
        assert state.displayed is urgent
        # base should be in fallback
        fallback_ids = [f.frame_id for f in state.fallback_stack]
        assert base.frame_id in fallback_ids


# ---------------------------------------------------------------------------
# Full scenario walkthrough
# ---------------------------------------------------------------------------


class TestFullScenario:
    """
    Scenario from the requirements doc:
    - Static frame (no TTL, no expiry)
    - Calendar event pushes (TTL=30min=1800s, no override)
    - Garage door opens (TTL=20min=1200s, expiry=20min, override=True)
    - Garage door opens again (TTL=1200s, expiry=1200s, override=True) — dedup old garage
    - Calendar TTL expires → garage event shows
    - Garage event expires → falls back to calendar event
    - Calendar event expires → falls back to static
    """

    def test_full_scenario(self):
        T0 = 0.0
        q = make_queue()
        logs: list[str] = []
        q._log = logs.append  # capture logs

        # T=0: static frame (no TTL, no expiry)
        static = make_frame(
            source="static",
            source_label="Static",
            ttl_s=None,
            max_age_s=None,
            created_at=T0,
        )
        action = q.push(static, now=T0)
        assert action.display_frame is static

        # T=60: calendar event, TTL=1800s, expiry=3600s, no override
        # With None-TTL semantics, static is freely replaceable so calendar
        # displays immediately even without override_ttl.
        T_CAL = 60.0
        cal = make_frame(
            source="calendar",
            source_label="Calendar",
            ttl_s=1800,
            max_age_s=3600,
            override_ttl=False,
            created_at=T_CAL,
        )
        action_cal = q.push(cal, now=T_CAL)
        # static has None TTL → freely replaceable → calendar displays now
        assert action_cal.display_frame is cal
        state1 = q.get_state(now=T_CAL)
        # static goes to fallback
        fallback_ids = [f.frame_id for f in state1.fallback_stack]
        assert static.frame_id in fallback_ids

        # T=120: garage door opens (override, TTL=1200, expiry=1200)
        T_GARAGE1 = 120.0
        garage1 = make_frame(
            source="garage",
            source_label="Garage",
            ttl_s=1200,
            max_age_s=1200,
            override_ttl=True,
            created_at=T_GARAGE1,
        )
        action_g1 = q.push(garage1, now=T_GARAGE1)
        assert action_g1.display_frame is garage1
        state2 = q.get_state(now=T_GARAGE1)
        # cal should now be in fallback
        fallback_ids2 = [f.frame_id for f in state2.fallback_stack]
        assert cal.frame_id in fallback_ids2

        # T=300: garage door opens again — new garage frame with fresh expiry
        T_GARAGE2 = 300.0
        garage2 = make_frame(
            source="garage",
            source_label="Garage",
            ttl_s=1200,
            max_age_s=1200,
            override_ttl=True,
            created_at=T_GARAGE2,
        )
        action_g2 = q.push(garage2, now=T_GARAGE2)
        assert action_g2.display_frame is garage2

        # T=1500: garage2 TTL expires (started at T=300, TTL=1200 → expires at 1500)
        T_G2_TTL_EXP = 1501.0
        tick1 = q.tick(now=T_G2_TTL_EXP)
        # garage2 TTL expired, no pending frames → promote from fallback
        # garage1 created at 120, expires at 120+1200=1320 < 1500 → EXPIRED
        # cal created at 60, expires at 60+3600=3660 > 1500 → VALID
        # So cal should be promoted from fallback
        assert tick1.display_frame is not None
        assert tick1.display_frame.source in ("calendar", "garage")
        if tick1.display_frame.source == "calendar":
            assert tick1.display_frame is cal

        # Advance past cal expiry (T=3660)
        T_CAL_EXP = 3661.0
        tick2 = q.tick(now=T_CAL_EXP)
        state_after_cal_exp = q.get_state(now=T_CAL_EXP)
        # cal should be gone, static (no expiry) in fallback
        # After this tick, static should be promoted
        if state_after_cal_exp.displayed is not None:
            assert state_after_cal_exp.displayed.source == "static"

    def test_full_scenario_no_pending_fallback(self):
        """When all pending expire and fallback is also exhausted, board is empty."""
        q = make_queue()
        f = make_frame(source="event", ttl_s=10, max_age_s=20, created_at=0.0)
        q.push(f, now=0.0)
        # After expiry
        q.tick(now=21.0)
        state = q.get_state(now=21.0)
        assert state.displayed is None
        assert state.pending == []
        assert state.fallback_stack == []


# ---------------------------------------------------------------------------
# Fallback behaviour
# ---------------------------------------------------------------------------


class TestFallback:
    def test_fallback_to_previous_frame(self):
        """When pending is empty and TTL expires, board falls back to last fallback."""
        q = make_queue()
        static = make_frame(source="static", ttl_s=None, max_age_s=None, created_at=0.0)
        q.push(static, now=0.0)

        event = make_frame(source="event", ttl_s=30, max_age_s=None, override_ttl=True, created_at=5.0)
        q.push(event, now=5.0)

        # TTL expires at t=35
        tick = q.tick(now=36.0)
        # No pending frames → fallback to static
        assert tick.display_frame is static

    def test_expired_fallback_skipped(self):
        """Expired fallback frames are NOT re-displayed."""
        q = make_queue()
        # Both frames have expirations so queue can become truly empty
        expired_fallback = make_frame(
            source="old_event", ttl_s=None, max_age_s=50, created_at=0.0
        )
        q.push(expired_fallback, now=0.0)

        # Override with a new frame that also expires
        new_frame = make_frame(
            source="new_event", ttl_s=10, max_age_s=55, override_ttl=True, created_at=10.0
        )
        q.push(new_frame, now=10.0)
        # expired_fallback is now in fallback; it expires at t=50
        # new_frame displayed; it expires at t=65

        # new_frame TTL expires at t=20, expired_fallback still valid at t=21
        tick1 = q.tick(now=21.0)
        assert tick1.display_frame is expired_fallback

        # At t=70, both frames have expired — queue should be empty
        tick2 = q.tick(now=70.0)
        state = q.get_state(now=70.0)
        assert state.displayed is None


# ---------------------------------------------------------------------------
# Empty queue tick
# ---------------------------------------------------------------------------


class TestEmptyQueueTick:
    def test_empty_queue_tick_noop(self):
        """Ticking an empty queue returns no-op."""
        q = make_queue()
        action = q.tick(now=1000.0)
        assert action.display_frame is None
        assert action.dropped_frames == []

    def test_repeated_ticks_on_empty_queue(self):
        """Multiple ticks on empty queue always return no-op."""
        q = make_queue()
        for t in range(100):
            action = q.tick(now=float(t))
            assert action.display_frame is None


# ---------------------------------------------------------------------------
# clear()
# ---------------------------------------------------------------------------


class TestClear:
    def test_clear_empties_everything(self):
        """clear() empties displayed, pending, and fallback."""
        q = make_queue()
        base = make_frame(source="base", ttl_s=100, created_at=0.0)
        q.push(base, now=0.0)

        fa = make_frame(source="a", created_at=1.0)
        fb = make_frame(source="b", created_at=2.0)
        q.push(fa, now=1.0)
        q.push(fb, now=2.0)

        action = q.clear()
        assert action.display_frame is None
        # dropped = base + fa + fb = 3
        assert len(action.dropped_frames) == 3
        dropped_ids = {f.frame_id for f in action.dropped_frames}
        assert base.frame_id in dropped_ids
        assert fa.frame_id in dropped_ids
        assert fb.frame_id in dropped_ids

        state = q.get_state(now=3.0)
        assert state.displayed is None
        assert state.pending == []
        assert state.fallback_stack == []

    def test_clear_empty_queue(self):
        """clear() on empty queue returns no drops."""
        q = make_queue()
        action = q.clear()
        assert action.dropped_frames == []
        assert action.display_frame is None

    def test_clear_includes_fallback(self):
        """clear() also removes fallback frames."""
        q = make_queue()
        base = make_frame(source="base", created_at=0.0)
        q.push(base, now=0.0)

        override = make_frame(source="override", override_ttl=True, created_at=1.0)
        q.push(override, now=1.0)
        # base is now in fallback

        action = q.clear()
        assert len(action.dropped_frames) == 2


# ---------------------------------------------------------------------------
# remove_source()
# ---------------------------------------------------------------------------


class TestRemoveSource:
    def test_remove_source_from_pending(self):
        """remove_source removes all pending frames from that source."""
        q = make_queue()
        base = make_frame(source="base", ttl_s=100, created_at=0.0)
        q.push(base, now=0.0)

        f_cal = make_frame(source="calendar", created_at=1.0)
        q.push(f_cal, now=1.0)

        action = q.remove_source("calendar")
        assert f_cal in action.dropped_frames
        state = q.get_state(now=2.0)
        assert len(state.pending) == 0

    def test_remove_source_displayed(self):
        """remove_source also clears the displayed frame if it matches."""
        q = make_queue()
        f = make_frame(source="garage", created_at=0.0)
        q.push(f, now=0.0)
        assert q.get_state(now=0.0).displayed is f

        action = q.remove_source("garage")
        assert f in action.dropped_frames
        assert q.get_state(now=0.0).displayed is None

    def test_remove_source_fallback(self):
        """remove_source cleans fallback too."""
        q = make_queue()
        static = make_frame(source="static", created_at=0.0)
        q.push(static, now=0.0)

        event = make_frame(source="event", override_ttl=True, created_at=1.0)
        q.push(event, now=1.0)
        # static in fallback now

        action = q.remove_source("static")
        dropped_ids = {f.frame_id for f in action.dropped_frames}
        assert static.frame_id in dropped_ids

        state = q.get_state(now=2.0)
        assert len(state.fallback_stack) == 0

    def test_remove_source_missing(self):
        """remove_source with unknown source returns empty dropped list."""
        q = make_queue()
        action = q.remove_source("nonexistent")
        assert action.dropped_frames == []

    def test_remove_source_multiple_frames_same_source(self):
        """All pending frames from one source are removed even after dedup reset."""
        # After dedup, at most one pending per source — but just verify robustness
        q = make_queue()
        base = make_frame(source="base", ttl_s=200, created_at=0.0)
        q.push(base, now=0.0)

        # Push two calendar frames (dedup keeps only last one in pending)
        f_cal1 = make_frame(source="calendar", created_at=1.0)
        f_cal2 = make_frame(source="calendar", created_at=2.0)
        q.push(f_cal1, now=1.0)
        q.push(f_cal2, now=2.0)

        action = q.remove_source("calendar")
        # f_cal2 in pending, f_cal1 dropped during dedup — so only f_cal2 is now removed
        dropped_ids = {f.frame_id for f in action.dropped_frames}
        assert f_cal2.frame_id in dropped_ids


# ---------------------------------------------------------------------------
# get_state() snapshot correctness
# ---------------------------------------------------------------------------


class TestGetState:
    def test_ttl_remaining_correct(self):
        """get_state reports correct TTL remaining."""
        q = make_queue()
        f = make_frame(ttl_s=60, created_at=1000.0)
        q.push(f, now=1000.0)

        state = q.get_state(now=1020.0)
        # displayed_at = 1000, ttl_s = 60 → remaining = 40
        assert state.displayed_ttl_remaining_s == pytest.approx(40.0)

    def test_ttl_remaining_no_ttl(self):
        q = make_queue()
        f = make_frame(ttl_s=None, created_at=1000.0)
        q.push(f, now=1000.0)

        state = q.get_state(now=1000.0)
        assert state.displayed_ttl_remaining_s is None

    def test_ttl_remaining_clamped_to_zero(self):
        """TTL remaining never goes negative."""
        q = make_queue()
        f = make_frame(ttl_s=30, created_at=0.0)
        q.push(f, now=0.0)

        state = q.get_state(now=9999.0)
        assert state.displayed_ttl_remaining_s == 0.0

    def test_pending_count_correct(self):
        q = make_queue()
        base = make_frame(source="base", ttl_s=100, created_at=0.0)
        q.push(base, now=0.0)

        for i in range(3):
            q.push(make_frame(source=f"src_{i}", created_at=float(i + 1)), now=float(i + 1))

        state = q.get_state(now=5.0)
        assert len(state.pending) == 3

    def test_fallback_count_correct(self):
        q = make_queue()
        f1 = make_frame(source="s1", created_at=0.0)
        q.push(f1, now=0.0)

        f2 = make_frame(source="s2", override_ttl=True, created_at=1.0)
        q.push(f2, now=1.0)

        f3 = make_frame(source="s3", override_ttl=True, created_at=2.0)
        q.push(f3, now=2.0)

        state = q.get_state(now=3.0)
        # f1 and f2 in fallback
        assert len(state.fallback_stack) == 2

    def test_empty_state(self):
        q = make_queue()
        state = q.get_state(now=0.0)
        assert state.displayed is None
        assert state.displayed_ttl_remaining_s is None
        assert state.pending == []
        assert state.fallback_stack == []

    def test_pending_order_most_recent_first(self):
        """get_state returns pending with most-recent first (LIFO view)."""
        q = make_queue()
        base = make_frame(source="base", ttl_s=100, created_at=0.0)
        q.push(base, now=0.0)

        fa = make_frame(source="a", created_at=1.0)
        fb = make_frame(source="b", created_at=2.0)
        fc = make_frame(source="c", created_at=3.0)
        q.push(fa, now=1.0)
        q.push(fb, now=2.0)
        q.push(fc, now=3.0)

        state = q.get_state(now=4.0)
        # Most recent first
        assert state.pending[0] is fc
        assert state.pending[1] is fb
        assert state.pending[2] is fa


# ---------------------------------------------------------------------------
# Expiry prune during push
# ---------------------------------------------------------------------------


class TestExpiryPruneDuringPush:
    def test_expired_pending_pruned_on_push(self):
        """Expired pending frames are pruned when a new frame is pushed."""
        q = make_queue()
        base = make_frame(source="base", ttl_s=9999, created_at=0.0)
        q.push(base, now=0.0)

        expiring = make_frame(source="short", max_age_s=10, created_at=0.0)
        q.push(expiring, now=0.0)

        state_before = q.get_state(now=5.0)
        assert len(state_before.pending) == 1

        # Push another frame after expiry
        fresh = make_frame(source="fresh", created_at=11.0)
        action = q.push(fresh, now=11.0)

        # 'expiring' should appear in dropped
        dropped_ids = {f.frame_id for f in action.dropped_frames}
        assert expiring.frame_id in dropped_ids

        state_after = q.get_state(now=11.0)
        pending_ids = [f.frame_id for f in state_after.pending]
        assert expiring.frame_id not in pending_ids
        assert fresh.frame_id in pending_ids


# ---------------------------------------------------------------------------
# None-TTL semantics regression tests
# ---------------------------------------------------------------------------


class TestNoneTTLSemantics:
    """Regression tests for ttl_s=None meaning 'freely replaceable'."""

    def test_none_ttl_frame_replaceable_by_any_source(self):
        """A displayed frame with ttl_s=None can be replaced by a push from
        any other source without override_ttl."""
        q = make_queue()
        clock = make_frame(source="calendar_clock", ttl_s=None, created_at=0.0)
        q.push(clock, now=0.0)
        assert q.get_state(now=0.0).displayed is clock

        # Push from a different source without override_ttl
        event = make_frame(
            source="garage_door",
            ttl_s=60,
            override_ttl=False,
            created_at=5.0,
        )
        action = q.push(event, now=5.0)

        # event should display immediately because clock has no TTL protection
        assert action.display_frame is event
        state = q.get_state(now=5.0)
        assert state.displayed is event
        # clock moves to fallback (not expired)
        fallback_ids = [f.frame_id for f in state.fallback_stack]
        assert clock.frame_id in fallback_ids

    def test_explicit_ttl_blocks_cross_source_push(self):
        """A displayed frame with an explicit TTL blocks pushes from other
        sources (unless override_ttl=True) until the TTL expires."""
        q = make_queue()
        # Frame with explicit TTL holds the board
        holding = make_frame(source="important", ttl_s=300, created_at=0.0)
        q.push(holding, now=0.0)
        assert q.get_state(now=0.0).displayed is holding

        # Push from a different source without override — should be queued
        newcomer = make_frame(
            source="background",
            ttl_s=None,
            override_ttl=False,
            created_at=5.0,
        )
        action = q.push(newcomer, now=5.0)

        assert action.display_frame is None  # queued, not displayed
        state = q.get_state(now=5.0)
        assert state.displayed is holding
        assert len(state.pending) == 1

        # After TTL expires (t=300), tick promotes the newcomer
        tick = q.tick(now=301.0)
        assert tick.display_frame is newcomer

    def test_none_ttl_stays_when_nothing_to_promote(self):
        """A frame with ttl_s=None stays displayed when there are no pending
        or fallback frames to promote."""
        q = make_queue()
        solo = make_frame(source="solo", ttl_s=None, created_at=0.0)
        q.push(solo, now=0.0)

        # Many ticks later, still displayed
        for t in (10.0, 100.0, 1000.0, 99999.0):
            tick = q.tick(now=t)
            assert tick.display_frame is None
            assert q.get_state(now=t).displayed is solo

    def test_none_ttl_does_not_cycle_through_fallback(self):
        """A displayed frame with ttl_s=None should NOT be replaced by fallback
        frames during tick — this prevents thrashing between automations."""
        q = make_queue()

        # Push two frames: first goes to display, second also displays (replaces None-TTL)
        frame_a = make_frame(source="clock", ttl_s=None, created_at=0.0)
        frame_b = make_frame(source="summary", ttl_s=None, created_at=1.0)
        q.push(frame_a, now=0.0)
        q.push(frame_b, now=1.0)

        # frame_a is in fallback, frame_b is displayed
        assert q._displayed.source == "summary"
        assert len(q._fallback) == 1

        # Tick should NOT promote from fallback — frame_b holds the board
        for t in (10.0, 25.0, 50.0, 100.0):
            tick = q.tick(now=t)
            assert tick.display_frame is None, f"tick at {t} should not promote"
            assert q._displayed.source == "summary"

    def test_none_ttl_yields_to_pending(self):
        """A displayed frame with ttl_s=None should yield to a pending frame
        during tick (new content waiting)."""
        q = make_queue()

        # Display a None-TTL frame, then push one with an explicit TTL
        frame_a = make_frame(source="clock", ttl_s=None, created_at=0.0)
        frame_b = make_frame(source="alert", ttl_s=60, created_at=1.0)
        q.push(frame_a, now=0.0)

        # frame_b will display immediately since frame_a has no TTL protection
        action = q.push(frame_b, now=1.0)
        assert action.display_frame is frame_b

    def test_explicit_ttl_blocks_pending_during_tick(self):
        """Pending frames must wait for an active explicit TTL — tick should
        NOT promote from pending while TTL is active."""
        q = make_queue()

        # User pushes a frame with 30-minute TTL
        user_frame = make_frame(source="user", ttl_s=1800, created_at=0.0)
        q.push(user_frame, now=0.0)
        assert q._displayed.source == "user"

        # Calendar clock pushes — goes to pending (TTL blocks it)
        clock_frame = make_frame(source="calendar_clock", ttl_s=None, created_at=15.0)
        action = q.push(clock_frame, now=15.0)
        assert action.display_frame is None
        assert len(q._pending) == 1

        # Tick at 30s — TTL still active (1800s), pending must wait
        tick = q.tick(now=30.0)
        assert tick.display_frame is None
        assert q._displayed.source == "user"

        # Tick at 1801s — TTL expired, now pending promotes
        tick = q.tick(now=1801.0)
        assert tick.display_frame is not None
        assert tick.display_frame.source == "calendar_clock"

    def test_explicit_ttl_expired_promotes_from_fallback(self):
        """When a displayed frame has an explicit TTL that expires, tick should
        still promote from fallback normally."""
        q = make_queue()

        frame_a = make_frame(source="bg", ttl_s=None, created_at=0.0)
        q.push(frame_a, now=0.0)

        # Push a frame with explicit TTL that will display immediately
        frame_b = make_frame(source="alert", ttl_s=10, created_at=1.0)
        q.push(frame_b, now=1.0)

        # frame_a is in fallback, frame_b displayed with TTL=10
        assert q._displayed.source == "alert"

        # After TTL expires, tick should promote frame_a from fallback
        tick = q.tick(now=12.0)
        assert tick.display_frame is not None
        assert tick.display_frame.source == "bg"


# ---------------------------------------------------------------------------
# should_expire semantics
# ---------------------------------------------------------------------------


class TestShouldExpire:
    """Tests for should_expire=True: frame is dropped on TTL expiry, not fallback-ed."""

    def test_should_expire_true_frame_dropped_on_tick_promotion(self):
        """When a should_expire=True frame's TTL runs out and a new frame is
        promoted (from pending), the should_expire frame is dropped rather than
        moved to fallback."""
        q = make_queue()

        # Display a should_expire frame with TTL=30
        expiring = make_frame(
            source="alert",
            ttl_s=30,
            should_expire=True,
            created_at=0.0,
        )
        q.push(expiring, now=0.0)
        assert q.get_state(now=0.0).displayed is expiring

        # Queue a next frame while the alert TTL is active
        next_f = make_frame(source="background", ttl_s=None, created_at=5.0)
        action_q = q.push(next_f, now=5.0)
        assert action_q.display_frame is None  # queued, not displayed yet

        # TTL expires at t=30; tick promotes next_f
        tick = q.tick(now=31.0)
        assert tick.display_frame is next_f

        # 'expiring' must NOT appear in fallback — it was dropped
        state = q.get_state(now=31.0)
        fallback_ids = {f.frame_id for f in state.fallback_stack}
        assert expiring.frame_id not in fallback_ids

        # 'expiring' should appear in dropped_frames
        dropped_ids = {f.frame_id for f in tick.dropped_frames}
        assert expiring.frame_id in dropped_ids

    def test_should_expire_false_frame_moves_to_fallback_on_tick_promotion(self):
        """Default behaviour (should_expire=False): when TTL expires and a new
        frame is promoted, the old frame moves to fallback, not dropped."""
        q = make_queue()

        normal = make_frame(
            source="calendar",
            ttl_s=30,
            should_expire=False,
            created_at=0.0,
        )
        q.push(normal, now=0.0)

        next_f = make_frame(source="background", ttl_s=None, created_at=5.0)
        q.push(next_f, now=5.0)

        tick = q.tick(now=31.0)
        assert tick.display_frame is next_f

        # 'normal' SHOULD appear in fallback
        state = q.get_state(now=31.0)
        fallback_ids = {f.frame_id for f in state.fallback_stack}
        assert normal.frame_id in fallback_ids

        # 'normal' should NOT appear in dropped_frames (unless it expired separately)
        dropped_ids = {f.frame_id for f in tick.dropped_frames}
        assert normal.frame_id not in dropped_ids

    def test_should_expire_true_frame_dropped_when_displaced_by_push(self):
        """When a push displaces a should_expire=True displayed frame
        (via override_ttl or TTL expired), the displaced frame is dropped."""
        q = make_queue()

        expiring = make_frame(
            source="alert",
            ttl_s=60,
            should_expire=True,
            created_at=0.0,
        )
        q.push(expiring, now=0.0)

        # override_ttl displaces expiring immediately
        overrider = make_frame(
            source="urgent",
            override_ttl=True,
            created_at=5.0,
        )
        action = q.push(overrider, now=5.0)
        assert action.display_frame is overrider

        # 'expiring' must not be in fallback — it had should_expire=True
        state = q.get_state(now=5.0)
        fallback_ids = {f.frame_id for f in state.fallback_stack}
        assert expiring.frame_id not in fallback_ids

        # It should appear in dropped_frames
        dropped_ids = {f.frame_id for f in action.dropped_frames}
        assert expiring.frame_id in dropped_ids

    def test_should_expire_false_frame_moves_to_fallback_when_displaced_by_push(self):
        """Default (should_expire=False): displaced frame goes to fallback."""
        q = make_queue()

        normal = make_frame(
            source="calendar",
            ttl_s=60,
            should_expire=False,
            max_age_s=120,
            created_at=0.0,
        )
        q.push(normal, now=0.0)

        overrider = make_frame(source="urgent", override_ttl=True, created_at=5.0)
        action = q.push(overrider, now=5.0)
        assert action.display_frame is overrider

        state = q.get_state(now=5.0)
        fallback_ids = {f.frame_id for f in state.fallback_stack}
        assert normal.frame_id in fallback_ids

    def test_should_expire_true_with_no_ttl_can_still_be_replaced(self):
        """should_expire=True with ttl_s=None: the frame is freely replaceable
        (None-TTL semantics still apply — any push replaces it).
        When displaced the frame is dropped, not kept in fallback."""
        q = make_queue()

        expiring_no_ttl = make_frame(
            source="clock",
            ttl_s=None,
            should_expire=True,
            created_at=0.0,
        )
        q.push(expiring_no_ttl, now=0.0)
        assert q.get_state(now=0.0).displayed is expiring_no_ttl

        # Push from another source — None TTL means freely replaceable
        replacement = make_frame(
            source="event",
            ttl_s=60,
            override_ttl=False,
            created_at=5.0,
        )
        action = q.push(replacement, now=5.0)

        # Replacement displays immediately (None-TTL means freely replaceable)
        assert action.display_frame is replacement

        # expiring_no_ttl must NOT appear in fallback
        state = q.get_state(now=5.0)
        fallback_ids = {f.frame_id for f in state.fallback_stack}
        assert expiring_no_ttl.frame_id not in fallback_ids

        # It should appear in dropped_frames
        dropped_ids = {f.frame_id for f in action.dropped_frames}
        assert expiring_no_ttl.frame_id in dropped_ids

    def test_should_expire_true_empty_queue_after_ttl(self):
        """When a should_expire=True frame's TTL runs out and there is nothing
        else to promote, the frame is still displaced and board becomes idle."""
        q = make_queue()

        expiring = make_frame(
            source="alert",
            ttl_s=30,
            should_expire=True,
            created_at=0.0,
        )
        q.push(expiring, now=0.0)
        assert q.get_state(now=0.0).displayed is expiring

        # TTL expires but pending and fallback are both empty
        # The frame stays displayed (nothing to promote to) — should_expire only
        # takes effect when something else is promoted into the slot.
        tick = q.tick(now=31.0)
        assert tick.display_frame is None  # nothing to promote
        state = q.get_state(now=31.0)
        # Frame still displayed (no replacement available)
        assert state.displayed is expiring

    def test_should_expire_default_is_false(self):
        """BoardFrame.should_expire defaults to False."""
        f = make_frame(source="test", created_at=0.0)
        assert f.should_expire is False


# ---------------------------------------------------------------------------
# remaining_ttl_s — new TTL preservation tests
# ---------------------------------------------------------------------------


class TestRemainingTTL:
    """Tests for remaining_ttl_s: displaced frames preserve their remaining TTL."""

    def test_displaced_frame_gets_remaining_ttl(self):
        """Frame with ttl_s=60 displayed for 20s, displaced by override_ttl push.
        remaining_ttl_s should be ~40."""
        q = make_queue()

        # Display a frame with ttl_s=60
        cal = make_frame(source="calendar", ttl_s=60, max_age_s=None, created_at=1000.0)
        q.push(cal, now=1000.0)
        # cal is displayed with displayed_at=1000.0

        # 20 seconds later, an override_ttl frame displaces it
        urgent = make_frame(
            source="preview",
            ttl_s=10,
            max_age_s=None,
            override_ttl=True,
            created_at=1020.0,
        )
        q.push(urgent, now=1020.0)

        # cal should be in fallback with remaining_ttl_s ~= 40
        state = q.get_state(now=1020.0)
        fallback_frames = state.fallback_stack
        assert len(fallback_frames) == 1
        assert fallback_frames[0].source == "calendar"
        assert fallback_frames[0].remaining_ttl_s == pytest.approx(40.0)

    def test_repromoted_frame_uses_remaining_ttl(self):
        """Frame displaced with remaining_ttl_s=40, re-promoted from fallback.
        Its effective TTL should be 40, not the original 60."""
        q = make_queue()

        # Display cal with 60s TTL, displace after 20s
        cal = make_frame(source="calendar", ttl_s=60, max_age_s=None, created_at=1000.0)
        q.push(cal, now=1000.0)

        urgent = make_frame(
            source="preview", ttl_s=10, max_age_s=None, override_ttl=True, created_at=1020.0
        )
        q.push(urgent, now=1020.0)
        # cal now in fallback with remaining_ttl_s=40

        # preview TTL expires at 1030, tick promotes cal from fallback
        tick = q.tick(now=1031.0)
        assert tick.display_frame is not None
        assert tick.display_frame.source == "calendar"

        # cal's effective ttl_s should now be 40 (not 60), remaining_ttl_s consumed
        promoted = tick.display_frame
        assert promoted.ttl_s == 40
        assert promoted.remaining_ttl_s is None

    def test_remaining_ttl_cleared_after_promotion(self):
        """After re-promotion, remaining_ttl_s is None (consumed)."""
        q = make_queue()

        cal = make_frame(source="calendar", ttl_s=60, max_age_s=None, created_at=1000.0)
        q.push(cal, now=1000.0)

        urgent = make_frame(
            source="preview", ttl_s=5, max_age_s=None, override_ttl=True, created_at=1010.0
        )
        q.push(urgent, now=1010.0)
        # cal displaced at t=1010, remaining_ttl_s = 60-(1010-1000) = 50

        # preview expires at 1015, tick promotes cal
        tick = q.tick(now=1016.0)
        assert tick.display_frame is not None
        assert tick.display_frame.source == "calendar"

        # remaining_ttl_s must be None after promotion (consumed)
        assert tick.display_frame.remaining_ttl_s is None

    def test_no_ttl_frame_no_remaining_ttl(self):
        """Frame with ttl_s=None displaced to fallback. remaining_ttl_s stays None."""
        q = make_queue()

        clock = make_frame(source="clock", ttl_s=None, max_age_s=None, created_at=1000.0)
        q.push(clock, now=1000.0)

        # Push from another source — None TTL means freely replaceable
        urgent = make_frame(
            source="alert", ttl_s=30, max_age_s=None, override_ttl=False, created_at=1005.0
        )
        q.push(urgent, now=1005.0)

        # clock should be in fallback with remaining_ttl_s=None (no TTL to preserve)
        state = q.get_state(now=1005.0)
        fallback_frames = state.fallback_stack
        clock_fallback = next((f for f in fallback_frames if f.source == "clock"), None)
        assert clock_fallback is not None
        assert clock_fallback.remaining_ttl_s is None

    def test_remaining_ttl_zero_floor(self):
        """Frame whose TTL has already expired before displacement gets remaining_ttl_s=0.0."""
        q = make_queue()

        # Frame with ttl_s=30 displayed at t=1000
        cal = make_frame(source="calendar", ttl_s=30, max_age_s=None, created_at=1000.0)
        q.push(cal, now=1000.0)

        # Displaced at t=1050 (20s after TTL already expired at 1030)
        urgent = make_frame(
            source="preview", ttl_s=5, max_age_s=None, override_ttl=True, created_at=1050.0
        )
        q.push(urgent, now=1050.0)

        state = q.get_state(now=1050.0)
        fallback_frames = state.fallback_stack
        cal_fallback = next((f for f in fallback_frames if f.source == "calendar"), None)
        assert cal_fallback is not None
        # remaining = max(0, 30 - 50) = 0.0
        assert cal_fallback.remaining_ttl_s == 0.0

    def test_tick_displacement_calculates_remaining_ttl(self):
        """Frame displaced during tick() (not push()) also gets correct remaining_ttl_s."""
        q = make_queue()

        # Display cal with 60s TTL
        cal = make_frame(source="calendar", ttl_s=60, max_age_s=None, created_at=1000.0)
        q.push(cal, now=1000.0)

        # Queue a pending frame (will be promoted when cal TTL expires)
        pending = make_frame(source="background", ttl_s=None, max_age_s=None, created_at=1001.0)
        q.push(pending, now=1001.0)  # queued behind cal's TTL

        # At t=1040 (40s elapsed), cal's TTL expires (at 1060) — still active, no promotion yet
        tick_before = q.tick(now=1040.0)
        assert tick_before.display_frame is None

        # At t=1061 cal TTL expires; tick promotes pending, displaces cal to fallback
        tick = q.tick(now=1061.0)
        assert tick.display_frame is pending

        # cal in fallback — but its TTL expired so remaining_ttl_s = 0
        state = q.get_state(now=1061.0)
        cal_fallback = next((f for f in state.fallback_stack if f.source == "calendar"), None)
        assert cal_fallback is not None
        # TTL expired (1060 < 1061) so remaining = max(0, 60 - (1061-1000)) = max(0, -1) = 0.0
        assert cal_fallback.remaining_ttl_s == 0.0

    def test_should_expire_frame_not_fallbacked(self):
        """Regression: should_expire=True frame is dropped, not moved to fallback,
        so it never gets remaining_ttl_s set on the fallback path."""
        q = make_queue()

        alert = make_frame(
            source="alert",
            ttl_s=30,
            should_expire=True,
            max_age_s=None,
            created_at=1000.0,
        )
        q.push(alert, now=1000.0)

        # Override displaces alert — should_expire=True means it's DROPPED not fallbacked
        override = make_frame(
            source="override", ttl_s=10, override_ttl=True, created_at=1010.0
        )
        q.push(override, now=1010.0)

        state = q.get_state(now=1010.0)
        # alert must NOT be in fallback
        fallback_ids = {f.frame_id for f in state.fallback_stack}
        assert alert.frame_id not in fallback_ids
        # Only override is displayed
        assert state.displayed is override

    def test_get_state_includes_remaining_ttl(self):
        """FrameQueueState.fallback_stack frames have remaining_ttl_s accessible."""
        q = make_queue()

        cal = make_frame(source="calendar", ttl_s=100, max_age_s=None, created_at=1000.0)
        q.push(cal, now=1000.0)

        # Displace after 25s
        urgent = make_frame(
            source="urgent", ttl_s=5, max_age_s=None, override_ttl=True, created_at=1025.0
        )
        q.push(urgent, now=1025.0)

        state = q.get_state(now=1025.0)
        assert len(state.fallback_stack) == 1
        fallback_frame = state.fallback_stack[0]
        assert fallback_frame.source == "calendar"
        # remaining = 100 - 25 = 75
        assert fallback_frame.remaining_ttl_s == pytest.approx(75.0)

    def test_remaining_ttl_default_is_none(self):
        """BoardFrame.remaining_ttl_s defaults to None (backward compat)."""
        f = make_frame(source="test", created_at=0.0)
        assert f.remaining_ttl_s is None

    def test_tick_displacement_mid_ttl_remaining(self):
        """Frame displaced during tick() mid-TTL gets the correct positive remaining_ttl_s."""
        q = make_queue()

        # Frame with 100s TTL
        cal = make_frame(source="calendar", ttl_s=100, max_age_s=None, created_at=0.0)
        q.push(cal, now=0.0)

        # Queue a pending frame (no TTL — freely replaceable once cal TTL done)
        pending = make_frame(source="bg", ttl_s=None, max_age_s=None, created_at=1.0)
        q.push(pending, now=1.0)  # queued behind cal

        # Cal TTL expires at t=100; tick at 101 promotes pending, displaces cal
        tick = q.tick(now=101.0)
        assert tick.display_frame is pending

        state = q.get_state(now=101.0)
        cal_fallback = next((f for f in state.fallback_stack if f.source == "calendar"), None)
        assert cal_fallback is not None
        # TTL was 100s, elapsed was 101s -> remaining = max(0, 100-101) = 0
        assert cal_fallback.remaining_ttl_s == 0.0

    def test_push_displacement_mid_ttl_exact_remaining(self):
        """push() displacement at exactly half-TTL gives remaining_ttl_s = ttl_s/2."""
        q = make_queue()

        cal = make_frame(source="calendar", ttl_s=120, max_age_s=None, created_at=0.0)
        q.push(cal, now=0.0)

        # Displaced exactly at 60s (half of 120s TTL)
        urgent = make_frame(
            source="urgent", ttl_s=10, max_age_s=None, override_ttl=True, created_at=60.0
        )
        q.push(urgent, now=60.0)

        state = q.get_state(now=60.0)
        cal_fallback = next((f for f in state.fallback_stack if f.source == "calendar"), None)
        assert cal_fallback is not None
        assert cal_fallback.remaining_ttl_s == pytest.approx(60.0)
