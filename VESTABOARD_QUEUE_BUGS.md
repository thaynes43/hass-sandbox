# Vestaboard queue: incorrect estimated display times + stale fallback re-entry with full TTL

## Bug 1: Fallback and pending frames show identical estimated display times

**Observed:** In the queue status card, a FALLBACK frame (#1 Weather Schedule) and the PENDING frame behind it (#2 Messages From Library) both show `~1m 4s` — the same value as the current frame's remaining TTL. The pending frame should show `~1m 4s + fallback's TTL`, not the same time.

**Root cause area:** `status_publisher.py` lines 103-143 — cumulative TTL calculation.

```python
cumulative_s = queue_state.displayed_ttl_remaining_s or 0.0
# ...
for f in queue_state.fallback_stack:
    est_display_at = cumulative_s
    frame_ttl = (
        f.remaining_ttl_s if f.remaining_ttl_s is not None
        else (f.ttl_s or 0)
    )
    # ...
    cumulative_s += frame_ttl   # <-- if frame_ttl is 0, cumulative doesn't advance
```

When a fallback frame has `remaining_ttl_s = 0.0` (TTL was fully consumed before displacement), `frame_ttl` is 0 and `cumulative_s` doesn't advance. The next item in the sequence gets the same `est_display_in_s` value.

**Why remaining_ttl_s can be 0:** In `frame_queue.py` tick() lines 362-370, when a displayed frame's TTL has already expired and a pending frame is promoted, the old frame goes to fallback with:
```python
elapsed = now - displaced.displayed_at
displaced.remaining_ttl_s = max(0.0, displaced.ttl_s - elapsed)  # 0 if TTL already expired
```

**Key question for the fix:** Should frames with `remaining_ttl_s = 0` even be in fallback? They'll display for ~0 seconds (one tick cycle) before immediately yielding. Consider either:
1. Not adding frames to fallback when their TTL is fully consumed (remaining would be 0)
2. Making the status publisher show a minimum display time so the cumulative advances

---

## Bug 2: Frame re-enters the board with full TTL after its TTL elapsed

**Observed:** After the "Messages From Library" frame's TTL elapsed, it took the board again with a fresh full 30-minute TTL. The two items queued behind it then both showed `~30m`.

**Root cause area:** `frame_queue.py` — interaction between tick() promotion, fallback, and same-source pushes.

### Likely sequence of events:

1. Messages From Library displayed with `ttl_s=1800` (30 min)
2. TTL expires → tick() at line 310-341 falls through to promotion (explicit TTL expired, `should_expire=False`)
3. Line 364: old Messages frame moved to fallback with `remaining_ttl_s = 0` (TTL fully consumed, but `_is_expired()` returns False because `max_age_s` hasn't elapsed)
4. Next frame promoted from pending/fallback
5. Messages From Library automation fires again → pushes a **new** frame with fresh `ttl_s=1800`
6. New frame either:
   - **Displays immediately** (if board empty or current frame's TTL expired) with `displayed_at = now` → full 30 min TTL. The `same_source` path (which preserves `displayed_at`) is NOT taken because the old frame was already displaced.
   - **Queues in pending** behind current active TTL
7. Meanwhile the **old** Messages frame (remaining_ttl_s=0) is still sitting in fallback — no same-source dedup exists for fallback (only for pending, lines 242-253)

### Three contributing issues:

**A. No same-source dedup in fallback** (`frame_queue.py` lines 242-253)
- Same-source dedup only applies to `_pending`. When a new frame is pushed from the same source, old pending frames are evicted, but old fallback frames are NOT.
- This means both an old fallback frame (remaining_ttl_s=0) and a new pending frame can coexist for the same source.

**B. Fallback priority over pending** (`frame_queue.py` lines 496-511, `_next_non_expired`)
- Fallback is always checked before pending. A stale fallback frame with `remaining_ttl_s=0` will be promoted before a fresh pending frame with full TTL.
- This causes a wasted display cycle: stale frame displays for ~0s (one tick), then immediately yields.

**C. Frames with expired TTL still enter fallback** (`frame_queue.py` line 364)
- The guard `not _is_expired(self._displayed, now)` checks `max_age_s`, NOT TTL expiration.
- A frame whose TTL is fully consumed but whose `max_age_s` hasn't elapsed will always go to fallback.
- These zombie frames with `remaining_ttl_s = 0` clutter fallback and cause both bugs.

### Files to investigate:

| File | Lines | What to check |
|------|-------|---------------|
| `appdaemon/apps/vestaboard_apps/_shared/frame_queue.py` | 362-370 | tick() displacing to fallback — should check TTL remaining before adding |
| `appdaemon/apps/vestaboard_apps/_shared/frame_queue.py` | 242-253 | Same-source dedup — should also apply to fallback |
| `appdaemon/apps/vestaboard_apps/_shared/frame_queue.py` | 496-511 | `_next_non_expired` — fallback-before-pending priority with 0-TTL frames |
| `appdaemon/apps/vestaboard_apps/vestaboard_controller/status_publisher.py` | 103-143 | Cumulative TTL calculation — handles 0-TTL fallback frames |
| `appdaemon/tests/test_vestaboard_frame_queue.py` | — | Add test cases for these edge cases |

### Suggested fix approach:

1. **Don't add frames to fallback when remaining_ttl_s would be 0** — if TTL is fully consumed, the frame has had its time; drop it instead of cycling through fallback for one tick.
2. **Add same-source dedup to fallback** — when a new frame is pushed, remove any existing fallback entries from the same source.
3. **Status publisher: handle 0-TTL fallback frames** — either skip them in the sequence or use a minimum display time for cumulative calculation.
