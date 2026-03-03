# Immich Fetcher & Photo Frame — Future Work

**Target cards:**
- `appdaemon/apps/immich_fetcher/immich-fetcher-card.js` (fetcher config card)
- `appdaemon/apps/photo_frame_viewer/photo-frame-viewer-card.js` (photo frame display card — needs major rework)

**Rules to follow:**
- `.cursor/rules/custom-card-guidelines.mdc` — **read before every change**. Update it with any new cross-device pitfalls discovered during implementation.
- `.cursor/rules/appdaemon-architecture.mdc` — relay script pattern, self-provisioning.
- `.cursor/rules/hass.mdc` — non-admin frontend rule, deployment, HA/AppDaemon container separation.

**Testing targets:**
- Desktop browser (Chrome/Edge)
- iOS Home Assistant Companion App (iPhone)
- UniFi Connect Display (Android webview — most fragile, see custom-card-guidelines §2)

**Cache busting after every card JS change:**
Use the MCP server tool `ha_config_set_dashboard_resource` to bump the `?v=N` query param. Resource IDs:
- Fetcher card: `a264f0c098c347fa96248062471a8a43` (path: `/local/photo-frame/immich-fetcher-card.js`)
- Viewer card: `a99ba776b907453787f021ab1ad572dd` (path: `/local/photo-frame/photo-frame-viewer-card.js`)

After bumping, instruct the user to hard-refresh (Ctrl+Shift+R / pull-to-refresh).

---

## Task 1: Filter row mobile layout (fetcher card)

**Problem:** Filter rows are a single cramped line. On mobile screens the name, badges, and 5+ icon buttons are unreadable and nearly impossible to tap.

**Solution:** Split each filter row into two lines:
- **Top line:** Status icon (play/alert), filter name, badge(s) — tappable to expand/collapse.
- **Bottom line:** Icon buttons (sync, reorder handle, expand chevron). The trash icon moves inside the expanded editor (see Task 3).

**Implementation notes:**
- Update the `.filter-header` layout in `_renderFilterList()`. Use `flex-wrap: wrap` or two explicit child divs (`.filter-header-top`, `.filter-header-bottom`).
- Use a `@media (max-width: 500px)` breakpoint if you want the two-line layout only on narrow screens, or always use two lines for consistency.
- Ensure `data-action` attributes and `data-idx` are preserved on all interactive elements.
- Test tap targets are at least 44×44px on mobile (Apple HIG / Android Material minimum).

**Test:** Verify on iOS and UniFi display that filter names are readable and all buttons are tappable without accidental hits on adjacent controls.

---

## Task 2: Drag-to-reorder filters (fetcher card)

**Problem:** Move-up / move-down buttons are disorienting — after moving a filter, your finger/cursor lands on a different filter.

**Solution:** Replace up/down buttons with drag-to-reorder using a drag handle icon (`mdi:drag` or the standard three-line grip `mdi:menu`).

**Implementation approach:**
1. Add a drag handle element to each filter row: `<ha-icon icon="mdi:drag" class="drag-handle" data-idx="${i}"></ha-icon>`
2. Implement touch-based drag reorder using `touchstart`/`touchmove`/`touchend` on the handle only:
   - On `touchstart` on a `.drag-handle`, record the starting index and Y position. Apply a `dragging` CSS class for visual feedback (e.g., slight lift/shadow).
   - On `touchmove`, calculate which filter slot the finger is over (by Y position relative to the filter list). Swap the dragged item in `this._filters` and re-render the list preview.
   - On `touchend`, finalize the new order and call `this._markDirty()` (do NOT auto-save — per custom-card-guidelines §4).
3. For desktop, implement the same with `mousedown`/`mousemove`/`mouseup` on the handle, or use `pointerdown`/`pointermove`/`pointerup` for a unified handler.
4. Remove the `move-filter-up` and `move-filter-down` `data-action` cases from `_dispatchAction`.

**Caution (custom-card-guidelines §2):**
- The drag listeners should be on the handle element only, not the entire row.
- Use `{ passive: false }` for the `touchmove` handler and call `e.preventDefault()` to prevent page scrolling while dragging. This is the ONE place where `preventDefault` on a touch event is correct — but only on the drag handle, never on form inputs.

**Test:** Verify drag works on all three platforms. Verify scrolling the card still works when not dragging. Verify order changes are not saved until "Save Configuration" is clicked.

---

## Task 3: Move delete button inside expanded filter (fetcher card)

**Problem:** The trash icon is on the filter row header, making it easy to tap accidentally — especially on mobile.

**Solution:** Remove the delete button from the filter header row. Add a "Delete Filter" button at the bottom of the expanded filter editor panel, styled as a danger action.

**Implementation:**
1. Remove the `<button class="btn-icon btn-danger" data-action="remove-filter" ...>` from the `.filter-header-right` div.
2. Add to the bottom of `_renderFilterEditor()`:
   ```html
   <div class="filter-delete-section">
     <button class="btn-text btn-danger" data-action="remove-filter" data-idx="${idx}">
       <ha-icon icon="mdi:delete"></ha-icon> Delete Filter
     </button>
   </div>
   ```
