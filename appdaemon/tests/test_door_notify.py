"""
Unit tests for door_notify (DoorNotify) including DetectionSummaryStore integration.

Covers both cover entity behavior (open/closed states, backward-compat with former
garage_door_notify) and binary_sensor behavior (on/off states, bulkhead config).

These tests run without AppDaemon; we mock hassapi and stub the store and thread behavior.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


# Mock hassapi before importing door_notify (tests run without AppDaemon)
class _MockHass:
    def __init__(self, ad, config):
        pass


mock_hass = MagicMock()
mock_hass.Hass = _MockHass
sys.modules["hassapi"] = mock_hass

# Ensure AppDaemon apps dir is importable (WSL/CI safe).
_APPS_DIR = Path(__file__).resolve().parents[1] / "apps"
if str(_APPS_DIR) not in sys.path:
    sys.path.insert(0, str(_APPS_DIR))

from door_notify.door_notify import DoorNotify  # noqa: E402


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

    def get_bundle_by_run_id(self, *args, **kwargs):
        self.calls.append(("get_bundle_by_run_id", args, kwargs))
        return None

    def wait_for_run_id(self, *args, **kwargs):
        self.calls.append(("wait_for_run_id", args, kwargs))
        return self._wait_bundle


class _ImmediateThread:
    def __init__(self, *, target, name=None):
        self._target = target
        self.name = name
        self.daemon = True

    def start(self):
        self._target()


def _make_app(args: dict, *, friendly_name: str = "Garage Door") -> DoorNotify:
    """Create a DoorNotify instance with mocked AppDaemon methods.

    Sets _open_state, _closed_state, and _intermediate_state_map from args
    (matching the logic in initialize()) so tests can call methods directly
    without invoking initialize().
    """
    ad = MagicMock()
    config = MagicMock()
    app = DoorNotify(ad, config)
    app.args = args
    app.log = MagicMock()
    app.listen_state = MagicMock()
    app.run_in = MagicMock(return_value="handle-1")
    app.cancel_timer = MagicMock()
    app.call_service = MagicMock()
    app.get_state = MagicMock(return_value=friendly_name)
    app.list_namespaces = MagicMock(return_value=["default"])
    app._pending = {}
    # Mirror initialize() state-mapping logic so methods work without calling initialize().
    app._open_state = str(args.get("door_open_state", DoorNotify.DEFAULTS["door_open_state"]))
    app._closed_state = str(args.get("door_closed_state", DoorNotify.DEFAULTS["door_closed_state"]))
    if app._open_state == "open" and app._closed_state == "closed":
        app._intermediate_state_map = {"opening": "closed", "closing": "open"}
    else:
        app._intermediate_state_map = args.get("intermediate_state_map", {})
    return app


# ---------------------------------------------------------------------------
# Cover entity tests (backward-compatible with former garage_door_notify)
# ---------------------------------------------------------------------------

class TestDoorNotifyCovers:
    """Tests for cover-entity-based notifications (garage config, open/closed states)."""

    def _make_app(self, args: dict) -> DoorNotify:
        return _make_app(args)

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
        app.call_service.assert_called_once_with(
            "notify/test_service",
            title="Title",
            message="Message",
            data={"url": "/detection-summary/garage", "clickAction": "/detection-summary/garage"},
        )

    def test_send_notifications_includes_image_when_provided(self):
        app = self._make_app({"notify_services": ["notify.test_service"]})
        app._send_notifications("Title", "Message", image_web_path="/api/camera_proxy/camera.best")
        app.call_service.assert_called_once_with(
            "notify/test_service",
            title="Title",
            message="Message",
            data={
                "image": "/api/camera_proxy/camera.best",
                "url": "/detection-summary/garage",
                "clickAction": "/detection-summary/garage",
            },
        )

    def test_send_notifications_includes_action_when_run_id_provided(self):
        app = self._make_app({"notify_services": ["notify.test_service"]})
        app._send_notifications("Title", "Message", run_id="run-123")
        app.call_service.assert_called_once_with(
            "notify/test_service",
            title="Title",
            message="Message",
            data={
                "url": "/detection-summary/garage",
                "clickAction": "/detection-summary/garage",
            },
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

        with patch("door_notify.door_notify.time.time", side_effect=[1000.0, 1000.0]):
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
            "best": {"summary": "Person in garage.", "image_url": "/api/camera_proxy/camera.best", "image_web_path": ""},
            "generated_image": {"image_url": "/api/camera_proxy/camera.gen"},
        }
        store = _FakeStore(bundle=None, wait_bundle=bundle)
        monkeypatch.setattr(sys.modules["door_notify.door_notify"], "DETECTION_SUMMARY_STORE", store)

        app = self._make_app(
            {"ai_enabled": True, "ai_bundle_key": "garage", "ai_wait_timeout_s": 5, "ai_max_bundle_age_s": 120}
        )
        got = app._get_detection_summary(10, 20)
        assert got is not None
        assert got["image"] == "/api/camera_proxy/camera.gen"
        # Wait call should extend the eligible window end by timeout_s
        wait_calls = [c for c in store.calls if c[0] == "wait_for_bundle"]
        assert wait_calls, "expected wait_for_bundle to be called"
        _, args, kwargs = wait_calls[0]
        # args: (bundle_key, window_start_epoch, window_end_epoch, timeout_s=...)
        assert args[1] == 10
        assert args[2] >= 25  # 20 + 5s extension at minimum
        assert any(c[0] == "mark_consumed" for c in store.calls)

    def test_pending_adopts_run_started_for_consolidation_window(self, monkeypatch):
        app = self._make_app(
            {
                "ai_enabled": True,
                "ai_bundle_key": "garage",
                "ai_use_detection_summary_events": True,
                "ai_window_pad_s": 5,
                "consolidation_delay": 300,
            }
        )
        # Create a pending door event
        app._pending["cover.door"] = {
            "state": "open",
            "timestamp": 100.0,
            "handle": "h",
            "door_name": "Garage Door",
            "from_display": "closed",
            "ai_run_id": None,
            "ai_run_started_ts": None,
        }
        monkeypatch.setattr(sys.modules["door_notify.door_notify"].time, "time", lambda: 120.0)

        # Simulate run_started arriving during consolidation window
        app._on_detection_summary_run_started(
            "detection_summary/run_started",
            {"bundle_key": "garage", "run_id": "r123", "started_ts": 115.0},
            {},
        )
        assert app._pending["cover.door"]["ai_run_id"] == "r123"

    def test_get_detection_summary_event_driven_waits_by_run_id(self, monkeypatch):
        bundle = {
            "run_id": "r99",
            "best": {"summary": "Person in garage.", "image_url": "/api/camera_proxy/camera.best", "image_web_path": ""},
            "generated_image": {"image_url": "/api/camera_proxy/camera.gen"},
        }
        store = _FakeStore(bundle=None, wait_bundle=bundle)
        monkeypatch.setattr(sys.modules["door_notify.door_notify"], "DETECTION_SUMMARY_STORE", store)

        app = self._make_app(
            {
                "ai_enabled": True,
                "ai_bundle_key": "garage",
                "ai_wait_timeout_s": 5,
                "ai_max_bundle_age_s": 120,
                "ai_use_detection_summary_events": True,
                "ai_run_started_lookback_s": 900,
            }
        )
        # Simulate that we observed a recent run_started event.
        app._latest_run_started = {"garage": {"run_id": "r99", "started_ts": 105.0}}
        monkeypatch.setattr(sys.modules["door_notify.door_notify"].time, "time", lambda: 110.0)

        got = app._get_detection_summary(10, 20)
        assert got is not None
        assert got["run_id"] == "r99"
        assert got["image"] == "/api/camera_proxy/camera.gen"
        assert any(c[0] == "wait_for_run_id" for c in store.calls)
        assert not any(c[0] == "wait_for_bundle" for c in store.calls)

    def test_on_delay_expired_schedules_async_send_with_ai(self, monkeypatch):
        # Force thread to run inline for determinism
        monkeypatch.setattr(sys.modules["door_notify.door_notify"].threading, "Thread", _ImmediateThread)

        bundle = {
            "run_id": "r2",
            "best": {"summary": "1 person standing center.", "image_url": "/api/camera_proxy/camera.best", "image_web_path": ""},
            "generated_image": {"image_url": "/api/camera_proxy/camera.gen"},
        }
        store = _FakeStore(bundle=bundle)
        monkeypatch.setattr(sys.modules["door_notify.door_notify"], "DETECTION_SUMMARY_STORE", store)

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
        monkeypatch.setattr(sys.modules["door_notify.door_notify"].time, "time", lambda: 110.0)

        app._on_delay_expired({"entity_id": entity_id})
        assert app._send_notifications.call_count == 1
        _, kwargs = app._send_notifications.call_args
        assert kwargs["image_web_path"] == "/api/camera_proxy/camera.gen"

    def test_consolidated_transition_cancels_timer_and_schedules_send(self, monkeypatch):
        monkeypatch.setattr(sys.modules["door_notify.door_notify"].threading, "Thread", _ImmediateThread)
        store = _FakeStore(bundle=None)
        monkeypatch.setattr(sys.modules["door_notify.door_notify"], "DETECTION_SUMMARY_STORE", store)

        app = self._make_app({"ai_enabled": False, "consolidation_delay": 300})
        app._send_notifications = MagicMock()

        entity_id = "cover.ratgdov25i_x_door"
        t = SimpleNamespace(now=1000.0)
        monkeypatch.setattr(sys.modules["door_notify.door_notify"].time, "time", lambda: t.now)

        # First transition schedules delayed notification
        app._on_door_state(entity_id, "state", "closed", "open", {})
        assert entity_id in app._pending

        # Second transition triggers consolidated send
        t.now = 1010.0
        app.run_in.side_effect = lambda cb, delay, **kw: cb(kw) if delay == 0 else "h"
        app._on_door_state(entity_id, "state", "open", "closed", {})

        app.cancel_timer.assert_called_once()
        assert app._send_notifications.call_count == 1


# ---------------------------------------------------------------------------
# Binary sensor tests (bulkhead config, on/off states)
# ---------------------------------------------------------------------------

class TestDoorNotifyBinarySensor:
    """Tests for binary_sensor-based door notifications (bulkhead config, on/off states)."""

    def _make_app(self, args: dict) -> DoorNotify:
        full_args = {
            "door_open_state": "on",
            "door_closed_state": "off",
            "notify_services": ["notify.test_service"],
            "notification_url": "/detection-summary/bulkhead",
            **args,
        }
        return _make_app(full_args, friendly_name="Bulkhead Door")

    def test_binary_sensor_open_transition_schedules_pending(self):
        """binary_sensor 'off' -> 'on' (door opened) schedules pending notification."""
        app = self._make_app({"ai_enabled": False})
        app._on_door_state("binary_sensor.usl_entry_contact", None, "off", "on", {})
        assert "binary_sensor.usl_entry_contact" in app._pending
        assert app._pending["binary_sensor.usl_entry_contact"]["state"] == "on"

    def test_binary_sensor_close_transition_schedules_pending(self):
        """binary_sensor 'on' -> 'off' (door closed) schedules pending notification."""
        app = self._make_app({"ai_enabled": False})
        app._on_door_state("binary_sensor.usl_entry_contact", None, "on", "off", {})
        assert "binary_sensor.usl_entry_contact" in app._pending
        assert app._pending["binary_sensor.usl_entry_contact"]["state"] == "off"

    def test_binary_sensor_no_intermediate_states(self):
        """binary_sensor has no intermediate states -- passthrough only."""
        app = self._make_app({})
        assert app._from_state_display("on") == "on"
        assert app._from_state_display("off") == "off"
        assert app._from_state_display(None) == "unknown"
        # Verify "opening"/"closing" are NOT mapped (unlike cover entities)
        assert app._from_state_display("opening") == "opening"
        assert app._from_state_display("closing") == "closing"

    def test_binary_sensor_build_notification_open(self):
        """'on' state maps to Opened action."""
        app = self._make_app({})
        title, message = app._build_notification("Bulkhead Door", "on", "off")
        assert title == "Bulkhead Door Opened"
        assert "is now on" in message
        assert "was off" in message

    def test_binary_sensor_build_notification_closed(self):
        """'off' state maps to Closed action."""
        app = self._make_app({})
        title, message = app._build_notification("Bulkhead Door", "off", "on")
        assert title == "Bulkhead Door Closed"
        assert "is now off" in message
        assert "was on" in message

    def test_binary_sensor_should_notify_filters(self):
        """Standard unknown/unavailable/same-state filtering applies."""
        app = self._make_app({})
        assert app._should_notify("off", "on") is True
        assert app._should_notify("on", "off") is True
        assert app._should_notify("on", "on") is False
        assert app._should_notify("unknown", "on") is False
        assert app._should_notify(None, "on") is False

    def test_binary_sensor_notification_url_in_data(self):
        """Notification uses bulkhead-specific URL."""
        app = self._make_app({"ai_enabled": False})
        app._send_notifications("Bulkhead Door Opened", "Bulkhead Door is now on (was off).")
        app.call_service.assert_called_once_with(
            "notify/test_service",
            title="Bulkhead Door Opened",
            message="Bulkhead Door is now on (was off).",
            data={
                "url": "/detection-summary/bulkhead",
                "clickAction": "/detection-summary/bulkhead",
            },
        )

    def test_binary_sensor_delay_expires_sends_notification(self, monkeypatch):
        """After consolidation delay, single open notification sent."""
        app = self._make_app({"ai_enabled": False})
        app._pending = {}
        app.run_in = MagicMock(return_value="handle_123")

        with patch("door_notify.door_notify.time.time", side_effect=[1000.0, 1000.0]):
            app._on_door_state("binary_sensor.usl_entry_contact", None, "off", "on", {})

        cb = app.run_in.call_args[0][0]
        kw = app.run_in.call_args[1]
        cb(kw)

        app.call_service.assert_called_once()
        _, call_kwargs = app.call_service.call_args
        assert "Bulkhead Door Opened" in call_kwargs["title"]
        assert "is now on" in call_kwargs["message"]

    def test_binary_sensor_consolidation_rapid_open_close(self, monkeypatch):
        """Rapid on->off consolidates into single notification."""
        monkeypatch.setattr(sys.modules["door_notify.door_notify"].threading, "Thread", _ImmediateThread)
        store = _FakeStore(bundle=None)
        monkeypatch.setattr(sys.modules["door_notify.door_notify"], "DETECTION_SUMMARY_STORE", store)

        app = self._make_app({"ai_enabled": False, "consolidation_delay": 300})
        app._send_notifications = MagicMock()

        entity_id = "binary_sensor.usl_entry_contact"
        t = SimpleNamespace(now=1000.0)
        monkeypatch.setattr(sys.modules["door_notify.door_notify"].time, "time", lambda: t.now)

        # First: off -> on (door opened)
        app._on_door_state(entity_id, "state", "off", "on", {})
        assert entity_id in app._pending
        assert app._pending[entity_id]["state"] == "on"

        # Second: on -> off (door closed) within consolidation window
        t.now = 1010.0
        app.run_in.side_effect = lambda cb, delay, **kw: cb(kw) if delay == 0 else "h"
        app._on_door_state(entity_id, "state", "on", "off", {})

        app.cancel_timer.assert_called_once()
        assert app._send_notifications.call_count == 1
        call_positional, _ = app._send_notifications.call_args
        assert "Opened & Closed" in call_positional[0]  # positional title arg

    def test_binary_sensor_consolidated_was_open_message(self, monkeypatch):
        """Consolidated notification correctly describes 'was open'."""
        app = self._make_app({})
        title, message = app._build_consolidated_notification("Bulkhead Door", was_open=True, duration_secs=30)
        assert title == "Bulkhead Door Opened & Closed"
        assert "was open for" in message

    def test_binary_sensor_ai_enrichment(self, monkeypatch):
        """AI enrichment attaches generated image when bundle available for bulkhead."""
        monkeypatch.setattr(sys.modules["door_notify.door_notify"].threading, "Thread", _ImmediateThread)

        bundle = {
            "run_id": "r-bulkhead-1",
            "best": {"summary": "Person at bulkhead.", "image_url": "", "image_web_path": ""},
            "generated_image": {"image_url": "/api/camera_proxy/camera.bulkhead_gen"},
        }
        store = _FakeStore(bundle=bundle)
        monkeypatch.setattr(sys.modules["door_notify.door_notify"], "DETECTION_SUMMARY_STORE", store)

        app = self._make_app(
            {
                "ai_enabled": True,
                "ai_bundle_key": "bulkhead",
                "ai_wait_timeout_s": 0,
                "ai_max_bundle_age_s": 120,
                "ai_window_pad_s": 0,
            }
        )
        app._send_notifications = MagicMock()

        entity_id = "binary_sensor.usl_entry_contact"
        app._pending[entity_id] = {
            "state": "on",
            "timestamp": 100.0,
            "handle": "handle-1",
            "door_name": "Bulkhead Door",
            "from_display": "off",
        }

        def run_in_side_effect(cb, delay, **kw):
            assert delay == 0
            cb(kw)
            return "h"

        app.run_in.side_effect = run_in_side_effect
        monkeypatch.setattr(sys.modules["door_notify.door_notify"].time, "time", lambda: 110.0)

        app._on_delay_expired({"entity_id": entity_id})

        assert app._send_notifications.call_count == 1
        _, kwargs = app._send_notifications.call_args
        assert kwargs["image_web_path"] == "/api/camera_proxy/camera.bulkhead_gen"

    def test_binary_sensor_run_started_adopted_for_pending(self, monkeypatch):
        """run_started event during consolidation window is attached to pending."""
        app = self._make_app(
            {
                "ai_enabled": True,
                "ai_bundle_key": "bulkhead",
                "ai_use_detection_summary_events": True,
                "ai_window_pad_s": 5,
                "consolidation_delay": 300,
            }
        )
        app._pending["binary_sensor.usl_entry_contact"] = {
            "state": "on",
            "timestamp": 100.0,
            "handle": "h",
            "door_name": "Bulkhead Door",
            "from_display": "off",
            "ai_run_id": None,
            "ai_run_started_ts": None,
        }
        monkeypatch.setattr(sys.modules["door_notify.door_notify"].time, "time", lambda: 120.0)

        app._on_detection_summary_run_started(
            "detection_summary/run_started",
            {"bundle_key": "bulkhead", "run_id": "r-bh-99", "started_ts": 115.0},
            {},
        )
        assert app._pending["binary_sensor.usl_entry_contact"]["ai_run_id"] == "r-bh-99"
