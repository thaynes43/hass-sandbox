"""Tests for DashboardNotify app — init, provisioning, relay, detection hook, sensor."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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

        # Should register timers
        assert app.run_every.call_count >= 2  # tick + carousel
        assert app.run_in.call_count >= 1  # async startup
        assert app.listen_event.call_count >= 1  # command event

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

        # Manually add notifications
        from dashboard_notify.notification_manager import Notification
        import time
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

        import time
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

    def test_dismiss_removes_notification(self):
        app = _make_app()
        app.initialize()

        import time
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


class TestSensorPublishing:
    def test_publishes_correct_state(self):
        app = _make_app()
        app.initialize()

        import time
        now = time.time()
        app._manager.add(Notification(
            id="test", notification_class="BasicTextImage",
            text="Hello", image_path="/p/t.png",
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
        assert "?t=" in attrs["notifications"][0]["image_url"]

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


class TestCarouselAdvance:
    def test_advance_increments_index(self):
        app = _make_app()
        app.initialize()

        import time
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

        import time
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

        import time
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
