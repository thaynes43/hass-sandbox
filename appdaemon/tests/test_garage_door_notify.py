"""
Unit tests for garage_door_notify (incl DetectionSummaryStore integration).

These tests run without AppDaemon; we mock hassapi and stub the store and thread behavior.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


# Mock hassapi before importing garage_door_notify (tests run without AppDaemon)
class _MockHass:
    def __init__(self, ad, config):
        pass


mock_hass = MagicMock()
mock_hass.Hass = _MockHass
sys.modules["hassapi"] = mock_hass


from garage_door_notify import GarageDoorNotify  # noqa: E402


class _FakeStore:
    def __init__(self, *, bundle=None, wait_bundle=None):
        self._bundle = bundle
        self._wait_bundle = wait_bundle
        self.calls = []

    def get_best_bundle(self, *args, **kwargs):
        self.calls.append(("get_best_bundle", args, kwargs))
        return self._bundle

    def wait_for_bundle(self, *args, **kwargs):
        self.calls.append(("wait_for_bundle", args, kwargs))
        return self._wait_bundle

    def mark_consumed(self, *args, **kwargs):
        self.calls.append(("mark_consumed", args, kwargs))
        return True


class _ImmediateThread:
    def __init__(self, *, target, name=None):
        self._target = target
        self.name = name
        self.daemon = True

    def start(self):
        self._target()


class TestGarageDoorNotify:
    def _make_app(self, args: dict) -> GarageDoorNotify:
        ad = MagicMock()
        config = MagicMock()
        app = GarageDoorNotify(ad, config)
        app.args = args
        app.log = MagicMock()
        app.listen_state = MagicMock()
        app.run_in = MagicMock(return_value="handle-1")
        app.cancel_timer = MagicMock()
        app.call_service = MagicMock()
        app.get_state = MagicMock(return_value="Garage Door")
        app.list_namespaces = MagicMock(return_value=["default"])
        app._pending = {}
        return app

    def test_should_notify_filters(self):
        app = self._make_app({})
        assert app._should_notify("closed", "open") is True
        assert app._should_notify("open", "open") is False
        assert app._should_notify("unknown", "open") is False
        assert app._should_notify(None, "open") is False

    def test_from_state_display_opening_closing(self):
        app = self._make_app({})
        assert app._from_state_display("opening") == "closed"
        assert app._from_state_display("closing") == "open"

    def test_from_state_display_passthrough(self):
        app = self._make_app({})
        assert app._from_state_display("open") == "open"
        assert app._from_state_display("closed") == "closed"
        assert app._from_state_display(None) == "unknown"

    def test_build_notification_open(self):
        app = self._make_app({})
        title, message = app._build_notification("Garage Door", "open", "closed")
        assert title == "Garage Door Opened"
        assert "is now open" in message
        assert "was closed" in message

    def test_build_notification_closed(self):
        app = self._make_app({})
        title, message = app._build_notification("Garage Door", "closed", "open")
        assert title == "Garage Door Closed"
        assert "is now closed" in message
        assert "was open" in message

    def test_send_notifications_calls_services(self):
        app = self._make_app({"notify_services": ["notify.test_service"]})
        app._send_notifications("Title", "Message")
        app.call_service.assert_called_once_with("notify/test_service", title="Title", message="Message")

    def test_send_notifications_includes_image_when_provided(self):
        app = self._make_app({"notify_services": ["notify.test_service"]})
        app._send_notifications("Title", "Message", image_web_path="/api/camera_proxy/camera.best")
        app.call_service.assert_called_once_with(
            "notify/test_service",
            title="Title",
            message="Message",
            data={"image": "/api/camera_proxy/camera.best"},
        )

    def test_send_notifications_multiple(self):
        app = self._make_app({"notify_services": ["notify.svc1", "notify.svc2"]})
        app._send_notifications("T", "M")
        assert app.call_service.call_count == 2
        calls = [c[0][0] for c in app.call_service.call_args_list]
        assert "notify/svc1" in calls
        assert "notify/svc2" in calls

    def test_format_duration(self):
        app = self._make_app({})
        assert app._format_duration(0) == "0 minutes and 0 seconds"
        assert app._format_duration(45) == "0 minutes and 45 seconds"
        assert app._format_duration(60) == "1 minute and 0 seconds"
        assert app._format_duration(65) == "1 minute and 5 seconds"
        assert app._format_duration(125) == "2 minutes and 5 seconds"
        assert app._format_duration(1) == "0 minutes and 1 second"

    def test_build_consolidated_notification_was_open(self):
        app = self._make_app({})
        title, message = app._build_consolidated_notification("Tesla Garage", was_open=True, duration_secs=125)
        assert title == "Tesla Garage Opened & Closed"
        assert "was open for 2 minutes and 5 seconds" in message

    def test_build_consolidated_notification_was_closed(self):
        app = self._make_app({})
        title, message = app._build_consolidated_notification("Tesla Garage", was_open=False, duration_secs=45)
        assert title == "Tesla Garage Closed & Opened"
        assert "was closed for 0 minutes and 45 seconds" in message

    def test_delay_expires_sends_single_notification_when_ai_disabled(self):
        app = self._make_app({"notify_services": ["notify.test"], "ai_enabled": False})
        app._pending = {}
        app.run_in = MagicMock(return_value="handle_123")

        with patch("garage_door_notify.time.time", side_effect=[1000.0, 1000.0]):
            app._on_door_state("cover.door", None, "closed", "open", {})

        # Simulate delay expiry: call the callback (AppDaemon passes kwargs dict)
        cb = app.run_in.call_args[0][0]
        kw = app.run_in.call_args[1]
        cb(kw)

        app.call_service.assert_called_once()
        _, call_kwargs = app.call_service.call_args
        assert "Garage Door Opened" in call_kwargs["title"]
        assert "is now open" in call_kwargs["message"]

    def test_get_detection_summary_waits_then_consumes(self, monkeypatch):
        bundle = {
            "run_id": "r1",
            "best": {"summary": "Person in garage.", "image_url": "/api/camera_proxy/camera.x", "image_web_path": ""},
        }
        store = _FakeStore(bundle=None, wait_bundle=bundle)
        monkeypatch.setattr(sys.modules["garage_door_notify"], "DETECTION_SUMMARY_STORE", store)

        app = self._make_app(
            {"ai_enabled": True, "ai_bundle_key": "garage", "ai_wait_timeout_s": 5, "ai_max_bundle_age_s": 120}
        )
        got = app._get_detection_summary(10, 20)
        assert got is not None
        assert got["image"] == "/api/camera_proxy/camera.x"
        assert any(c[0] == "mark_consumed" for c in store.calls)

    def test_on_delay_expired_schedules_async_send_with_ai(self, monkeypatch):
        # Force thread to run inline for determinism
        monkeypatch.setattr(sys.modules["garage_door_notify"].threading, "Thread", _ImmediateThread)

        bundle = {
            "run_id": "r2",
            "best": {"summary": "1 person standing center.", "image_url": "/api/camera_proxy/camera.best", "image_web_path": ""},
        }
        store = _FakeStore(bundle=bundle)
        monkeypatch.setattr(sys.modules["garage_door_notify"], "DETECTION_SUMMARY_STORE", store)

        app = self._make_app(
            {
                "ai_enabled": True,
                "ai_bundle_key": "garage",
                "ai_wait_timeout_s": 0,
                "ai_max_bundle_age_s": 120,
                "ai_window_pad_s": 0,
            }
        )
        app._send_notifications = MagicMock()

        entity_id = "cover.ratgdov25i_x_door"
        app._pending[entity_id] = {
            "state": "open",
            "timestamp": 100.0,
            "handle": "handle-1",
            "door_name": "Garage Door",
            "from_display": "closed",
        }

        # run_in should call the callback immediately when delay==0
        def run_in_side_effect(cb, delay, **kw):
            assert delay == 0
            cb(kw)
            return "h"

        app.run_in.side_effect = run_in_side_effect
        monkeypatch.setattr(sys.modules["garage_door_notify"].time, "time", lambda: 110.0)

        app._on_delay_expired({"entity_id": entity_id})
        assert app._send_notifications.call_count == 1
        _, kwargs = app._send_notifications.call_args
        assert kwargs["image_web_path"] == "/api/camera_proxy/camera.best"

    def test_consolidated_transition_cancels_timer_and_schedules_send(self, monkeypatch):
        monkeypatch.setattr(sys.modules["garage_door_notify"].threading, "Thread", _ImmediateThread)
        store = _FakeStore(bundle=None)
        monkeypatch.setattr(sys.modules["garage_door_notify"], "DETECTION_SUMMARY_STORE", store)

        app = self._make_app({"ai_enabled": False, "consolidation_delay": 300})
        app._send_notifications = MagicMock()

        entity_id = "cover.ratgdov25i_x_door"
        t = SimpleNamespace(now=1000.0)
        monkeypatch.setattr(sys.modules["garage_door_notify"].time, "time", lambda: t.now)

        # First transition schedules delayed notification
        app._on_door_state(entity_id, "state", "closed", "open", {})
        assert entity_id in app._pending

        # Second transition triggers consolidated send
        t.now = 1010.0
        app.run_in.side_effect = lambda cb, delay, **kw: cb(kw) if delay == 0 else "h"
        app._on_door_state(entity_id, "state", "open", "closed", {})

        app.cancel_timer.assert_called_once()
        assert app._send_notifications.call_count == 1
