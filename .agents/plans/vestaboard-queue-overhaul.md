# Vestaboard Queue Behavior Overhaul

## Overview

Rewrite the frame queue to use FIFO ordering for both pending and fallback, change `should_expire` to mean "auto-leave the board on TTL expiry", and always preserve displaced frames in fallback regardless of `should_expire`. Update README, tests, status publisher, and Lovelace card queue UI.

## Architecture context

```
frame_queue.py (pure Python)      vestaboard_controller_app.py (_tick)
     |                                      |
     v                                      v
  push() / tick()                    calls queue.tick(), writes board
     |                                      |
     v                                      v
  status_publisher.py               vestaboard-configuration-card.js
  (builds sensor attrs)             (renders queue view in Vestaboard+ tab)
```

### Key behavior changes (the source of truth for all agents)

**Change 1 — `should_expire` semantics:**
- OLD: `should_expire=True` means dropped when *displaced* by another frame. TTL expiry alone does NOT remove it.
- NEW: `should_expire=True` means when TTL elapses, the frame **auto-leaves the board** and we promote from fallback/pending. `should_expire=False` means the frame **holds the board** after TTL until a new push displaces it.

**Change 2 — Fallback queue:**
- OLD: LIFO. Only `should_expire=False` frames go to fallback when displaced.
- NEW: FIFO (first displaced = first re-promoted). **ALL** frames displaced before their TTL expires go to fallback, regardless of `should_expire`. Remaining TTL is preserved and resumed on re-promotion (this part is unchanged).

**Change 3 — Pending queue:**
- OLD: LIFO (most recently pushed promoted first).
- NEW: FIFO (first pushed = first promoted). Same-source dedup still applies (newer overwrites older from same source).

**Change 4 — Promotion priority:**
- OLD: Pending before fallback.
- NEW: **Fallback before pending.** Fallback frames were already on the board and deserve to finish their time. Pending frames are new content that can wait.

**Change 5 — UI combined sequence view:**
- The Vestaboard+ queue section should show a combined numbered sequence: fallback frames first (1, 2, ...), then pending frames, with estimated time-to-display based on cumulative TTLs of current + preceding frames.

## Constraints

- All existing tests must pass after updates (some will need modification for new behavior)
- Import path `vestaboard_apps.vestaboard_controller.vestaboard_controller_app.VestaboardControllerApp` must NOT change
- `_shared/frame_queue.py` must remain pure Python (no AppDaemon dependency)
- Tests that patch `VestaboardClient` at the controller module path must still work
- Card JS must follow `.cursor/rules/custom-card-guidelines.mdc` and card agent instructions at `appdaemon/apps/vestaboard_apps/vestaboard_configuration/vestaboard-configuration-card-agent.md`
- Do NOT deploy to production. All changes stay in repo.
- Do NOT commit. User will test first.

## Files touched

| File | Track | Action |
|------|-------|--------|
| `appdaemon/apps/vestaboard_apps/vestaboard_controller/README.md` | A (Docs) | Rewrite queue sections |
| `appdaemon/tests/test_vestaboard_controller_app.py` | B (Tests + Queue impl) | Add/update tests for new behavior |
| `appdaemon/apps/vestaboard_apps/_shared/frame_queue.py` | B (Tests + Queue impl) | Implement new FIFO + should_expire + fallback logic |
| `appdaemon/apps/vestaboard_apps/vestaboard_controller/vestaboard_controller_app.py` | B (Tests + Queue impl) | Update `_tick()` for should_expire auto-removal |
| `appdaemon/apps/vestaboard_apps/vestaboard_controller/status_publisher.py` | C (Status + Card) | Add combined sequence view data |
| `appdaemon/apps/vestaboard_apps/vestaboard_configuration/vestaboard-configuration-card.js` | C (Status + Card) | Render combined sequence view |

## Parallelism analysis

| Step | Files touched | Dependencies | Track |
|------|---------------|-------------|-------|
| Step 1: README | README.md | none | A |
| Step 2: Tests + queue impl | frame_queue.py, test_vestaboard_controller_app.py, vestaboard_controller_app.py | none | B |
| Step 3: Status publisher + card UI | status_publisher.py, vestaboard-configuration-card.js | Step 2 (needs new queue API) | C |
| Step 4: Validation | (read-only) | Steps 1, 2, 3 | V |

