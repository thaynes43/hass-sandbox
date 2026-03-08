# AppDaemon Testing

## Summary

AppDaemon has a built-in pytest-based test framework. **Mocking Home Assistant calls is possible** by patching `call_service`, `get_state`, etc. on the app instance before triggering callbacks. The recommended approach is **dependency injection or patching** so unit tests don't require a live HA instance.

## AppDaemon's Built-in Testing

From the [AppDaemon TESTING docs](https://appdaemon.readthedocs.io/en/latest/TESTING.html):

- **pytest** + **pytest-asyncio**
- **`ad` fixture** — runs a full AppDaemon instance; apps are disabled by default
- **`run_app_for_time`** — runs an app temporarily with modified args
- **Functional tests** — fire events, trigger state changes, assert on logs; require AppDaemon running
- **Unit tests** — don't require AppDaemon; currently limited to datetime/timedelta parsing
- **Plugin tests** — "aren't yet covered" (no official pattern for mocking HA)

## Mocking HA Calls

### Option 1: Patch on the app instance (recommended)

Use `unittest.mock.patch.object` to patch `call_service`, `get_state`, etc. on the app before triggering callbacks:

```python
# tests/test_garage_door_notify.py
import pytest
from unittest.mock import patch, MagicMock

from garage_door_notify import GarageDoorNotify

def test_build_notification():
    """Unit test for pure logic - no HA, no mocking."""
    # Create app with minimal mock AD
    app = GarageDoorNotify(MagicMock(), MagicMock())
    app.args = {}
    title, message = app._build_notification("Garage Door", "open", "closed")
    assert title == "Garage Door Opened"
    assert "was closed" in message

def test_send_notifications_calls_services():
    """Mock call_service and assert it's called correctly."""
    app = GarageDoorNotify(MagicMock(), MagicMock())
    app.args = {"notify_services": ["notify.test_service"]}
    with patch.object(app, "call_service") as mock_call:
        app._send_notifications("Title", "Message")
        mock_call.assert_called_once_with("notify/test_service", title="Title", message="Message")
```

### Option 2: Extract logic into pure functions

Move logic into pure functions that take a "notify" callable. The app becomes thin glue; unit tests call the pure functions with a mock:

```python
# garage_door_notify.py
def build_notification(door_name: str, to_state: str, from_display: str) -> tuple[str, str]:
    action = "Opened" if to_state == "open" else "Closed"
    title = f"{door_name} {action}"
    message = f"{door_name} is now {to_state} (was {from_display})."
    return title, message

class GarageDoorNotify(hass.Hass):
    def _on_door_state(self, ...):
        ...
        title, message = build_notification(door_name, new, from_display)
        self._send_notifications(title, message)
```

Unit test:

```python
from garage_door_notify import build_notification

def test_build_notification():
    title, message = build_notification("Garage", "open", "closed")
    assert title == "Garage Opened"
    assert "was closed" in message
```

### Option 3: Use AppDaemon's functional test framework

Run the app inside AppDaemon's `ad` fixture, use `set_state` or events to simulate HA, and assert on logs or a captured mock. This requires AppDaemon's test setup (conf, plugins, etc.) and is heavier. The AppDaemon repo uses this for `test_hello_world`, `test_event_callback`, etc.

## Recommendation

- **Option 2 (pure functions)** — best for business logic; no mocking, fast tests
- **Option 1 (patch)** — when you need to test the full callback flow including `call_service`
- **Option 3 (functional)** — for integration-style tests when you want to run the app in a real AppDaemon context

## Example: Testing garage_door_notify

The `garage_door_notify` app has helper methods (`_should_notify`, `_from_state_display`, `_build_notification`) that are easy to unit test without mocking. Add pytest and tests to the project:

```bash
pip install pytest
```

Create `appdaemon/apps/tests/test_garage_door_notify.py` and test the pure logic. Use `patch.object` for `_send_notifications` or `call_service` when testing the full flow.
