# Plan: School Lunch Menu Display Feature

## Overview

Add a school lunch menu display to the wall-display Home Assistant dashboard. The system fetches daily lunch menus from the School Nutrition and Fitness API for multiple schools, displays tomorrow's entrees at a glance, and provides a detail popup with weekly view, full month calendars per school, and a settings tab.

### Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Wall Display Browser                                           │
│  ┌──────────────────────┐  ┌──────────────────────────────────┐ │
│  │ school-lunch-card.js │  │ school-lunch-detail-card.js      │ │
│  │ (at-a-glance)        │  │ (popup: week, month, settings)   │ │
│  │ Shows tomorrow's     │  │ Tabs: This Week | per-school     │ │
│  │ entrees for selected │  │ month calendar | Settings        │ │
│  │ schools. Tap → popup │  │                                  │ │
│  └──────────┬───────────┘  └──────────┬───────────────────────┘ │
│             │ read state              │ callService (relay)      │
└─────────────┼─────────────────────────┼─────────────────────────┘
              ▼                         ▼
┌─────────────────────────────────────────────────────────────────┐
│  Home Assistant                                                  │
│  sensor.school_lunch_menu          (JSON attributes)             │
│  input_text.school_lunch_selected_schools (JSON array)           │
│  script.school_lunch_relay → fires school_lunch_command event    │
└──────────────────────────────────┬──────────────────────────────┘
                                   │ listen_event
                                   ▼
