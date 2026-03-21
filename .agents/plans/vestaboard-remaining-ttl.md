# Plan: Vestaboard Remaining TTL Tracking

## Problem

When a frame with `override_ttl=True` (e.g. preview button) pre-empts the current displayed frame, the displaced frame goes to the fallback stack. When the preview's short TTL expires, the fallback frame gets re-promoted with `displayed_at = now`, restarting its full TTL from scratch. A calendar frame with 30min TTL that used 10min before being displaced gets a fresh 30min when it comes back.

## Architecture overview

```
                     push(frame)
                         |
                    FrameQueue
                   (frame_queue.py)
                   /     |      \
             pending  displayed  fallback
                         |
              VestaboardControllerApp
           (vestaboard_controller_app.py)
                         |
                   publishes sensor
                   (queue_state attr)
                         |
            VestaboardConfigurationApp
          (vestaboard_configuration_app.py)
                         |
                  re-publishes for card
                  (fallback_source attr → queue attr)
                         |
               vestaboard-configuration-card.js
                   (renders UI)
```

Three data hops for fallback info:
1. `frame_queue.py` → `FrameQueueState` (Python dataclass)
2. `vestaboard_controller_app.py` line 1177-1184 → serializes fallback frames into `queue_state.fallback[]` dicts
3. `vestaboard_configuration_app.py` line 733 → reads `queue_state.fallback[0].source` as `fallback_source`
4. `vestaboard-configuration-card.js` line 1179 → reads `fallback_source` and renders

## Constraints

- Do NOT change the external push API (automations should not need to know about `remaining_ttl_s`)
- Do NOT deploy to production — all changes stay in dev until merged to `main`
- `frame_queue.py` is pure Python — no AppDaemon dependencies
- Card JS follows delegated touch+click pattern with shadow DOM
- `remaining_ttl_s` defaults to `None` everywhere — backward compatible

---

## Implementation detail

### 1. `BoardFrame` dataclass — add `remaining_ttl_s` field

**File:** `appdaemon/apps/vestaboard_apps/_shared/frame_queue.py`
**Lines:** 19-58 (BoardFrame dataclass)

Add a new optional field after `refresh_interval_minutes`:

```python
remaining_ttl_s: Optional[float] = field(default=None)
```

This field means "when this frame is re-promoted from fallback, use this value as the effective TTL instead of the original `ttl_s`." It is only set when a frame is displaced to fallback mid-TTL.

### 2. `push()` — calculate remaining TTL when displacing to fallback

**File:** `appdaemon/apps/vestaboard_apps/_shared/frame_queue.py`
**Method:** `push()`, lines 159-261

In the `should_display_now` branch, when the old displayed frame is moved to fallback (line 200: `self._fallback.append(self._displayed)`), calculate and store the remaining TTL:

```python
# Before appending to fallback, calculate remaining TTL
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
self._fallback.append(displaced)
```

This must happen in the block at lines 199-200 (the `else` clause that appends to fallback when `should_expire` is False).

### 3. `tick()` — use remaining TTL when promoting from fallback

**File:** `appdaemon/apps/vestaboard_apps/_shared/frame_queue.py`
**Method:** `tick()`, lines 263-369

At line 355 (`next_frame.displayed_at = now`), after setting `displayed_at`, check if the frame has `remaining_ttl_s` and apply it:

```python
next_frame.displayed_at = now
# If frame was displaced mid-TTL, use the remaining TTL
if next_frame.remaining_ttl_s is not None:
    self._log(
        f"[FrameQueue] tick → re-promoting with remaining_ttl_s="
        f"{next_frame.remaining_ttl_s:.1f} (original ttl_s={next_frame.ttl_s}) | "
        f"frame={next_frame.frame_id} source={next_frame.source!r}"
    )
    next_frame.ttl_s = int(next_frame.remaining_ttl_s)  # int to match original type
    next_frame.remaining_ttl_s = None  # consumed
self._displayed = next_frame
```

**Important:** Also apply this logic in `tick()` when the *old* displayed frame is moved to fallback (lines 344-353). The same remaining-TTL calculation applies:

```python
if self._displayed is not None and not _is_expired(self._displayed, now):
    if self._displayed.should_expire:
        # ... existing drop logic ...
    else:
        # Calculate remaining TTL before moving to fallback
        displaced = self._displayed
        if (displaced.ttl_s is not None
                and displaced.displayed_at is not None):
            elapsed = now - displaced.displayed_at
            displaced.remaining_ttl_s = max(0.0, displaced.ttl_s - elapsed)
        self._fallback.append(displaced)
```

### 4. Controller status publishing — include `remaining_ttl_s` in fallback dicts

**File:** `appdaemon/apps/vestaboard_apps/vestaboard_controller/vestaboard_controller_app.py`
**Lines:** 1177-1184 (fallback serialization in queue_state)

Add `remaining_ttl_s` to each fallback frame dict:

