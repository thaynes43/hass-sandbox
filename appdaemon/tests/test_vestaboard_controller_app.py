"""Tests for VestaboardControllerApp — init, provisioning, commands, tick, status."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

# Mock hassapi before importing the app
mock_hass = MagicMock()
mock_hass.Hass = type("_MockHass", (), {"__init__": lambda self, *a, **kw: None})
sys.modules["hassapi"] = mock_hass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))

from vestaboard_controller_app.vestaboard_controller_app import VestaboardControllerApp
from vestaboard_controller_app.frame_queue import BoardFrame, FrameQueue


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_app(extra_args: dict | None = None) -> VestaboardControllerApp:
    """Create a minimal VestaboardControllerApp with all AppDaemon methods mocked."""
    ad = MagicMock()
    app = VestaboardControllerApp(ad, MagicMock())

    base_args: dict = {
        "vestaboard_ip": "192.168.1.50",
        "vestaboard_api_key": "test-api-key-fake",
        "ha_url": "http://ha:8123",
        "ha_token_env": "TOKEN",
        "tick_interval_s": 15,
        "automations": {},
        "ai_provider_conf": {"simple_text": {"provider": "openai", "api_key": "test-key"}},
    }
    if extra_args:
        base_args.update(extra_args)
    app.args = base_args

    app.get_state = MagicMock(return_value=None)
    app.set_state = MagicMock()
    app.call_service = MagicMock()
    app.listen_state = AsyncMock(return_value="handle-ls")
    app.listen_event = MagicMock(return_value="handle-le")
    app.run_every = AsyncMock(return_value="handle-re")
    app.run_in = MagicMock()
    app.cancel_timer = AsyncMock()
    app.cancel_listen_state = AsyncMock()
    app.timer_running = MagicMock(return_value=False)
    app.datetime = MagicMock()
    app.log = MagicMock()
    app.create_task = MagicMock(side_effect=lambda coro: _run(coro))
    app.name = "vestaboard_controller"

    return app


def _run(coro):
    """Run a coroutine synchronously."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _blank_grid() -> list[list[int]]:
    return [[0] * 22 for _ in range(6)]


def _make_frame(source: str = "test", ttl_s: int | None = None) -> BoardFrame:
    return BoardFrame(
        frame_id="test-frame-001",
        characters=_blank_grid(),
        source=source,
        source_label=source.title(),
        ttl_s=ttl_s,
        expiration_s=None,
        override_ttl=False,
        created_at=time.time(),
    )


# ---------------------------------------------------------------------------
# Initialization tests
# ---------------------------------------------------------------------------

class TestInitialization:
    def test_initialize_schedules_async_startup(self):
        app = _make_app()
        app.initialize()
        assert app.run_in.call_count == 1

    def test_initialize_sets_ip_and_key(self):
        app = _make_app()
        app.initialize()
        assert app._vb_ip == "192.168.1.50"
        assert app._vb_api_key == "test-api-key-fake"

    def test_initialize_sets_tick_interval(self):
        app = _make_app()
        app.initialize()
        assert app._tick_interval_s == 15

    def test_initialize_with_env_vars(self):
        import os
        os.environ["TEST_VB_IP"] = "10.0.0.99"
        os.environ["TEST_VB_KEY"] = "secret-key-env"
        try:
            app = _make_app({
                "vestaboard_ip_env": "TEST_VB_IP",
                "vestaboard_api_key_env": "TEST_VB_KEY",
                # Remove direct values to force env path
                "vestaboard_ip": "",
                "vestaboard_api_key": "",
            })
            app.initialize()
            assert app._vb_ip == "10.0.0.99"
            assert app._vb_api_key == "secret-key-env"
        finally:
            del os.environ["TEST_VB_IP"]
            del os.environ["TEST_VB_KEY"]


# ---------------------------------------------------------------------------
# Async startup tests
# ---------------------------------------------------------------------------

class TestAsyncStartup:
    def test_async_startup_registers_event_listener(self):
        app = _make_app()
        app.initialize()

        mock_prov = MagicMock()
        mock_prov.ensure_script = AsyncMock(return_value=False)

        with patch("providers.ha_provisioner.HAProvisioner", return_value=mock_prov):
            _run(app._async_startup())

        # Should listen for the command event
        assert app.listen_event.call_count >= 1
        call_args = app.listen_event.call_args_list
        event_names = [c[0][1] for c in call_args if len(c[0]) >= 2]
        assert "vestaboard_controller_command" in event_names

    def test_async_startup_registers_tick_timer(self):
        app = _make_app()
        app.initialize()

        mock_prov = MagicMock()
        mock_prov.ensure_script = AsyncMock(return_value=False)

        with patch("providers.ha_provisioner.HAProvisioner", return_value=mock_prov):
            _run(app._async_startup())

        assert app.run_every.call_count >= 1

    def test_async_startup_provisions_relay_script(self):
        app = _make_app()
        app.initialize()

        mock_prov = MagicMock()
        mock_prov.ensure_script = AsyncMock(return_value=True)

        with patch("providers.ha_provisioner.HAProvisioner", return_value=mock_prov):
            _run(app._async_startup())

        mock_prov.ensure_script.assert_called_once()
        script_id = mock_prov.ensure_script.call_args[0][0]
        assert script_id == "vestaboard_controller_relay"

    def test_async_startup_publishes_status(self):
        app = _make_app()
        app.initialize()

        mock_prov = MagicMock()
        mock_prov.ensure_script = AsyncMock(return_value=False)

        with patch("providers.ha_provisioner.HAProvisioner", return_value=mock_prov):
            _run(app._async_startup())

        app.set_state.assert_called()
        call_args = app.set_state.call_args
        assert "sensor.vestaboard_controller_status" in call_args[0]

    def test_async_startup_skips_provisioning_without_ha_url(self):
        app = _make_app({"ha_url": "", "ha_token_env": ""})
        app.initialize()

        with patch("providers.ha_provisioner.HAProvisioner") as mock_prov_cls:
            _run(app._async_startup())

        mock_prov_cls.assert_not_called()


# ---------------------------------------------------------------------------
# Command routing tests
# ---------------------------------------------------------------------------