┌─────────────────────────────────────────────────────────────────┐
│  AppDaemon                                                       │
│  school_lunch_app.py                                             │
│  - Resolves download IDs → MongoDB IDs on startup                │
│  - Fetches current month menus for ALL configured schools daily  │
│  - Handles relay commands: select_schools, fetch_month           │
│  - Uses providers/school_menu/client.py (HTTP in provider layer) │
└─────────────────────────────────────────────────────────────────┘
```

### Constraints

- **DO NOT manually deploy to production.** All changes stay on `feature/school-lunch` branch.
- **All external HTTP calls are in `providers/school_menu/`** — the app layer must NOT make direct HTTP requests (security rule S2).
- **No credentials in app code** — `ha_token_env` passes env var name only (S1).
- **Provider layer is already built** — do not modify `appdaemon/providers/school_menu/`.
- **Cards must work on Android/UniFi wall displays, iOS, and desktop** — follow all custom card rules.

### Branch

All work is on `feature/school-lunch` (already checked out).

### Version

Current `VERSION` is `0.4.4`. Bump to `0.5.0` (minor — new feature).

---

## Implementation Details

### Track A: AppDaemon App (Agent 1)

**Files to create:**
- `appdaemon/apps/school_lunch_app/__init__.py` (empty)
- `appdaemon/apps/school_lunch_app/school_lunch_app.py`
- `appdaemon/apps/school_lunch_app/README.md`
- `appdaemon/tests/test_school_lunch_app.py`

**Files to modify:**
- `appdaemon/apps/apps-prod.yaml` — add `school_lunch_app` entry
- `appdaemon/apps/apps-dev.yaml` — add `school_lunch_app_dev` entry

#### App module: `school_lunch_app.py`

Class: `SchoolLunchApp(hass.Hass)`

**Imports and sys.path fix:**
```python
import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parents[2]))
```

**Constants:**
```python
SENSOR_ENTITY_ID = "sensor.school_lunch_menu"
SELECTION_ENTITY_ID = "input_text.school_lunch_selected_schools"
```

**`initialize()` method:**
- Store config from `self.args`: `sid`, `menus` list, `default_selected`
- Call `self.run_in(self._on_startup, 0)` for async startup

**`_on_startup` / `_async_startup` pattern** (same as immich_fetcher):
1. Provision entities via `ha_provisioner`:
   - `input_text` helper named "School Lunch Selected Schools" with `max=255`, `initial=json.dumps(default_selected)`
   - Relay script `school_lunch_relay` firing `school_lunch_command` event
2. Create `SchoolMenuClient` with `sid` from config
3. Resolve all download IDs to MongoDB IDs (call `client.resolve_menu_id()` for each menu in config)
4. Store resolved IDs in `self._resolved_menus` dict: `{name: {download_id, mongo_id, site_code}}`
5. Fetch current month menus for ALL configured schools
6. Publish initial sensor state via `self.set_state()`
7. Register event listener: `self.listen_event(self._on_command, "school_lunch_command")`
8. Schedule daily fetch: `self.run_daily(self._daily_fetch, datetime.time(5, 0))` — 5 AM refresh
9. Log summary of resolved menus and initial fetch results

**Sensor state format** (published via `set_state`):
- State value: `"ok"` or `"error"`
- Attributes:
```python
{
    "schools": [
        {
            "name": "Elementary",
            "month": 3,       # 1-indexed display month
            "year": 2026,
            "days": [
                {
                    "day": 3,
                    "items": [
                        {"name": "Chicken Nuggets", "category": "Entrees", "is_ancillary": False},
                        {"name": "Milk Choice", "category": "Milk", "is_ancillary": True}
                    ]
                }
            ],
            "prev_month_id": "abc123",
            "next_month_id": "def456"
        }
    ],
    "last_updated": "2026-03-15T10:00:00"
}
```

**IMPORTANT:** Use `MenuMonth.display_month` (1-indexed) when building the sensor attributes, not the raw 0-indexed `month` field.

**`_daily_fetch` method:**
- Fetch current month for ALL configured schools (even unchecked ones)
- On success, update sensor state
- On failure, log error, set state to `"error"`, keep last good data in attributes

**`_on_command` handler** — route by `command` field:
- `"select_schools"` — payload has `{"schools": ["Elementary", "High School"]}`. Validate at least 1 school. Update `input_text.school_lunch_selected_schools` via `self.call_service("input_text/set_value", entity_id=SELECTION_ENTITY_ID, value=json.dumps(schools))`. Log the change.
- `"fetch_month"` — payload has `{"school": "Elementary", "menu_id": "abc123"}`. Fetch that specific month via `client.fetch_menu(menu_id)`. Update only that school's data in the sensor attributes. This supports prev/next month navigation in the detail card.
- Unknown commands: log warning.

**`_fetch_all_menus` async method:**
- For each resolved menu, call `client.fetch_menu(mongo_id)`
- Build the schools list for sensor attributes
- Return the list (caller publishes to sensor)
- Each fetch should be wrapped in try/except — log errors per school but don't fail the whole batch

**`_publish_sensor` method:**
- Call `self.set_state(SENSOR_ENTITY_ID, state="ok", attributes={...})`
- The `set_state` call with `namespace="default"` is implicit

**Error handling:**
- If resolve_menu_id fails for a school during startup, log ERROR and skip that school (others should still work)
- If fetch_menu fails during daily fetch, log ERROR per school, keep last good data for that school
- If all fetches fail, set sensor state to `"error"` but keep stale attributes

**Logging requirements:**
- INFO: startup complete with school count, daily fetch started/completed, school selection changed
- WARNING: unknown command, fetch failure for individual school
- ERROR: all fetches failed, resolve_menu_id failure, provisioning failure
- DEBUG: individual menu fetch details, payload parsing

#### App config entries

**`apps-prod.yaml`** — add at end of file:
```yaml
school_lunch_app:
  module: school_lunch_app.school_lunch_app
  class: SchoolLunchApp
  disable: true
  ha_url: !secret ha_url
  ha_token_env: TOKEN
  sid: "0802121850414637"
  menus:
    - name: "Elementary"
      download_id: "853700"
    - name: "Middle School"
      download_id: "854234"
    - name: "High School"
      download_id: "854323"
  default_selected:
    - "Elementary"
    - "Middle School"
```

**`apps-dev.yaml`** — add:
```yaml
school_lunch_app_dev:
  module: school_lunch_app.school_lunch_app
  class: SchoolLunchApp
  ha_url: !secret ha_url
  ha_token_env: TOKEN
  sid: "0802121850414637"
  menus:
    - name: "Elementary"
      download_id: "853700"
    - name: "Middle School"
      download_id: "854234"
    - name: "High School"
      download_id: "854323"
  default_selected:
    - "Elementary"
    - "Middle School"
