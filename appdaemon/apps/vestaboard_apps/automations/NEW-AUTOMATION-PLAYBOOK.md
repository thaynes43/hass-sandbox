# New Vestaboard+ Automation Playbook

### When to use this

Use this playbook when adding a new automation app to the Vestaboard+ system. An automation is an independent AppDaemon app that generates frames and pushes them to the controller's queue. Examples: calendar_clock, weather_schedule, messages_from_library, ai_art_generator.

---

## Architecture overview

```
Your new automation app (hass.Hass + VestaboardAutomation)
  → register_with_controller() on startup
  → generate_frame() produces a 6×22 grid
  → push_frame() fires event to controller
  → controller manages queue, TTL, display

Controller → fires config/enabled/generate events back to your app
```

All communication is event-based — no `get_app()` references or AppDaemon `dependencies:` entries. Your automation can run in a different AppDaemon instance than the controller.

---

## Step 1: Create the automation package

Create a new directory under `appdaemon/apps/vestaboard_apps/automations/`:

```
automations/
└── your_automation/
    ├── __init__.py          # empty
    ├── your_automation_app.py
    └── README.md            # required by project standards
```

## Step 2: Implement the app class

### Skeleton

```python
"""Your automation description."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

# sys.path fix — adjust parents depth for your nesting level
# automations/<name>/<file>.py → parents[4] reaches appdaemon/
sys.path.append(str(Path(__file__).resolve().parents[4]))

import hassapi as hass

from providers.vestaboard.character_encoding import text_to_grid, blank_grid
from vestaboard_apps._shared.base import VestaboardAutomation


class YourAutomationApp(hass.Hass, VestaboardAutomation):
    # --- Required class attributes ---
    automation_type = "your_automation"           # unique machine name
    display_name = "Your Automation"              # shown in Vestaboard+ UI
    display_description = "What this automation does."

    # --- Default queue behavior ---
    default_ttl_s = None          # None = use UI config ttl_minutes
    default_max_age_s = None
    default_should_expire = True  # True = auto-leave board on TTL expiry

    # --- Default UI config (seeded to config store on first registration) ---
    DEFAULT_UI_CONFIG = {
        "enabled": False,
        "ttl_minutes": 10,
        "should_expire": True,
        # Add your automation-specific config keys here
    }

    def initialize(self) -> None:
        # Register with the controller — do NOT start timers here.
        # Wait for the controller to fire back config via vb_auto_config.
        self.register_with_controller()

    def terminate(self) -> None:
        self.deregister_from_controller()

    async def generate_frame(self, **kwargs) -> list[list[int]]:
        """Generate a 6×22 character grid. Called by the base class."""
        # Build your frame here
        grid = text_to_grid("HELLO WORLD", justify="center", align="center")
        return grid

    def get_config_schema(self) -> dict:
        """Define UI-editable config fields for the Vestaboard+ card."""
        return {
            "enabled": {"type": "bool", "label": "Enabled", "default": False},
            "ttl_minutes": {"type": "int", "label": "Display Time (minutes)",
                           "min": 1, "max": 120, "default": 10},
            "should_expire": {"type": "bool", "label": "Auto-remove after TTL",
                             "default": True},
            # Add your custom fields here
        }
```

### Key rules

1. **Do NOT start timers in `initialize()`** — wait for the controller's config event. The controller fires `vb_auto_config` after registration with the persisted UI config. Start your timers in `on_config_updated()` or `set_enabled()`.

2. **Always call `register_with_controller()`** in `initialize()` and `deregister_from_controller()` in `terminate()`.

3. **Implement `generate_frame()`** — this is the only abstract method. Return a 6×22 grid of Vestaboard character codes. Return `blank_grid()` on failure.

## Step 3: Choose your trigger pattern

### Pattern A: Random interval (e.g., messages_from_library, ai_art_generator)

Use the base class helpers `_start_random_interval()` and `_on_random_fire()`:

```python
def on_config_updated(self, config: dict[str, Any]) -> None:
    super().on_config_updated(config)
    if self.args.get("enabled", False):
        self._start_random_interval()

def set_enabled(self, enabled: bool) -> None:
    if enabled:
        self._start_random_interval()
        self.create_task(self._generate_and_push())
    else:
        self._cancel_random_interval()
```

### Pattern B: Scheduled daily times (e.g., weather_schedule)

Use AppDaemon's `run_daily()`:

```python
def on_config_updated(self, config: dict[str, Any]) -> None:
    super().on_config_updated(config)
    if "time_list" in config:
        self._register_daily_timers()
```

### Pattern C: Periodic tick (e.g., calendar_clock)

Use AppDaemon's `run_every()`:

```python
def _start_timer(self) -> None:
    interval_s = int(self.args.get("update_interval_minutes", 5)) * 60
    self._timer_handle = self.run_every(self._on_tick, dt.now(), interval_s)
```

### Pattern D: Event-driven with rotation (e.g., calendar_summary)