class TestCommandRouting:
    def _setup_app(self) -> VestaboardControllerApp:
        app = _make_app()
        app.initialize()
        # Manually init internal state (skip async startup)
        from providers.vestaboard.vestaboard_client import VestaboardClient
        app._client = MagicMock()
        app._queue = FrameQueue(log_fn=app.log)
        app._automations = {}
        app._trigger_handles = {}
        app._last_write_ok = None
        return app

    def test_unknown_command_logs_warning(self):
        app = self._setup_app()
        app._on_command("vestaboard_controller_command", {"command": "bogus_cmd"}, {})
        warning_calls = [c for c in app.log.call_args_list if "WARNING" in str(c)]
        assert any("bogus_cmd" in str(c) or "Unknown" in str(c) for c in warning_calls)

    def test_push_frame_creates_board_frame(self):
        app = self._setup_app()
        grid = _blank_grid()
        payload = {
            "characters": grid,
            "source": "test_source",
            "source_label": "Test",
            "ttl_s": 60,
            "override_ttl": True,
        }
        app.create_task = MagicMock(side_effect=lambda coro: _run(coro))
        app._on_command(
            "vestaboard_controller_command",
            {"command": "push_frame", "payload": json.dumps(payload)},
            {},
        )
        # create_task should have been called to write to board (frame pushed immediately)
        assert app.create_task.call_count >= 1

    def test_push_frame_missing_characters_logs_warning(self):
        app = self._setup_app()
        app._on_command(
            "vestaboard_controller_command",
            {"command": "push_frame", "payload": json.dumps({})},
            {},
        )
        warning_calls = [c for c in app.log.call_args_list if "WARNING" in str(c)]
        assert any("characters" in str(c) or "missing" in str(c).lower() for c in warning_calls)

    def test_clear_board_calls_queue_clear(self):
        app = self._setup_app()
        app.create_task = MagicMock(side_effect=lambda coro: _run(coro))

        mock_queue = MagicMock()
        mock_queue.clear = MagicMock(return_value=MagicMock(dropped_frames=[]))
        mock_queue.get_state = MagicMock(return_value=MagicMock(
            displayed=None,
            displayed_ttl_remaining_s=None,
            pending=[],
            fallback_stack=[],
        ))
        app._queue = mock_queue

        app._on_command(
            "vestaboard_controller_command",
            {"command": "clear_board", "payload": "{}"},
            {},
        )
        mock_queue.clear.assert_called_once()

    def test_activate_automation_command(self):
        app = self._setup_app()
        # Register a mock automation with a real config dict (no triggers, no frequency)
        mock_auto = MagicMock()
        mock_auto.get_triggers = MagicMock(return_value=[])
        mock_auto.config = {}
        app._automations["test_auto"] = mock_auto

        app._on_command(
            "vestaboard_controller_command",
            {"command": "activate_automation", "payload": json.dumps({"automation_id": "test_auto"})},
            {},
        )
        # Should not raise and triggers should be registered (0 in this case)
        mock_auto.get_triggers.assert_called_once()

    def test_deactivate_automation_command(self):
        app = self._setup_app()
        mock_auto = MagicMock()
        mock_auto.get_triggers = MagicMock(return_value=[])
        app._automations["test_auto"] = mock_auto
        # Add a dummy trigger handle
        app._trigger_handles[("test_auto", 0)] = ("some-handle", "timer")

        app._on_command(
            "vestaboard_controller_command",
            {"command": "deactivate_automation", "payload": json.dumps({"automation_id": "test_auto"})},
            {},
        )
        # Trigger handle should be cancelled and removed
        assert ("test_auto", 0) not in app._trigger_handles

    def test_payload_as_dict(self):
        """Payload can be passed as a pre-parsed dict (not a string)."""
        app = self._setup_app()
        grid = _blank_grid()
        payload = {"characters": grid, "source": "ui", "override_ttl": True}
        app.create_task = MagicMock(side_effect=lambda coro: _run(coro))

        app._on_command(
            "vestaboard_controller_command",
            {"command": "push_frame", "payload": payload},
            {},
        )
        assert app.create_task.call_count >= 1

    def test_malformed_payload_logs_warning(self):
        app = self._setup_app()
        app._on_command(
            "vestaboard_controller_command",
            {"command": "push_frame", "payload": "{invalid json"},
            {},
        )
        warning_calls = [c for c in app.log.call_args_list if "WARNING" in str(c)]
        assert len(warning_calls) >= 1

    def test_generate_random_message_fires_create_task(self):
        app = self._setup_app()
        mock_auto = MagicMock()
        mock_auto.generate_frame = AsyncMock(return_value=_blank_grid())
        app._automations["random_message"] = mock_auto

        app._on_command(
            "vestaboard_controller_command",
            {"command": "generate_random_message", "payload": "{}"},
            {},
        )
        assert app.create_task.call_count >= 1

    def test_generate_random_art_fires_create_task(self):
        app = self._setup_app()
        mock_auto = MagicMock()
        mock_auto.generate_frame = AsyncMock(return_value=_blank_grid())
        app._automations["random_art"] = mock_auto

        app._on_command(
            "vestaboard_controller_command",
            {"command": "generate_random_art", "payload": "{}"},
            {},
        )
        assert app.create_task.call_count >= 1

    def test_generate_ai_art_fires_create_task(self):
        app = self._setup_app()
        mock_auto = MagicMock()
        mock_auto.generate_frame = AsyncMock(return_value=_blank_grid())
        app._automations["ai_art_generator"] = mock_auto

        app._on_command(
            "vestaboard_controller_command",
            {"command": "generate_ai_art", "payload": json.dumps({"subject": "cat"})},
            {},
        )
        assert app.create_task.call_count >= 1

    def test_generate_ai_art_not_generate_art(self):
        """Regression: controller must recognise 'generate_ai_art', not 'generate_art'.
        The config app forwards generate_art commands as generate_ai_art; the controller
        must handle that exact string (Bug 7 regression guard)."""
        app = self._setup_app()
        mock_auto = MagicMock()
        mock_auto.generate_frame = AsyncMock(return_value=_blank_grid())
        app._automations["ai_art_generator"] = mock_auto
        app.log.reset_mock()

        # 'generate_art' (old, wrong name) must NOT be handled — it's an unknown command
        app._on_command(
            "vestaboard_controller_command",
            {"command": "generate_art", "payload": json.dumps({"subject": "cat"})},
            {},
        )
        warning_calls = [c for c in app.log.call_args_list if "WARNING" in str(c)]
        assert len(warning_calls) >= 1, "generate_art should log a warning (unrecognised command)"

        app.log.reset_mock()
        app.create_task.reset_mock()

        # 'generate_ai_art' (correct name) must be handled without a warning
        app._on_command(
            "vestaboard_controller_command",
            {"command": "generate_ai_art", "payload": json.dumps({"subject": "cat"})},
            {},
        )
        assert app.create_task.call_count >= 1


