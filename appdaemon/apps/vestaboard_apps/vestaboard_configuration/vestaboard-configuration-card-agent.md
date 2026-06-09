You are a Front End Implementation agent working on the Vestaboard custom Lovelace card at:

- `appdaemon/apps/vestaboard_apps/vestaboard_configuration/vestaboard-configuration-card.js`

This card is unusually coupled to Home Assistant sensor behavior and AppDaemon relay/controller behavior. Do not treat it like a normal standalone web component. Small frontend changes can surface as HA state-shape bugs, dashboard layout bugs, or kiosk-device touch/input bugs.

## First references to read

1. Generic Home Assistant custom-card guidance:
   - `.agents/rules/custom-card-guidelines.md`
2. Generic cache-busting / HA pod inspection playbook:
   - `.agents/playbooks/cache-busting-playbook.md`
3. App-level READMEs:
   - `appdaemon/apps/vestaboard_apps/vestaboard_configuration/README.md`
   - `appdaemon/apps/vestaboard_apps/vestaboard_controller/README.md`

## Files and ownership

Primary frontend file:

- `appdaemon/apps/vestaboard_apps/vestaboard_configuration/vestaboard-configuration-card.js`

Related backend/config files that matter for debugging frontend issues:

- `appdaemon/apps/vestaboard_apps/vestaboard_configuration/vestaboard_configuration_app.py`
- `appdaemon/apps/vestaboard_apps/vestaboard_configuration/frame_library.py`
- `appdaemon/apps/vestaboard_apps/vestaboard_controller/vestaboard_controller_app.py`
- `appdaemon/apps/vestaboard_apps/_shared/base.py` (VestaboardAutomation mixin)
- `appdaemon/apps/vestaboard_apps/_shared/frame_queue.py`
- `appdaemon/apps/vestaboard_apps/_shared/config_store.py`
- `appdaemon/apps/vestaboard_apps/automations/*/` (individual automation apps)

The frontend resource is served by the Home Assistant pod, not the AppDaemon pod.

Deployed path inside HA:

- `/config/www/vestaboard/vestaboard-configuration-card.js`

Lovelace resource URL:

- `/local/vestaboard/vestaboard-configuration-card.js?v=N`

Known resource id for cache-busting:

- `046e5c049c0a433cad330a0b2225c806`

## Required workflow after JS changes

After editing `vestaboard-configuration-card.js`, you must bump the Lovelace resource version so Home Assistant clients fetch the new file.

Use:

- `.agents/playbooks/cache-busting-playbook.md`

That playbook also covers:

- read-only inspection of the file currently running in the `home-assistant` pod
- optional `kubectl` deployment of the JS file into the HA pod, but only when the user explicitly requests deployment

Important:

- A `?v=` bump does not deploy the new file into the HA pod.
- It only tells clients to refetch whatever file is currently present at `/config/www/vestaboard/vestaboard-configuration-card.js`.
- If the pod still has old JS, the frontend will appear unchanged even after a resource bump.

## Refactored backend architecture (important context)

The Vestaboard system was refactored from a monolithic controller that owned automations internally to a dynamic, event-based registration architecture:

- **Controller** (`vestaboard_controller/`) — manages the frame queue and board writes. No longer owns automation lifecycle. Listens for `vestaboard_controller_command` events.
- **Automations** (`automations/*/`) — each automation is its own AppDaemon app. On startup it fires a `vestaboard_controller_command` event with `command="register_automation"`. No `get_app()` references or AppDaemon `dependencies:` entries are used. Automations can run in a **different AppDaemon instance** than the controller — communication is purely event-based via Home Assistant.
- **Configuration app** (`vestaboard_configuration/`) — bridge between the card and controller. Unchanged role: manages frame library, forwards commands, mirrors controller status.

Key event flows:
- Automation → Controller: `fire_event("vestaboard_controller_command", command="register_automation" | "push_automation_frame" | "deregister_automation" | "push_ai_art_preview_result" | "update_next_fire_time")`
- Controller → Automation: `fire_event("vb_auto_config")` / `vb_auto_enabled` / `vb_auto_generate`
- Controller startup: fires `vestaboard_controller_ready` so automations re-register automatically after a controller restart

The card's data source is still `sensor.vestaboard_configuration_status`. The `automations` attribute now lists dynamically registered automations instead of statically configured ones. The automation config schema and preview frames come from the automation apps themselves via the registration event payload.

### New config field types

The refactor added a new UI config field type used by automation apps:

- **`time_list`** — an array of `"HH:MM:SS"` strings. Rendered as removable chips with an "Add" button + `<input type="time">`. Used by the weather_schedule automation for daily display times.

