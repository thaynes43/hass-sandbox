"""Tests for PhotoFrameViewerApp relay command routing.

Verifies that _on_command correctly routes relay events from the dashboard
card to the appropriate internal handlers.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, call, patch

# Mock hassapi before importing the app
mock_hass = MagicMock()
mock_hass.Hass = type("_MockHass", (), {"__init__": lambda self, *a, **kw: None})
sys.modules["hassapi"] = mock_hass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))

from photo_frame_viewer.photo_frame_viewer_app import PhotoFrameViewerApp


def _make_app(extra_args: dict | None = None) -> PhotoFrameViewerApp:
    """Create a PhotoFrameViewerApp initialized and ready for command routing tests."""
    td = tempfile.mkdtemp(prefix="pfv_relay_test_")

    # Write some source files so initialize() can scan the dir
    for fname in ["IMG_001.jpg", "IMG_002.jpg", "IMG_003.jpg"]:
        Path(td, fname).write_bytes(b"")

    ad = MagicMock()
    app = PhotoFrameViewerApp(ad, MagicMock())

    base_args: dict = {
        "source_dir": td,
        "ha_local_url_base": "/local/photo-frame/live",
        "state_dir": td,
        "default_interval_s": 10,
    }
    if extra_args:
        base_args.update(extra_args)
    app.args = base_args

    app.get_state = MagicMock(return_value=None)
    app.set_state = MagicMock()
    app.call_service = MagicMock()
    app.listen_state = MagicMock()
    app.listen_event = MagicMock()
    app.run_every = MagicMock()
    app.run_in = MagicMock()
    app.cancel_timer = MagicMock()
    app.timer_running = MagicMock(return_value=False)
    app.datetime = MagicMock()
    app.log = MagicMock()
    app.create_task = MagicMock()
    app.name = "photo_frame_viewer_wall_display"

    app.initialize()
    # Reset mocks after initialize so tests start clean
    app.call_service.reset_mock()
    app.set_state.reset_mock()
    app.log.reset_mock()

    return app


def _dispatch(app: PhotoFrameViewerApp, command: str, payload: dict | None = None) -> None:
    """Simulate the relay script firing a command event."""
    data = {
        "command": command,
        "payload": json.dumps(payload or {}),
    }
    app._on_command(f"{app._entity_prefix}_photo_frame_command", data, {})


class TestTogglePause:
    def test_toggle_pause_from_playing(self):
        app = _make_app()
        assert app._paused is False

        _dispatch(app, "toggle_pause")

        assert app._paused is True

    def test_toggle_pause_publishes_sensor_paused(self):
        app = _make_app()
        _dispatch(app, "toggle_pause")

        app.set_state.assert_called()
        last_call = app.set_state.call_args
        assert last_call[1]["state"] == "paused"
        assert last_call[1]["attributes"]["paused"] == "true"

    def test_toggle_pause_from_paused(self):
        app = _make_app()
        app._paused = True

        _dispatch(app, "toggle_pause")

        assert app._paused is False

    def test_toggle_pause_from_paused_publishes_playing(self):
        app = _make_app()
        app._paused = True

        _dispatch(app, "toggle_pause")

        last_call = app.set_state.call_args
        assert last_call[1]["state"] == "playing"
        assert last_call[1]["attributes"]["paused"] == "false"

    def test_toggle_pause_cancels_timer_when_pausing(self):
        app = _make_app()
        app._timer_handle = MagicMock()  # simulate running timer
        app.timer_running.return_value = True

        _dispatch(app, "toggle_pause")

        app.cancel_timer.assert_called()

    def test_toggle_pause_schedules_timer_when_resuming(self):
        app = _make_app()
        app._paused = True

        _dispatch(app, "toggle_pause")

        # run_in called to schedule next tick
        app.run_in.assert_called()

    def test_toggle_pause_schedules_auto_resume_when_configured(self):
        app = _make_app({"pause_auto_resume_s": 120})
        app.run_in.reset_mock()

        _dispatch(app, "toggle_pause")

        app.run_in.assert_any_call(app._handle_pause_auto_resume, 120.0)

    def test_toggle_pause_cancels_auto_resume_when_resuming_manually(self):
        app = _make_app({"pause_auto_resume_s": 120})
        auto_resume_handle = MagicMock()
        app._paused = True
        app._pause_auto_resume_handle = auto_resume_handle
        app._pause_auto_resume_deadline = 123.0
        app.timer_running.return_value = True

        _dispatch(app, "toggle_pause")

        app.cancel_timer.assert_any_call(auto_resume_handle)
        assert app._pause_auto_resume_handle is None
        assert app._pause_auto_resume_deadline is None

    def test_auto_resume_callback_resumes_and_publishes(self):
        app = _make_app({"pause_auto_resume_s": 120})
        app._paused = True
        app._pause_auto_resume_handle = MagicMock()
        app._pause_auto_resume_deadline = 123.0
        app.set_state.reset_mock()
        app.run_in.reset_mock()

        app._handle_pause_auto_resume({})

        assert app._paused is False
        last_call = app.set_state.call_args
        assert last_call[1]["state"] == "playing"
        assert last_call[1]["attributes"]["paused"] == "false"
        assert app._pause_auto_resume_deadline is None
        app.run_in.assert_called()


class TestSetInterval:
    def test_set_interval_valid(self):
        app = _make_app()
        _dispatch(app, "set_interval", {"seconds": 30})

        assert app._interval == 30.0

    def test_set_interval_publishes_sensor(self):
        app = _make_app()
        _dispatch(app, "set_interval", {"seconds": 25})

        app.set_state.assert_called()
        last_call = app.set_state.call_args
        assert last_call[1]["attributes"]["interval_seconds"] == 25.0

    def test_set_interval_clamps_to_minimum(self):
        app = _make_app()
        _dispatch(app, "set_interval", {"seconds": 0.5})

        assert app._interval == 1.0

    def test_set_interval_invalid_string_ignored(self):
        app = _make_app()
        original = app._interval
        _dispatch(app, "set_interval", {"seconds": "banana"})

        assert app._interval == original
        # Warning was logged
        log_messages = [str(c) for c in app.log.call_args_list]
        assert any("invalid" in m.lower() or "warning" in m.lower()
                   for m in log_messages)

    def test_set_interval_negative_ignored(self):
        app = _make_app()
        original = app._interval
        _dispatch(app, "set_interval", {"seconds": -5})

        assert app._interval == original

    def test_set_interval_zero_ignored(self):
        app = _make_app()
        original = app._interval
        _dispatch(app, "set_interval", {"seconds": 0})

        assert app._interval == original

    def test_set_interval_persists_state(self):
        """set_interval saves to state file for persistence across restarts."""
        app = _make_app()
        _dispatch(app, "set_interval", {"seconds": 45})

        state_path = Path(app._state_file)
        assert state_path.is_file()
        data = json.loads(state_path.read_text())
        assert data["interval_seconds"] == 45.0

    def test_set_interval_reschedules_timer(self):
        app = _make_app()
        _dispatch(app, "set_interval", {"seconds": 20})

        # run_in called to reschedule with new interval
        app.run_in.assert_called()


class TestSetPauseAutoResume:
    def test_set_pause_auto_resume_valid(self):
        app = _make_app()

        _dispatch(app, "set_pause_auto_resume", {"seconds": 300})

        assert app.pause_auto_resume_s == 300.0

    def test_set_pause_auto_resume_zero_disables(self):
        app = _make_app()

        _dispatch(app, "set_pause_auto_resume", {"seconds": 0})

        assert app.pause_auto_resume_s == 0.0

    def test_set_pause_auto_resume_invalid_ignored(self):
        app = _make_app()
        original = app.pause_auto_resume_s

        _dispatch(app, "set_pause_auto_resume", {"seconds": "banana"})

        assert app.pause_auto_resume_s == original

    def test_set_pause_auto_resume_persists_state(self):
        app = _make_app()

        _dispatch(app, "set_pause_auto_resume", {"seconds": 300})

        state_path = Path(app._state_file)
        assert state_path.is_file()
        data = json.loads(state_path.read_text())
        assert data["pause_auto_resume_s"] == 300.0

    def test_set_pause_auto_resume_updates_sensor(self):
        app = _make_app()

        _dispatch(app, "set_pause_auto_resume", {"seconds": 300})

        attrs = app.set_state.call_args[1]["attributes"]
        assert attrs["pause_auto_resume_s"] == 300.0

    def test_set_pause_auto_resume_reschedules_when_paused(self):
        app = _make_app()
        app._paused = True
        app.run_in.reset_mock()

        _dispatch(app, "set_pause_auto_resume", {"seconds": 300})

        app.run_in.assert_any_call(app._handle_pause_auto_resume, 300.0)

    def test_set_pause_auto_resume_cancels_existing_when_disabled_while_paused(self):
        app = _make_app()
        auto_resume_handle = MagicMock()
        app._paused = True
        app._pause_auto_resume_handle = auto_resume_handle
        app._pause_auto_resume_deadline = 123.0
        app.timer_running.return_value = True

        _dispatch(app, "set_pause_auto_resume", {"seconds": 0})

        app.cancel_timer.assert_any_call(auto_resume_handle)
        assert app._pause_auto_resume_handle is None
        assert app._pause_auto_resume_deadline is None


class TestNextCommand:
    def test_next_calls_select_next(self):
        app = _make_app()
        _dispatch(app, "next")

        app.call_service.assert_called_with(
            "input_select/select_next",
            entity_id=app.picker_entity_id,
            cycle=True,
        )

    def test_next_does_not_publish_sensor(self):
        """next just routes to input_select, doesn't touch the sensor."""
        app = _make_app()
        _dispatch(app, "next")

        app.set_state.assert_not_called()