**Track A** (README) and **Track B** (Tests + Queue impl) can run in parallel.
**Track C** (Status + Card) depends on Track B completing.
**Track V** (Validation) runs last.

---

## Step 1 — README update (Track A)

**Agent type:** Implementation Agent
**Playbooks:** `.cursor/rules/appdaemon-documentation.mdc`
**Files:** `appdaemon/apps/vestaboard_apps/vestaboard_controller/README.md`

### What to change

1. **Frame queue concepts table** (line ~18-27):
   - Change LIFO to FIFO for both pending and fallback descriptions
   - Change `should_expire` description: "If `True`, the frame **auto-leaves the board** when TTL expires. Promoted from fallback/pending fills the board. If `False`, frame holds board after TTL until displaced."
   - Add: "Fallback priority: Fallback is drawn from BEFORE pending (displaced frames resume first)"

2. **Queue lifecycle diagram** (line ~32-57):
   - Update the ASCII diagram: pending is FIFO not LIFO, fallback is FIFO
   - Remove the "if should_expire=False" / "DROPPED if should_expire=True" distinction for displacement. ALL displaced frames go to fallback.
   - Add: "should_expire=True + TTL expired -> DROPPED (auto-leaves board)"
   - Add: "should_expire=False + TTL expired -> HOLDS board"

3. **CURRENT section** (line ~59-66):
   - Add: "When TTL expires and `should_expire=True`: frame is removed from the board. Fallback is consulted first, then pending."
   - Add: "When TTL expires and `should_expire=False`: frame stays on the board until a new push arrives."

4. **PENDING section** (line ~68-76):
   - Change "LIFO" to "FIFO" everywhere
   - Selection order: FIFO (first pushed = first promoted)

5. **FALLBACK section** (line ~88-123):
   - Change: ALL displaced frames go to fallback (not just `should_expire=False`)
   - Change: FIFO order (first displaced = first re-promoted)
   - Change: Fallback is consulted BEFORE pending
   - Update the "should_expire controls what happens" section to describe new behavior:
     - `should_expire=True`: Frame auto-leaves board on TTL expiry. If displaced mid-TTL, goes to fallback with remaining TTL.
     - `should_expire=False`: Frame holds board after TTL. If displaced, goes to fallback with remaining TTL.
   - Remove: "Critical: TTL expiry alone does NOT move a frame to fallback" — this is now wrong
   - Update example scenarios to match new behavior

6. **Automation lifecycle sections** (line ~125-229):
   - Update all automation descriptions to reflect: `should_expire=True` now means auto-leaves on TTL, not dropped-on-displacement
   - Calendar clock: `should_expire=False` means it holds the board indefinitely (unchanged behavior for TTL=None)
   - Library/AI messages: `should_expire=True` means they auto-leave after TTL, AND if displaced they go to fallback

### Success criteria
- All LIFO references changed to FIFO
- `should_expire` described as TTL-expiry-based auto-removal
- Fallback described as universal (all displaced frames) and FIFO
- Fallback-before-pending priority documented
- Examples updated to match new behavior

---

## Step 2 — Tests + Queue implementation (Track B)

**Agent type:** Implementation Agent (use Opus for complexity)
**Playbooks:** `.cursor/rules/appdaemon-coding-guidelines.mdc`, `.cursor/rules/logging-standards.mdc`
**Files:**
- `appdaemon/apps/vestaboard_apps/_shared/frame_queue.py`
- `appdaemon/apps/vestaboard_apps/vestaboard_controller/vestaboard_controller_app.py`
- `appdaemon/tests/test_vestaboard_controller_app.py`
**Rule files to read first:**
- `.cursor/rules/appdaemon-coding-guidelines.mdc`
- `.cursor/rules/logging-standards.mdc`
- `.cursor/rules/security-policy.mdc`

### frame_queue.py changes