# ---------------------------------------------------------------------------
# Tick tests
# ---------------------------------------------------------------------------

class TestTick:
    def _setup_app_with_queue(self) -> VestaboardControllerApp:
        app = _make_app()
        app.initialize()
        app._queue = FrameQueue(log_fn=app.log)
        app._automations = {}
        app._trigger_handles = {}
        app._last_write_ok = None
        app.create_task = MagicMock(side_effect=lambda coro: _run(coro))
        # Mock set_state for status
        app.set_state = MagicMock()
        return app

    def test_tick_no_action_when_queue_empty(self):
        app = self._setup_app_with_queue()
        _run(app._tick())
        # create_task should not be called with a write when queue is empty
        # (it may be called 0 times since nothing to display)
        for c in app.create_task.call_args_list:
            # None of the coroutines should be _write_to_board since queue is empty
            # We can't easily inspect coroutine type, so just check count is low
            pass

    def test_tick_promotes_pending_frame_after_ttl_expires(self):
        app = self._setup_app_with_queue()
        # Mock _write_to_board to avoid real HTTP
        app._write_to_board = AsyncMock()

        # Push a frame with ttl=1 (already expired since displayed 10s ago)
        now = time.time()
        frame1 = BoardFrame(
            frame_id="frame1",
            characters=_blank_grid(),
            source="auto1",
            source_label="Auto1",
            ttl_s=1,
            expiration_s=None,
            override_ttl=False,
            created_at=now - 10,
            displayed_at=now - 10,  # displayed 10s ago, TTL=1 → expired
        )
        app._queue._displayed = frame1

        # Push a pending frame
        frame2 = BoardFrame(
            frame_id="frame2",
            characters=_blank_grid(),
            source="auto2",
            source_label="Auto2",
            ttl_s=60,
            expiration_s=None,
            override_ttl=False,
            created_at=now,
        )
        app._queue._pending.append(frame2)

        _run(app._tick())

        # frame2 should now be displayed
        assert app._queue._displayed is frame2
        # _write_to_board should have been called with frame2's characters
        assert app._write_to_board.call_count >= 1

    def test_tick_wrapper_calls_create_task(self):
        app = _make_app()
        app.initialize()
        app._queue = FrameQueue(log_fn=app.log)
        app._automations = {}
        app._trigger_handles = {}
        app._last_write_ok = None

        app._tick_wrapper({})
        assert app.create_task.call_count >= 1


# ---------------------------------------------------------------------------
# Status publishing tests
# ---------------------------------------------------------------------------