class TestPreviousCommand:
    def test_previous_calls_select_previous(self):
        app = _make_app()
        _dispatch(app, "previous")

        app.call_service.assert_called_with(
            "input_select/select_previous",
            entity_id=app.picker_entity_id,
            cycle=True,
        )

    def test_previous_does_not_publish_sensor(self):
        app = _make_app()
        _dispatch(app, "previous")

        app.set_state.assert_not_called()


class TestSelectCommand:
    def test_select_calls_select_option(self):
        app = _make_app()
        _dispatch(app, "select", {"image": "IMG_002.jpg"})

        app.call_service.assert_called_with(
            "input_select/select_option",
            entity_id=app.picker_entity_id,
            option="IMG_002.jpg",
        )

    def test_select_with_empty_image_does_nothing(self):
        app = _make_app()
        _dispatch(app, "select", {"image": ""})

        app.call_service.assert_not_called()

    def test_select_with_missing_image_key_does_nothing(self):
        app = _make_app()
        _dispatch(app, "select", {})

        app.call_service.assert_not_called()


class TestUnknownCommand:
    def test_unknown_command_logs_warning(self):
        app = _make_app()
        _dispatch(app, "frobnicate")

        log_messages = [str(c) for c in app.log.call_args_list]
        assert any("unknown" in m.lower() or "warning" in m.lower()
                   for m in log_messages)

    def test_unknown_command_does_not_crash(self):
        app = _make_app()
        # Should not raise
        _dispatch(app, "unknown_command_xyz")

    def test_invalid_json_payload_logs_warning(self):
        app = _make_app()
        # Send malformed payload
        app._on_command(
            "wall_display_photo_frame_command",
            {"command": "toggle_pause", "payload": "not-json{"},
            {},
        )

        log_messages = [str(c) for c in app.log.call_args_list]
        assert any("invalid" in m.lower() or "warning" in m.lower()
                   for m in log_messages)


