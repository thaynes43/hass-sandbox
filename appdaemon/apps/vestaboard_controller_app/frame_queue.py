"""
Frame Queue for Vestaboard controller.

Manages a LIFO display queue with TTL, expiration, fallback, and override
semantics. Pure Python — no AppDaemon dependencies.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class BoardFrame:
    """A single frame destined for (or displayed on) the Vestaboard.

    Args:
        frame_id: Unique identifier (uuid.uuid4().hex recommended).
        characters: 6 x 22 grid of Vestaboard character codes.
        source: Machine-readable source identifier (e.g. "calendar_clock").
        source_label: Human-readable label for logging / UI.
        ttl_s: How long (seconds) to hold the board after display before
            yielding to the next pending frame.  None = hold indefinitely.
        expiration_s: Maximum lifetime (seconds) measured from created_at.
            After this the frame is invalid and must be discarded.  None = no
            expiry.
        override_ttl: If True, immediately display this frame even if the
            currently displayed frame still has an active TTL.
        created_at: Unix timestamp when the frame was created (time.time()).
        displayed_at: Unix timestamp when the frame was first displayed.
            None until the frame is actually shown on the board.
    """

    frame_id: str
    characters: list[list[int]]
    source: str
    source_label: str
    ttl_s: Optional[int]
    expiration_s: Optional[int]
    override_ttl: bool
    created_at: float
    displayed_at: Optional[float] = field(default=None)


@dataclass
class FrameQueueAction:
    """Result returned by push / tick / clear / remove_source.

    Args:
        display_frame: Frame that should now be sent to the board.  None means
            no change (current display is still valid or queue is empty).
        dropped_frames: Frames that were evicted and will not be shown.
        reason: Short human-readable explanation of why this action occurred.
    """

    display_frame: Optional[BoardFrame]
    dropped_frames: list[BoardFrame]
    reason: str


@dataclass
class FrameQueueState:
    """A point-in-time snapshot of queue state.

    Args:
        displayed: Frame currently shown on the board (None if board is idle).
        displayed_ttl_remaining_s: Seconds until current TTL expires.  None if
            the displayed frame has no TTL or has not been displayed yet.
        pending: Frames waiting to be shown (LIFO — index 0 is next up).
        fallback_stack: Previously displayed frames that can be re-shown when
            pending is empty.
    """

    displayed: Optional[BoardFrame]
    displayed_ttl_remaining_s: Optional[float]
    pending: list[BoardFrame]
    fallback_stack: list[BoardFrame]


# ---------------------------------------------------------------------------
# Helper predicates
# ---------------------------------------------------------------------------


def _is_expired(frame: BoardFrame, now: float) -> bool:
    """Return True if the frame's absolute expiration has passed."""
    if frame.expiration_s is None:
        return False
    return now >= frame.created_at + frame.expiration_s


def _ttl_expired(frame: BoardFrame, now: float) -> bool:
    """Return True if the frame's TTL has run out since it was displayed."""
    if frame.ttl_s is None:
        return False
    if frame.displayed_at is None:
        return False
    return now >= frame.displayed_at + frame.ttl_s


# ---------------------------------------------------------------------------
# FrameQueue
# ---------------------------------------------------------------------------