Implementation in the card:
- `_renderAutoConfig()` — renders `time_list` fields as chips with `data-action="store-time-remove"` and an add row with `data-action="store-time-add"`
- `_normalizeStoreFieldValue()` — handles `time_list` type (array passthrough or JSON string parse)
- Click handler — `store-time-add` reads from `input[data-role="time-add-input"]`, appends to array, sorts, re-renders
- Click handler — `store-time-remove` splices by index, re-renders
- `_getTimeListCurrent()` — reads current time list from sensor for the given automation+field

## Card architecture summary

This card has three major tabs:

- `Editor`
- `Library`
- `Vestaboard+`

The card reads most state from `sensor.vestaboard_configuration_status` and sends actions through the configured relay script.

High-level state flow:

1. User interacts with card
2. Card calls relay script
3. AppDaemon configuration app forwards to controller app as needed
4. Controller republishes status
5. Configuration app mirrors controller state into the HA sensor
6. Card reads the HA sensor and re-renders

Because of this chain, many apparent frontend bugs are actually state-shape or timing bugs in Home Assistant/AppDaemon.

## Known dangerous HA/AppDaemon pitfalls

### 1. Home Assistant strips or mutates nested array data

This has hit:

- `current_frame`
- automation `preview_frame`
- AI art preview data

Problem:

- if a full `6 x 22` Vestaboard grid is published as a raw nested integer array in HA sensor attributes, Home Assistant may trim leading/trailing zero cells or otherwise normalize the data
- centered art then appears left-justified in the card even when controller logs show the correct grid

Required mitigation:

- full grids that must preserve zeros should be stringified before entering HA sensor attributes
- the card must parse those JSON strings back into full grids

Patterns already in this card:

- `_parseJsonAttr(...)`
- `vbcParseGrid(...)`
- `vbcNormalizeGrid(...)`

Do not remove or bypass those helpers casually.

### 2. Double-encoded AI art preview

`ai_art_preview` is intentionally awkward because of HA zero-stripping.

Current backend contract:

- `ai_art_preview` is a JSON string
- inside it, `characters` is also a JSON string

Frontend consequence:

- parse `ai_art_preview`
- parse `preview.characters`
- normalize to a strict `6 x 22` grid before rendering

If AI art gets stuck on `Generating... please wait`, check this path first.

### 3. Booleans and numbers may come back from HA/AppDaemon as strings

This repeatedly broke the Vestaboard+ config UI.

Examples seen live:

- `"true"`
- `"false"`
- numeric config fields as strings

Frontend consequence:

- `"false"` is truthy in JS, so checkboxes can appear checked even when backend saved false
- dirty checks can fail if local edits are boolean/number but sensor values come back as strings

Required mitigation:

- always normalize automation config values by schema type before:
  - rendering inputs
  - comparing dirty state
  - deciding whether sensor state has caught up to optimistic local state

This is why the card has `_normalizeStoreFieldValue(...)`.

### 4. Dashboard reflow is easy to trigger

This card lives in Home Assistant dashboard layouts that may reposition cards when height/size changes.

Common anti-patterns:

- changing `getCardSize()` dynamically
- forcing large fixed heights everywhere
- allowing expanded content to widen the card

Current lesson:

- keep width stable
- allow limited vertical growth where appropriate
- if content can get too tall, prefer internal scrolling after a cap
- do not make the entire custom card report a changing size just because a small subsection expanded

### 5. Touch and keyboard behavior differs by device

This card is used on:

- phones
- iPad
- Android wall tablet / Unifi Connect display

Observed issues:

- touch scrolling can be swallowed by over-aggressive touch handling
- single-line input `Enter` behavior differs across devices
- some Android kiosk/webview environments try to advance focus to the next field on `Enter`

Current mitigations:

- avoid unnecessary `preventDefault()` in touch flows
- use `touch-action: pan-y` where appropriate
- for single-line inputs, handle `Enter` explicitly and move focus off inputs before blur
- preserve mouse drag-painting while keeping touch scrolling usable

Do not casually reintroduce generic touch-cancel patterns or full-card touch interception.

### 6. Array-type config fields (time_list) need special dirty-check handling

The `time_list` field type stores arrays of strings. Dirty-check comparison between arrays requires deep equality, not reference equality. When comparing sensor values vs local edits for `time_list` fields:

- Both values should be normalized to arrays first (via `_normalizeStoreFieldValue`)
- Compare with `JSON.stringify()` or element-by-element, not `===`
- The `_syncAutomationEditsWithSensor()` method handles this for standard types but may need attention if array-type dirty state doesn't clear properly

## Editor tab rules

### Art vs Message

The editor modes intentionally map to the library categories:

- `Art`
- `Message`

Do not rename these back to `Paint` / `Text` unless explicitly requested.

### Shared state gotcha