#### `_next_non_expired()` (line 495-512)
Change from LIFO to FIFO for both fallback and pending, and change priority order:

```python
def _next_non_expired(self, now: float) -> Optional[BoardFrame]:
    """Return the best next frame to display (FIFO from fallback first, then pending).

    Fallback takes priority (displaced frames resume first).
    Within fallback: first displaced = first re-promoted (index 0).
    Within pending: first pushed = first promoted (index 0).
    """
    # Try fallback first (displaced frames get priority)
    for f in self._fallback:
        if not _is_expired(f, now):
            return f

    # Then try pending (FIFO — oldest first)
    for f in self._pending:
        if not _is_expired(f, now):
            return f

    return None
```

#### `push()` (line 160-272)
Change displacement logic — ALL displaced frames go to fallback, regardless of `should_expire`:

- Remove the `if self._displayed.should_expire:` branch that drops frames (around line 193-199)
- Instead, always move the displaced frame to fallback (with remaining TTL calculation)
- Same-source still drops (that's dedup, not displacement)
- Update log messages: remove "LIFO" references, say "FIFO"
- The `self._pending.append(frame)` at line 257 stays (appending to end is correct for FIFO since we now pop from index 0)

#### `tick()` (line 274-394)
Add `should_expire=True` TTL auto-removal logic:

Current tick logic at line 312-331 needs a new branch:
```python
# NEW: should_expire=True + TTL expired → auto-remove from board
if has_explicit_ttl and explicit_ttl_expired and self._displayed.should_expire:
    self._log(
        f"[FrameQueue] tick → should_expire=True + TTL expired — "
        f"removing frame={self._displayed.frame_id} "
        f"source={self._displayed.source!r} from board"
    )
    dropped.append(self._displayed)
    self._displayed = None
    # Fall through to promotion logic below
```

Also update the existing displacement logic in tick (around line 353-369):
- Remove the `if self._displayed.should_expire:` branch that drops
- Always move displaced frame to fallback

#### `get_state()` (line 396-412)
Change the list ordering to FIFO (remove `reversed()`):
```python
pending=list(self._pending),  # FIFO order (index 0 = next up)
fallback_stack=list(self._fallback),  # FIFO order (index 0 = next up)
```

#### `FrameQueueState` docstring (line 79-94)
Update docstrings:
- pending: "Frames waiting to be shown (FIFO — index 0 is next up)."
- fallback_stack: "Previously displaced frames (FIFO — index 0 is next to resume)."

#### Class docstring (line 129-146)
Update from LIFO to FIFO description. Update fallback description.

### vestaboard_controller_app.py `_tick()` changes

The `_tick()` method (line 391-466) calls `self._queue.tick(now)` and then acts on the result. The main queue logic changes are in `frame_queue.py`, but the controller needs to handle the case where `should_expire=True` causes auto-removal:

- When `action.display_frame` is not None and it came from fallback re-promotion after a `should_expire` auto-removal, the board write already happens correctly.
- When `action.display_frame` is None but `action.dropped_frames` is not empty (should_expire frame expired, nothing to promote), the existing code at line 421-422 already calls `_read_board_state()` — this handles it.

**Likely no changes needed in `_tick()`** beyond what `frame_queue.py` provides. But verify this during implementation.

### Test changes

#### New tests to add

| Test name | What it verifies |
|-----------|-----------------|
| `test_should_expire_true_ttl_elapsed_auto_removes` | Frame with `should_expire=True` + TTL expired → tick auto-removes from board, promotes next |
| `test_should_expire_false_ttl_elapsed_holds_board` | Frame with `should_expire=False` + TTL expired → tick does NOT remove, frame stays |
| `test_should_expire_true_displaced_goes_to_fallback` | Force-push displaces `should_expire=True` frame → it goes to fallback (not dropped) |
| `test_fallback_is_fifo` | First displaced frame is re-promoted first |
| `test_pending_is_fifo` | First pushed pending frame is promoted first |
| `test_fallback_before_pending` | Fallback frame is promoted before pending frame |
| `test_displaced_remaining_ttl_preserved_in_fallback` | Displaced frame retains remaining TTL in fallback |
| `test_remaining_ttl_resumed_on_repromotion` | Re-promoted frame gets remaining TTL not original |
| `test_same_source_dedup_still_works_fifo` | Same-source newer frame replaces older in pending |
| `test_should_expire_true_no_ttl_holds_board` | `should_expire=True` with `ttl_s=None` — no auto-removal (no TTL to expire) |
| `test_combined_scenario_preview_displaces_resumes` | Preview force-push → displaces → fallback → preview TTL expires → fallback resumes |

#### Existing tests to update

Search for tests that assert LIFO behavior or assert that `should_expire=True` frames are dropped on displacement. These need updating:

- Tests checking `reversed()` ordering of pending/fallback
- Tests checking that `should_expire=True` frames are dropped (not moved to fallback) on push displacement
- Tests checking LIFO promotion order
- The `_next_non_expired` tests

Use this grep to find them:
```bash
grep -n "should_expire\|LIFO\|fallback.*drop\|reversed\|_next_non_expired" appdaemon/tests/test_vestaboard_controller_app.py
```

### Test command
```bash
source /home/thaynes/workspace/hass-sandbox/.claude/worktrees/eager-stargazing-rivest/.venv/bin/activate && cd /home/thaynes/workspace/hass-sandbox/.claude/worktrees/eager-stargazing-rivest/appdaemon && python -m pytest tests/test_vestaboard_controller_app.py -v --tb=short
```

### Success criteria
- `_next_non_expired` uses FIFO: iterates `self._fallback` then `self._pending` (no `reversed()`)
- `push()` displacement always goes to fallback (no `should_expire` drop branch)
- `tick()` auto-removes `should_expire=True` frames when TTL expires
- `tick()` leaves `should_expire=False` frames on board when TTL expires
- `get_state()` returns FIFO order (no `reversed()`)
- All new tests pass
- All existing tests pass (updated as needed)

---

## Step 3 — Status publisher + Card UI (Track C)

**Agent type:** Implementation Agent (use Opus for card JS complexity)
**Playbooks:** `.cursor/rules/custom-card-guidelines.mdc`, card agent instructions at `appdaemon/apps/vestaboard_apps/vestaboard_configuration/vestaboard-configuration-card-agent.md`
**Files:**
- `appdaemon/apps/vestaboard_apps/vestaboard_controller/status_publisher.py`
- `appdaemon/apps/vestaboard_apps/vestaboard_configuration/vestaboard-configuration-card.js`
**Dependencies:** Step 2 must be complete (needs FIFO queue API)

### status_publisher.py changes

Add a `queue_sequence` attribute to the sensor output. This is the combined, numbered sequence of frames that will display next, with estimated times.

In `build_attributes()`, after the existing `queue_state` dict (around line 121-148), add:

```python
# Build combined queue sequence (fallback first, then pending)
# with estimated display times based on cumulative TTLs
queue_sequence = []
cumulative_s = queue_state.displayed_ttl_remaining_s or 0.0
seq_num = 1

# Fallback frames first (FIFO — index 0 is next)
for f in queue_state.fallback_stack:
    est_display_at = cumulative_s
    frame_ttl = f.remaining_ttl_s if f.remaining_ttl_s is not None else (f.ttl_s or 0)
    queue_sequence.append({
        "seq": seq_num,
        "frame_id": f.frame_id,
        "source": f.source,
        "source_label": f.source_label,
        "zone": "fallback",
        "est_display_in_s": round(est_display_at, 1),
        "ttl_s": frame_ttl,
    })
    cumulative_s += frame_ttl
    seq_num += 1

# Pending frames next (FIFO — index 0 is next)
for f in queue_state.pending:
    est_display_at = cumulative_s
    frame_ttl = f.ttl_s or 0
    queue_sequence.append({
        "seq": seq_num,
        "frame_id": f.frame_id,
        "source": f.source,
        "source_label": f.source_label,
        "zone": "pending",
        "est_display_in_s": round(est_display_at, 1),
        "ttl_s": frame_ttl,
    })
    cumulative_s += frame_ttl
    seq_num += 1
```

Add `"queue_sequence": queue_sequence` to the attributes dict.

### vestaboard-configuration-card.js changes

Update `_renderQueueSection()` (line 1199-1281) to render the combined sequence view.

Replace the separate pending/fallback sections with a unified numbered list:

```javascript
// Read the new queue_sequence attribute
const queueSequence = this._sensorAttr("queue_sequence", []);

// Render combined sequence
const sequenceItems = (Array.isArray(queueSequence) ? queueSequence : []).map((item) => `
  <div class="queue-item queue-seq-item queue-zone-${this._esc(item.zone || 'unknown')}">
    <span class="queue-seq-num">#${item.seq}</span>
    <span class="queue-source">${this._esc(item.source_label || item.source || "\u2014")}</span>
    <span class="queue-zone-badge">${this._esc(item.zone)}</span>
    <span class="queue-est-time">~${vbcFormatDuration(item.est_display_in_s)}</span>
  </div>
`).join("");
```

Keep the existing Current, Upcoming, and Sleep sections. Replace the separate Pending and Fallback sections with:
```html
<div class="queue-sequence">
  <span class="queue-label">Up Next (${queueSequence.length}):</span>
  ${sequenceItems || '<span class="queue-value">nothing queued</span>'}
</div>
```

Add CSS for the new elements:
- `.queue-seq-num` — small bold number
- `.queue-zone-badge` — small pill/tag showing "fallback" or "pending"
- `.queue-est-time` — right-aligned estimated time
- `.queue-zone-fallback` — subtle background tint for fallback items
- `.queue-zone-pending` — subtle background tint for pending items

Update `_stampKey()` (around line 269-276) to include `queue_sequence` in the change detection stamp.

### Card validation
```bash
node --check /home/thaynes/workspace/hass-sandbox/.claude/worktrees/eager-stargazing-rivest/appdaemon/apps/vestaboard_apps/vestaboard_configuration/vestaboard-configuration-card.js
```

### Success criteria
- `status_publisher.py` outputs `queue_sequence` attribute with numbered entries
- Each entry has: seq, frame_id, source, source_label, zone, est_display_in_s, ttl_s
- Card renders combined sequence view with numbers and estimated times
- Zone badges distinguish fallback vs pending items
- Card passes `node --check`
- Existing queue header count still works

---

## Step 4 — Validation (Track V)

**Agent type:** Validation Agent (read-only)
**Dependencies:** Steps 1, 2, 3 all complete

### Validation checklist

#### frame_queue.py
- [ ] `_next_non_expired()` iterates `self._fallback` before `self._pending` (no `reversed()`)
- [ ] `push()` displacement: ALL displaced frames go to fallback, never dropped based on `should_expire`
- [ ] `push()` same-source: still drops (dedup, not displacement)
- [ ] `tick()`: `should_expire=True` + explicit TTL expired → auto-removes frame from board
- [ ] `tick()`: `should_expire=False` + explicit TTL expired → frame stays on board (when pending exists, it can be displaced)
- [ ] `tick()`: displacement in tick also always goes to fallback (no `should_expire` drop)
- [ ] `get_state()` returns FIFO order (no `reversed()`)
- [ ] Class docstring updated to say FIFO
- [ ] `FrameQueueState` docstring updated
- [ ] No `reversed(self._pending)` or `reversed(self._fallback)` anywhere in the file
- [ ] Remaining TTL preservation logic unchanged (still works)

#### vestaboard_controller_app.py
- [ ] `_tick()` correctly handles queue actions from updated frame_queue
- [ ] Import path unchanged
- [ ] No new AppDaemon dependencies added to frame_queue.py

#### Tests
- [ ] New test: `should_expire=True` + TTL expired → auto-removed
- [ ] New test: `should_expire=False` + TTL expired → holds board
- [ ] New test: displaced `should_expire=True` frame goes to fallback
- [ ] New test: fallback is FIFO
- [ ] New test: pending is FIFO
- [ ] New test: fallback promoted before pending
- [ ] New test: combined scenario (preview displaces, fallback resumes)
- [ ] All existing tests pass (updated for new behavior)
- [ ] Full test suite passes: `python -m pytest tests/ -v --tb=short`

#### status_publisher.py
- [ ] `queue_sequence` attribute present in output
- [ ] Fallback entries listed before pending entries
- [ ] Each entry has: seq, frame_id, source, source_label, zone, est_display_in_s, ttl_s
- [ ] `est_display_in_s` is cumulative (current TTL remaining + preceding frame TTLs)

#### vestaboard-configuration-card.js
- [ ] Combined sequence view renders in Vestaboard+ tab
- [ ] Sequence numbers visible
- [ ] Zone badges (fallback/pending) visible
- [ ] Estimated times shown
- [ ] `node --check` passes
- [ ] No `preventDefault()` on input/select/textarea touchend
- [ ] `_stampKey()` includes queue_sequence data

#### README.md
- [ ] All LIFO references replaced with FIFO
- [ ] `should_expire` described as TTL-based auto-removal
- [ ] Fallback described as universal + FIFO
- [ ] Fallback-before-pending priority documented
- [ ] Automation lifecycle examples updated
- [ ] No contradictions with implementation

---

## Agent prompts

### Track A — README Implementation Agent

```text
You are an Implementation Agent. Your task is fully described in the plan file at:

  /home/thaynes/workspace/hass-sandbox/.claude/worktrees/eager-stargazing-rivest/.agents/plans/vestaboard-queue-overhaul.md

Read the full plan file before doing anything else. Focus on **Step 1 — README update (Track A)** only.

Also read these rule files before making any changes:
- .cursor/rules/appdaemon-documentation.mdc

Your scope is limited to this single file:
- appdaemon/apps/vestaboard_apps/vestaboard_controller/README.md

Also read the current implementation for reference (do NOT modify these):
- appdaemon/apps/vestaboard_apps/_shared/frame_queue.py

The README must describe the NEW intended behavior (from the plan), not the current code.
The code will be updated separately by another agent to match.

DO NOT modify any code files. DO NOT commit. DO NOT deploy.
```

### Track B — Tests + Queue Implementation Agent

```text
You are an Implementation Agent. Your task is fully described in the plan file at:

  /home/thaynes/workspace/hass-sandbox/.claude/worktrees/eager-stargazing-rivest/.agents/plans/vestaboard-queue-overhaul.md

Read the full plan file before doing anything else. Focus on **Step 2 — Tests + Queue implementation (Track B)** only.

Also read these rule files before making any changes:
- .cursor/rules/appdaemon-coding-guidelines.mdc
- .cursor/rules/logging-standards.mdc
- .cursor/rules/security-policy.mdc

Your scope is limited to these files:
- appdaemon/apps/vestaboard_apps/_shared/frame_queue.py
- appdaemon/apps/vestaboard_apps/vestaboard_controller/vestaboard_controller_app.py
- appdaemon/tests/test_vestaboard_controller_app.py

Write the new tests FIRST (test-driven), then implement the queue changes, then update
existing tests that fail due to the behavioral change. The new behavior is defined in
the "Key behavior changes" section of the plan file.

Run the full test suite after all changes and fix any failures:

  source /home/thaynes/workspace/hass-sandbox/.claude/worktrees/eager-stargazing-rivest/.venv/bin/activate && cd /home/thaynes/workspace/hass-sandbox/.claude/worktrees/eager-stargazing-rivest/appdaemon && python -m pytest tests/test_vestaboard_controller_app.py -v --tb=short

DO NOT modify README.md, status_publisher.py, or the card JS. DO NOT commit. DO NOT deploy.
```

### Track C — Status Publisher + Card UI Implementation Agent

```text
You are an Implementation Agent. Your task is fully described in the plan file at:

  /home/thaynes/workspace/hass-sandbox/.claude/worktrees/eager-stargazing-rivest/.agents/plans/vestaboard-queue-overhaul.md

Read the full plan file before doing anything else. Focus on **Step 3 — Status publisher + Card UI (Track C)** only.

IMPORTANT: This step depends on Step 2 (Track B) being complete. Before starting,
verify that frame_queue.py has been updated by checking that `_next_non_expired`
iterates `self._fallback` before `self._pending` (no `reversed()`).

Also read these files before making any changes:
- .cursor/rules/custom-card-guidelines.mdc
- appdaemon/apps/vestaboard_apps/vestaboard_configuration/vestaboard-configuration-card-agent.md
- appdaemon/apps/vestaboard_apps/vestaboard_controller/README.md (for new behavior description)

Your scope is limited to these files:
- appdaemon/apps/vestaboard_apps/vestaboard_controller/status_publisher.py
- appdaemon/apps/vestaboard_apps/vestaboard_configuration/vestaboard-configuration-card.js

After JS changes, run:
  node --check /home/thaynes/workspace/hass-sandbox/.claude/worktrees/eager-stargazing-rivest/appdaemon/apps/vestaboard_apps/vestaboard_configuration/vestaboard-configuration-card.js

Also run the full test suite to make sure status_publisher changes don't break anything:
  source /home/thaynes/workspace/hass-sandbox/.claude/worktrees/eager-stargazing-rivest/.venv/bin/activate && cd /home/thaynes/workspace/hass-sandbox/.claude/worktrees/eager-stargazing-rivest/appdaemon && python -m pytest tests/test_vestaboard_controller_app.py -v --tb=short

DO NOT modify frame_queue.py or the test file. DO NOT commit. DO NOT deploy.
```

### Validation Agent

```text
You are a Validation Agent. Review the implementation described in the plan file at:

  /home/thaynes/workspace/hass-sandbox/.claude/worktrees/eager-stargazing-rivest/.agents/plans/vestaboard-queue-overhaul.md

Read the full plan file — the "Step 4 — Validation" section lists every requirement to verify.

Also read these rule files:
- .cursor/rules/appdaemon-coding-guidelines.mdc
- .cursor/rules/custom-card-guidelines.mdc
- .cursor/rules/logging-standards.mdc

DO NOT modify any files. Your job is to READ and VERIFY only.

Verify each checklist item by reading the relevant source files. Run the full test suite
and include the result in your report:

  source /home/thaynes/workspace/hass-sandbox/.claude/worktrees/eager-stargazing-rivest/.venv/bin/activate && cd /home/thaynes/workspace/hass-sandbox/.claude/worktrees/eager-stargazing-rivest/appdaemon && python -m pytest tests/ -v --tb=short

Also run the JS syntax check:
  node --check /home/thaynes/workspace/hass-sandbox/.claude/worktrees/eager-stargazing-rivest/appdaemon/apps/vestaboard_apps/vestaboard_configuration/vestaboard-configuration-card.js

Output a PASS or FAIL verdict.

If FAIL, list every failing checklist item with:
  - File path and method/line where the issue is
  - What is wrong or missing
  - What the fix should be

Then produce a copy-pasteable prompt for the relevant Implementation Agent in a fenced
text block.
```

---

## Implementation Agent re-prompt template

```text
You are Implementation Agent <A/B/C> for the Vestaboard Queue Overhaul plan.

Validation Agent has completed a read-only validation pass. The following defects
were found that you must fix.

DEFECT 1

File: <path>

<What is wrong. What the fix should be.>

REQUIRED FIX

1. <First action>
2. <Second action>

Read the plan file at:
  /home/thaynes/workspace/hass-sandbox/.claude/worktrees/eager-stargazing-rivest/.agents/plans/vestaboard-queue-overhaul.md

Do not commit or deploy. Run the full test suite after your changes:

  source /home/thaynes/workspace/hass-sandbox/.claude/worktrees/eager-stargazing-rivest/.venv/bin/activate && cd /home/thaynes/workspace/hass-sandbox/.claude/worktrees/eager-stargazing-rivest/appdaemon && python -m pytest tests/ -v --tb=short
```

---

## Execution order

1. **Parallel:** Launch Track A (README) and Track B (Tests + Queue impl) simultaneously
2. **Sequential:** After Track B completes, launch Track C (Status + Card)
3. **Sequential:** After all tracks complete, launch Validation
4. **If validation fails:** Re-prompt the relevant Implementation Agent
5. **Final:** Planner performs final review, asks user to test before commit