```

#### Unit tests: `test_school_lunch_app.py`

Follow the pattern from `test_immich_fetcher.py`:
- Mock `hassapi` before importing app
- Add `sys.path` fixes for `apps/` and repo root
- Mock `HAProvisioner` with `AsyncMock`
- Mock `SchoolMenuClient` methods

**Test cases:**

| Test | What it verifies |
|------|------------------|
| `test_initialize_calls_startup` | `initialize()` registers `run_in` callback |
| `test_provision_entities` | Startup calls `ensure_helper` for input_text and `ensure_script` for relay |
| `test_resolve_menu_ids_on_startup` | Startup calls `resolve_menu_id` for each configured menu |
| `test_resolve_partial_failure` | If one school fails to resolve, others still work |
| `test_fetch_all_menus` | Fetches menu for each resolved school, publishes sensor |
| `test_fetch_partial_failure` | If one school fetch fails, others still publish |
| `test_sensor_state_format` | Published sensor state has correct structure (schools list, last_updated) |
| `test_sensor_uses_display_month` | Month values in sensor attributes are 1-indexed |
| `test_command_select_schools` | `select_schools` command updates input_text entity |
| `test_command_select_schools_min_one` | Rejects empty school list |
| `test_command_fetch_month` | `fetch_month` command fetches specific month and updates sensor |
| `test_command_unknown` | Unknown command logs warning |
| `test_daily_fetch_scheduled` | `run_daily` is called during startup |
| `test_daily_fetch_updates_sensor` | Daily fetch refreshes all school menus |

#### README.md

Follow the template from `.cursor/rules/appdaemon-documentation.mdc`. Include:
- What the app does
- Config reference (sid, menus, default_selected, ha_url, ha_token_env)
- Provisioned entities (sensor, input_text, relay script)
- Manual prerequisites: Lovelace resource registration for both card JS files
- Relay commands: select_schools, fetch_month
- Sensor attribute schema

---

### Track B: At-a-Glance Card (Agent 2)

**File to create:**
- `appdaemon/apps/school_lunch_app/school-lunch-card.js`

**Reference files to read (read-only):**
- `appdaemon/apps/photo_frame_viewer/photo-display-card.js` — pattern for compact display card with navigation
- `.claude/rules/custom-cards.md` — mandatory patterns
- `.cursor/rules/custom-card-guidelines.mdc` — full detail

**Depends on:** Track A (sensor entity structure must be finalized first — the sensor attribute schema above is the contract)

#### Card behavior

**Display logic:**
1. Read `sensor.school_lunch_menu` attributes → `schools` array
2. Read `input_text.school_lunch_selected_schools` state → JSON array of selected school names
3. Filter schools to only selected ones
4. Determine "target day":
   - If current day is Friday after 3 PM, Saturday, or Sunday → show Monday's menu
   - Otherwise → show tomorrow's menu (next calendar day)
   - If target day has no menu data (e.g., holiday) → show "No menu" message
5. For each selected school, show the target day's **entree items only**:
   - Filter to items where `category` contains "Entree" (case-insensitive)
   - Also exclude items where `is_ancillary` is true
   - Show school name as header, entree names as bullet list
6. Tapping anywhere navigates to `config.navigation_path` (default `#school-lunch-popup`)

**Card registration:**
```javascript
customElements.define("school-lunch-card", SchoolLunchCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: "school-lunch-card",
  name: "School Lunch Card",
  description: "At-a-glance school lunch menu for tomorrow",
});
```

**Config properties:**
- `status_entity` — default `"sensor.school_lunch_menu"`
- `selection_entity` — default `"input_text.school_lunch_selected_schools"`
- `navigation_path` — default `"#school-lunch-popup"`