class TestSensorStatePublishing:
    def test_sensor_entity_id_matches_prefix(self):
        app = _make_app()
        assert app._sensor_entity_id == "sensor.wall_display_photo_frame_status"

    def test_publish_sensor_state_includes_all_attributes(self):
        app = _make_app()
        app._last_published_local_url = "/local/photo-frame/live/1/IMG_001.jpg"
        app._cache_bust = "1234567890"
        app._paused = False
        app._interval = 15.0
        app._current_gen_id = "1"

        app._publish_sensor_state()

        app.set_state.assert_called_once()
        entity_id = app.set_state.call_args[0][0]
        attrs = app.set_state.call_args[1]["attributes"]

        assert entity_id == "sensor.wall_display_photo_frame_status"
        assert attrs["image_url"] == "/local/photo-frame/live/1/IMG_001.jpg"
        assert attrs["cache_bust"] == "1234567890"
        assert attrs["paused"] == "false"
        assert attrs["interval_seconds"] == 15.0
        assert attrs["current_gen"] == "1"

    def test_publish_sensor_state_playing_when_not_paused(self):
        app = _make_app()
        app._paused = False
        app._publish_sensor_state()

        state = app.set_state.call_args[1]["state"]
        assert state == "playing"

    def test_publish_sensor_state_paused_when_paused(self):
        app = _make_app()
        app._paused = True
        app._publish_sensor_state()

        state = app.set_state.call_args[1]["state"]
        assert state == "paused"

    def test_publish_sensor_state_includes_auto_resume_metadata(self):
        app = _make_app({"pause_auto_resume_s": 90})
        app._paused = True
        app._pause_auto_resume_deadline = 12345.0

        app._publish_sensor_state()

        attrs = app.set_state.call_args[1]["attributes"]
        assert attrs["pause_auto_resume_s"] == 90.0
        assert attrs["pause_resume_at"] == 12345.0


class TestRuntimeStatePersistence:
    def test_initialize_loads_persisted_interval_and_auto_resume(self):
        td = tempfile.mkdtemp(prefix="pfv_runtime_state_")
        for fname in ["IMG_001.jpg", "IMG_002.jpg"]:
            Path(td, fname).write_bytes(b"")
        Path(td, "state.json").write_text(
            json.dumps({
                "interval_seconds": 42,
                "pause_auto_resume_s": 900,
            })
        )

        ad = MagicMock()
        app = PhotoFrameViewerApp(ad, MagicMock())
        app.args = {
            "source_dir": td,
            "ha_local_url_base": "/local/photo-frame/live",
            "state_dir": td,
            "default_interval_s": 10,
            "pause_auto_resume_s": 600,
        }
        app.get_state = MagicMock(return_value=None)
        app.set_state = MagicMock()
        app.call_service = MagicMock()
        app.listen_state = MagicMock()
        app.listen_event = MagicMock()
        app.run_every = MagicMock()
        app.run_in = MagicMock()
        app.cancel_timer = MagicMock()
        app.timer_running = MagicMock(return_value=False)
        app.datetime = MagicMock()
        app.log = MagicMock()
        app.create_task = MagicMock()
        app.name = "photo_frame_viewer_wall_display"

        app.initialize()

        assert app._interval == 42.0
        assert app.pause_auto_resume_s == 900.0