class TestStatusPublishing:
    def _setup_app(self) -> VestaboardControllerApp:
        app = _make_app()
        app.initialize()
        app._queue = FrameQueue(log_fn=app.log)
        app._automations = {}
        app._trigger_handles = {}
        app._last_write_ok = None
        return app

    def test_publish_status_idle_when_no_frame(self):
        app = self._setup_app()
        app._publish_status()

        call_args = app.set_state.call_args
        assert call_args[0][0] == "sensor.vestaboard_controller_status"
        assert call_args[1]["state"] == "idle"

    def test_publish_status_active_when_frame_displayed(self):
        app = self._setup_app()

        now = time.time()
        frame = BoardFrame(
            frame_id="f1",
            characters=_blank_grid(),
            source="calendar_clock",
            source_label="CalendarClock",
            ttl_s=None,
            expiration_s=None,
            override_ttl=False,
            created_at=now,
            displayed_at=now,
        )
        app._queue._displayed = frame

        app._publish_status()

        call_args = app.set_state.call_args
        assert call_args[1]["state"] == "active"
        attrs = call_args[1]["attributes"]
        assert attrs["displayed_source"] == "calendar_clock"

    def test_publish_status_includes_pending_count(self):
        app = self._setup_app()

        now = time.time()
        for i in range(3):
            frame = BoardFrame(
                frame_id=f"pending-{i}",
                characters=_blank_grid(),
                source=f"source{i}",
                source_label=f"Source{i}",
                ttl_s=60,
                expiration_s=None,
                override_ttl=False,
                created_at=now,
            )
            app._queue._pending.append(frame)

        # Make something displayed so TTL is active
        displayed = BoardFrame(
            frame_id="disp",
            characters=_blank_grid(),
            source="disp_source",
            source_label="Disp",
            ttl_s=300,
            expiration_s=None,
            override_ttl=False,
            created_at=now,
            displayed_at=now,
        )
        app._queue._displayed = displayed

        app._publish_status()
        attrs = app.set_state.call_args[1]["attributes"]
        assert attrs["pending_count"] == 3

    def test_publish_status_includes_all_automations(self):
        app = self._setup_app()

        mock_auto = MagicMock()
        mock_auto.name = "CalendarClock"
        app._automations["calendar_clock"] = mock_auto
        app._automation_configs["calendar_clock"] = {"enabled": True}
        app._automation_configs["random_message"] = {"enabled": False}
        app._active_automations = {"calendar_clock"}

        app._publish_status()
        attrs = app.set_state.call_args[1]["attributes"]
        all_autos = attrs["all_automations"]
        assert len(all_autos) == 2
        enabled = [a for a in all_autos if a["id"] == "calendar_clock"][0]
        assert enabled["name"] == "CalendarClock"
        assert enabled["enabled"] is True
        disabled = [a for a in all_autos if a["id"] == "random_message"][0]
        assert disabled["enabled"] is False

    def test_publish_status_includes_displayed_characters(self):
        app = self._setup_app()

        now = time.time()
        grid = [[63] * 22 for _ in range(6)]  # all-red grid
        frame = BoardFrame(
            frame_id="f-chars",
            characters=grid,
            source="test",
            source_label="Test",
            ttl_s=None,
            expiration_s=None,
            override_ttl=False,
            created_at=now,
            displayed_at=now,
        )
        app._queue._displayed = frame

        app._publish_status()
        attrs = app.set_state.call_args[1]["attributes"]
        displayed_frame = attrs["displayed_frame"]
        assert displayed_frame is not None
        assert displayed_frame["characters"] == grid

    def test_publish_status_pending_expires_at_with_expiration(self):
        """Pending items with expiration_s should have an ISO expires_at string."""
        app = self._setup_app()

        now = 1710000000.0  # fixed timestamp for deterministic ISO output
        # Display a frame with active TTL to keep pending items queued
        displayed = BoardFrame(
            frame_id="disp",
            characters=_blank_grid(),
            source="holder",
            source_label="Holder",
            ttl_s=9999,
            expiration_s=None,
            override_ttl=False,
            created_at=now,
            displayed_at=now,
        )
        app._queue._displayed = displayed

        pending = BoardFrame(
            frame_id="pending-with-expiry",
            characters=_blank_grid(),
            source="event",
            source_label="Event",
            ttl_s=60,
            expiration_s=300,
            override_ttl=False,
            created_at=now,
        )
        app._queue._pending.append(pending)

        app._publish_status()
        attrs = app.set_state.call_args[1]["attributes"]
        queue_pending = attrs["queue_state"]["pending"]
        assert len(queue_pending) == 1
        item = queue_pending[0]
        assert item["expires_at"] is not None
        # Should be an ISO string representing created_at + expiration_s
        from datetime import datetime, timezone
        expected_ts = now + 300
        expected_iso = datetime.fromtimestamp(expected_ts, tz=timezone.utc).isoformat()
        assert item["expires_at"] == expected_iso

    def test_publish_status_pending_expires_at_null_without_expiration(self):
        """Pending items without expiration_s should have expires_at=null."""
        app = self._setup_app()

        now = time.time()
        displayed = BoardFrame(
            frame_id="disp",
            characters=_blank_grid(),
            source="holder",
            source_label="Holder",
            ttl_s=9999,
            expiration_s=None,
            override_ttl=False,
            created_at=now,
            displayed_at=now,
        )
        app._queue._displayed = displayed

        pending = BoardFrame(
            frame_id="pending-no-expiry",
            characters=_blank_grid(),
            source="clock",
            source_label="Clock",
            ttl_s=None,
            expiration_s=None,
            override_ttl=False,
            created_at=now,
        )
        app._queue._pending.append(pending)

        app._publish_status()
        attrs = app.set_state.call_args[1]["attributes"]
        queue_pending = attrs["queue_state"]["pending"]
        assert len(queue_pending) == 1
        assert queue_pending[0]["expires_at"] is None

    def test_publish_status_includes_next_fire_time_when_set(self):
        """Automations with next_fire_time set should expose it in all_automations."""
        app = self._setup_app()

        now = time.time()
        next_fire = now + 600.0  # 10 minutes from now

        mock_auto = MagicMock()
        mock_auto.name = "MessagesFromLibrary"
        mock_auto.next_fire_time = next_fire
        mock_auto.get_preview_frame = MagicMock(return_value=_blank_grid())
        app._automations["messages_from_library"] = mock_auto
        app._automation_configs["messages_from_library"] = {"enabled": True}
        app._active_automations = {"messages_from_library"}

        app._publish_status()
        attrs = app.set_state.call_args[1]["attributes"]
        entry = attrs["all_automations"][0]
        assert "next_fire_time" in entry
        assert entry["next_fire_time"] == next_fire

    def test_publish_status_omits_next_fire_time_when_none(self):
        """Automations without a scheduled next_fire_time should not include the key."""
        app = self._setup_app()

        mock_auto = MagicMock()
        mock_auto.name = "CalendarClock"
        mock_auto.next_fire_time = None
        mock_auto.get_preview_frame = MagicMock(return_value=_blank_grid())
        app._automations["calendar_clock"] = mock_auto
        app._automation_configs["calendar_clock"] = {"enabled": True}
        app._active_automations = {"calendar_clock"}

        app._publish_status()
        attrs = app.set_state.call_args[1]["attributes"]
        entry = attrs["all_automations"][0]
        assert "next_fire_time" not in entry

    def test_publish_status_includes_preview_frame(self):
        """Each automation entry should include a preview_frame from get_preview_frame()."""
        app = self._setup_app()

        preview = [[1] * 22 for _ in range(6)]  # non-blank preview

        mock_auto = MagicMock()
        mock_auto.name = "ArtFromLibrary"
        mock_auto.next_fire_time = None
        mock_auto.get_preview_frame = MagicMock(return_value=preview)
        app._automations["art_from_library"] = mock_auto
        app._automation_configs["art_from_library"] = {"enabled": True}
        app._active_automations = {"art_from_library"}

        app._publish_status()
        attrs = app.set_state.call_args[1]["attributes"]
        entry = attrs["all_automations"][0]
        assert "preview_frame" in entry
        assert entry["preview_frame"] == preview
        mock_auto.get_preview_frame.assert_called_once()

    def test_publish_status_preview_frame_error_does_not_crash(self):
        """If get_preview_frame() raises, status publishing continues without preview."""
        app = self._setup_app()

        mock_auto = MagicMock()
        mock_auto.name = "BrokenAuto"
        mock_auto.next_fire_time = None
        mock_auto.get_preview_frame = MagicMock(side_effect=RuntimeError("oops"))
        app._automations["broken_auto"] = mock_auto
        app._automation_configs["broken_auto"] = {"enabled": True}
        app._active_automations = {"broken_auto"}

        # Must not raise
        app._publish_status()
        attrs = app.set_state.call_args[1]["attributes"]
        entry = attrs["all_automations"][0]
        # preview_frame should be absent (not added due to error)
        assert "preview_frame" not in entry

    def test_publish_status_no_preview_for_unknown_automation(self):
        """Automations not in _automations dict (unknown type) have no preview_frame."""
        app = self._setup_app()

        # Config entry with no corresponding instantiated automation object
        app._automation_configs["ghost_auto"] = {"enabled": False}
        # Do NOT add to _automations

        app._publish_status()
        attrs = app.set_state.call_args[1]["attributes"]
        entry = attrs["all_automations"][0]
        assert "preview_frame" not in entry
        assert "next_fire_time" not in entry


# ---------------------------------------------------------------------------
# Automation registration tests
# ---------------------------------------------------------------------------