**Mandatory patterns (from custom-cards.md):**
- Shadow DOM with `attachShadow({ mode: "open" })`
- `setConfig(config)` and `set hass(hass)` methods
- Snapshot-based re-render optimization (like photo-display-card.js `_snapshot()`)
- Touch/click deduplication: `touchend` handler calls `preventDefault()`, sets 400ms `_touchActive` flag; `click` handler gates on `if (this._touchActive) return`
- Focus guard: skip re-render if `shadowRoot.activeElement` exists (not applicable here since no inputs, but include the check for consistency)

**Styling:**
- Compact card suitable for wall display column
- School name in bold, entree items below
- "Tomorrow's Lunch" or "Monday's Lunch" as header based on target day logic
- Subtle food-related icon (mdi:silverware-fork-knife or similar via HA icon font)
- Match the visual density of other wall-display cards

---

### Track C: Detail Card (Agent 3)

**File to create:**
- `appdaemon/apps/school_lunch_app/school-lunch-detail-card.js`

**Reference files to read (read-only):**
- `appdaemon/apps/immich_fetcher/immich-fetcher-card.js` — pattern for tabbed UI with settings
- `appdaemon/apps/photo_frame_viewer/photo-frame-viewer-card.js` — pattern for relay calls and complex state
- `.claude/rules/custom-cards.md` — mandatory patterns
- `.cursor/rules/custom-card-guidelines.mdc` — full detail

**Depends on:** Track A (sensor entity structure)

#### Card behavior

**Tab structure:**
1. **"This Week"** tab (default):
   - 5-column grid: Mon, Tue, Wed, Thu, Fri
   - One row per selected school
   - Each cell shows the day's entree items (filtered same as at-a-glance card)
   - Tomorrow's column highlighted with a subtle accent background
   - If today is weekend, highlight Monday

2. **Per-school tabs** (one per school in the `schools` array from sensor, not just selected):
   - Tab label = school name (e.g., "Elementary")
   - Full month calendar grid: 5 columns (Mon-Fri), rows = weeks of the month
   - Current week row highlighted with subtle background
   - Day number in corner of each cell, menu items listed
   - Filter to entree items (same filtering as at-a-glance)
   - Prev/next month navigation arrows at top
   - Clicking prev/next sends `fetch_month` relay command:
     ```javascript
     this._callRelay("fetch_month", {
       school: schoolName,
       menu_id: school.prev_month_id  // or next_month_id
     });
     ```

3. **"Settings"** tab:
   - Checkbox for each school in the `schools` array
   - Checked = selected (from `input_text.school_lunch_selected_schools`)
   - Changing selection sends `select_schools` relay command:
     ```javascript
     this._callRelay("select_schools", { schools: selectedSchoolNames });
     ```
   - Enforce minimum 1 selected (disable last remaining checkbox or show validation message)
   - Last updated timestamp from sensor attributes

**Config properties:**
- `status_entity` — default `"sensor.school_lunch_menu"`
- `selection_entity` — default `"input_text.school_lunch_selected_schools"`
- `relay_script` — default `"school_lunch_relay"`

**`_callRelay` method:**
```javascript
_callRelay(command, data) {
  if (!this._hass) return;
  this._hass.callService("script", this._config.relay_script, {
    command,
    payload: JSON.stringify(data || {}),
  }).catch((err) => {
    console.warn("school-lunch-detail-card: relay failed", command, err);
  });
}
```

**Mandatory patterns (from custom-cards.md):**
- Shadow DOM, `setConfig()`, `set hass()`
- Snapshot-based re-render optimization
- Touch/click deduplication (400ms flag)
- **Critical**: Never `preventDefault()` on `touchend` when target is `<input>`, `<select>`, `<textarea>` — the Settings tab has checkboxes
- Focus guard: skip re-render if `shadowRoot.activeElement` (protects checkbox interaction)
- Card registration with `customElements.define()` + `window.customCards.push()`

**Calendar grid construction:**
- Given month/year, compute which weeks contain days of the month
- Only show Mon-Fri columns (skip weekends)
- Each week is a row
- Days outside the month are empty cells
- Match menu days to calendar cells by day number

