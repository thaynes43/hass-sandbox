"""Unit tests for garage_door_notify app."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

# Mock hassapi before importing garage_door_notify (tests run without AppDaemon)
class _MockHass:
    def __init__(self, ad, config):
        pass

mock_hass = MagicMock()
mock_hass.Hass = _MockHass
sys.modules["hassapi"] = mock_hass
# Add apps to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))

from garage_door_notify import GarageDoorNotify


class TestGarageDoorNotify:
    """Unit tests for GarageDoorNotify."""

    def _make_app(self, args=None):
        """Create app instance with mocked AD."""
        ad = MagicMock()
        config = MagicMock()
        app = GarageDoorNotify(ad, config)
        app.args = args or {}
        app.call_service = MagicMock()  # Mock HA call
        return app

    def test_should_notify_valid_change(self):
        app = self._make_app()
        assert app._should_notify("closed", "open") is True
        assert app._should_notify("open", "closed") is True

    def test_should_notify_skips_unknown_unavailable(self):
        app = self._make_app()
        assert app._should_notify("unknown", "open") is False
        assert app._should_notify("closed", "unavailable") is False
        assert app._should_notify(None, "open") is False
        assert app._should_notify("open", None) is False

    def test_should_notify_skips_no_change(self):
        app = self._make_app()
        assert app._should_notify("open", "open") is False

    def test_from_state_display_opening_closing(self):
        app = self._make_app()
        assert app._from_state_display("opening") == "closed"
        assert app._from_state_display("closing") == "open"

    def test_from_state_display_passthrough(self):
        app = self._make_app()
        assert app._from_state_display("open") == "open"
        assert app._from_state_display("closed") == "closed"
        assert app._from_state_display(None) == "unknown"

    def test_build_notification_open(self):
        app = self._make_app()
        title, message = app._build_notification("Garage Door", "open", "closed")
        assert title == "Garage Door Opened"
        assert "is now open" in message
        assert "was closed" in message

    def test_build_notification_closed(self):
        app = self._make_app()
        title, message = app._build_notification("Garage Door", "closed", "open")
        assert title == "Garage Door Closed"
        assert "is now closed" in message
        assert "was open" in message

    def test_send_notifications_calls_services(self):
        app = self._make_app({"notify_services": ["notify.test_service"]})
        app._send_notifications("Title", "Message")
        app.call_service.assert_called_once_with(
            "notify/test_service", title="Title", message="Message"
        )

    def test_send_notifications_multiple(self):
        app = self._make_app({
            "notify_services": ["notify.svc1", "notify.svc2"]
        })
        app._send_notifications("T", "M")
        assert app.call_service.call_count == 2
        calls = [c[0][0] for c in app.call_service.call_args_list]
        assert "notify/svc1" in calls
        assert "notify/svc2" in calls

    def test_notification_output_for_state_changes(self):
        """Output exact title and message for key state transitions (run with -s to see)."""
        app = self._make_app()
        door_name = "Garage Door"
        transitions = [
            ("opening", "closed"),
            ("closing", "open"),
            ("unavailable", "closed"),
            ("unavailable", "open"),
        ]
        for old, new in transitions:
            from_display = app._from_state_display(old)
            title, message = app._build_notification(door_name, new, from_display)
            print(f"\n{old} -> {new}:")
            print(f"  title:   {title}")
            print(f"  message: {message}")
