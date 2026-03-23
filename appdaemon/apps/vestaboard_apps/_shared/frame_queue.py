"""
Frame Queue for Vestaboard controller.

Manages a FIFO display queue with TTL, expiration, fallback, and override
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
            yielding to the next pending frame.  None = no TTL protection,
            meaning any frame from another source can freely replace this one.
        max_age_s: Maximum age (seconds) measured from created_at.  After
            this the frame is stale and will be dropped from the queue even
            if it was never displayed.  None = no age limit (stays in queue
            indefinitely until displayed or replaced).
        override_ttl: If True, immediately display this frame even if the
            currently displayed frame still has an active TTL.
        should_expire: If True, when the TTL elapses the frame **auto-leaves
            the board** and the next frame (from fallback or pending) is
            promoted.  If displaced before TTL expires, the frame still goes
            to fallback with remaining TTL preserved.  If False (default),
            the frame **holds the board** after TTL until a new push displaces
            it.
        created_at: Unix timestamp when the frame was created (time.time()).
        displayed_at: Unix timestamp when the frame was first displayed.
            None until the frame is actually shown on the board.
    """

    frame_id: str
    characters: list[list[int]]
    source: str
    source_label: str
    ttl_s: Optional[int]
    max_age_s: Optional[int]
    override_ttl: bool
    created_at: float
    should_expire: bool = field(default=False)
    displayed_at: Optional[float] = field(default=None)
    template: Optional[str] = field(default=None)
    refresh_interval_minutes: Optional[int] = field(default=None)
    remaining_ttl_s: Optional[float] = field(default=None)


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
        pending: Frames waiting to be shown (FIFO — index 0 is next up).
        fallback_stack: Previously displaced frames (FIFO — index 0 is next
            to resume).  Fallback is consulted before pending on promotion.
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
    if frame.max_age_s is None:
        return False
    return now >= frame.created_at + frame.max_age_s


def _ttl_expired(frame: BoardFrame, now: float) -> bool:
    """Return True if the frame's TTL has run out since it was displayed.

    When ``ttl_s`` is None the frame has **no TTL protection**, meaning any
    other source can freely replace it.  This returns ``True`` so that
    incoming frames from different sources are not blocked.  If an automation
    wants to *hold* the board it must set an explicit ``ttl_s`` value.
    """
    if frame.ttl_s is None:
        return True
    if frame.displayed_at is None:
        return False
    return now >= frame.displayed_at + frame.ttl_s


# ---------------------------------------------------------------------------
# FrameQueue
# ---------------------------------------------------------------------------