class FrameQueue:
    """LIFO frame queue with TTL, expiration, fallback, and override support.

    Design decisions
    ----------------
    * ``pending`` is a list treated as a stack: the *last* element is the most
      recently pushed frame.  ``pop()`` (from the end) gives LIFO behaviour.
    * ``fallback_stack`` holds previously *displayed* frames, most recent last.
      When pending is empty we fall back to the newest non-expired fallback.
    * Same-source deduplication: only one pending frame per source is kept.
      A newer push from the same source replaces the older pending frame.
    * The currently displayed frame is tracked in ``_displayed`` and is NOT
      also kept in pending or fallback while it is being shown.

    Args:
        log_fn: Callable that accepts a single str message.  Normally pass
            ``self.log`` from an AppDaemon app, or ``print`` in tests.
    """

    def __init__(self, log_fn: Callable[[str], None]) -> None:
        self._log = log_fn
        self._displayed: Optional[BoardFrame] = None
        # pending: index 0 = oldest, index -1 = most recent (LIFO pop from end)
        self._pending: list[BoardFrame] = []
        # fallback: index -1 = most recently displayed
        self._fallback: list[BoardFrame] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def push(self, frame: BoardFrame, now: float) -> FrameQueueAction:
        """Add a frame to the queue (or display it immediately).

        Returns a FrameQueueAction indicating whether the board should be
        updated and which frames were dropped.
        """
        dropped: list[BoardFrame] = []

        # Step 1: Prune expired frames everywhere
        dropped += self._prune_expired(now)

        # Step 2: Decide whether to display now or queue
        should_display_now = (
            frame.override_ttl
            or self._displayed is None
            or _ttl_expired(self._displayed, now)
        )

        if should_display_now:
            # Move old displayed to fallback (if it hasn't expired)
            if self._displayed is not None and not _is_expired(self._displayed, now):
                self._fallback.append(self._displayed)

            frame.displayed_at = now
            self._displayed = frame

            if frame.override_ttl:
                reason = "override_ttl=True — pre-empting any active TTL"
            elif len(self._fallback) == 0 and not self._pending:
                reason = "queue was empty — displaying immediately"
            else:
                reason = "TTL expired or no displayed frame — displaying immediately"

            self._log(
                f"[FrameQueue] push → display now | frame={frame.frame_id} "
                f"source={frame.source!r} | pending={len(self._pending)} "
                f"fallback={len(self._fallback)} | reason={reason}"
            )

            return FrameQueueAction(
                display_frame=frame,
                dropped_frames=dropped,
                reason=reason,
            )

        # Step 3: Queue behind active TTL (LIFO, same-source dedup)
        existing_idx = next(
            (i for i, f in enumerate(self._pending) if f.source == frame.source),
            None,
        )
        if existing_idx is not None:
            old = self._pending.pop(existing_idx)
            dropped.append(old)
            self._log(
                f"[FrameQueue] push → dedup same-source | evicted={old.frame_id} "
                f"source={frame.source!r} new={frame.frame_id}"
            )

        self._pending.append(frame)

        reason = (
            f"active TTL on displayed frame — queued (LIFO) | "
            f"pending={len(self._pending)} fallback={len(self._fallback)}"
        )
        self._log(
            f"[FrameQueue] push → queued | frame={frame.frame_id} "
            f"source={frame.source!r} | {reason}"
        )

        return FrameQueueAction(
            display_frame=None,
            dropped_frames=dropped,
            reason=reason,
        )

    def tick(self, now: float) -> FrameQueueAction:
        """Advance queue state based on elapsed time.

        Should be called periodically (e.g. every second) so that TTL
        transitions are processed even when no new frames are pushed.

        Returns a FrameQueueAction.  display_frame is set only if the board
        should be updated this tick.
        """
        dropped: list[BoardFrame] = []

        # Step 1: Prune expired frames
        dropped += self._prune_expired(now)

        # Step 2: If displayed frame has expired, clear it
        if self._displayed is not None and _is_expired(self._displayed, now):
            self._log(
                f"[FrameQueue] tick → displayed frame expired | "
                f"frame={self._displayed.frame_id} source={self._displayed.source!r}"
            )
            dropped.append(self._displayed)
            self._displayed = None

        # Step 3: If nothing displayed or TTL expired, try to promote
        need_promotion = self._displayed is None or _ttl_expired(self._displayed, now)
        if not need_promotion:
            return FrameQueueAction(display_frame=None, dropped_frames=dropped, reason="TTL still active")

        next_frame = self._next_non_expired(now)
        if next_frame is None:
            if dropped:
                return FrameQueueAction(
                    display_frame=None,
                    dropped_frames=dropped,
                    reason="queue empty after expiration prune",
                )
            return FrameQueueAction(
                display_frame=None,
                dropped_frames=[],
                reason="no-op — queue empty",
            )

        source = "pending" if next_frame in self._pending else "fallback"
        if source == "pending":
            self._pending.remove(next_frame)
        else:
            self._fallback.remove(next_frame)

        # Move old displayed to fallback before promoting
        if self._displayed is not None and not _is_expired(self._displayed, now):
            self._fallback.append(self._displayed)

        next_frame.displayed_at = now
        self._displayed = next_frame

        reason = (
            f"TTL expired or empty — promoted from {source} | "
            f"frame={next_frame.frame_id} source={next_frame.source!r} | "
            f"pending={len(self._pending)} fallback={len(self._fallback)}"
        )
        self._log(f"[FrameQueue] tick → promoted | {reason}")

        return FrameQueueAction(
            display_frame=next_frame,
            dropped_frames=dropped,
            reason=reason,
        )

    def get_state(self, now: float) -> FrameQueueState:
        """Return a snapshot of the current queue state."""
        ttl_remaining: Optional[float] = None
        if (
            self._displayed is not None
            and self._displayed.ttl_s is not None
            and self._displayed.displayed_at is not None
        ):
            remaining = (self._displayed.displayed_at + self._displayed.ttl_s) - now
            ttl_remaining = max(0.0, remaining)

        return FrameQueueState(
            displayed=self._displayed,
            displayed_ttl_remaining_s=ttl_remaining,
            pending=list(reversed(self._pending)),  # most recent first
            fallback_stack=list(reversed(self._fallback)),  # most recent first
        )

    def clear(self) -> FrameQueueAction:
        """Empty the queue entirely, returning all dropped frames."""
        dropped: list[BoardFrame] = []
        if self._displayed is not None:
            dropped.append(self._displayed)
        dropped.extend(self._pending)
        dropped.extend(self._fallback)

        self._displayed = None
        self._pending = []
        self._fallback = []

        self._log(
            f"[FrameQueue] clear — dropped {len(dropped)} frame(s)"
        )
        return FrameQueueAction(
            display_frame=None,
            dropped_frames=dropped,
            reason="clear() called",
        )

    def remove_source(self, source: str) -> FrameQueueAction:
        """Remove all frames from a given source and return what was dropped."""
        dropped: list[BoardFrame] = []

        if self._displayed is not None and self._displayed.source == source:
            dropped.append(self._displayed)
            self._displayed = None

        still_pending = [f for f in self._pending if f.source != source]
        dropped.extend(f for f in self._pending if f.source == source)
        self._pending = still_pending

        still_fallback = [f for f in self._fallback if f.source != source]
        dropped.extend(f for f in self._fallback if f.source == source)
        self._fallback = still_fallback

        self._log(
            f"[FrameQueue] remove_source={source!r} — dropped {len(dropped)} frame(s) | "
            f"pending={len(self._pending)} fallback={len(self._fallback)}"
        )
        return FrameQueueAction(
            display_frame=None,
            dropped_frames=dropped,
            reason=f"remove_source({source!r})",
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _prune_expired(self, now: float) -> list[BoardFrame]:
        """Remove expired frames from pending and fallback; return them."""
        dropped: list[BoardFrame] = []

        new_pending = []
        for f in self._pending:
            if _is_expired(f, now):
                dropped.append(f)
                self._log(
                    f"[FrameQueue] prune → expired pending frame={f.frame_id} "
                    f"source={f.source!r}"
                )
            else:
                new_pending.append(f)
        self._pending = new_pending

        new_fallback = []
        for f in self._fallback:
            if _is_expired(f, now):
                dropped.append(f)
                self._log(
                    f"[FrameQueue] prune → expired fallback frame={f.frame_id} "
                    f"source={f.source!r}"
                )
            else:
                new_fallback.append(f)
        self._fallback = new_fallback

        return dropped

    def _next_non_expired(self, now: float) -> Optional[BoardFrame]:
        """Return the best next frame to display (LIFO from pending, then fallback).

        Pending takes priority.  Within pending we want the most recently pushed
        (last element).  In fallback we want the most recently displayed (last
        element).
        """
        # Try pending from most recent (end of list)
        for f in reversed(self._pending):
            if not _is_expired(f, now):
                return f

        # Fall back to most recently displayed non-expired fallback
        for f in reversed(self._fallback):
            if not _is_expired(f, now):
                return f

        return None