**Styling:**
- Tab bar at top with horizontal scrolling if needed
- Active tab has accent underline/background
- Calendar grid with subtle borders
- Current week row: light accent background (e.g., rgba with 0.1 alpha)
- Tomorrow column in "This Week": similar accent
- Responsive text sizing for wall display readability

---

### Track D: Dashboard Config + Version Bump (Agent 4)

**Files to modify:**
- `VERSION` — bump to `0.5.0`

**MCP operations (no file changes, live HA updates):**
- Register both JS files as Lovelace dashboard resources
- Add `school-lunch-card` to wall-display dashboard column 2
- Add popup section with `school-lunch-detail-card`

**Depends on:** Tracks A, B, C all complete

#### Dashboard resource registration

Register two Lovelace resources via MCP `ha_config_set_dashboard_resource` (or `ha_config_set_inline_dashboard_resource`):
1. `school-lunch-card.js` — URL will be wherever the card JS is served from (likely `/hacsfiles/` or `/local/`)
2. `school-lunch-detail-card.js` — same

Note: Since the JS files live in the AppDaemon app directory (not in `/config/www/`), they need to be served somehow. Check how existing cards (photo-display-card.js, immich-fetcher-card.js) are registered and follow the same pattern. They may be copied to HA via a deploy step or served from a different path.

#### Wall-display dashboard updates

Use MCP `ha_config_get_dashboard` to read the current wall-display dashboard, then `ha_config_set_dashboard` to update it:

1. **Add at-a-glance card** to column 2 (after cruise countdown card):
   ```yaml
   type: custom:school-lunch-card
   status_entity: sensor.school_lunch_menu
   selection_entity: input_text.school_lunch_selected_schools
   navigation_path: "#school-lunch-popup"
   ```

2. **Add popup section** (section index 3 or wherever other popups live):
   ```yaml
   - type: custom:bubble-card
     card_type: pop-up
     hash: "#school-lunch-popup"
     name: School Lunch Menu
     icon: mdi:silverware-fork-knife
   - type: custom:school-lunch-detail-card
     status_entity: sensor.school_lunch_menu
     selection_entity: input_text.school_lunch_selected_schools
     relay_script: school_lunch_relay
   ```

**CRITICAL**: Read the dashboard FIRST to get the `config_hash`. The `ha_config_set_dashboard` call will fail without the current hash (optimistic concurrency).

#### Version bump

Edit `VERSION` file at repo root (`/home/thaynes/workspace/hass-sandbox/VERSION`):
- Change `0.4.4` to `0.5.0`

---

## Parallelism Analysis

| Todo | Files touched | Dependencies | Track |
|------|---------------|-------------|-------|
| AppDaemon app + tests + README | `apps/school_lunch_app/*`, `apps-prod.yaml`, `apps-dev.yaml`, `tests/test_school_lunch_app.py` | none | A |
| At-a-glance card | `apps/school_lunch_app/school-lunch-card.js` | Track A (sensor schema) | B |
| Detail card | `apps/school_lunch_app/school-lunch-detail-card.js` | Track A (sensor schema) | C |
| Dashboard config + version | `VERSION`, MCP operations | Tracks A, B, C | D |

**Execution order:**
1. **Phase 1**: Track A (AppDaemon app) — must complete first to finalize sensor schema
2. **Phase 2**: Tracks B and C in parallel (no file overlap, both read-only on sensor schema)
3. **Phase 3**: Track D (dashboard config + version bump) — after all code is written
4. **Phase 4**: Validation Agent reviews everything

---

## Test Commands

```bash
# Run all tests (from repo root)
source .venv/bin/activate && cd appdaemon && python -m pytest tests/ -v --tb=short

# Run only school lunch app tests
source .venv/bin/activate && cd appdaemon && python -m pytest tests/test_school_lunch_app.py -v --tb=short

# Run school menu provider tests (should still pass)
source .venv/bin/activate && cd appdaemon && python -m pytest tests/test_school_menu_client.py -v --tb=short
```