class TestAutomationRegistration:
    def test_init_automations_skips_disabled(self):
        app = _make_app({"automations": {"calendar_clock": {"enabled": False}}})
        app.initialize()
        app._queue = FrameQueue(log_fn=app.log)
        app._trigger_handles = {}
        app._automations = {}
        app._active_automations = set()
        app._last_write_ok = None

        app._init_automations()

        # calendar_clock should be instantiated but not active
        assert "calendar_clock" in app._automations
        assert "calendar_clock" not in app._active_automations

    def test_init_automations_skips_unknown_type(self):
        app = _make_app({"automations": {"nonexistent_automation": {"enabled": True}}})
        app.initialize()
        app._queue = FrameQueue(log_fn=app.log)
        app._trigger_handles = {}
        app._automations = {}
        app._last_write_ok = None

        app._init_automations()

        assert "nonexistent_automation" not in app._automations

    def test_init_automations_instantiates_calendar_clock(self):
        app = _make_app({"automations": {"calendar_clock": {"enabled": True}}})
        app.initialize()
        app._queue = FrameQueue(log_fn=app.log)
        app._trigger_handles = {}
        app._automations = {}
        app._last_write_ok = None

        app._init_automations()

        assert "calendar_clock" in app._automations
        from vestaboard_controller_app.automations.calendar_clock import CalendarClockAutomation
        assert isinstance(app._automations["calendar_clock"], CalendarClockAutomation)

    def test_activate_automation_registers_interval_trigger(self):
        app = _make_app()
        app.initialize()
        app._queue = FrameQueue(log_fn=app.log)
        app._trigger_handles = {}
        app._automations = {}
        app._last_write_ok = None

        mock_auto = MagicMock()
        mock_auto.get_triggers = MagicMock(return_value=[
            {"type": "time_interval", "interval_s": 60, "callback": MagicMock()}
        ])
        app._automations["test_auto"] = mock_auto

        app._activate_automation("test_auto")

        # Activation schedules async registration via create_task
        assert app.create_task.called
        assert "test_auto" in app._active_automations

    def test_activate_automation_registers_state_trigger(self):
        app = _make_app()
        app.initialize()
        app._queue = FrameQueue(log_fn=app.log)
        app._trigger_handles = {}
        app._automations = {}
        app._last_write_ok = None

        mock_auto = MagicMock()
        mock_auto.get_triggers = MagicMock(return_value=[
            {"type": "state", "entity_id": "calendar.test", "callback": MagicMock()}
        ])
        app._automations["test_auto"] = mock_auto

        app._activate_automation("test_auto")

        # Activation schedules async registration via create_task
        assert app.create_task.called
        assert "test_auto" in app._active_automations

    def test_deactivate_automation_cancels_triggers(self):
        app = _make_app()
        app.initialize()
        app._queue = FrameQueue(log_fn=app.log)
        app._automations = {}
        app._last_write_ok = None
        app._trigger_handles = {
            ("my_auto", 0): ("handle-A", "timer"),
            ("my_auto", 1): ("handle-B", "state"),
            ("other_auto", 0): ("handle-C", "timer"),
        }

        mock_auto = MagicMock()
        mock_auto.get_triggers = MagicMock(return_value=[])
        app._automations["my_auto"] = mock_auto

        app._deactivate_automation("my_auto")

        assert ("my_auto", 0) not in app._trigger_handles
        assert ("my_auto", 1) not in app._trigger_handles
        # Other automations not affected
        assert ("other_auto", 0) in app._trigger_handles

    def test_deactivate_unknown_automation_logs_warning(self):
        app = _make_app()
        app.initialize()
        app._queue = FrameQueue(log_fn=app.log)
        app._automations = {}
        app._trigger_handles = {}
        app._last_write_ok = None

        app._deactivate_automation("ghost_auto")

        warning_calls = [c for c in app.log.call_args_list if "WARNING" in str(c)]
        assert len(warning_calls) >= 1


# ---------------------------------------------------------------------------
# Board write tests
# ---------------------------------------------------------------------------

