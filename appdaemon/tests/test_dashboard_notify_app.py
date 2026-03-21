"""Tests for DashboardNotify app — init, provisioning, relay, detection hook, sensor."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

# Mock hassapi before importing the app
mock_hass = MagicMock()
mock_hass.Hass = type("_MockHass", (), {"__init__": lambda self, *a, **kw: None})
sys.modules["hassapi"] = mock_hass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))

from dashboard_notify.dashboard_notify_app import DashboardNotify
from dashboard_notify.notification_manager import Notification, priority_for_class


def _make_app(extra_args: dict | None = None) -> DashboardNotify:
    """Create a minimal DashboardNotify with all AppDaemon methods mocked."""
    ad = MagicMock()
    app = DashboardNotify(ad, MagicMock())

    td = tempfile.mkdtemp(prefix="dn_test_")
    base_args: dict = {
        "ha_url": "http://ha:8123",
        "ha_token_env": "TOKEN",
        "media_fs_root": td,
        "www_subdir": "dashboard-notify",
        "carousel_interval_s": 10,
        "notifications": [
            {
                "id": "test_notif",
                "class": "BasicTextImage",
                "text": "Test notification",
                "prompt_hint": "test scene",
                "schedule": {"start": "00:00", "end": "23:59"},
                "ttl_s": 3600,
            }
        ],
        "ai_provider_conf": {
            "image": {
                "provider": "openai",
                "api_key": "test-key-fake",
                "model": "gpt-image-1.5",
            }
        },
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
    app.name = "dashboard_notify"

    return app


def _run(coro):
    """Run a coroutine synchronously."""
    return asyncio.get_event_loop().run_until_complete(coro)


class TestInitialization:
    def test_initialize_sets_up_state(self):
        app = _make_app()
        app.initialize()

        # Should register carousel timer and startup callbacks
        assert app.run_every.call_count >= 1  # carousel
        assert app.run_in.call_count >= 2  # async startup + startup reconcile
        assert app.listen_event.call_count >= 1  # command event

    def test_initialize_no_tick_loop(self):
        """After refactor, there should be no run_every for _tick."""
        app = _make_app()
        app.initialize()

        # run_every should only be called for carousel, not for _tick
        run_every_callbacks = [c[0][0] for c in app.run_every.call_args_list]
        for cb in run_every_callbacks:
            assert cb != app._tick if hasattr(app, "_tick") else True

    def test_initialize_creates_media_dirs(self):
        app = _make_app()
        app.initialize()

        media_dir = app._media_dir
        assert os.path.isdir(os.path.join(media_dir, "staged"))
        assert os.path.isdir(os.path.join(media_dir, "generated"))

    def test_initialize_publishes_initial_state(self):
        app = _make_app()
        app.initialize()

        app.set_state.assert_called()
        call_args = app.set_state.call_args
        assert call_args[0][0] == "sensor.dashboard_notify_status"

    def test_initialize_with_detection_hook(self):
        app = _make_app({
            "detection_summary_hook": {
                "enabled": True,
                "ttl_s": 7200,
                "bundle_keys": ["garage", "front_door"],
            }
        })
        app.initialize()

        # Should have extra listen_event for detection
        event_names = [
            call[0][1] for call in app.listen_event.call_args_list
        ]
        assert "detection_summary/run_published" in event_names

    def test_initialize_schedules_startup_reconcile(self):
        """Startup reconcile should be scheduled via run_in."""
        app = _make_app()
        app.initialize()

        run_in_callbacks = [c[0][0] for c in app.run_in.call_args_list]
        assert app._startup_reconcile in run_in_callbacks

    def test_initialize_supports_env_backed_paths_and_ha_url(self):
        with patch(
            "dashboard_notify.dashboard_notify_app.resolve_arg_secret",
            side_effect=lambda args, key, **kwargs: {
                "media_fs_root": "/tmp/dashboard-media",
                "ha_url": "http://ha:8123",
            }.get(key, kwargs.get("default", "")),
        ):
            app = _make_app({"media_fs_root": "", "media_fs_root_env": "MEDIA_FS_ROOT", "ha_url": "", "ha_url_env": "HA_URL"})
            app.initialize()

        assert app._media_fs_root == "/tmp/dashboard-media"
        assert app._ha_url == "http://ha:8123"

    def test_initialize_reads_fixed_text_settings(self):
        app = _make_app({
            "notification_text_target_chars": 42,
            "notification_text_truncate_suffix": "..",
        })
        app.initialize()

        assert app._notification_text_target_chars == 42
        assert app._notification_text_truncate_suffix == ".."


class TestNotificationTextFormatting:
    def test_pads_short_text_to_fixed_width(self):
        app = _make_app({"notification_text_target_chars": 10})
        app.initialize()

        assert app._format_notification_text("Hello") == "Hello     "

    def test_truncates_long_text_to_fixed_width_with_suffix(self):
        app = _make_app({
            "notification_text_target_chars": 10,
            "notification_text_truncate_suffix": "...",
        })
        app.initialize()

        assert app._format_notification_text("abcdefghijklm") == "abcdefg..."

    def test_normalizes_whitespace_before_padding(self):
        app = _make_app({"notification_text_target_chars": 12})
        app.initialize()

        assert app._format_notification_text("Hello\n\n   world") == "Hello world "


class TestProvisioning:
    def test_provision_creates_relay_script(self):
        app = _make_app()
        app.initialize()

        mock_prov = MagicMock()
        mock_prov.ensure_script = AsyncMock(return_value=True)

        with patch("providers.ha_provisioner.HAProvisioner", return_value=mock_prov):
            _run(app._provision_entities())

        mock_prov.ensure_script.assert_called_once()
        call_args = mock_prov.ensure_script.call_args
        assert call_args[0][0] == "dashboard_notify_relay"

        script_config = call_args[0][1]
        assert script_config["sequence"][0]["event"] == "dashboard_notify_command"
        assert script_config["mode"] == "queued"
        assert "command" in script_config["fields"]

    def test_skips_provisioning_without_credentials(self):
        app = _make_app({"ha_url": "", "ha_token_env": ""})
        app.initialize()

        with patch("providers.ha_provisioner.HAProvisioner") as MockClass:
            _run(app._provision_entities())

        MockClass.assert_not_called()

    def test_provision_error_logged(self):
        app = _make_app()
        app.initialize()

        mock_prov = MagicMock()
        mock_prov.ensure_script = AsyncMock(side_effect=Exception("Connection refused"))

        with patch("providers.ha_provisioner.HAProvisioner", return_value=mock_prov):
            _run(app._provision_entities())

        log_messages = [str(c) for c in app.log.call_args_list]
        assert any("error" in m.lower() or "warning" in m.lower() for m in log_messages)


class TestRelayCommands:
    def test_next_advances_index(self):
        app = _make_app()
        app.initialize()

        now = time.time()
        for i in range(3):
            app._manager.add(Notification(
                id=f"n{i}", notification_class="BasicTextImage",
                text=f"Notif {i}", image_path=f"/p/{i}.png",
                local_url=f"/local/{i}.png", created_at=now,
                expires_at=now + 3600, priority=50, source_id="test",
            ))

        app._current_index = 0
        app._handle_command("", {"command": "next"}, {})
        assert app._current_index == 1

    def test_previous_wraps(self):
        app = _make_app()
        app.initialize()

        now = time.time()
        for i in range(3):
            app._manager.add(Notification(
                id=f"n{i}", notification_class="BasicTextImage",
                text=f"Notif {i}", image_path=f"/p/{i}.png",
                local_url=f"/local/{i}.png", created_at=now,
                expires_at=now + 3600, priority=50, source_id="test",
            ))

        app._current_index = 0
        app._handle_command("", {"command": "previous"}, {})
        assert app._current_index == 2

    def test_toggle_pause(self):
        app = _make_app()
        app.initialize()

        assert app._paused is False
        app._handle_command("", {"command": "toggle_pause"}, {})
        assert app._paused is True
        app._handle_command("", {"command": "toggle_pause"}, {})
        assert app._paused is False

    def test_toggle_pause_schedules_auto_resume_when_configured(self):
        app = _make_app({"pause_auto_resume_s": 120})
        app.initialize()
        app.run_in.reset_mock()

        app._handle_command("", {"command": "toggle_pause"}, {})

        app.run_in.assert_any_call(app._handle_pause_auto_resume, 120.0)

    def test_toggle_pause_cancels_auto_resume_when_resuming_manually(self):
        app = _make_app({"pause_auto_resume_s": 120})
        app.initialize()
        auto_resume_handle = MagicMock()
        app._paused = True
        app._pause_auto_resume_handle = auto_resume_handle
        app._pause_auto_resume_deadline = 123.0
        app.timer_running.return_value = True

        app._handle_command("", {"command": "toggle_pause"}, {})

        app.cancel_timer.assert_called_with(auto_resume_handle)
        assert app._pause_auto_resume_handle is None
        assert app._pause_auto_resume_deadline is None

    def test_auto_resume_callback_resumes_and_publishes(self):
        app = _make_app({"pause_auto_resume_s": 120})
        app.initialize()
        app._paused = True
        app._pause_auto_resume_handle = MagicMock()
        app._pause_auto_resume_deadline = 123.0
        app.set_state.reset_mock()

        app._handle_pause_auto_resume({})

        assert app._paused is False
        attrs = app.set_state.call_args[1]["attributes"]
        assert attrs["paused"] is False
        assert app._pause_auto_resume_deadline is None

    def test_dismiss_removes_notification(self):
        app = _make_app()
        app.initialize()

        now = time.time()
        app._manager.add(Notification(
            id="to_dismiss", notification_class="BasicTextImage",
            text="Dismiss me", image_path="/p/d.png",
            local_url="/local/d.png", created_at=now,
            expires_at=now + 3600, priority=50, source_id="test",
        ))

        app._current_index = 0
        app._handle_command("", {"command": "dismiss"}, {})
        assert app._manager.count() == 0

    def test_dismiss_cancels_expiry_timer(self):
        """Dismissing a notification should cancel its expiry timer."""
        app = _make_app()
        app.initialize()

        now = time.time()
        app._manager.add(Notification(
            id="to_dismiss", notification_class="BasicTextImage",
            text="Dismiss me", image_path="/p/d.png",
            local_url="/local/d.png", created_at=now,
            expires_at=now + 3600, priority=50, source_id="test",
        ))

        # Simulate an expiry handle being registered
        fake_handle = MagicMock()
        app._expiry_handles["to_dismiss"] = fake_handle

        app._current_index = 0
        app._handle_command("", {"command": "dismiss"}, {})

        # Expiry handle should be cancelled
        app.cancel_timer.assert_called_with(fake_handle)
        assert "to_dismiss" not in app._expiry_handles


class TestDetectionHook:
    def test_handles_detection_event(self):
        app = _make_app({
            "detection_summary_hook": {
                "enabled": True,
                "ttl_s": 7200,
                "bundle_keys": ["garage"],
            }
        })
        app.initialize()

        # Create a fake generated image at the detection-summary path structure
        run_dir = os.path.join(
            app._media_fs_root, "detection-summary", "garage", "runs", "abc123"
        )
        os.makedirs(run_dir, exist_ok=True)
        img_path = os.path.join(run_dir, "generated.png")
        with open(img_path, "wb") as f:
            f.write(b"fake image data")

        app._handle_detection_published(
            "detection_summary/run_published",
            {
                "bundle_key": "garage",
                "run_id": "abc123",
                "summary": "Person detected in garage",
            },
            {},
        )

        assert app._manager.has("detection_garage_abc123")
        n = app._manager.get("detection_garage_abc123")
        assert n.notification_class == "PreexistingImage"
        assert "Person detected" in n.text

    def test_detection_event_formats_text_to_fixed_width(self):
        app = _make_app({
            "notification_text_target_chars": 12,
            "detection_summary_hook": {
                "enabled": True,
                "ttl_s": 7200,
                "bundle_keys": ["garage"],
            }
        })
        app.initialize()

        run_dir = os.path.join(
            app._media_fs_root, "detection-summary", "garage", "runs", "fmt123"
        )
        os.makedirs(run_dir, exist_ok=True)
        with open(os.path.join(run_dir, "generated.png"), "wb") as f:
            f.write(b"fake image data")

        app._handle_detection_published(
            "detection_summary/run_published",
            {"bundle_key": "garage", "run_id": "fmt123", "summary": "Hello"},
            {},
        )

        n = app._manager.get("detection_garage_fmt123")
        assert n is not None
        assert n.text == "Hello       "

    def test_detection_event_sets_expiry_timer(self):
        """Detection notifications should schedule an expiry timer."""
        app = _make_app({
            "detection_summary_hook": {
                "enabled": True,
                "ttl_s": 7200,
                "bundle_keys": ["garage"],
            }
        })
        app.initialize()
        app.run_in.reset_mock()

        run_dir = os.path.join(
            app._media_fs_root, "detection-summary", "garage", "runs", "run1"
        )
        os.makedirs(run_dir, exist_ok=True)
        with open(os.path.join(run_dir, "generated.png"), "wb") as f:
            f.write(b"fake image data")

        app._handle_detection_published(
            "detection_summary/run_published",
            {"bundle_key": "garage", "run_id": "run1", "summary": "Test"},
            {},
        )

        # Should schedule an expiry timer
        assert app.run_in.called
        expiry_callbacks = [c[0][0] for c in app.run_in.call_args_list]
        assert app._on_notification_expired in expiry_callbacks

    def test_detection_event_calls_stage_service(self):
        """Detection events should trigger the staging shell command."""
        app = _make_app({
            "detection_summary_hook": {
                "enabled": True,
                "ttl_s": 7200,
                "bundle_keys": ["garage"],
            }
        })
        app.initialize()
        app.call_service.reset_mock()

        run_dir = os.path.join(
            app._media_fs_root, "detection-summary", "garage", "runs", "run2"
        )
        os.makedirs(run_dir, exist_ok=True)
        with open(os.path.join(run_dir, "generated.png"), "wb") as f:
            f.write(b"fake image data")

        app._handle_detection_published(
            "detection_summary/run_published",
            {"bundle_key": "garage", "run_id": "run2", "summary": "Test"},
            {},
        )

        # Should call staging shell command once (no retry)
        stage_calls = [c for c in app.call_service.call_args_list
                       if "dashboard_notify_stage" in str(c)]
        assert len(stage_calls) == 1

    def test_ignores_unknown_bundle_key(self):
        app = _make_app({
            "detection_summary_hook": {
                "enabled": True,
                "ttl_s": 7200,
                "bundle_keys": ["garage"],
            }
        })
        app.initialize()

        app._handle_detection_published(
            "detection_summary/run_published",
            {
                "bundle_key": "unknown_camera",
                "run_id": "xyz",
                "summary": "Something",
            },
            {},
        )

        assert app._manager.count() == 0

    def test_ignores_missing_image(self):
        app = _make_app({
            "detection_summary_hook": {
                "enabled": True,
                "ttl_s": 7200,
                "bundle_keys": ["garage"],
            }
        })
        app.initialize()

        # No image file created, so path won't exist
        app._handle_detection_published(
            "detection_summary/run_published",
            {
                "bundle_key": "garage",
                "run_id": "abc",
                "summary": "Test",
            },
            {},
        )

        assert app._manager.count() == 0

    def test_ignores_duplicate_detection_event(self):
        """A notification already in manager should not be added twice."""
        app = _make_app({
            "detection_summary_hook": {
                "enabled": True,
                "ttl_s": 7200,
                "bundle_keys": ["garage"],
            }
        })
        app.initialize()

        run_dir = os.path.join(
            app._media_fs_root, "detection-summary", "garage", "runs", "dup123"
        )
        os.makedirs(run_dir, exist_ok=True)
        with open(os.path.join(run_dir, "generated.png"), "wb") as f:
            f.write(b"fake image data")

        app._handle_detection_published(
            "detection_summary/run_published",
            {"bundle_key": "garage", "run_id": "dup123", "summary": "Test"},
            {},
        )
        # Second call with same run_id
        # Recreate image (first call copies it to staged, original still there)
        app._handle_detection_published(
            "detection_summary/run_published",
            {"bundle_key": "garage", "run_id": "dup123", "summary": "Test"},
            {},
        )

        assert app._manager.count() == 1


class TestSensorPublishing:
    def test_publishes_correct_state(self):
        app = _make_app()
        app.initialize()

        now = time.time()
        # Create a real temp file so _versioned_media_url can read its mtime
        img_path = os.path.join(app._media_dir, "staged", "test.png")
        os.makedirs(os.path.dirname(img_path), exist_ok=True)
        with open(img_path, "wb") as f:
            f.write(b"fake")

        app._manager.add(Notification(
            id="test", notification_class="BasicTextImage",
            text="Hello", image_path=img_path,
            local_url="/local/t.png", created_at=now,
            expires_at=now + 3600, priority=50, source_id="test",
        ))

        app._publish_state()

        call_args = app.set_state.call_args
        assert call_args[0][0] == "sensor.dashboard_notify_status"
        assert call_args[1]["state"] == "1 notification"
        attrs = call_args[1]["attributes"]
        assert len(attrs["notifications"]) == 1
        assert attrs["notifications"][0]["id"] == "test"
        assert "?v=" in attrs["notifications"][0]["image_url"]

    def test_publishes_placeholder_when_empty(self):
        app = _make_app()
        app.initialize()
        app._placeholder_url = "/local/dashboard-notify/placeholder.png"

        app._publish_state()

        call_args = app.set_state.call_args
        attrs = call_args[1]["attributes"]
        assert "placeholder_url" in attrs

    def test_clamps_index(self):
        app = _make_app()
        app.initialize()
        app._current_index = 5

        app._publish_state()

        assert app._current_index == 0

    def test_publish_state_includes_auto_resume_metadata(self):
        app = _make_app({"pause_auto_resume_s": 90})
        app.initialize()
        app._paused = True
        app._pause_auto_resume_deadline = 12345.0

        app._publish_state()

        attrs = app.set_state.call_args[1]["attributes"]
        assert attrs["pause_auto_resume_s"] == 90.0
        assert attrs["pause_resume_at"] == 12345.0


class TestCarouselAdvance:
    def test_advance_increments_index(self):
        app = _make_app()
        app.initialize()

        now = time.time()
        for i in range(3):
            app._manager.add(Notification(
                id=f"n{i}", notification_class="BasicTextImage",
                text=f"N{i}", image_path=f"/p/{i}.png",
                local_url=f"/local/{i}.png", created_at=now,
                expires_at=now + 3600, priority=50, source_id="test",
            ))

        app._current_index = 0
        app._carousel_advance({})
        assert app._current_index == 1

    def test_advance_wraps_around(self):
        app = _make_app()
        app.initialize()

        now = time.time()
        for i in range(3):
            app._manager.add(Notification(
                id=f"n{i}", notification_class="BasicTextImage",
                text=f"N{i}", image_path=f"/p/{i}.png",
                local_url=f"/local/{i}.png", created_at=now,
                expires_at=now + 3600, priority=50, source_id="test",
            ))

        app._current_index = 2
        app._carousel_advance({})
        assert app._current_index == 0

    def test_advance_skipped_when_paused(self):
        app = _make_app()
        app.initialize()

        now = time.time()
        app._manager.add(Notification(
            id="n0", notification_class="BasicTextImage",
            text="N0", image_path="/p/0.png",
            local_url="/local/0.png", created_at=now,
            expires_at=now + 3600, priority=50, source_id="test",
        ))

        app._paused = True
        app._current_index = 0
        app._carousel_advance({})
        assert app._current_index == 0

    def test_advance_noop_when_empty(self):
        app = _make_app()
        app.initialize()
        app._current_index = 0
        app._carousel_advance({})
        assert app._current_index == 0


class TestThreadedGeneration:
    """Tests for the two-phase generation model (request → background → complete)."""

    def test_request_notification_generation_reserves_slot(self):
        """_request_notification_generation should add nid to _active_generations."""
        app = _make_app()
        app.initialize()
        # Prevent thread from actually starting
        with patch.object(app, "_start_generation_thread"):
            config = {
                "id": "school_bus",
                "class": "BasicTextImage",
                "text": "Bus arriving",
                "prompt_hint": "school bus",
                "ttl_s": 3600,
            }
            app._request_notification_generation(config, time.time())

        assert "school_bus" in app._active_generations

    def test_request_notification_generation_no_provider(self):
        """Without an image provider, generation should be skipped gracefully."""
        app = _make_app()
        app.initialize()
        app._image_provider = None

        with patch.object(app, "_start_generation_thread") as mock_start:
            config = {"id": "test", "class": "BasicTextImage", "text": "T"}
            app._request_notification_generation(config, time.time())

        mock_start.assert_not_called()

    def test_request_notification_generation_deduplicates(self):
        """Should not start a second thread if already generating."""
        app = _make_app()
        app.initialize()
        app._active_generations.add("school_bus")

        with patch.object(app, "_start_generation_thread") as mock_start:
            config = {"id": "school_bus", "class": "BasicTextImage", "text": "T"}
            app._request_notification_generation(config, time.time())

        mock_start.assert_not_called()

    def test_request_notification_generation_formats_text_to_fixed_width(self):
        app = _make_app({"notification_text_target_chars": 12})
        app.initialize()

        with patch.object(app, "_start_generation_thread") as mock_start:
            config = {
                "id": "school_bus",
                "class": "BasicTextImage",
                "text": "Test notification",
            }
            app._request_notification_generation(config, time.time())

        job = mock_start.call_args[0][0]
        assert job["text"] == "Test noti..."

    def test_complete_notification_generation_adds_notification(self):
        """_complete_notification_generation should add notification and publish state."""
        app = _make_app()
        app.initialize()
        app._active_generations.add("school_bus")

        now = time.time()
        result = {
            "success": True,
            "nid": "school_bus",
            "notification_class": "BasicTextImage",
            "text": "Bus arriving",
            "output_path": "/media/dashboard-notify/generated/school_bus_123.png",
            "staged_path": "/media/dashboard-notify/staged/school_bus_123.png",
            "local_url": "/local/dashboard-notify/school_bus_123.png",
            "now": now,
            "ttl_s": 3600,
        }

        app._complete_notification_generation({"result": result})

        assert app._manager.has("school_bus")
        assert "school_bus" not in app._active_generations
        # Should publish state
        app.set_state.assert_called()

    def test_complete_notification_generation_calls_stage_once(self):
        """Completion callback should call staging shell command exactly once."""
        app = _make_app()
        app.initialize()
        app._active_generations.add("school_bus")
        app.call_service.reset_mock()

        now = time.time()
        result = {
            "success": True,
            "nid": "school_bus",
            "notification_class": "BasicTextImage",
            "text": "Bus arriving",
            "output_path": "/media/dashboard-notify/generated/school_bus_123.png",
            "staged_path": "/media/dashboard-notify/staged/school_bus_123.png",
            "local_url": "/local/dashboard-notify/school_bus_123.png",
            "now": now,
            "ttl_s": 3600,
        }

        app._complete_notification_generation({"result": result})

        stage_calls = [c for c in app.call_service.call_args_list
                       if "dashboard_notify_stage" in str(c)]
        assert len(stage_calls) == 1

    def test_complete_notification_generation_schedules_expiry(self):
        """Completion callback should schedule an expiry timer."""
        app = _make_app()
        app.initialize()
        app._active_generations.add("school_bus")
        app.run_in.reset_mock()

        now = time.time()
        result = {
            "success": True,
            "nid": "school_bus",
            "notification_class": "BasicTextImage",
            "text": "Bus arriving",
            "output_path": "/media/dashboard-notify/generated/school_bus_123.png",
            "staged_path": "/media/dashboard-notify/staged/school_bus_123.png",
            "local_url": "/local/dashboard-notify/school_bus_123.png",
            "now": now,
            "ttl_s": 3600,
        }

        app._complete_notification_generation({"result": result})

        # Should schedule an expiry timer
        expiry_callbacks = [c[0][0] for c in app.run_in.call_args_list]
        assert app._on_notification_expired in expiry_callbacks

    def test_complete_notification_generation_clears_slot_on_failure(self):
        """Even on failure, the generation slot should be released."""
        app = _make_app()
        app.initialize()
        app._active_generations.add("school_bus")

        result = {
            "success": False,
            "nid": "school_bus",
            "error": "Provider timeout",
        }

        app._complete_notification_generation({"result": result})

        assert "school_bus" not in app._active_generations
        assert not app._manager.has("school_bus")

    def test_complete_notification_generation_stale_guard(self):
        """If notification already exists, completion should skip adding it again."""
        app = _make_app()
        app.initialize()

        now = time.time()
        # Pre-add the notification (simulates it being added via another path)
        app._manager.add(Notification(
            id="school_bus", notification_class="BasicTextImage",
            text="Already here", image_path="/p/s.png",
            local_url="/local/s.png", created_at=now,
            expires_at=now + 3600, priority=50, source_id="school_bus",
        ))
        app._active_generations.add("school_bus")

        result = {
            "success": True,
            "nid": "school_bus",
            "notification_class": "BasicTextImage",
            "text": "Bus arriving",
            "output_path": "/p/new.png",
            "staged_path": "/s/new.png",
            "local_url": "/local/new.png",
            "now": now,
            "ttl_s": 3600,
        }

        app._complete_notification_generation({"result": result})

        # Notification count should still be 1 (not doubled)
        assert app._manager.count() == 1
        assert "school_bus" not in app._active_generations

    def test_complete_placeholder_generation_updates_url(self):
        """_complete_placeholder_generation should set placeholder URL and publish."""
        app = _make_app()
        app.initialize()
        app._active_generations.add("placeholder")

        now = time.time()
        result = {
            "success": True,
            "output_path": "/media/dashboard-notify/generated/no_notifications_123.png",
            "staged_path": "/media/dashboard-notify/staged/no_notifications_123.png",
            "local_url": "/local/dashboard-notify/no_notifications_123.png",
            "now": now,
        }

        app._complete_placeholder_generation({"result": result})

        assert app._placeholder_url == "/local/dashboard-notify/no_notifications_123.png"
        assert "placeholder" not in app._active_generations
        app.set_state.assert_called()

    def test_complete_placeholder_generation_discards_when_notifications_appear(self):
        """If notifications appeared while generating, placeholder should be discarded."""
        app = _make_app()
        app.initialize()
        app._active_generations.add("placeholder")

        # Add a notification so manager is non-empty
        now = time.time()
        app._manager.add(Notification(
            id="real_notif", notification_class="BasicTextImage",
            text="Real notification", image_path="/p/r.png",
            local_url="/local/r.png", created_at=now,
            expires_at=now + 3600, priority=50, source_id="test",
        ))

        result = {
            "success": True,
            "local_url": "/local/dashboard-notify/no_notifications_123.png",
            "now": now,
        }

        app._complete_placeholder_generation({"result": result})

        # Placeholder should NOT be set because real notifications exist
        assert app._placeholder_url == ""
        assert "placeholder" not in app._active_generations

    def test_complete_placeholder_generation_clears_slot_on_failure(self):
        """Even on failure, placeholder slot should be released."""
        app = _make_app()
        app.initialize()
        app._active_generations.add("placeholder")

        result = {"success": False, "error": "Provider error"}
        app._complete_placeholder_generation({"result": result})

        assert "placeholder" not in app._active_generations

    def test_complete_placeholder_generation_schedules_refresh(self):
        """After successful placeholder generation, a refresh timer should be scheduled."""
        app = _make_app()
        app.initialize()
        app._active_generations.add("placeholder")
        app.run_in.reset_mock()

        now = time.time()
        result = {
            "success": True,
            "output_path": "/media/dashboard-notify/generated/no_notifications_123.png",
            "staged_path": "/media/dashboard-notify/staged/no_notifications_123.png",
            "local_url": "/local/dashboard-notify/no_notifications_123.png",
            "now": now,
        }

        app._complete_placeholder_generation({"result": result})

        refresh_callbacks = [c[0][0] for c in app.run_in.call_args_list]
        assert app._on_placeholder_refresh in refresh_callbacks


class TestNoRetryStaging:
    """Verify that the old retry pattern (_stage_retry) has been removed."""

    def test_no_stage_retry_method(self):
        """The _stage_retry method should not exist."""
        app = _make_app()
        assert not hasattr(app, "_stage_retry"), "_stage_retry should have been removed"

    def test_no_stage_to_www_method(self):
        """The _stage_to_www method should not exist."""
        app = _make_app()
        assert not hasattr(app, "_stage_to_www"), "_stage_to_www should have been removed"

    def test_detection_event_single_stage_call(self):
        """Detection events trigger exactly one staging call, not two."""
        app = _make_app({
            "detection_summary_hook": {
                "enabled": True,
                "ttl_s": 7200,
                "bundle_keys": ["garage"],
            }
        })
        app.initialize()
        app.call_service.reset_mock()

        run_dir = os.path.join(
            app._media_fs_root, "detection-summary", "garage", "runs", "singlestage"
        )
        os.makedirs(run_dir, exist_ok=True)
        with open(os.path.join(run_dir, "generated.png"), "wb") as f:
            f.write(b"fake")

        app._handle_detection_published(
            "detection_summary/run_published",
            {"bundle_key": "garage", "run_id": "singlestage", "summary": "Test"},
            {},
        )

        stage_calls = [c for c in app.call_service.call_args_list
                       if "dashboard_notify_stage" in str(c)]
        assert len(stage_calls) == 1

    def test_sync_staged_dir_single_stage_call(self):
        """_sync_staged_dir triggers exactly one shell command when files are removed."""
        app = _make_app()
        app.initialize()

        # Create a stale staged file
        staged_dir = os.path.join(app._media_dir, "staged")
        stale = os.path.join(staged_dir, "stale_old.png")
        with open(stale, "wb") as f:
            f.write(b"stale")

        app.call_service.reset_mock()
        app._sync_staged_dir()

        stage_calls = [c for c in app.call_service.call_args_list
                       if "dashboard_notify_stage" in str(c)]
        assert len(stage_calls) == 1


class TestExplicitTimers:
    """Verify the explicit timer model replacing the _tick loop."""

    def test_no_tick_method(self):
        """The _tick method should not exist after refactor."""
        app = _make_app()
        assert not hasattr(app, "_tick"), "_tick should have been removed"

    def test_startup_reconcile_scheduled(self):
        """Startup reconcile should be scheduled via run_in on initialize."""
        app = _make_app()
        app.initialize()

        run_in_callbacks = [c[0][0] for c in app.run_in.call_args_list]
        assert app._startup_reconcile in run_in_callbacks

    def test_schedule_boundaries_scheduled_on_startup(self):
        """Per-config schedule boundary timers should be set up during startup reconcile."""
        app = _make_app({
            "notifications": [
                {
                    "id": "bus_pickup",
                    "class": "BasicTextImage",
                    "text": "Bus arriving",
                    "schedule": {"start": "08:00", "end": "09:00"},
                    "ttl_s": 3600,
                }
            ]
        })
        app.initialize()
        app.run_in.reset_mock()

        # Run startup reconcile
        now = time.time()
        with patch.object(app, "_request_notification_generation"):
            with patch.object(app, "_request_placeholder_generation"):
                app._startup_reconcile({})

        # Should schedule start/end boundary timers for the config
        assert "bus_pickup" in app._schedule_handles

    def test_on_schedule_start_requests_generation(self):
        """_on_schedule_start should request generation if notification absent."""
        app = _make_app()
        app.initialize()

        config = {
            "id": "bus_pickup",
            "class": "BasicTextImage",
            "text": "Bus arriving",
            "schedule": {"start": "08:00", "end": "09:00"},
            "ttl_s": 3600,
        }

        with patch.object(app, "_request_notification_generation") as mock_req:
            app._on_schedule_start({"nid": "bus_pickup", "config": config})

        mock_req.assert_called_once()

    def test_on_schedule_start_skips_if_already_present(self):
        """_on_schedule_start should not request generation if notification exists."""
        app = _make_app()
        app.initialize()

        now = time.time()
        app._manager.add(Notification(
            id="bus_pickup", notification_class="BasicTextImage",
            text="Bus arriving", image_path="/p/b.png",
            local_url="/local/b.png", created_at=now,
            expires_at=now + 3600, priority=50, source_id="bus_pickup",
        ))

        config = {
            "id": "bus_pickup",
            "schedule": {"start": "08:00", "end": "09:00"},
        }

        with patch.object(app, "_request_notification_generation") as mock_req:
            app._on_schedule_start({"nid": "bus_pickup", "config": config})

        mock_req.assert_not_called()

    def test_on_schedule_end_removes_notification(self):
        """_on_schedule_end should remove the notification and publish state."""
        app = _make_app()
        app.initialize()

        now = time.time()
        app._manager.add(Notification(
            id="bus_pickup", notification_class="BasicTextImage",
            text="Bus arriving", image_path="/p/b.png",
            local_url="/local/b.png", created_at=now,
            expires_at=now + 3600, priority=50, source_id="bus_pickup",
        ))

        config = {
            "id": "bus_pickup",
            "schedule": {"start": "08:00", "end": "09:00"},
        }

        app._on_schedule_end({"nid": "bus_pickup", "config": config})

        assert not app._manager.has("bus_pickup")
        app.set_state.assert_called()

    def test_on_schedule_end_reschedules_next_end(self):
        """_on_schedule_end should schedule the next end boundary."""
        app = _make_app()
        app.initialize()
        app.run_in.reset_mock()

        config = {
            "id": "bus_pickup",
            "schedule": {"start": "08:00", "end": "09:00"},
        }

        app._on_schedule_end({"nid": "bus_pickup", "config": config})

        # Should schedule a new end timer
        end_callbacks = [c[0][0] for c in app.run_in.call_args_list]
        assert app._on_schedule_end in end_callbacks

    def test_expiry_timer_fires_removal(self):
        """_on_notification_expired should remove the notification."""
        app = _make_app()
        app.initialize()

        now = time.time()
        app._manager.add(Notification(
            id="expiring", notification_class="BasicTextImage",
            text="Expiring", image_path="/p/e.png",
            local_url="/local/e.png", created_at=now,
            expires_at=now + 1, priority=50, source_id="test",
        ))

        app._on_notification_expired({"nid": "expiring"})

        assert not app._manager.has("expiring")
        app.set_state.assert_called()

    def test_expiry_timer_noop_if_already_removed(self):
        """_on_notification_expired should be safe if notification is already gone."""
        app = _make_app()
        app.initialize()

        # Call with a nid that doesn't exist
        app._on_notification_expired({"nid": "nonexistent"})

        # Should not raise
        # set_state is called at initialize, check it's not called AGAIN
        call_count_before = app.set_state.call_count
        app._on_notification_expired({"nid": "also_nonexistent"})
        assert app.set_state.call_count == call_count_before


class TestStartupReconcile:
    """Tests for the startup reconciliation logic."""

    def test_startup_reconcile_requests_active_schedule(self):
        """Startup reconcile should enqueue generation for currently active schedules."""
        app = _make_app({
            "notifications": [
                {
                    "id": "always_on",
                    "class": "BasicTextImage",
                    "text": "Always on",
                    "schedule": {"start": "00:00", "end": "23:59"},
                    "ttl_s": 3600,
                }
            ]
        })
        app.initialize()

        with patch.object(app, "_request_notification_generation") as mock_req:
            with patch.object(app, "_request_placeholder_generation"):
                app._startup_reconcile({})

        mock_req.assert_called_once()

    def test_startup_reconcile_requests_placeholder_when_empty(self):
        """Startup reconcile should request placeholder if no notifications active."""
        app = _make_app({
            "notifications": [
                {
                    "id": "night_only",
                    "class": "BasicTextImage",
                    "text": "Night schedule",
                    "schedule": {"start": "22:00", "end": "23:00"},
                    "ttl_s": 3600,
                }
            ]
        })
        app.initialize()

        # Run at a time when schedule is inactive
        with patch("time.time", return_value=1741525200.0):  # daytime
            with patch.object(app, "_request_notification_generation") as mock_req:
                with patch.object(app, "_request_placeholder_generation") as mock_ph:
                    app._startup_reconcile({})

        mock_ph.assert_called_once()

    def test_startup_reconcile_schedules_boundaries_for_all_configs(self):
        """Startup reconcile should call _schedule_next_boundaries for each config."""
        app = _make_app({
            "notifications": [
                {"id": "c1", "class": "BasicTextImage", "text": "A",
                 "schedule": {"start": "08:00", "end": "09:00"}, "ttl_s": 3600},
                {"id": "c2", "class": "BasicTextImage", "text": "B",
                 "schedule": {"start": "12:00", "end": "13:00"}, "ttl_s": 3600},
            ]
        })
        app.initialize()

        with patch.object(app, "_request_notification_generation"):
            with patch.object(app, "_request_placeholder_generation"):
                app._startup_reconcile({})

        assert "c1" in app._schedule_handles
        assert "c2" in app._schedule_handles


class TestBackfillDetection:
    """Tests for startup detection-summary backfill."""

    def test_backfill_adds_within_ttl_bundle(self):
        """Backfill should add detection notifications still within TTL."""
        app = _make_app({
            "detection_summary_hook": {
                "enabled": True,
                "ttl_s": 7200,
                "bundle_keys": ["garage"],
            }
        })
        app.initialize()

        now = time.time()
        # Create a run that is 1 hour old (within 2hr TTL)
        created_at = now - 3600
        run_id = "backfill_run_001"
        run_dir = os.path.join(
            app._media_fs_root, "detection-summary", "garage", "runs", run_id
        )
        os.makedirs(run_dir, exist_ok=True)
        with open(os.path.join(run_dir, "generated.png"), "wb") as f:
            f.write(b"fake image")
        import json
        with open(os.path.join(run_dir, "summary.json"), "w") as f:
            json.dump({"summary": {"bundle_key": "garage", "run_id": run_id, "created_at_epoch": created_at, "run_text": "Backfill person"}}, f)

        app._backfill_detection_bundles(now)

        nid = f"detection_garage_{run_id}"
        assert app._manager.has(nid), f"Expected {nid} to be backfilled"
        n = app._manager.get(nid)
        assert "Backfill person" in n.text

    def test_backfill_formats_text_to_fixed_width(self):
        app = _make_app({
            "notification_text_target_chars": 12,
            "detection_summary_hook": {
                "enabled": True,
                "ttl_s": 7200,
                "bundle_keys": ["garage"],
            }
        })
        app.initialize()

        now = time.time()
        created_at = now - 3600
        run_id = "backfill_format"
        run_dir = os.path.join(
            app._media_fs_root, "detection-summary", "garage", "runs", run_id
        )
        os.makedirs(run_dir, exist_ok=True)
        with open(os.path.join(run_dir, "generated.png"), "wb") as f:
            f.write(b"fake image")
        import json
        with open(os.path.join(run_dir, "summary.json"), "w") as f:
            json.dump(
                {
                    "summary": {
                        "bundle_key": "garage",
                        "run_id": run_id,
                        "created_at_epoch": created_at,
                        "run_text": "Hello",
                    }
                },
                f,
            )

        app._backfill_detection_bundles(now)

        n = app._manager.get(f"detection_garage_{run_id}")
        assert n is not None
        assert n.text == "Hello       "

    def test_backfill_skips_expired_bundle(self):
        """Backfill should skip bundles whose TTL has passed."""
        app = _make_app({
            "detection_summary_hook": {
                "enabled": True,
                "ttl_s": 7200,
                "bundle_keys": ["garage"],
            }
        })
        app.initialize()

        now = time.time()
        # Create a run that is 3 hours old (beyond 2hr TTL)
        created_at = now - 10800
        run_id = "expired_run_001"
        run_dir = os.path.join(
            app._media_fs_root, "detection-summary", "garage", "runs", run_id
        )
        os.makedirs(run_dir, exist_ok=True)
        with open(os.path.join(run_dir, "generated.png"), "wb") as f:
            f.write(b"fake image")
        import json
        with open(os.path.join(run_dir, "summary.json"), "w") as f:
            json.dump({"summary": {"bundle_key": "garage", "run_id": run_id, "created_at_epoch": created_at, "run_text": "Expired"}}, f)

        app._backfill_detection_bundles(now)

        nid = f"detection_garage_{run_id}"
        assert not app._manager.has(nid), "Expired bundle should not be backfilled"

    def test_backfill_skips_missing_image(self):
        """Backfill should skip runs without a generated.png."""
        app = _make_app({
            "detection_summary_hook": {
                "enabled": True,
                "ttl_s": 7200,
                "bundle_keys": ["garage"],
            }
        })
        app.initialize()

        now = time.time()
        created_at = now - 1800
        run_id = "no_image_run"
        run_dir = os.path.join(
            app._media_fs_root, "detection-summary", "garage", "runs", run_id
        )
        os.makedirs(run_dir, exist_ok=True)
        # No generated.png, only summary.json
        import json
        with open(os.path.join(run_dir, "summary.json"), "w") as f:
            json.dump({"summary": {"bundle_key": "garage", "run_id": run_id, "created_at_epoch": created_at, "run_text": "No image"}}, f)

        app._backfill_detection_bundles(now)

        nid = f"detection_garage_{run_id}"
        assert not app._manager.has(nid)

    def test_backfill_deduplicates_existing(self):
        """Backfill should not add a notification already in the manager."""
        app = _make_app({
            "detection_summary_hook": {
                "enabled": True,
                "ttl_s": 7200,
                "bundle_keys": ["garage"],
            }
        })
        app.initialize()

        now = time.time()
        created_at = now - 1800
        run_id = "dup_run"
        run_dir = os.path.join(
            app._media_fs_root, "detection-summary", "garage", "runs", run_id
        )
        os.makedirs(run_dir, exist_ok=True)
        with open(os.path.join(run_dir, "generated.png"), "wb") as f:
            f.write(b"fake")
        import json
        with open(os.path.join(run_dir, "summary.json"), "w") as f:
            json.dump({"summary": {"bundle_key": "garage", "run_id": run_id, "created_at_epoch": created_at, "run_text": "Dup"}}, f)

        nid = f"detection_garage_{run_id}"
        # Pre-add the notification
        app._manager.add(Notification(
            id=nid, notification_class="PreexistingImage",
            text="Already here", image_path="/p/d.png",
            local_url="/local/d.png", created_at=created_at,
            expires_at=created_at + 7200, priority=50, source_id="garage",
        ))

        app._backfill_detection_bundles(now)

        assert app._manager.count() == 1  # Still just one, not duplicated
