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

## Metadata Cache Refresh (albums/people)

**Issue observed:** Newly created Immich albums did not appear in the fetcher card album dropdown until app restart. Manually typing the album name still worked.

**Root cause:** The backend caches (`people_available`, `albums_available`) were only refreshed on app startup, and the card dropdown reads those cached sensor attributes.

**Short-term fix implemented (backend only):**
- `immich_fetcher_app.py` now tracks metadata cache freshness and refreshes stale metadata before fetch runs (default every 30 minutes via `metadata_refresh_minutes`).
- This requires no frontend changes and keeps existing card behavior.
- Result: New albums/people appear automatically after the next scheduled/manual fetch that triggers a stale-cache refresh.

**Long-term robust plan (frontend + backend):**
- Add explicit relay commands (for example, `refresh_metadata`) that the card can call when the user opens album/people pickers or toggles into album/search modes.
- Optionally expose a `last_metadata_refresh` sensor attribute and a UI hint/button in the card so users can force a metadata refresh on demand.
- Keep the backend TTL refresh as a safety net even after event-driven refresh is added.

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

## Task 4b: Collapse expanded filters on save (fetcher card)

**Problem:** After editing filter settings and clicking "Save Configuration", all expanded filters stay open. This is jarring — the user just finished editing, and the open editors are stale (they reflect the now-saved state, not a work-in-progress). It also makes it easy to accidentally re-edit something without realizing changes were already persisted.

**Solution:** When the save action completes successfully, collapse all expanded filters.

**Implementation:**
1. In the save handler (the `save-config` action in `_dispatchAction`), after the save service call succeeds and `_dirty` is cleared, set `this._expandedFilter = -1` (or `null`, whatever sentinel the card uses for "none expanded").
2. Trigger a re-render so the filter list draws with all rows collapsed.
3. No additional `_markDirty()` — collapsing is a UI-only state change, not a config change.

**Caution:**
- If the save fails (network error, backend rejection), do **not** collapse — the user needs the editor open to retry or fix the issue.
- This pairs well with Task 4 Tier 1 (unsaved banner): save clears the banner **and** collapses filters in one gesture, giving clear visual feedback that the operation completed.

**Test:** Expand two filters, edit one, click Save. Verify both filters collapse after save. Verify that on a save failure (e.g., disconnect AppDaemon), filters remain expanded.

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

## Task 7: Thumbnail carousel (viewer controls card — settings popup)

**Problem:** The Previous / Next buttons on `photo-frame-viewer-card.js` (the controls card in the settings popup, above the fetcher card) are nearly useless because there's no visual context for what you're navigating to. Users end up using the main dashboard Bubble Card instead, because at least there they see the photo change. The controls card feels disconnected from the content.

**Solution:** Add a compact thumbnail carousel strip between the status bar and the Previous/Next buttons (or replace the button row entirely). The current photo is centered, with sequential neighbors fading out toward the edges. The strip must fit within the existing card footprint — no height increase.

**Design:**
- A single horizontal row of small square thumbnails (~40–48px), horizontally centered.
- The active photo is highlighted (brighter, subtle border or scale-up). Neighbors fade progressively with `opacity` (e.g., 1.0, 0.7, 0.4, 0.2).
- The strip wraps cyclically — if the current photo is near the start/end of the list, thumbnails wrap around.
- Tapping a thumbnail navigates directly to that photo (`input_select/select_option`).
- Previous/Next buttons can become thin arrow icons flanking the strip, or removed entirely if tap-to-navigate on thumbnails is sufficient.
- On narrow screens the strip should show fewer thumbnails (e.g., 5 instead of 7) via a `@media` breakpoint or dynamic JS calculation.

**Data source:**
- The `input_select` entity's `options` attribute provides the ordered list of photo labels (e.g., `photo_0001.jpg` through `photo_0020.jpg`).
- Thumbnail URLs can be constructed from the `ha_local_url_base` config + gen folder + label, with the `cache_bust` attribute appended. The sensor's `image_url` attribute already contains the pattern — the card can derive the base path from it.
- Alternatively, expose a `thumbnail_base_url` attribute on the sensor so the card doesn't have to parse URL patterns.

**Implementation notes:**
1. In `_render()`, read `options` from the picker entity and `image_url` from the sensor to derive the base URL path.
2. Render a `.thumbnail-strip` container with `display: flex; align-items: center; justify-content: center; gap: 4px; overflow: hidden;`.
3. Each thumbnail: `<img src="..." class="thumb ${isActive ? 'active' : ''}" data-action="select-photo" data-label="${label}" style="opacity: ${opacity}" />`.
4. Add `select-photo` to `dispatchAction`: `this._callService("input_select", "select_option", { entity_id: this._config.picker_entity, option: el.dataset.label })`.
5. Thumbnails should use `object-fit: cover; width: 44px; height: 44px; border-radius: 4px;` for uniform squares regardless of photo orientation.
6. Use `loading="lazy"` on non-visible thumbnails to avoid loading all 20 images at once.
7. Consider a CSS `scroll-snap` approach if the strip needs to be swipeable on touch devices.