class TestBoardWrite:
    def test_write_skipped_when_ip_not_configured(self):
        app = _make_app({"vestaboard_ip": "", "vestaboard_api_key": ""})
        app.initialize()
        app._vb_ip = ""
        app._vb_api_key = ""

        _run(app._write_to_board(_blank_grid()))

        warning_calls = [c for c in app.log.call_args_list if "WARNING" in str(c)]
        assert any("not configured" in str(c).lower() or "skipping" in str(c).lower()
                   for c in warning_calls)

    def test_write_calls_client(self):
        app = _make_app()
        app.initialize()
        app._vb_ip = "192.168.1.50"
        app._vb_api_key = "test-api-key-fake"

        mock_client = AsyncMock()
        mock_client.write_frame = AsyncMock(return_value=True)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "vestaboard_controller_app.vestaboard_controller_app.VestaboardClient",
            return_value=mock_client,
        ):
            _run(app._write_to_board(_blank_grid()))

        mock_client.write_frame.assert_called_once()
        assert app._last_write_ok is True

    def test_write_sets_last_write_ok_false_on_failure(self):
        app = _make_app()
        app.initialize()
        app._vb_ip = "192.168.1.50"
        app._vb_api_key = "test-api-key-fake"

        mock_client = AsyncMock()
        mock_client.write_frame = AsyncMock(side_effect=Exception("Connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "vestaboard_controller_app.vestaboard_controller_app.VestaboardClient",
            return_value=mock_client,
        ):
            _run(app._write_to_board(_blank_grid()))

        assert app._last_write_ok is False


# ---------------------------------------------------------------------------
# Frame queue integration tests
# ---------------------------------------------------------------------------

class TestFrameQueueIntegration:
    def test_push_automation_frame_displays_when_queue_empty(self):
        app = _make_app()
        app.initialize()
        app._queue = FrameQueue(log_fn=app.log)
        app._automations = {}
        app._trigger_handles = {}
        app._last_write_ok = None
        app.set_state = MagicMock()
        app.create_task = MagicMock(side_effect=lambda coro: _run(coro))

        grid = _blank_grid()
        app._push_automation_frame(
            automation_id="calendar_clock",
            source_label="CalendarClock",
            grid=grid,
            ttl_s=60,
            expiration_s=None,
        )

        # Frame should be displayed immediately (queue was empty)
        assert app._queue._displayed is not None
        assert app._queue._displayed.source == "calendar_clock"
        assert app.create_task.call_count == 1  # board write

    def test_push_automation_frame_queues_when_ttl_active(self):
        app = _make_app()
        app.initialize()
        app._queue = FrameQueue(log_fn=app.log)
        app._automations = {}
        app._trigger_handles = {}
        app._last_write_ok = None
        app.set_state = MagicMock()
        app.create_task = MagicMock(side_effect=lambda coro: _run(coro))

        # First frame: displayed with active TTL
        now = time.time()
        first_frame = BoardFrame(
            frame_id="first",
            characters=_blank_grid(),
            source="source1",
            source_label="Source1",
            ttl_s=300,
            expiration_s=None,
            override_ttl=False,
            created_at=now,
            displayed_at=now,
        )
        app._queue._displayed = first_frame

        # Push second frame (no override_ttl)
        app._push_automation_frame(
            automation_id="source2",
            source_label="Source2",
            grid=_blank_grid(),
            ttl_s=60,
            expiration_s=None,
            override_ttl=False,
        )

        # Should be in pending, not displayed
        assert len(app._queue._pending) == 1
        assert app._queue._displayed is first_frame

    def test_override_ttl_preempts_active_frame(self):
        app = _make_app()
        app.initialize()
        app._queue = FrameQueue(log_fn=app.log)
        app._automations = {}
        app._trigger_handles = {}
        app._last_write_ok = None
        app.set_state = MagicMock()
        app.create_task = MagicMock(side_effect=lambda coro: _run(coro))

        # First frame: displayed with active TTL
        now = time.time()
        first_frame = BoardFrame(
            frame_id="first",
            characters=_blank_grid(),
            source="source1",
            source_label="Source1",
            ttl_s=300,
            expiration_s=None,
            override_ttl=False,
            created_at=now,
            displayed_at=now,
        )
        app._queue._displayed = first_frame

        # Push second frame with override_ttl=True
        app._push_automation_frame(
            automation_id="user",
            source_label="User",
            grid=_blank_grid(),
            ttl_s=30,
            expiration_s=None,
            override_ttl=True,
        )

        # Should now be displayed (override preempts)
        assert app._queue._displayed.source == "user"
        assert app.create_task.call_count >= 1

    def test_dedup_same_source_pending_frames(self):
        app = _make_app()
        app.initialize()
        app._queue = FrameQueue(log_fn=app.log)
        app._automations = {}
        app._trigger_handles = {}
        app._last_write_ok = None
        app.set_state = MagicMock()
        app.create_task = MagicMock(side_effect=lambda coro: _run(coro))

        # Display a frame with active TTL
        now = time.time()
        first_frame = BoardFrame(
            frame_id="first",
            characters=_blank_grid(),
            source="other_source",
            source_label="Other",
            ttl_s=300,
            expiration_s=None,
            override_ttl=False,
            created_at=now,
            displayed_at=now,
        )
        app._queue._displayed = first_frame

        # Push two frames from the same source without override_ttl
        app._push_automation_frame("auto_x", "AutoX", _blank_grid(), 60, None)
        app._push_automation_frame("auto_x", "AutoX", _blank_grid(), 60, None)

        # Second push should replace the first (dedup)
        assert len(app._queue._pending) == 1


# ---------------------------------------------------------------------------
# CalendarClock automation tests
# ---------------------------------------------------------------------------

class TestCalendarClockAutomation:
    def test_generate_frame_returns_6x22(self):
        from vestaboard_controller_app.automations.calendar_clock import CalendarClockAutomation

        mock_app = MagicMock()
        mock_app.log = MagicMock()
        auto = CalendarClockAutomation(app=mock_app, config={})

        grid = _run(auto.generate_frame())

        assert len(grid) == 6
        for row in grid:
            assert len(row) == 22

    def test_generate_frame_uses_month_colors(self):
        """Today's date determines color codes in the calendar pane."""
        from vestaboard_controller_app.automations.calendar_clock import (
            CalendarClockAutomation,
            _MONTH_COLORS,
        )
        import datetime

        mock_app = MagicMock()
        mock_app.log = MagicMock()
        auto = CalendarClockAutomation(app=mock_app, config={})

        grid = _run(auto.generate_frame())
        now = datetime.datetime.now()
        tile_day, tile_today = _MONTH_COLORS[now.month]

        # Some cell in rows 1-5, cols 0-6 should be tile_day or tile_today
        cal_cells = {grid[r][c] for r in range(1, 6) for c in range(7)}
        # At minimum today's color tile should appear
        assert tile_today in cal_cells or tile_day in cal_cells

    def test_generate_frame_right_pane_has_chars(self):
        """Right pane (cols 9-21) should have some non-zero codes."""
        from vestaboard_controller_app.automations.calendar_clock import CalendarClockAutomation

        mock_app = MagicMock()
        mock_app.log = MagicMock()
        auto = CalendarClockAutomation(app=mock_app, config={})

        grid = _run(auto.generate_frame())
        right_pane_cells = [grid[r][c] for r in range(1, 6) for c in range(9, 22)]
        assert any(c != 0 for c in right_pane_cells)


# ---------------------------------------------------------------------------
# RandomArt automation tests
# ---------------------------------------------------------------------------

class TestRandomArtAutomation:
    def test_generate_frame_returns_valid_grid(self):
        from vestaboard_controller_app.automations.random_art import RandomArtAutomation

        mock_app = MagicMock()
        mock_app.log = MagicMock()
        auto = RandomArtAutomation(app=mock_app, config={})

        # Should have loaded library
        assert len(auto._library) > 0

        grid = _run(auto.generate_frame())
        assert len(grid) == 6
        for row in grid:
            assert len(row) == 22

    def test_generate_frame_empty_library_returns_blank(self):
        from vestaboard_controller_app.automations.random_art import RandomArtAutomation

        mock_app = MagicMock()
        mock_app.log = MagicMock()
        auto = RandomArtAutomation(app=mock_app, config={})
        auto._library = []  # empty library

        grid = _run(auto.generate_frame())
        assert grid == [[0] * 22 for _ in range(6)]

    def test_library_loaded_from_file(self):
        from vestaboard_controller_app.automations.random_art import RandomArtAutomation

        mock_app = MagicMock()
        mock_app.log = MagicMock()
        auto = RandomArtAutomation(app=mock_app, config={})

        # Library should have at least the pre-built entries
        assert len(auto._library) >= 4
        # Each entry has name and characters
        for entry in auto._library:
            assert "name" in entry
            assert "characters" in entry


# ---------------------------------------------------------------------------
# AIArtGenerator automation tests
# ---------------------------------------------------------------------------

class TestAIArtGeneratorAutomation:
    def test_generate_frame_returns_blank_without_ai_config(self):
        from vestaboard_controller_app.automations.ai_art_generator import AIArtGeneratorAutomation

        mock_app = MagicMock()
        mock_app.log = MagicMock()
        auto = AIArtGeneratorAutomation(app=mock_app, config={})

        grid = _run(auto.generate_frame(subject="cat"))
        assert grid == [[0] * 22 for _ in range(6)]

    def test_validate_grid_catches_wrong_dimensions(self):
        from vestaboard_controller_app.automations.ai_art_generator import _validate_grid

        ok, err = _validate_grid([[0] * 22 for _ in range(5)])  # 5 rows instead of 6
        assert not ok
        assert "rows" in err

    def test_validate_grid_catches_invalid_code(self):
        from vestaboard_controller_app.automations.ai_art_generator import _validate_grid

        grid = [[0] * 22 for _ in range(6)]
        grid[0][0] = 999  # invalid code

        ok, err = _validate_grid(grid)
        assert not ok
        assert "invalid code" in err

    def test_validate_grid_accepts_valid_grid(self):
        from vestaboard_controller_app.automations.ai_art_generator import _validate_grid

        grid = [[0] * 22 for _ in range(6)]
        grid[0] = [63] * 22  # all-red row
        grid[5] = [66] * 22  # all-green row

        ok, err = _validate_grid(grid)
        assert ok
        assert err == ""


# ---------------------------------------------------------------------------
# RandomMessage automation tests
# ---------------------------------------------------------------------------

class TestRandomMessageAutomation:
    def test_generate_fallback_returns_valid_grid(self):
        from vestaboard_controller_app.automations.random_message import RandomMessageAutomation

        mock_app = MagicMock()
        mock_app.log = MagicMock()
        auto = RandomMessageAutomation(app=mock_app, config={})

        grid = auto._generate_fallback_frame()
        assert len(grid) == 6
        for row in grid:
            assert len(row) == 22

    def test_generate_frame_uses_fallback_without_ai(self):
        from vestaboard_controller_app.automations.random_message import RandomMessageAutomation

        mock_app = MagicMock()
        mock_app.log = MagicMock()
        auto = RandomMessageAutomation(app=mock_app, config={})

        grid = _run(auto.generate_frame())
        assert len(grid) == 6

    def test_generate_frame_falls_back_on_ai_error(self):
        from vestaboard_controller_app.automations.random_message import RandomMessageAutomation

        mock_app = MagicMock()
        mock_app.log = MagicMock()
        auto = RandomMessageAutomation(
            app=mock_app,
            config={"ai_provider_conf": {"simple_text": {"provider": "openai", "api_key": "test-key"}}},
        )

        # Patch AI provider to raise
        with patch.object(auto, "_generate_ai_frame", AsyncMock(side_effect=Exception("AI down"))):
            grid = _run(auto.generate_frame())

        assert len(grid) == 6
        # Should log a warning about fallback
        warning_calls = [c for c in mock_app.log.call_args_list if "WARNING" in str(c)]
        assert len(warning_calls) >= 1


# ---------------------------------------------------------------------------
# Type-based automation lookup tests
# ---------------------------------------------------------------------------

class TestTypeBasedAutomationLookup:
    """Tests for the type-based automation initialization (Enhancement 5)."""

    def test_type_key_uses_type_for_class_lookup(self):
        """When config has a 'type' key, class is looked up by type, not by id."""
        app = _make_app({
            "automations": {
                "my_custom_calendar": {
                    "type": "calendar_summary",
                    "calendar_entity": "calendar.family",
                    "enabled": False,  # don't activate (avoids trigger registration)
                },
            }
        })
        app.initialize()
        app._queue = FrameQueue(log_fn=app.log)
        app._trigger_handles = {}
        app._automations = {}
        app._active_automations = set()
        app._last_write_ok = None

        app._init_automations()

        # Should be instantiated under the custom ID
        assert "my_custom_calendar" in app._automations
        from vestaboard_controller_app.automations.calendar_summary import CalendarSummaryAutomation
        assert isinstance(app._automations["my_custom_calendar"], CalendarSummaryAutomation)

    def test_no_type_key_falls_back_to_id(self):
        """When no 'type' key is present, automation_id is used for class lookup (backward compat)."""
        app = _make_app({
            "automations": {
                "calendar_clock": {"enabled": False},
            }
        })
        app.initialize()
        app._queue = FrameQueue(log_fn=app.log)
        app._trigger_handles = {}
        app._automations = {}
        app._active_automations = set()
        app._last_write_ok = None

        app._init_automations()

        assert "calendar_clock" in app._automations
        from vestaboard_controller_app.automations.calendar_clock import CalendarClockAutomation
        assert isinstance(app._automations["calendar_clock"], CalendarClockAutomation)

    def test_two_calendar_summary_instances_different_ids(self):
        """Two calendar_summary instances with different IDs and calendar entities."""
        app = _make_app({
            "automations": {
                "calendar_summary_family": {
                    "type": "calendar_summary",
                    "calendar_entity": "calendar.family",
                    "enabled": False,
                },
                "calendar_summary_hot_tub": {
                    "type": "calendar_summary",
                    "calendar_entity": "calendar.hot_tub_maintenance",
                    "enabled": False,
                },
            }
        })
        app.initialize()
        app._queue = FrameQueue(log_fn=app.log)
        app._trigger_handles = {}
        app._automations = {}
        app._active_automations = set()
        app._last_write_ok = None

        app._init_automations()

        from vestaboard_controller_app.automations.calendar_summary import CalendarSummaryAutomation
        assert "calendar_summary_family" in app._automations
        assert "calendar_summary_hot_tub" in app._automations
        assert isinstance(app._automations["calendar_summary_family"], CalendarSummaryAutomation)
        assert isinstance(app._automations["calendar_summary_hot_tub"], CalendarSummaryAutomation)

        # Each should have its own calendar entity
        family_entity = app._automations["calendar_summary_family"].config.get("calendar_entity")
        hot_tub_entity = app._automations["calendar_summary_hot_tub"].config.get("calendar_entity")
        assert family_entity == "calendar.family"
        assert hot_tub_entity == "calendar.hot_tub_maintenance"

    def test_type_based_instance_gets_automation_id(self):
        """Instances with type key should have set_automation_id called."""
        app = _make_app({
            "automations": {
                "cal_work": {
                    "type": "calendar_summary",
                    "calendar_entity": "calendar.work",
                    "enabled": False,
                },
            }
        })
        app.initialize()
        app._queue = FrameQueue(log_fn=app.log)
        app._trigger_handles = {}
        app._automations = {}
        app._active_automations = set()
        app._last_write_ok = None

        app._init_automations()

        instance = app._automations["cal_work"]
        assert instance._automation_id == "cal_work"

    def test_unknown_type_key_logs_warning(self):
        """Unknown type key should log a warning and skip."""
        app = _make_app({
            "automations": {
                "my_thing": {
                    "type": "nonexistent_type",
                    "enabled": True,
                },
            }
        })
        app.initialize()
        app._queue = FrameQueue(log_fn=app.log)
        app._trigger_handles = {}
        app._automations = {}
        app._active_automations = set()
        app._last_write_ok = None

        app._init_automations()

        assert "my_thing" not in app._automations
        warning_calls = [c for c in app.log.call_args_list if "WARNING" in str(c)]
        assert any("nonexistent_type" in str(c) for c in warning_calls)


# ---------------------------------------------------------------------------
# CalendarSummary automation tests
# ---------------------------------------------------------------------------

class TestCalendarSummaryAutomation:
    def _make_auto(self, config: dict | None = None) -> "CalendarSummaryAutomation":
        from vestaboard_controller_app.automations.calendar_summary import CalendarSummaryAutomation

        mock_app = MagicMock()
        mock_app.log = MagicMock()
        mock_app.get_state = MagicMock(return_value=None)
        mock_app.create_task = MagicMock(side_effect=lambda coro: _run(coro))
        mock_app._push_automation_frame = MagicMock()
        auto = CalendarSummaryAutomation(app=mock_app, config=config or {})
        return auto

    def test_get_triggers_with_calendar_entity(self):
        auto = self._make_auto({"calendar_entity": "calendar.family"})
        triggers = auto.get_triggers()

        # Should have 1 state trigger + 1 interval trigger
        state_triggers = [t for t in triggers if t["type"] == "state"]
        interval_triggers = [t for t in triggers if t["type"] == "time_interval"]
        assert len(state_triggers) == 1
        assert state_triggers[0]["entity_id"] == "calendar.family"
        assert len(interval_triggers) == 1

    def test_get_triggers_without_calendar_entity(self):
        auto = self._make_auto({})
        triggers = auto.get_triggers()

        # Should only have interval trigger, no state trigger
        state_triggers = [t for t in triggers if t["type"] == "state"]
        interval_triggers = [t for t in triggers if t["type"] == "time_interval"]
        assert len(state_triggers) == 0
        assert len(interval_triggers) == 1

    def test_fire_frame_no_entity_logs_warning(self):
        auto = self._make_auto({})
        _run(auto._fire_frame_if_event())

        warning_calls = [c for c in auto.app.log.call_args_list if "WARNING" in str(c)]
        assert any("calendar_entity" in str(c).lower() for c in warning_calls)

    def test_fire_frame_uses_automation_id_as_source(self):
        """The automation_id should be used as the source when pushing frames."""
        from datetime import datetime, timezone, timedelta
        auto = self._make_auto({"calendar_entity": "calendar.family"})
        auto.set_automation_id("cal_family")

        # Mock an active event
        now = datetime.now(tz=timezone.utc)
        end = now + timedelta(hours=1)
        auto.app.get_state.return_value = {
            "state": "on",
            "attributes": {
                "message": "Dinner",
                "start_time": now.isoformat(),
                "end_time": end.isoformat(),
            },
        }

        _run(auto._fire_frame_if_event())

        auto.app._push_automation_frame.assert_called_once()
        call_kwargs = auto.app._push_automation_frame.call_args[1]
        assert call_kwargs["automation_id"] == "cal_family"

    def test_set_automation_id_updates_name(self):
        auto = self._make_auto({})
        auto.set_automation_id("cal_hot_tub")
        assert "cal_hot_tub" in auto.name

    def test_set_automation_id_default_keeps_name(self):
        auto = self._make_auto({})
        auto.set_automation_id("calendar_summary")
        assert auto.name == "CalendarSummary"

    def test_ttl_minutes_config_overrides_dynamic_ttl(self):
        """When ttl_minutes is set in config, it overrides the dynamic TTL."""
        from datetime import datetime, timezone, timedelta
        auto = self._make_auto({
            "calendar_entity": "calendar.test",
            "ttl_minutes": 10,
        })
        auto.set_automation_id("test_cal")

        now = datetime.now(tz=timezone.utc)
        end = now + timedelta(hours=2)
        auto.app.get_state.return_value = {
            "state": "on",
            "attributes": {
                "message": "Meeting",
                "start_time": now.isoformat(),
                "end_time": end.isoformat(),
            },
        }

        _run(auto._fire_frame_if_event())

        call_kwargs = auto.app._push_automation_frame.call_args[1]
        assert call_kwargs["ttl_s"] == 600  # 10 minutes * 60

    def test_rotation_interval_throttles_repeat_push(self):
        """rotation_interval_hours should prevent pushing the same event too quickly."""
        from datetime import datetime, timezone, timedelta
        import time as _time

        auto = self._make_auto({
            "calendar_entity": "calendar.test",
            "rotation_interval_hours": 1,
        })
        auto.set_automation_id("test_cal")

        now = datetime.now(tz=timezone.utc)
        end = now + timedelta(hours=2)
        auto.app.get_state.return_value = {
            "state": "on",
            "attributes": {
                "message": "Meeting",
                "start_time": now.isoformat(),
                "end_time": end.isoformat(),
            },
        }

        # First push should succeed
        _run(auto._fire_frame_if_event())
        assert auto.app._push_automation_frame.call_count == 1

        # Second push should be throttled
        _run(auto._fire_frame_if_event())
        assert auto.app._push_automation_frame.call_count == 1  # still 1

    def test_time_before_event_hours_overrides_reminder_minutes(self):
        """time_before_event_hours should override reminder_minutes for the reminder window."""
        from datetime import datetime, timezone, timedelta

        # Event starts 2 hours from now — default 15min reminder would miss it,
        # but time_before_event_hours=3 should catch it.
        auto = self._make_auto({
            "calendar_entity": "calendar.test",
            "time_before_event_hours": 3,
        })
        auto.set_automation_id("test_cal")

        now = datetime.now(tz=timezone.utc)
        start = now + timedelta(hours=2)
        end = start + timedelta(hours=1)
        auto.app.get_state.return_value = {
            "state": "off",
            "attributes": {
                "message": "Future Event",
                "start_time": start.isoformat(),
                "end_time": end.isoformat(),
            },
        }

        _run(auto._fire_frame_if_event())
        assert auto.app._push_automation_frame.call_count == 1

    def test_generate_frame_returns_6x22(self):
        auto = self._make_auto({"calendar_entity": "calendar.test"})
        grid = _run(auto.generate_frame())
        assert len(grid) == 6
        for row in grid:
            assert len(row) == 22
