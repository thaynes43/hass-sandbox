# Vestaboard Architecture Refactor Options

This document captures options and recommendations for refactoring the Vestaboard Controller and Configuration apps to support a decentralized, pluggable automation app architecture within AppDaemon.

## Requirements Overview
1. **Decoupled Automations**: Moving automations out of the controller into their own independent AppDaemon apps.
2. **Dynamic UI Configuration**: The Vestaboard+ frontend needs a generic way to determine the schema of config options for these automation apps (e.g., types, constraints, validation) so the UI can dynamically generate form fields.
3. **Queue Interface**: A generic interface for automations to schedule frames with the controller, know when they are active on the board, and dynamically update the active frame (e.g., updating a live countdown timer).
4. **AppDaemon Features**: Leverage AppDaemon's native features for app dependencies, directory structures, and multiple instances.
5. **Project Organization**: Re-organize into a cleaner `vestaboard_apps` folder structure.

---

## 1. Folder Structures & Organization

AppDaemon recursively searches the `apps/` directory for Python modules. We can reorganize the files into a grouped folder without breaking how AppDaemon parses apps. The root `apps/` directory won't be overly cluttered with automation-specific folders.

**Option A (Recommended): Consolidated Vestaboard Folder**
```
appdaemon/apps/vestaboard_apps/
├── vestaboard_controller/
│   └── vestaboard_controller_app.py
├── vestaboard_configuration/
│   └── vestaboard_configuration_app.py
└── automations/
    ├── calendar_summary/
    │   └── calendar_summary_app.py
    ├── weather_schedule/
    │   └── weather_schedule_app.py
    └── random_art/
        └── random_art_app.py
```
* **Pros**: Keeps all vestaboard-related code cleanly encapsulated in one root folder. Easily scaled for new automations.
* **Cons**: Import paths and `module:` definitions in `apps-dev.yaml` and `apps-prod.yaml` will need to be updated to match the new python module namespace (e.g., `module: vestaboard_apps.automations.calendar_summary.calendar_summary_app`).

---

## 2. AppDaemon Dependencies & Multiple Instances

Since automations require the controller and configuration app to be running, we must ensure proper boot order and reload behavior.

**AppDaemon `dependencies` Directive**:
AppDaemon natively supports declaring dependencies in the `apps.yaml` configurations. By adding a `dependencies` list to each automation, AppDaemon ensures the target apps start beforehand, and if the target app is reloaded, the dependent app will also reload.

**Multiple Instances**:
AppDaemon natively handles spinning up multiple instances of the same class by defining new top-level YAML keys mapped to the same module/class but with different config variables (like `calendar_entity`).

**YAML Configuration Example**:
```yaml
calendar_summary_family:
  module: vestaboard_apps.automations.calendar_summary.calendar_summary_app
  class: CalendarSummaryApp
  dependencies:
    - vestaboard_controller_dev
    - vestaboard_configuration_dev
  calendar_entity: calendar.hayneshome01886_gmail_com

calendar_summary_holidays:
  module: vestaboard_apps.automations.calendar_summary.calendar_summary_app
  class: CalendarSummaryApp
  dependencies:
    - vestaboard_controller_dev
    - vestaboard_configuration_dev
  calendar_entity: calendar.holidays_for_united_states_ma
```

---

## 3. Communication Interface (Controller ↔ Automations)

The automation apps and controller need a robust interface. The Controller needs the frame payload, and the Automation needs to know when its frame goes live so it can push updates (like time countdowns).

**Option A: Direct Object Reference (`self.get_app()`) (Recommended)**
AppDaemon provides a method `self.get_app("app_name")` which returns the actual instantiated Python object of the target app.
* **Registration**: On startup, an automation app fetches the controller: `controller = self.get_app("vestaboard_controller_dev")` and registers itself: `controller.register_automation(self.name, self)`.
* **Schema Definition**: The controller can call `automation_instance.get_config_schema()` to retrieve UI layout information.
* **Frame Lifecycle**: The controller calls methods directly on the automation object, such as `automation_instance.on_board_activated()` or `automation_instance.get_frame_characters()`.

**Option B: AppDaemon Event Bus (`self.fire_event()`)**
Decouple completely by using custom internal AppDaemon events.
* **Registration**: Automations fire `vestaboard_automation_register` with a payload of their schema and an `app_id`.
* **Frame Lifecycle**: Automations fire `vestaboard_queue_frame`. When the controller promotes the frame to the board, it fires `vestaboard_frame_active_{app_id}` so the automation knows to start its dynamic update loop.
* **Critique**: While highly decoupled, dealing with complex schema sharing and strictly-timed object updates via asynchronous events can become fragile and harder to trace compared to direct object references.

---

## 4. UI Configuration Schema (Generic Validation)

To support dynamic forms on the Lovelace custom card (`vestaboard-configuration-card.js`), each automation should define a configuration schema that the Configuration app can aggregate and send to the frontend via the `sensor.vestaboard_configuration_status` state attributes.

**Schema Structure Proposal**:
Automations must return a JSON-serializable schema:
```python
def get_config_schema(self):
    return {
        "id": self.name,
        "name": "Calendar Summary",
        "description": "Displays upcoming events with a countdown.",
        "fields": [
            {"key": "min_frequency", "label": "Min Frequency (mins)", "type": "number", "default": 15, "min": 1, "max": 120},
            {"key": "force_push", "label": "Force Push Frame", "type": "boolean", "default": False},
            {"key": "trigger_times", "label": "Trigger Times", "type": "time_list", "description": "List of HH:MM:SS times"}
        ]
    }
```
* **Configuration App Role**: The Configuration App acts as the bridge. It can request the registered automations from the Controller, compile a global list of schemas, and publish them in its sensor payload.
* **Custom Card Role**: The JS card loops through `"fields"` and renders an `<input type="number">`, `<ha-switch>`, or custom time-list UI element based on the `"type"` field, applying HTML5 validation like `min` and `max`.

---

## 5. Next Steps for the Planning Agent

When constructing the step-by-step refactor plan, agents should refer to these files:

- **Deployment/Dev Configs**:
  `/home/thaynes/workspace/hass-sandbox/.claude/worktrees/eager-stargazing-rivest/appdaemon/apps/apps-dev.yaml`
  `/home/thaynes/workspace/hass-sandbox/.claude/worktrees/eager-stargazing-rivest/appdaemon/apps/apps-prod.yaml`
- **Controller App (Registration & Queue)**:
  `/home/thaynes/workspace/hass-sandbox/.claude/worktrees/eager-stargazing-rivest/appdaemon/apps/vestaboard_controller_app/vestaboard_controller_app.py`
  (Must be modified to accept external AppDaemon app registrations instead of instantiating internal automation classes).
- **Configuration App (Schema Aggregation)**:
  `/home/thaynes/workspace/hass-sandbox/.claude/worktrees/eager-stargazing-rivest/appdaemon/apps/vestaboard_configuration_app/vestaboard_configuration_app.py`
  (Must be modified to publish dynamic schema info to its sensor).
- **Custom JS Card**:
  `/home/thaynes/workspace/hass-sandbox/.claude/worktrees/eager-stargazing-rivest/appdaemon/apps/vestaboard_configuration_app/vestaboard-configuration-card.js`
  (Must be refactored to parse the `fields` schema and render inputs dynamically, handling input validation).