---

## Validation Checklist

### AppDaemon App
- [ ] `school_lunch_app.py` extends `hass.Hass`
- [ ] `sys.path.append(str(Path(__file__).resolve().parents[2]))` present before provider imports
- [ ] App uses `providers/school_menu/client.py` for all HTTP calls (no direct HTTP in app code)
- [ ] `ha_token_env` used as env var name, not token value (S1)
- [ ] Async startup pattern: `run_in` → `create_task` → `_async_startup`
- [ ] `ensure_helper` called for `input_text` with `max=255`
- [ ] `ensure_script` called for relay script with correct event name
- [ ] `listen_event` registered for `school_lunch_command`
- [ ] `run_daily` registered for periodic fetch
- [ ] Sensor attributes use `display_month` (1-indexed), not raw `month` (0-indexed)
- [ ] `select_schools` command enforces minimum 1 school
- [ ] `fetch_month` command updates only the requested school's data
- [ ] Error handling: partial failures don't crash the whole app
- [ ] Logging at appropriate levels (INFO/WARNING/ERROR/DEBUG)

### App Config
- [ ] `apps-prod.yaml` has `school_lunch_app` with `disable: true`
- [ ] `apps-dev.yaml` has `school_lunch_app_dev` without `disable`
- [ ] Both configs have `ha_url: !secret ha_url` and `ha_token_env: TOKEN`
- [ ] Module path is `school_lunch_app.school_lunch_app`

### Unit Tests
- [ ] `test_school_lunch_app.py` exists with at least 10 test cases
- [ ] Tests mock `hassapi`, `HAProvisioner`, and `SchoolMenuClient`
- [ ] Tests verify provisioning calls
- [ ] Tests verify sensor state format
- [ ] Tests verify command routing
- [ ] All tests pass: `python -m pytest tests/test_school_lunch_app.py -v --tb=short`

### At-a-Glance Card
- [ ] `school-lunch-card.js` exists in `apps/school_lunch_app/`
- [ ] Extends `HTMLElement`, uses `attachShadow({ mode: "open" })`
- [ ] Implements `setConfig()` and `set hass()`
- [ ] Touch/click deduplication with 400ms `_touchActive` flag
- [ ] Snapshot-based re-render optimization
- [ ] Shows tomorrow's entrees (Friday PM through Sunday → Monday)
- [ ] Filters to entree category items only
- [ ] Reads both `sensor.school_lunch_menu` and `input_text.school_lunch_selected_schools`
- [ ] Tap navigates to `navigation_path`
- [ ] Registered with `customElements.define` and `window.customCards.push`

### Detail Card
- [ ] `school-lunch-detail-card.js` exists in `apps/school_lunch_app/`
- [ ] Extends `HTMLElement`, uses `attachShadow({ mode: "open" })`
- [ ] Implements `setConfig()` and `set hass()`
- [ ] Touch/click deduplication with 400ms flag
- [ ] **No `preventDefault()` on touchend for checkbox/input/select/textarea targets**
- [ ] Focus guard: skips re-render when `shadowRoot.activeElement` exists
- [ ] "This Week" tab shows Mon-Fri grid with tomorrow highlighted
- [ ] Per-school tabs show full month calendar (Mon-Fri only)
- [ ] Current week row highlighted in month view
- [ ] Prev/next month arrows send `fetch_month` via relay
- [ ] Settings tab with checkboxes, min-1 enforcement
- [ ] Settings changes sent via `select_schools` relay command
- [ ] `_callRelay` uses `hass.callService("script", relay_script, {command, payload})`
- [ ] Registered with `customElements.define` and `window.customCards.push`

### README
- [ ] `README.md` exists in `apps/school_lunch_app/`
- [ ] Documents config reference, provisioned entities, manual prerequisites
- [ ] Documents relay commands and sensor schema

### Version
- [ ] `VERSION` file contains `0.5.0`

### Full Test Suite
- [ ] All existing tests still pass
- [ ] New tests pass
- [ ] No import errors

---

## Agent Prompts

