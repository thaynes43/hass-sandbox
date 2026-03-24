# Vestaboard Library & UI Improvements Plan

## Context

The Vestaboard card has two libraries (art and message) backed by the `category` field on `LibraryFrame`. Several UX flows are broken or missing around saving, viewing, and categorizing frames. The editor's Text mode also needs layout rebalancing.

---

## Bug Fix: "Generating... please wait" never clears

**Problem**: When the user clicks "Generate" for AI art, the card sets a generating flag and shows "Generating... please wait". The backend generates successfully and pushes the frame to the board, but the card never clears the flag.

**Root cause**: The card doesn't detect when generation completes. The controller publishes status after pushing the frame, but the card's `set hass()` handler doesn't check for a new `displayed_frame` from `art_generated_by_ai` to clear the generating state.

**Fix (JS card)**:
- In `set hass()`, when status sensor updates, check if `displayed_frame.source === "art_generated_by_ai"` (or whichever source matches the pending generation).
- Clear the generating flag and show the generated art with a "Save to Library" option.
- Also handle error case: if the controller publishes an error or the frame never arrives within a timeout (e.g., 60s), clear the flag and show an error message.

---

## Feature: AI Art → Preview → Save to Library

**Current flow**: Generate → push to board → no save option.

**Desired flow**:
1. User enters subject, clicks "Generate"
2. Card shows "Generating..." spinner
3. Backend generates art, pushes to board, publishes status
4. Card detects completion, clears spinner, shows the generated art preview
5. Card shows "Save to Art Library" button below the preview
6. User clicks save → card sends `save_art_to_library` command with frame data, name (defaulting to subject), and creator
7. After saving, frame appears in the Art library tab

**Backend**: Already supports `save_art_to_library` command — no backend changes needed.

**Frontend changes** (JS card):
- After generation completes, render a preview grid of the generated art
- Show "Save to Art Library" button that sends `save_art_to_library` with `{ frame, name: subject, creator: selectedCreator }`
- Optionally allow user to edit the name before saving

---

## Feature: Editor saves to correct library based on mode

**Current behavior**: The editor's "Save to Library" always sends `save_frame` which saves with `category="message"`.

**Desired behavior**:
- **Paint mode** → save to Art library (`category="art"`)
- **Text mode** → save to Messages library (`category="message"`)

**Backend change**:
- `_cmd_save_frame` should read `category` from the payload: `category = str(payload.get("category", "message"))`
- Pass it through to `LibraryFrame` constructor

**Frontend change** (JS card):
- When saving from Paint tab, include `category: "art"` in the save_frame payload
- When saving from Text tab, include `category: "message"` (or omit, since it's the default)

---

## Feature: Move items between Art and Messages libraries

**Desired behavior**: Each library entry gets a "Move to Art" or "Move to Messages" action (depending on current library).

**Backend**:
- `update_frame` already supports updating arbitrary fields including `category`
- Card just needs to send `update_frame` with `{ frame_id, category: "art" }` or `{ frame_id, category: "message" }`

**Frontend change** (JS card):
- Add a "Move to Art" / "Move to Messages" link/button on each library card
- On click, send `update_frame` command with the new category
- Refresh the library view after the status sensor updates

---

## UI Fix: Text editor grid too large

**Problem**: In Text mode, the 6×22 preview grid takes up the same space as in Paint mode, leaving the text input area cramped. The grid is far too large relative to the text box.

**Desired behavior**:
- **Paint mode**: Grid stays at current size (optimized for finger-painting on touchscreens — do NOT change)
- **Text mode**: Grid is smaller (read-only preview), text input area gets more vertical space

**Implementation approach** (JS card):
- When in Text mode, apply a CSS class to the grid container that reduces its size (e.g., `max-height: 150px` or scale it down with a wrapper)
- Give the text input/textarea more vertical space
- The grid in Text mode is read-only (shows the text preview) so it doesn't need to be finger-tap-sized
- Consider using CSS like:
  ```css
  .editor-grid.text-mode {
    /* Shrink the preview grid in text mode */
    max-height: 160px;
  }
  .editor-grid.text-mode .vb-cell {
    /* Smaller cells since they're not interactive */
    width: 12px;
    height: 12px;
  }
  ```
- Make sure the Paint mode grid is completely unaffected — use separate CSS classes or mode-specific styling

**Key constraint**: The Paint grid cell size is optimized for touchscreen finger painting. Do not change Paint mode grid dimensions.

---

## Implementation Order

1. **Bug fix**: Clear "Generating..." state (JS card only)
2. **AI Art save flow**: Preview + "Save to Art Library" button (JS card only)
3. **Editor category routing**: Paint→art, Text→message (JS card + 1-line backend change)
4. **Move between libraries**: Add move action to library cards (JS card only, backend already supports)
5. **Text editor layout**: Rebalance grid vs text input sizing (JS card CSS only)

## Files to modify

- `appdaemon/apps/vestaboard_configuration_app/vestaboard-configuration-card.js` — all frontend changes
- `appdaemon/apps/vestaboard_configuration_app/vestaboard_configuration_app.py` — read `category` from `save_frame` payload (1 line)
- Deploy card JS to HA pod and bump `?v=N` per cache-busting playbook