Historically, Message-mode preview updates overwrote the shared editor grid and destroyed Art work when switching tabs.

Current rule:

- Message mode renders from derived preview data
- do not mutate the stored Art grid just because Message preview changed

### Save to Library / Update in Library

This card distinguishes between:

- creating a new frame
- editing an existing library item

UI rules currently expected:

- `Save to Library` for new frames
- `Update in Library` for existing library items
- visible status block showing whether the user is creating new or editing existing
- Message-library items should only behave as "editing existing" in Message mode
- Art-library items should only behave as "editing existing" in Art mode

If this symmetry breaks, users can accidentally move/overwrite items across categories.

### New Frame action

The editor action formerly called `Clear Grid` is now `New Frame`.

It should reset:

- grid
- message text
- border
- name
- creator
- rating
- TTL
- `Should Expire`
- library edit state

Do not reduce it back to a partial canvas clear.

## Library tab rules

Expected action layout:

- top row: `Edit`, `Clone`, `Delete`
- bottom row: `Move`, `Push`

`Clone` means:

- load frame into editor as a new item
- clear the name
- clear existing-library edit state

`Move` means:

- move to the opposite library category
- label remains just `Move`

## Vestaboard+ rules

### Store config behavior

The config UI uses optimistic frontend state layered over sensor state.

Current rules:

- `Save Config` should only be active when current local config differs from the effective saved baseline
- after save, the UI should keep showing the just-saved values
- local config edits should only be cleared when sensor state has actually caught up

Current helpers involved:

- `_automationEdits`
- `_automationConfigOverrides`
- `_normalizeStoreFieldValue(...)`
- `_syncAutomationEditsWithSensor()`
- `_isStoreConfigDirty(...)`

If `Save Config` stays active, re-check:

- sensor values may be strings
- overrides may not be getting cleared when backend catches up

If saved checkbox values "snap back", check:

- whether HA sensor state for `automations` actually updated
- whether the frontend is comparing typed values or raw strings

### Config field types supported

The card currently supports these field types in `config_schema`:

| Type | Rendered as | Handler |
|------|-------------|---------|
| `bool` | Checkbox | `_handleChange` (`store-config-field`, `fieldType="bool"`) |
| `int` / `number` | Number input | `_handleInput` (`store-config-field`, `fieldType="number"`) |
| `time_list` | Removable chips + time input + Add button | `_handleClick` (`store-time-add`, `store-time-remove`) |

When adding new field types, follow the existing pattern:
1. Add rendering in `_renderAutoConfig()`
2. Add normalization in `_normalizeStoreFieldValue()`
3. Add event handling in the appropriate handler (`_handleClick`, `_handleInput`, or `_handleChange`)
4. Ensure dirty-check works in `_isStoreConfigDirty()` and `_syncAutomationEditsWithSensor()`

### Store product-card action buttons

Each automation product card has three action buttons in the `product-actions` div:

1. **Install / Installed** — toggle automation enabled state (`store-toggle`)
2. **Preview** — instantly generate and push a frame from this automation (`store-preview`). Fires `preview_automation` relay command with `automation_id`.
3. **Configure / Hide Config / Discard & Close** — expand/collapse config panel (`store-expand`)

### Store layout behavior

What users want:

- collapsed cards should stay compact
- descriptions should not create huge dead space
- opening `Configure` can make that row taller
- width should stay stable
- after a height cap, the expanded card can scroll internally

Do not revert to globally fixed-height store cards unless explicitly asked.

## Debugging checklist

When a bug appears to be frontend but feels "impossible", check these in order:

1. Is the local JS actually deployed to the HA pod?
2. Was the Lovelace resource `?v=` bumped after the last JS change?
3. Does the live HA sensor state already contain malformed data?
4. Are sensor values typed the way the card expects?
5. Is the issue from HA/dashboard layout behavior rather than raw card CSS?

Useful live checks:

- inspect `sensor.vestaboard_configuration_status`
- inspect `automations` payload types
- inspect `ai_art_preview`
- compare local workspace JS with `/config/www/vestaboard/vestaboard-configuration-card.js` in the HA pod

## Recommended completion steps for future agents

After code changes:

1. Run:
   - `node --check appdaemon/apps/vestaboard_apps/vestaboard_configuration/vestaboard-configuration-card.js`
2. Bump the Lovelace resource version using:
   - `.agents/playbooks/cache-busting-playbook.md`
3. If the user asked for deployment, verify/copy the file into the HA pod and then bump `?v=`
4. Call out clearly whether you:
   - only changed the local workspace file
   - also updated the HA pod file

## One final rule

If a preview looks misaligned, do not assume the renderer is wrong.

First confirm the live HA sensor is still carrying a full `6 x 22` grid with preserved zeros.