### Implementation Agent A — AppDaemon App

```text
You are Implementation Agent A for the School Lunch Menu feature.

Your task is fully described in the plan file at:

  /home/thaynes/workspace/hass-sandbox/.agents/plans/school-lunch-menu.md

Read the full plan file before doing anything else. Focus on the "Track A: AppDaemon App"
section. It contains the complete specification for the app module, config entries, unit
tests, and README.

Also read these files before making any changes:
- .cursor/rules/appdaemon-architecture.mdc (self-provisioning, relay script, async startup)
- .cursor/rules/appdaemon-coding-guidelines.mdc (coding conventions)
- .agents/playbooks/ha-provisioner.md (provisioner API details)
- appdaemon/providers/school_menu/client.py (provider you will use)
- appdaemon/providers/school_menu/types.py (data types — note month is 0-indexed)
- appdaemon/apps/immich_fetcher/immich_fetcher_app.py (reference app pattern)
- appdaemon/tests/test_immich_fetcher.py (reference test pattern)

Work through all Track A items in order:
1. Create app module with __init__.py
2. Create school_lunch_app.py
3. Add entries to apps-prod.yaml and apps-dev.yaml
4. Create README.md
5. Create test_school_lunch_app.py

After completing all code changes, run the full test suite and fix any failures:

  source .venv/bin/activate && cd appdaemon && python -m pytest tests/ -v --tb=short

DO NOT manually deploy to production. All changes stay in the dev environment.
```

### Implementation Agent B — At-a-Glance Card

```text
You are Implementation Agent B for the School Lunch Menu feature.

Your task is fully described in the plan file at:

  /home/thaynes/workspace/hass-sandbox/.agents/plans/school-lunch-menu.md

Read the full plan file before doing anything else. Focus on the "Track B: At-a-Glance Card"
section. It contains the complete specification for school-lunch-card.js.

Also read these files before making any changes:
- .claude/rules/custom-cards.md (MANDATORY card patterns — touch dedup, shadow DOM, focus guard)
- .cursor/rules/custom-card-guidelines.mdc (full detail on card patterns)
- appdaemon/apps/photo_frame_viewer/photo-display-card.js (reference card — study the snapshot
  pattern, touch/click dedup, and navigation)
- appdaemon/apps/school_lunch_app/school_lunch_app.py (read to understand the sensor attribute
  schema your card will consume)

Create the file:
  appdaemon/apps/school_lunch_app/school-lunch-card.js

After completing the card, verify no syntax errors by reviewing the file.
There are no JS tests to run, but ensure the full Python test suite still passes:

  source .venv/bin/activate && cd appdaemon && python -m pytest tests/ -v --tb=short

DO NOT manually deploy to production. All changes stay in the dev environment.
```

### Implementation Agent C — Detail Card

```text
You are Implementation Agent C for the School Lunch Menu feature.

Your task is fully described in the plan file at:

  /home/thaynes/workspace/hass-sandbox/.agents/plans/school-lunch-menu.md

Read the full plan file before doing anything else. Focus on the "Track C: Detail Card"
section. It contains the complete specification for school-lunch-detail-card.js.

Also read these files before making any changes:
- .claude/rules/custom-cards.md (MANDATORY card patterns — touch dedup, shadow DOM, focus guard,
  CRITICAL Android checkbox rule)
- .cursor/rules/custom-card-guidelines.mdc (full detail on card patterns)
- appdaemon/apps/immich_fetcher/immich-fetcher-card.js (reference for tabbed UI + settings)
- appdaemon/apps/photo_frame_viewer/photo-frame-viewer-card.js (reference for relay calls)
- appdaemon/apps/school_lunch_app/school_lunch_app.py (read to understand the sensor attribute
  schema and relay commands your card will use)

Create the file:
  appdaemon/apps/school_lunch_app/school-lunch-detail-card.js

This is the most complex card. Pay special attention to:
- The Settings tab has checkboxes — NEVER call preventDefault() on touchend for input elements
- Calendar grid construction for the month view
- The relay call pattern for fetch_month and select_schools commands
- Tab state management

After completing the card, verify no syntax errors by reviewing the file.
Ensure the full Python test suite still passes:

  source .venv/bin/activate && cd appdaemon && python -m pytest tests/ -v --tb=short

DO NOT manually deploy to production. All changes stay in the dev environment.
```

