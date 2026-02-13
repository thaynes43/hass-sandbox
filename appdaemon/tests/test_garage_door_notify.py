"""Unit tests for garage_door_notify app."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

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
        app.log = MagicMock()
        app.get_state = MagicMock(return_value="Garage Door")
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

    def test_consolidated_notification_output(self):
        """Output consolidated notification format for key durations (run with -s to see)."""
        app = self._make_app()
        door_name = "Tesla Garage Door"
        for was_open, duration_secs in [
            (True, 45),
            (True, 65),
            (True, 125),
            (False, 30),
            (False, 120),
        ]:
            title, message = app._build_consolidated_notification(
                door_name, was_open=was_open, duration_secs=duration_secs
            )
            state = "open" if was_open else "closed"
            print(f"\nWas {state} for {duration_secs}s ({app._format_duration(duration_secs)}):")
            print(f"  title:   {title}")
            print(f"  message: {message}")

    # --- Consolidation tests ---

    def test_format_duration(self):
        app = self._make_app()
        assert app._format_duration(0) == "0 minutes and 0 seconds"
        assert app._format_duration(45) == "0 minutes and 45 seconds"
        assert app._format_duration(60) == "1 minute and 0 seconds"
        assert app._format_duration(65) == "1 minute and 5 seconds"
        assert app._format_duration(125) == "2 minutes and 5 seconds"
        assert app._format_duration(1) == "0 minutes and 1 second"

    def test_build_consolidated_notification_was_open(self):
        app = self._make_app()
        title, message = app._build_consolidated_notification("Tesla Garage", was_open=True, duration_secs=125)
        assert title == "Tesla Garage Opened & Closed"
        assert "was open for 2 minutes and 5 seconds before it closed" in message

    def test_build_consolidated_notification_was_closed(self):
        app = self._make_app()
        title, message = app._build_consolidated_notification("Tesla Garage", was_open=False, duration_secs=45)
        assert title == "Tesla Garage Closed & Opened"
        assert "was closed for 0 minutes and 45 seconds before it opened" in message

    def test_delay_expires_sends_single_notification(self):
        """When consolidation delay expires, send single notification (no second transition)."""
        app = self._make_app({"notify_services": ["notify.test"]})
        app._pending = {}
        app.run_in = MagicMock(return_value="handle_123")

        app._on_door_state("cover.door", None, "closed", "open", {})

        app.run_in.assert_called_once()
        callback = app.run_in.call_args[0][0]
        kwargs = app.run_in.call_args[1]
        assert kwargs["new_state"] == "open"
        assert kwargs["door_name"] == "Garage Door"

        # Simulate delay expiry: call the callback (AppDaemon passes kwargs dict)
        callback(kwargs)

        app.call_service.assert_called_once()
        _, call_kwargs = app.call_service.call_args
        assert "Garage Door Opened" in call_kwargs["title"]
        assert "is now open" in call_kwargs["message"]

    def test_second_transition_during_delay_sends_consolidated_notification(self):
        """When second transition happens during delay, send consolidated message."""
        app = self._make_app({"notify_services": ["notify.test"]})
        app._pending = {}
        app.run_in = MagicMock(return_value="handle_123")
        app.cancel_timer = MagicMock()

        with patch("time.time", side_effect=[1000.0, 1065.0]):
            app._on_door_state("cover.door", None, "closed", "open", {})
            app._on_door_state("cover.door", None, "open", "closed", {})

        app.cancel_timer.assert_called_once_with("handle_123")
        app.call_service.assert_called_once()
        _, call_kwargs = app.call_service.call_args
        assert "Opened & Closed" in call_kwargs["title"]
        assert "was open for 1 minute and 5 seconds before it closed" in call_kwargs["message"]

    def test_second_transition_closed_then_open_consolidated(self):
        """closed -> open during delay: consolidated 'was closed for X'."""
        app = self._make_app({"notify_services": ["notify.test"]})
        app._pending = {}
        app.run_in = MagicMock(return_value="handle_123")
        app.cancel_timer = MagicMock()

        with patch("time.time", side_effect=[1000.0, 1030.0]):
            app._on_door_state("cover.door", None, "open", "closed", {})
            app._on_door_state("cover.door", None, "closed", "open", {})

        app.cancel_timer.assert_called_once_with("handle_123")
        app.call_service.assert_called_once()
        _, call_kwargs = app.call_service.call_args
        assert "Closed & Opened" in call_kwargs["title"]
        assert "was closed for 0 minutes and 30 seconds before it opened" in call_kwargs["message"]