```python
"fallback": [
    {
        "frame_id": f.frame_id,
        "source": f.source,
        "source_label": f.source_label,
        "remaining_ttl_s": f.remaining_ttl_s,
    }
    for f in state.fallback_stack
],
```

### 5. Configuration app — pass fallback data through to card

**File:** `appdaemon/apps/vestaboard_apps/vestaboard_configuration/vestaboard_configuration_app.py`
**Lines:** 732-734

Change the `fallback_source` to pass the full fallback list so the card has access to `remaining_ttl_s`:

```python
"fallback_source": (queue_state.get("fallback", [{}])[0].get("source")
                    if queue_state.get("fallback") else None),
"fallback_frames": queue_state.get("fallback", []),
```

### 6. Card JS — render remaining TTL for fallback frames

**File:** `appdaemon/apps/vestaboard_apps/vestaboard_configuration/vestaboard-configuration-card.js`
**Lines:** 1232-1235 (fallback rendering in `_renderQueueSection`)

Replace the simple fallback display with a list that shows `remaining_ttl_s` when present:

```javascript
const fallbackFrames = this._sensorAttr("fallback_frames", []);
// ... render each fallback frame with remaining_ttl_s displayed in orange/red
```

For each fallback frame with `remaining_ttl_s`, render something like:
```
Fallback (2):
  calendar_clock — TTL paused: 14m 22s
  static_frame
```

The "TTL paused" text should use a warning color (orange/red) via a CSS class like `ttl-paused`.

Add CSS class:
```css
.ttl-paused {
  color: #ff9800;
  font-size: 0.85em;
}
```

Also update the `_changeKey()` method (line ~259) if `fallback_frames` should trigger re-renders.

---

## Test case table

| Test name | What it verifies |
|-----------|-----------------|
| `test_displaced_frame_gets_remaining_ttl` | Frame with ttl_s=60 displayed for 20s, displaced by override_ttl push. `remaining_ttl_s` should be ~40 |
| `test_repromoted_frame_uses_remaining_ttl` | Frame displaced with remaining_ttl_s=40, re-promoted from fallback. Its effective TTL should be 40, not the original 60 |
| `test_remaining_ttl_cleared_after_promotion` | After re-promotion, `remaining_ttl_s` is None (consumed) |
| `test_no_ttl_frame_no_remaining_ttl` | Frame with ttl_s=None displaced to fallback. `remaining_ttl_s` should remain None |
| `test_remaining_ttl_zero_floor` | Frame whose TTL already expired before displacement gets `remaining_ttl_s=0.0` |
| `test_tick_displacement_calculates_remaining_ttl` | Frame displaced during `tick()` (not `push()`) also gets correct `remaining_ttl_s` |
| `test_should_expire_frame_not_fallbacked` | Existing behavior: `should_expire=True` frame is dropped, not moved to fallback (regression guard) |
| `test_get_state_includes_remaining_ttl` | `FrameQueueState.fallback_stack` frames have `remaining_ttl_s` accessible |

---

## Parallelism analysis

| Todo | Files touched | Dependencies | Track |
|------|---------------|-------------|-------|
| frame_queue.py changes | `frame_queue.py` | none | A |
| controller_app.py changes | `vestaboard_controller_app.py` | frame_queue.py | A |
| configuration_app.py changes | `vestaboard_configuration_app.py` | controller_app.py | A |
| card JS changes | `vestaboard-configuration-card.js` | configuration_app.py | A |
| tests | `test_vestaboard_frame_queue.py` | frame_queue.py | A |

**Decision: 1 Implementation Agent** — all files are in a single dependency chain, and the frame queue changes must be done before the controller/config/card changes.

---

## Validation checklist

### Frame Queue (`frame_queue.py`)
- [ ] `BoardFrame` has `remaining_ttl_s: Optional[float] = field(default=None)` field
- [ ] `push()`: when displacing a frame to fallback, calculates `remaining_ttl_s = max(0, ttl_s - elapsed)` when `ttl_s` and `displayed_at` are both set
- [ ] `push()`: does NOT set `remaining_ttl_s` when frame has `ttl_s=None`
- [ ] `push()`: does NOT set `remaining_ttl_s` when frame has `should_expire=True` (dropped, not fallbacked)
- [ ] `tick()`: when promoting a frame with `remaining_ttl_s` set, assigns `ttl_s = remaining_ttl_s` (as int) and clears `remaining_ttl_s`
- [ ] `tick()`: when displacing the current frame to fallback, calculates `remaining_ttl_s` the same as `push()`
- [ ] `tick()`: promoted frame gets `displayed_at = now` (existing behavior preserved)
- [ ] No changes to `_ttl_expired()` — it already works based on `ttl_s` and `displayed_at`

### Controller (`vestaboard_controller_app.py`)
- [ ] Fallback frame dicts in `queue_state` include `"remaining_ttl_s": f.remaining_ttl_s`