### Implementation Agent D — Dashboard Config + Version Bump

```text
You are Implementation Agent D for the School Lunch Menu feature.

Your task is fully described in the plan file at:

  /home/thaynes/workspace/hass-sandbox/.agents/plans/school-lunch-menu.md

Read the full plan file before doing anything else. Focus on the "Track D: Dashboard Config +
Version Bump" section.

Also read these files before making any changes:
- .claude/rules/ha-yaml.md (HA YAML change communication protocol)
- .agents/playbooks/ha-dashboard.md (dashboard editing via MCP — config_hash pitfall!)

Your tasks:
1. Bump VERSION from 0.4.4 to 0.5.0
2. Register both card JS files as Lovelace dashboard resources (check how existing cards like
   photo-display-card.js are registered and follow the same pattern)
3. Add school-lunch-card to wall-display dashboard column 2 (after cruise countdown)
4. Add popup section with bubble-card pop-up + school-lunch-detail-card

CRITICAL: When updating the dashboard, read it first to get the config_hash. The update
will fail without the current hash.

After completing changes, run the test suite to verify nothing broke:

  source .venv/bin/activate && cd appdaemon && python -m pytest tests/ -v --tb=short

DO NOT manually deploy to production. All changes stay in the dev environment.
```

### Validation Agent

```text
You are a Validation Agent. Review the implementation described in the plan file at:

  /home/thaynes/workspace/hass-sandbox/.agents/plans/school-lunch-menu.md

Read the full plan file — the "Validation checklist" section lists every requirement to verify.

Also read these rule files:
- .claude/rules/appdaemon.md
- .claude/rules/custom-cards.md
- .claude/rules/security.md
- .cursor/rules/appdaemon-architecture.mdc
- .cursor/rules/custom-card-guidelines.mdc

DO NOT modify any files. Your job is to READ and VERIFY only.

Verify each checklist item by reading the relevant source files:
- appdaemon/apps/school_lunch_app/school_lunch_app.py
- appdaemon/apps/school_lunch_app/school-lunch-card.js
- appdaemon/apps/school_lunch_app/school-lunch-detail-card.js
- appdaemon/apps/school_lunch_app/README.md
- appdaemon/apps/apps-prod.yaml
- appdaemon/apps/apps-dev.yaml
- appdaemon/tests/test_school_lunch_app.py
- VERSION

Run the full test suite and include the result in your report:

  source .venv/bin/activate && cd appdaemon && python -m pytest tests/ -v --tb=short

Output a PASS or FAIL verdict.

If FAIL, list every failing checklist item with:
  - File path and method/line where the issue is
  - What is wrong or missing
  - What the fix should be

Then produce a copy-pasteable prompt for the relevant Implementation Agent in a fenced
```text``` block.
```

---

## Implementation Agent Re-prompt Template

```text
You are Implementation Agent <A/B/C/D> for the School Lunch Menu feature.

Validation Agent has completed a read-only validation pass. The following defects
were found that you must fix.

DEFECT 1

File: <path>

<What is wrong. What the fix should be.>

REQUIRED FIX

1. <First action>
2. <Second action>

Read the plan file at /home/thaynes/workspace/hass-sandbox/.agents/plans/school-lunch-menu.md
and the relevant rules before making changes. Do not manually deploy to production.
Run the full test suite after your changes and confirm it passes:

  source .venv/bin/activate && cd appdaemon && python -m pytest tests/ -v --tb=short
```

---

## Final Planner Review

After the Validation Agent returns PASS:
1. Re-read all implemented files and compare to this plan
2. Run the full test suite
3. Code-review for missed bugs, weak validation, stale config drift
4. Fix any remaining issues directly
5. Verify the PR is ready for draft creation