**Caution:**
- Loading 20 thumbnail images may be heavy. Consider limiting the visible strip to ~7–9 thumbnails centered on the current index and only rendering those `<img>` tags.
- Append `cache_bust` to each thumbnail URL to avoid stale images after a batch swap.
- Follow custom-card-guidelines §2 for touch handling on the thumbnails.

**AppDaemon change (optional but recommended):**
- Add a `url_base` attribute to the status sensor (e.g., `/local/photo-frame/live/4/`) so the card can construct thumbnail URLs without parsing the full `image_url`. This decouples the card from the URL format.

**Test:** Verify on all three platforms. Verify the active thumbnail updates when the slideshow advances. Verify tapping a thumbnail navigates to that photo. Verify the strip doesn't increase the card's overall height.

---

## Task 8: Multi-location filters (backend + fetcher card)

**Problem:** A `PhotoFilter` currently supports a single `location: Optional[str]` field. When traveling, photos from a trip are often spread across many nearby towns (e.g., "Florence", "Fiesole", "Siena"). Creating a separate filter per town is tedious, and combining them requires a workaround like a `search` filter with a vague query.

**Solution:** Allow a filter to specify **multiple locations**, and allow a **LocationAlias** to map a friendly name to **multiple** `{city, state, country}` tuples (so a single alias like "Tuscany Trip" resolves to several towns).

### Backend changes

**`photo_providers/types.py` — `PhotoFilter`:**
- Change `location: Optional[str]` to `locations: Optional[List[str]]` (list of location names or alias keys).
- Keep backward compatibility: `from_dict` should accept both `"location": "Paris"` (legacy, wraps in a list) and `"locations": ["Paris", "Florence"]`.
- `to_dict` should always serialize as `"locations"`.
- `validate()` should reject empty strings in the list.

**`photo_providers/types.py` — `LocationAlias`:**
- Change from a single `{city, state, country}` to a **list of location specs**:
  ```python
  @dataclass
  class LocationAlias:
      """Maps a friendly name to one or more reverse-geocode locations."""
      specs: List[LocationSpec]  # each is {city?, state?, country?}

  @dataclass
  class LocationSpec:
      city: Optional[str] = None
      state: Optional[str] = None
      country: Optional[str] = None
  ```
- Keep backward compatibility in `from_dict`: if the raw dict has `city`/`state`/`country` at the top level (old format), wrap it into a single-element `specs` list.
- `to_dict` should always serialize the `specs` list.
- `validate()` should require at least one spec, and each spec must have at least one field.

**`photo_providers/immich_selectors.py` (or wherever location filtering happens):**
- When resolving locations for a filter: expand each entry in `locations` — if it matches a LocationAlias key, use all specs from that alias; otherwise treat it as a literal city name.
- Query Immich for assets matching **any** of the resolved locations (union/OR).
- Selection strategy for multi-location results: **random** by default (shuffle the combined pool). Optionally support `location_strategy: "round_robin"` on the filter to pull evenly from each location, but default to random for simplicity.

**`immich_fetcher/models.py` — `FetcherConfig`:**
- No structural change needed; `location_aliases` dict already exists. The values just change shape (list of specs instead of single spec).

### Card changes (`immich-fetcher-card.js`)

- **Filter editor location field:** Replace the single location text input with a tag/chip input that supports multiple locations. Each chip is a location name. Typing shows autocomplete from known alias names.
- **Location alias editor:** Update the alias editor to allow adding multiple `{city, state, country}` rows per alias. Each row has city/state/country fields. A "+" button adds another row. Show the count of locations in the alias badge.
- When saving config, serialize `locations` (list) and the new `LocationAlias` format.

### Migration

- On config load (`FetcherConfig.from_dict`), if a filter has the old `location` key (string), convert to `locations: [location]`.
- On alias load, if the raw dict has `city`/`state`/`country` at the top level, wrap into `specs: [{city, state, country}]`.
- The persisted config JSON will be updated to the new format on next save.

**Test:** Create a filter with `locations: ["Tuscany Trip"]` where "Tuscany Trip" is an alias mapping to `[{city: "Florence"}, {city: "Siena"}, {city: "Fiesole"}]`. Verify photos from all three cities appear. Test with a mix of aliases and literal city names. Test legacy single-location config loads correctly.

---

## Task 9: Location alias enhancements — grouping trips and regions (fetcher card)

**Problem (extends Task 8):** After Task 8 adds multi-spec LocationAliases, the card UI needs a good way to discover and create them. Currently the user has to know exact city names from Immich's reverse-geocode data. For trip grouping, the user wants to select from cities that appear in their photo library, not guess.

**Solution:**

### Backend: Location discovery endpoint

- Add a new relay command `get_locations` that returns all distinct `{city, state, country}` tuples from the user's Immich library (from the metadata cache, or a new Immich API call).
- The card can then show a picker of known locations when building an alias.

### Card: Alias builder UX