### Configuration app (`vestaboard_configuration_app.py`)
- [ ] New attribute `fallback_frames` passes full fallback list from `queue_state`
- [ ] Existing `fallback_source` attribute still works (backward compat for any other consumers)

### Card JS (`vestaboard-configuration-card.js`)
- [ ] Reads `fallback_frames` attribute
- [ ] Renders fallback frames list (not just the first source)
- [ ] Shows remaining TTL for frames that have `remaining_ttl_s` set, in a warning color
- [ ] Fallback frames without `remaining_ttl_s` render normally (no TTL indicator)
- [ ] Change key includes `fallback_frames` for re-render triggering

### Tests (`test_vestaboard_frame_queue.py`)
- [ ] Test: displaced frame gets correct `remaining_ttl_s`
- [ ] Test: re-promoted frame uses remaining TTL instead of original
- [ ] Test: `remaining_ttl_s` cleared after promotion
- [ ] Test: no-TTL frame does not get `remaining_ttl_s`
- [ ] Test: `should_expire=True` frame is dropped (not fallbacked) — regression
- [ ] Test: displacement during `tick()` also calculates `remaining_ttl_s`
- [ ] All existing tests still pass

### Security
- [ ] No secrets or credentials added
- [ ] No new external HTTP calls

---

## Agent prompts

### Implementation Agent

```text
You are an Implementation Agent. Your task is fully described in the plan file at:

  .agents/plans/vestaboard-remaining-ttl.md

Read the full plan file before doing anything else. It contains architecture context,
detailed implementation instructions, test case tables, and a validation checklist.

Also read these rule files before making any changes:
- .cursor/rules/appdaemon-coding-guidelines.mdc
- .cursor/rules/logging-standards.mdc
- .cursor/rules/custom-card-guidelines.mdc
- .cursor/rules/security-policy.mdc

Work through all todos in the plan in order:
1. Add remaining_ttl_s field to BoardFrame
2. Update push() to calculate remaining_ttl_s on displacement
3. Update tick() to use remaining_ttl_s on re-promotion and calculate on displacement
4. Update controller status publishing to include remaining_ttl_s
5. Update configuration app to pass fallback_frames
6. Update card JS to render remaining TTL for fallback frames
7. Write tests for all new behavior

After completing all code changes, run the full test suite and fix any failures:

  source .venv/bin/activate && cd appdaemon && python -m pytest tests/test_vestaboard_frame_queue.py -v --tb=short

Then run the full suite to check for regressions:

  source .venv/bin/activate && cd appdaemon && python -m pytest tests/ -v --tb=short

DO NOT manually deploy to production. All changes stay in the dev environment until merged to main.
```

### Validation Agent

```text
You are a Validation Agent. Review the implementation described in the plan file at:

  .agents/plans/vestaboard-remaining-ttl.md

Read the full plan file — the "Validation checklist" section lists every requirement to verify.

Also read these rule files:
- .cursor/rules/appdaemon-coding-guidelines.mdc
- .cursor/rules/logging-standards.mdc
- .cursor/rules/custom-card-guidelines.mdc
- .cursor/rules/security-policy.mdc

DO NOT modify any files. Your job is to READ and VERIFY only.

Verify each checklist item by reading the relevant source files:
- appdaemon/apps/vestaboard_apps/_shared/frame_queue.py
- appdaemon/apps/vestaboard_apps/vestaboard_controller/vestaboard_controller_app.py
- appdaemon/apps/vestaboard_apps/vestaboard_configuration/vestaboard_configuration_app.py
- appdaemon/apps/vestaboard_apps/vestaboard_configuration/vestaboard-configuration-card.js
- appdaemon/tests/test_vestaboard_frame_queue.py

Run the full test suite and include the result in your report:

  source .venv/bin/activate && cd appdaemon && python -m pytest tests/ -v --tb=short

Output a PASS or FAIL verdict.

If FAIL, list every failing checklist item with:
  - File path and method/line where the issue is
  - What is wrong or missing
  - What the fix should be

Then produce a copy-pasteable prompt for the Implementation Agent in a fenced
```text``` block.
```

---

## Re-prompt template (for Validation Agent to use on FAIL)

```text
You are the Implementation Agent for the Vestaboard remaining TTL plan.

Validation Agent has completed a read-only validation pass. The following defects
were found that you must fix.

DEFECT 1

File: <path>

<What is wrong. What the fix should be.>

REQUIRED FIX

1. <First action>
2. <Second action>

Read the plan file at .agents/plans/vestaboard-remaining-ttl.md and rules before
making changes. Do not manually deploy to production.

Run the full test suite after your changes and confirm it passes:

  source .venv/bin/activate && cd appdaemon && python -m pytest tests/ -v --tb=short
```

---

## Final review (Planner)

After Validation returns PASS:
1. Re-read all modified files and compare to the plan
2. Run the full test suite
3. Code-review for edge cases, stale config drift, leftover artifacts
4. Fix any remaining issues directly