class FrameQueue:
    """FIFO frame queue with TTL, expiration, fallback, and override support.

    Design decisions
    ----------------
    * ``pending`` is a FIFO list: index 0 is the oldest pushed frame and is
      promoted first.  ``append()`` adds to the end.
    * ``fallback`` holds previously *displaced* frames in FIFO order (first
      displaced = first re-promoted at index 0).  **All** displaced frames go
      to fallback regardless of ``should_expire``.
    * Promotion priority: fallback before pending (displaced frames resume
      their remaining time before new content is shown).
    * ``should_expire=True`` means the frame **auto-leaves the board** when
      its TTL elapses.  ``should_expire=False`` means the frame **holds the
      board** after TTL until a new push displaces it.
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
        # pending: index 0 = oldest (FIFO — promoted first)
        self._pending: list[BoardFrame] = []
        # fallback: index 0 = first displaced (FIFO — re-promoted first)
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
        # Same-source frames always replace the displayed frame (e.g. clock updates)
        same_source = (
            self._displayed is not None
            and self._displayed.source == frame.source
        )
        should_display_now = (
            frame.override_ttl
            or self._displayed is None
            or _ttl_expired(self._displayed, now)
            or same_source
        )

        if should_display_now:
            # Move old displayed to fallback (if it hasn't expired),
            # but discard same-source frames (they're just stale updates).
            # ALL displaced frames go to fallback regardless of should_expire.
            if self._displayed is not None and same_source:
                dropped.append(self._displayed)
            elif (self._displayed is not None
                    and not _is_expired(self._displayed, now)):
                displaced = self._displayed
                if (displaced.ttl_s is not None
                        and displaced.displayed_at is not None):
                    elapsed = now - displaced.displayed_at
                    displaced.remaining_ttl_s = max(0.0, displaced.ttl_s - elapsed)
                    self._log(
                        f"[FrameQueue] push → displaced frame to fallback with "
                        f"remaining_ttl_s={displaced.remaining_ttl_s:.1f} | "
                        f"frame={displaced.frame_id} source={displaced.source!r}"
                    )
                # Same-source dedup in fallback: remove older frame from same source
                old_fb = [f for f in self._fallback if f.source == displaced.source]
                for old in old_fb:
                    self._fallback.remove(old)
                    dropped.append(old)
                    self._log(
                        f"[FrameQueue] push → fallback dedup same-source | "
                        f"evicted={old.frame_id} source={displaced.source!r}"
                    )
                self._fallback.append(displaced)

            # For same-source updates, preserve the original displayed_at so
            # TTL counts from when this source first claimed the board.
            # This prevents automations with periodic updates (rotation,
            # countdown, weather refresh) from holding the board forever.
            if same_source and self._displayed.displayed_at is not None:
                frame.displayed_at = self._displayed.displayed_at
            else:
                frame.displayed_at = now
            self._displayed = frame

            if frame.override_ttl:
                reason = "override_ttl=True — pre-empting any active TTL"
            elif same_source:
                reason = f"same source {frame.source!r} — replacing displayed frame"
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

        # Step 3: Queue behind active TTL (FIFO, same-source dedup)
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
            f"active TTL on displayed frame — queued (FIFO) | "
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
        #
        # Key distinction for tick vs push:
        # - ttl_s=None means "no TTL protection" — a *push* from another source
        #   can freely replace this frame.  But during *tick* (no new frame
        #   arrived), a None-TTL frame should hold the board rather than cycling
        #   through fallback every tick interval.
        # - ttl_s=<int> that expired means the frame actively wants to yield.
        #
        # Promotion rules:
        #   (a) nothing displayed → promote
        #   (b) explicit TTL still active → block (pending frames must wait)
        #   (c) explicit TTL expired → promote from pending or fallback
        #   (d) no TTL + pending frames → promote (new content should show)
        #   (e) no TTL + no pending → hold board (prevent fallback cycling)
        if self._displayed is not None:
            has_explicit_ttl = self._displayed.ttl_s is not None
            explicit_ttl_expired = has_explicit_ttl and _ttl_expired(self._displayed, now)
            has_pending = bool(self._pending)

            # (b) Explicit TTL still active — block all promotion
            if has_explicit_ttl and not explicit_ttl_expired:
                return FrameQueueAction(
                    display_frame=None,
                    dropped_frames=dropped,
                    reason="TTL still active",
                )

            # NEW: should_expire=True + explicit TTL expired → auto-remove from board
            if has_explicit_ttl and explicit_ttl_expired and self._displayed.should_expire:
                self._log(
                    f"[FrameQueue] tick → should_expire=True + TTL expired — "
                    f"removing frame={self._displayed.frame_id} "
                    f"source={self._displayed.source!r} from board"
                )
                dropped.append(self._displayed)
                self._displayed = None
                # Fall through to promotion logic below

            # (e) No TTL, no pending — hold board to prevent fallback cycling
            elif not has_explicit_ttl and not has_pending:
                return FrameQueueAction(
                    display_frame=None,
                    dropped_frames=dropped,
                    reason="no TTL — holding board (tick)",
                )

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

        # Move old displayed to fallback before promoting.
        # ALL displaced frames go to fallback regardless of should_expire.
        if self._displayed is not None and not _is_expired(self._displayed, now):
            displaced = self._displayed
            if (displaced.ttl_s is not None
                    and displaced.displayed_at is not None):
                elapsed = now - displaced.displayed_at
                displaced.remaining_ttl_s = max(0.0, displaced.ttl_s - elapsed)
            # Same-source dedup in fallback
            old_fb = [f for f in self._fallback if f.source == displaced.source]
            for old in old_fb:
                self._fallback.remove(old)
                dropped.append(old)
            self._fallback.append(displaced)

        next_frame.displayed_at = now
        # If frame was displaced mid-TTL, use the remaining TTL instead of original
        if next_frame.remaining_ttl_s is not None:
            self._log(
                f"[FrameQueue] tick → re-promoting with remaining_ttl_s="
                f"{next_frame.remaining_ttl_s:.1f} (original ttl_s={next_frame.ttl_s}) | "
                f"frame={next_frame.frame_id} source={next_frame.source!r}"
            )
            next_frame.ttl_s = int(next_frame.remaining_ttl_s)
            next_frame.remaining_ttl_s = None  # consumed
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
            pending=list(self._pending),  # FIFO order (index 0 = next up)
            fallback_stack=list(self._fallback),  # FIFO order (index 0 = next up)
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
            elif f.remaining_ttl_s is not None and f.remaining_ttl_s <= 0:
                dropped.append(f)
                self._log(
                    f"[FrameQueue] prune → exhausted fallback frame={f.frame_id} "
                    f"source={f.source!r} (remaining_ttl_s=0)"
                )
            else:
                new_fallback.append(f)
        self._fallback = new_fallback

        return dropped

    def _next_non_expired(self, now: float) -> Optional[BoardFrame]:
        """Return the best next frame to display (FIFO from fallback first, then pending).

        Fallback takes priority (displaced frames resume first).
        Within fallback: first displaced = first re-promoted (index 0).
        Within pending: first pushed = first promoted (index 0).

        Fallback frames with ``remaining_ttl_s <= 0`` are skipped — they have
        already exhausted their display time and should not be re-promoted.
        """
        # Try fallback first (displaced frames get priority)
        for f in self._fallback:
            if _is_expired(f, now):
                continue
            # Skip fallback frames that have exhausted their display time
            if f.remaining_ttl_s is not None and f.remaining_ttl_s <= 0:
                continue
            return f

        # Then try pending (FIFO — oldest first)
        for f in self._pending:
            if not _is_expired(f, now):
                return f

        return None