3. Style `.filter-delete-section` with `margin-top: 16px; border-top: 1px solid var(--divider-color); padding-top: 12px; text-align: right;`.
4. The `_dispatchAction` "remove-filter" case already exists and calls `_markDirty()` — no change needed there.

**Test:** Verify the delete button only appears when a filter is expanded. Verify it requires "Save Configuration" to persist (already implemented via `_markDirty`).

---

## Task 4: Unsaved changes indicator (fetcher card)

**Problem:** Users forget to click "Save Configuration" before closing the popup. Changes are silently lost.

**Solution (two tiers):**

### Tier 1 (easy): Visual annotation on the Save button
When `this._dirty` is true, add a pulsing dot or highlighted border to the "Save Configuration" button and a small banner above it:
```html
${this._dirty ? '<div class="unsaved-banner">⚠ You have unsaved changes</div>' : ""}
```
Style the banner with `color: var(--warning-color); font-size: 0.85em; text-align: center; padding: 4px;`.

### Tier 2 (harder): Feedback when closing the popup
The fetcher card lives inside a Bubble Card popup. Intercepting the popup close is difficult because the popup is a separate custom element. Two options:
- **Option A:** Use `beforeunload`-style detection — not available in shadow DOM / HA frontend.
- **Option B:** Surface the dirty state as an HA entity attribute on `sensor.immich_fetcher_status`. The Bubble Card popup button (or a photo frame viewer card) could then show a visual indicator (e.g., a dot on the settings gear icon) when unsaved changes exist. This requires:
  1. Card sets `this._hass.callService("input_boolean", "turn_on", { entity_id: "input_boolean.immich_fetcher_unsaved" })` when dirty, and `turn_off` when saved or reloaded.
  2. The photo frame viewer card (Task 5) reads this entity and shows an indicator on the settings icon.
  3. The `input_boolean` should be provisioned by `immich_fetcher_app.py` via `ha_provisioner`.

**Recommendation:** Implement Tier 1 first. Tier 2 can come after Task 5.

---

## Task 5: Custom photo frame viewer card (viewer card)

**Problem:** The current photo frame display uses a markdown card with a raw `<img>` tag. This causes the card to resize dynamically based on photo aspect ratio (portrait vs landscape), which makes the entire dashboard shift constantly.

**Files:**
- Card JS: `appdaemon/apps/photo_frame_viewer/photo-frame-viewer-card.js`
- Dashboard card YAML: `home-assistant/cards/global/photo-frame-viewer/wall-display-photo-frame-viewer.yaml`
- Resource ID for cache bust: `a99ba776b907453787f021ab1ad572dd`

**Current viewer card** (`photo-frame-viewer-card.js`) is a settings/controls card. This task creates a NEW display card or replaces the markdown card with a proper custom card that includes both the image display and the controls.

**Requirements:**
1. **Fixed-size container:** The card must reserve a fixed height regardless of image aspect ratio. Images are displayed inside this container using `object-fit: contain` so they fit without cropping or stretching. The container background should be a dark/neutral color (e.g., `var(--card-background-color)` or `#1a1a2e`).
2. **Resizable via card config:** The user sets `aspect_ratio` (e.g., `16:9`, `4:3`, `3:4`) or `height` (e.g., `400px`, `60vh`) in the card YAML config. The card uses this to set the container size. Default to `4:3` if not specified.
3. **Controls overlay or header bar:** Previous, Pause/Play, Next buttons and the image picker dropdown. These can be a header bar above the image (like the current Bubble Card row) or an overlay that appears on hover/tap.
4. **Settings gear:** A gear icon that navigates to `#photo-frame-settings` (the popup with the fetcher config card). If Task 4 Tier 2 is implemented, show an unsaved-changes dot on this icon.
5. **Image display:**
   - Read `input_text.wall_display_photo_frame_image_local_url` for the image URL.
   - Read `input_text.wall_display_photo_frame_cache_bust` and append `?cb=<value>` to the URL.
   - Use `<img>` with `object-fit: contain; width: 100%; height: 100%;` inside the fixed container.
6. **Slideshow controls:**
   - Previous/Next: call `input_select.select_previous` / `input_select.select_next` with `cycle: true`.
   - Pause/Play: call `input_boolean.toggle` on the paused entity.
   - These use `hass.callService()` directly (not relay scripts) since they target standard HA helpers.

**Implementation approach:**
1. Start from the existing `photo-frame-viewer-card.js` skeleton — it already has entity constants, `_callService`, snapshot-based rendering.
2. Replace its current controls-only render with a combined image + controls layout.
3. Add the fixed container with `aspect-ratio` CSS property (well-supported in modern browsers):
   ```css
   .frame-container {
     position: relative;
     width: 100%;
     aspect-ratio: var(--pfv-aspect-ratio, 4/3);
     background: var(--pfv-bg, #1a1a2e);
     overflow: hidden;
     border-radius: var(--ha-card-border-radius, 12px);
   }
   .frame-container img {
     width: 100%;
     height: 100%;
     object-fit: contain;
   }
   ```