- When editing a LocationAlias, show a searchable list of all known locations (cities from the library).
- User can check multiple cities to add them to the alias.
- Group the location list by state/country for easier browsing (e.g., all Italian cities grouped under "Italy").
- Show a count badge on each alias in the alias list (e.g., "Tuscany Trip (3 locations)").

### Card: Quick "Create from filter results"

- After a fetch completes with location-based results, offer a shortcut: "Save these locations as an alias" that captures the distinct cities from the fetched photos into a new alias.

**Test:** Create an alias using the picker, verify all selected cities are included. Verify the alias badge shows the correct count. Test the quick-create flow after a location-based fetch.

---

## Known bugs

### Bug 1: Active filter indicator drifts after reorder (fetcher card)

**Severity:** Medium — confusing UX, but no data loss.

**Symptoms:** When moving a filter up or down with the arrow buttons, the play/active indicator does not follow the moved filter. It either stays on the original index (so it now points at a different filter) or jumps unexpectedly. Needs further testing to pin down the exact behavior — the active indicator may be tracked by array index rather than by filter identity.

**Likely cause:** The card tracks the "currently playing" filter by index (e.g., comparing against the backend's active filter index from the sensor). When `move-filter-up` / `move-filter-down` reorders `this._filters`, the index-to-filter mapping changes, but the active index from the sensor hasn't been updated yet (it reflects the saved order, not the unsaved reorder). This creates a mismatch between the displayed indicator and the actual active filter.

**Possible fix:**
- Track the active filter by **name** (or a stable identifier) rather than by array index.
- After a reorder, resolve the active filter's name back to its new index for display purposes.
- This bug will be partially mooted by Task 2 (drag-to-reorder replaces the arrow buttons), but the underlying index-vs-identity tracking issue should still be fixed since it could affect drag reorder too.

**Workaround:** Save after reordering — the backend updates its active index to match the new order, and the indicator corrects on the next render.

**Related:** Task 2 (drag-to-reorder) replaces the arrow buttons entirely, which may eliminate this bug as a side effect. Implement Task 2 first, then re-evaluate whether Bug 1 persists with the new reorder mechanism before investing in a separate fix.

---

## Recommended implementation order

Each task should be implemented, tested, and cache-busted independently before moving to the next:

| Order | Task | Card | Risk | Notes |
|-------|------|------|------|-------|
| 1 | Task 3: Move delete inside expanded filter | Fetcher | Low | Small, safe change. Test on all devices. |
| 2 | Task 1: Two-line filter row layout | Fetcher | Medium | Layout change; may affect touch targets. Test thoroughly on UniFi. |
| 3 | Task 4 Tier 1: Unsaved changes banner | Fetcher | Low | Additive, no regression risk. |
| 4 | Task 4b: Collapse filters on save | Fetcher | Low | Tiny change, pairs with Task 4 Tier 1. Do immediately after. |
| 5 | Task 7: Thumbnail carousel | Viewer (controls) | Medium | Independent. Enhances existing `photo-frame-viewer-card.js` in settings popup. Image loading perf needs care. |
| 6 | Task 2: Drag-to-reorder | Fetcher | High | Complex touch handling. Follow custom-card-guidelines §2 carefully. Only `preventDefault` on the drag handle. Test scrolling vs dragging. |
| 7 | Task 8: Multi-location filters | Both | Medium | Backend model changes + card UI. Do before Task 9. Backward-compatible migration required for existing configs. |
| 8 | Task 9: Location alias enhancements | Fetcher | Medium | Depends on Task 8. New location discovery endpoint + picker UI. |
| 9 | Task 5: Photo frame display card | Viewer (new) | High | New card. Replaces markdown card on main dashboard. Test aspect ratio behavior with many photo orientations. |
| 10 | Task 6: Heartbeat / offline detection | Both | Low | Touches both AppDaemon apps and both cards. Independent of other tasks. |
| 11 | Task 4 Tier 2: Cross-card dirty indicator | Both | Medium | Requires provisioner changes + coordination between cards. Do after Task 5. |
| 12 | Metadata Cache Refresh (albums/people) — long-term event-driven refresh | Fetcher | Low | Lower priority now that TTL-based backend refresh is in place. Add frontend-triggered metadata refresh + optional last-refresh indicator for robust on-demand updates. |

---

## Reminders for the implementing agent

- **Read `.cursor/rules/custom-card-guidelines.mdc` before starting.** It contains hard-won lessons about touch handling, `preventDefault`, input focus, and cache busting.
- **Update `custom-card-guidelines.mdc`** with any new findings (e.g., drag-to-reorder touch patterns, `aspect-ratio` CSS quirks on Android webviews).
- **Never auto-save** structural changes (add/remove/reorder). Always use `_markDirty()` per custom-card-guidelines §4.
- **Cache bust after every JS change** using MCP `ha_config_set_dashboard_resource`. Resource IDs are listed at the top of this document.
- **Card JS files** are copied manually to `/config/www/photo-frame/` on the HA server (not part of the Docker image deploy).
- **Test on the UniFi Connect Display** for every change involving touch or form inputs. It is the most fragile target.