Use AppDaemon's `listen_state()` + `run_in()` for rotation and countdown timers.

## Step 4: Handle refresh pushes correctly (CRITICAL)

**If your automation pushes multiple frames during a single display cycle** (periodic refreshes, rotation, countdown updates), you MUST check `is_displayed()` before each refresh push. This prevents stale frames from accumulating in the pending queue when another frame displaces yours.

### When to use `is_displayed()`

| Push type | Needs check? | Why |
|-----------|-------------|-----|
| Initial push (first frame after timer fires) | **No** | This is a new frame entering the queue normally |
| Random interval fire (`_on_random_fire`) | **No** | Single fire, then reschedules — no refresh cycle |
| Periodic refresh (weather 15-min update) | **YES** | Pushes stale frames if displaced |
| Rotation timer (calendar next event) | **YES** | Queues stale rotation frames if displaced |
| Countdown update (calendar time text) | **YES** | Queues identical frames if displaced |
| Same-source update (clock every N min) | **No** | Same-source dedup keeps pending at 1 |

### How to use it

```python
async def _refresh_my_content(self) -> None:
    """Periodic refresh during display cycle."""
    # REQUIRED: stop refreshing if we lost the board
    if not self.is_displayed():
        self.log("No longer displayed — stopping refresh", level="INFO")
        return  # Don't push, don't schedule next refresh

    # ... generate and push updated frame ...
    self.push_frame(grid, ttl_s=remaining_ttl, ...)

    # Schedule next refresh
    await self._schedule_next_refresh()
```

### Why this matters

Without the check, this happens:
1. Your automation displays a frame with TTL=60m
2. At 15 minutes, a force-push (weather, urgent calendar) takes the board
3. Your refresh timer fires at 15-minute intervals, pushing frames with `override_ttl=False`
4. Those frames go to the **pending queue** (not same-source update, because you're not displayed)
5. Same-source dedup keeps it to 1 pending frame, BUT the frame has a full remaining TTL
6. Hours later, when everything ahead of it expires, your stale frame gets promoted to the board

## Step 5: Add YAML entries

### apps-prod.yaml

```yaml
your_automation:
  module: vestaboard_apps.automations.your_automation.your_automation_app
  class: YourAutomationApp
  disable: true
  # Add any YAML-level config keys here (api keys, file paths, etc.)
```

### apps-dev.yaml

```yaml
your_automation_dev:
  module: vestaboard_apps.automations.your_automation.your_automation_app
  class: YourAutomationApp
  # Same config but with dev paths, no disable: true
```

**Keep both files in sync** — same module/class paths, same config keys (different values for paths).

## Step 6: Write the README

Every automation MUST have a `README.md` in its package directory. Follow the template in `.cursor/rules/appdaemon-documentation.mdc`. Required sections:

1. Summary (what it does)
2. How it works (step-by-step lifecycle)
3. Architecture diagram (event flow)
4. Dependencies
5. Self-provisioned entities (usually "None")
6. Config reference (YAML keys + UI-editable keys)
7. YAML example
8. Manual setup required
9. Upstream/downstream dependencies

## Step 7: Write tests

Add tests in `appdaemon/tests/test_your_automation.py`. At minimum:

- `generate_frame()` returns a valid 6×22 grid
- `generate_frame()` returns `blank_grid()` on failure
- Config schema has required fields
- Registration fires the correct event
- Enable/disable starts/stops timers
- **If using refresh pushes**: test that `is_displayed()` returning False stops the refresh cycle

## Step 8: Update documentation map

Add your automation to `.cursor/rules/appdaemon-documentation.mdc` in the vestaboard apps section.

## Step 9: Verify

```bash
# Run your tests
source .venv/bin/activate && cd appdaemon && python -m pytest tests/test_your_automation.py -v --tb=short

# Run full suite to check for regressions
python -m pytest tests/ -v --tb=short

# Local dev test
appdaemon -c appdaemon 2>&1 | tee audit-log-capture.txt
# Watch for: registration, config pushback, frame generation, board writes
```

---

## Checklist

- [ ] Package directory with `__init__.py`, app module, README.md
- [ ] Class extends `hass.Hass, VestaboardAutomation`
- [ ] `automation_type`, `display_name`, `display_description` set
- [ ] `DEFAULT_UI_CONFIG` with `enabled`, `ttl_minutes`, `should_expire`
- [ ] `register_with_controller()` in `initialize()`
- [ ] `deregister_from_controller()` in `terminate()`
- [ ] `generate_frame()` implemented, returns 6×22 grid or `blank_grid()`
- [ ] `get_config_schema()` returns field definitions
- [ ] Timers NOT started in `initialize()` — deferred to config event
- [ ] `is_displayed()` check in all refresh/rotation/countdown push paths
- [ ] `apps-prod.yaml` entry with `disable: true`
- [ ] `apps-dev.yaml` entry (no `disable`)
- [ ] README.md with all required sections
- [ ] Tests written and passing
- [ ] Documentation map updated