4. Read `aspect_ratio` from `this._config` in `setConfig()` and set `--pfv-aspect-ratio` as a CSS variable.
5. Add touch/click event delegation following custom-card-guidelines §2.
6. Update `wall-display-photo-frame-viewer.yaml` to use the new card type and remove the markdown card.

**Test:** Verify portrait and landscape photos both display without the card changing size. Verify controls work on all three platforms. Verify the settings gear navigates to the popup.

---

## Task 6: Heartbeat / offline detection (both cards)

**Problem:** When AppDaemon goes down, the sensor entities (`sensor.wall_display_photo_frame_status`, `sensor.immich_fetcher_status`) keep their last-known state in HA indefinitely. The frontend cards have no way to know the backend is gone — they keep showing "Paused" or "Idle" instead of "Offline".

**Solution:** Add a `heartbeat` attribute (Unix timestamp) to each AppDaemon app's status sensor. Each app periodically updates it via `run_every` (e.g., every 30 seconds). The card JS compares `heartbeat` to `Date.now()` — if stale beyond a threshold (e.g., 90 seconds), display an "Offline" or "App Unavailable" status with a distinct icon/color.

**AppDaemon changes (both apps):**
1. In `initialize()`, register a periodic heartbeat: `self.run_every(self._heartbeat_tick, "now", 30)`
2. `_heartbeat_tick` updates the sensor attribute: include `heartbeat: time.time()` in the `set_state()` attributes dict.
3. Since both apps already call `set_state()` for other reasons, the heartbeat attribute can be merged into existing `set_state()` calls. Add it as a standard attribute alongside `paused`, `image_url`, etc.

**Card JS changes (both cards):**
1. In `_render()`, read the `heartbeat` attribute from the sensor.
2. Compare `heartbeat * 1000` to `Date.now()`. If the difference exceeds a staleness threshold (e.g., 90000ms), override the status display:
   - Icon: `mdi:connection` or `mdi:alert-circle-outline`
   - Label: "Offline" or "App Unavailable"
   - Color: `var(--error-color, #f44336)`
3. Optionally disable action buttons (pause, sync, etc.) when offline — they won't be processed anyway.

**Files:**
- `appdaemon/apps/photo_frame_viewer/photo_frame_viewer_app.py` — add heartbeat attribute
- `appdaemon/apps/immich_fetcher/immich_fetcher_app.py` — add heartbeat attribute
- `appdaemon/apps/photo_frame_viewer/photo-frame-viewer-card.js` — read heartbeat, show offline
- `appdaemon/apps/immich_fetcher/immich-fetcher-card.js` — read heartbeat, show offline

**Test:** Stop AppDaemon while both cards are visible. After ~90 seconds, both cards should switch to "Offline". Restart AppDaemon — cards should recover to normal status within one heartbeat interval (30s).

---

## Recommended implementation order

Each task should be implemented, tested, and cache-busted independently before moving to the next:

| Order | Task | Card | Risk | Notes |
|-------|------|------|------|-------|
| 1 | Task 3: Move delete inside expanded filter | Fetcher | Low | Small, safe change. Test on all devices. |
| 2 | Task 1: Two-line filter row layout | Fetcher | Medium | Layout change; may affect touch targets. Test thoroughly on UniFi. |
| 3 | Task 4 Tier 1: Unsaved changes banner | Fetcher | Low | Additive, no regression risk. |
| 4 | Task 2: Drag-to-reorder | Fetcher | High | Complex touch handling. Follow custom-card-guidelines §2 carefully. Only `preventDefault` on the drag handle. Test scrolling vs dragging. |
| 5 | Task 5: Photo frame viewer card | Viewer | High | New card. Replaces markdown card on dashboard. Test aspect ratio behavior with many photo orientations. |
| 6 | Task 6: Heartbeat / offline detection | Both | Low | Touches both AppDaemon apps and both cards. Do before or after Task 5. |
| 7 | Task 4 Tier 2: Cross-card dirty indicator | Both | Medium | Requires provisioner changes + coordination between cards. Do after Task 5. |

---

## Reminders for the implementing agent

- **Read `.cursor/rules/custom-card-guidelines.mdc` before starting.** It contains hard-won lessons about touch handling, `preventDefault`, input focus, and cache busting.
- **Update `custom-card-guidelines.mdc`** with any new findings (e.g., drag-to-reorder touch patterns, `aspect-ratio` CSS quirks on Android webviews).
- **Never auto-save** structural changes (add/remove/reorder). Always use `_markDirty()` per custom-card-guidelines §4.
- **Cache bust after every JS change** using MCP `ha_config_set_dashboard_resource`. Resource IDs are listed at the top of this document.
- **Do not run `deploy.py`** unless the user explicitly asks. Card JS files are copied manually to `/config/www/photo-frame/` on the HA server.
- **Test on the UniFi Connect Display** for every change involving touch or form inputs. It is the most fragile target.
