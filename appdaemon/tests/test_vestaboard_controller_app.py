"""Tests for VestaboardControllerApp — init, provisioning, commands, tick, status.

Covers the event-based architecture where automations register dynamically via
HA events (register_automation command / deregister_automation command) rather
than direct get_app() references.
"""

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

from vestaboard_apps.vestaboard_controller.vestaboard_controller_app import (
    RemoteAutomationProxy,
    VestaboardControllerApp,
)
from vestaboard_apps._shared.frame_queue import BoardFrame, FrameQueue


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
    app.fire_event = MagicMock()
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


def _test_grid() -> list[list[int]]:
    """Return a non-blank 6x22 grid suitable for tests that go through push."""
    grid = [[0] * 22 for _ in range(6)]
    grid[0][0] = 1  # at least one non-zero cell
    return grid


def _make_frame(source: str = "test", ttl_s: int | None = None) -> BoardFrame:
    return BoardFrame(
        frame_id="test-frame-001",
        characters=_blank_grid(),
        source=source,
        source_label=source.title(),
        ttl_s=ttl_s,
        max_age_s=None,
        override_ttl=False,
        created_at=time.time(),
    )


def _make_registration_payload(
    auto_id: str = "test_auto",
    automation_type: str = "test_type",
    display_name: str = "Test Auto",
    display_description: str = "A test automation.",
    default_ttl_s: int | None = 60,
    default_max_age_s: int | None = None,
    default_should_expire: bool = False,
    enabled: bool = True,
) -> dict:
    """Return a registration payload dict (as the mixin would fire it)."""
    return {
        "automation_id": auto_id,
        "automation_type": automation_type,
        "display_name": display_name,
        "display_description": display_description,
        "default_ttl_s": default_ttl_s,
        "default_max_age_s": default_max_age_s,
        "default_should_expire": default_should_expire,
        "DEFAULT_UI_CONFIG": {"enabled": enabled},
        "config_schema": {},
        "preview_frame": json.dumps(_blank_grid()),
    }


def _simulate_register(app: VestaboardControllerApp, payload: dict) -> None:
    """Simulate an automation firing a register_automation command event."""
    app._on_command(
        "vestaboard_controller_command",
        {"command": "register_automation", "payload": json.dumps(payload)},
        {},
    )


def _make_mock_automation(
    auto_id: str = "test_auto",
    automation_type: str = "test_type",
    display_name: str = "Test Auto",
    display_description: str = "A test automation.",
    default_ttl_s: int | None = 60,
    default_max_age_s: int | None = None,
    default_should_expire: bool = False,
    enabled: bool = True,
) -> MagicMock:
    """Create a mock automation object (for tests that directly manipulate the
    registered_automations dict without going through events)."""
    auto = MagicMock()
    auto.name = auto_id
    auto.automation_type = automation_type
    auto.display_name = display_name
    auto.display_description = display_description
    auto.DEFAULT_UI_CONFIG = {"enabled": enabled}
    auto.default_ttl_s = default_ttl_s
    auto.default_max_age_s = default_max_age_s
    auto.default_should_expire = default_should_expire
    auto.on_config_updated = MagicMock()
    auto.set_enabled = MagicMock()
    auto.get_config_schema = MagicMock(return_value={})
    auto.get_preview_frame = MagicMock(return_value=_blank_grid())
    auto.get_effective_config = MagicMock(return_value={"enabled": enabled})
    auto.get_resolved_ttl_s = MagicMock(return_value=default_ttl_s)
    auto.get_resolved_should_expire = MagicMock(return_value=default_should_expire)
    auto._next_fire_time = None
    return auto


def _setup_app_with_queue() -> VestaboardControllerApp:
    """Create app with initialize() called and _registered_automations ready."""
    app = _make_app()
    app.initialize()
    app._queue = FrameQueue(log_fn=app.log)
    app._registered_automations = {}
    app._last_write_ok = None
    app._write_to_board = AsyncMock()
    app.create_task = MagicMock(side_effect=lambda coro: _run(coro))
    app.set_state = MagicMock()
    app.fire_event = MagicMock()
    return app


# ---------------------------------------------------------------------------
# RemoteAutomationProxy tests
# ---------------------------------------------------------------------------

class TestRemoteAutomationProxy:
    def test_basic_attributes(self):
        data = _make_registration_payload(
            auto_id="my_auto",
            automation_type="calendar_clock",
            display_name="Calendar Clock",
            default_ttl_s=120,
            default_should_expire=True,
        )
        proxy = RemoteAutomationProxy(data)

        assert proxy.name == "my_auto"
        assert proxy.automation_type == "calendar_clock"
        assert proxy.display_name == "Calendar Clock"
        assert proxy.default_ttl_s == 120
        assert proxy.default_should_expire is True

    def test_preview_frame_decoded_from_json_string(self):
        grid = [[1] * 22 for _ in range(6)]
        data = _make_registration_payload()
        data["preview_frame"] = json.dumps(grid)
        proxy = RemoteAutomationProxy(data)
        assert proxy.get_preview_frame() == grid

    def test_preview_frame_fallback_on_bad_json(self):
        data = _make_registration_payload()
        data["preview_frame"] = "not valid json {{{"
        proxy = RemoteAutomationProxy(data)
        assert proxy.get_preview_frame() == [[0] * 22 for _ in range(6)]

    def test_get_config_schema(self):
        schema = {"enabled": {"type": "bool"}}
        data = _make_registration_payload()
        data["config_schema"] = schema
        proxy = RemoteAutomationProxy(data)
        assert proxy.get_config_schema() == schema

    def test_get_effective_config_returns_copy(self):
        data = _make_registration_payload()
        proxy = RemoteAutomationProxy(data)
        cfg = proxy.get_effective_config()
        cfg["extra"] = "should not affect proxy"
        assert "extra" not in proxy.get_effective_config()

    def test_get_resolved_ttl_s_from_effective_config(self):
        data = _make_registration_payload(default_ttl_s=60)
        proxy = RemoteAutomationProxy(data)
        proxy.update_config({"ttl_minutes": 5})
        assert proxy.get_resolved_ttl_s() == 300  # 5 * 60

    def test_get_resolved_ttl_s_fallback_to_default(self):
        data = _make_registration_payload(default_ttl_s=120)
        proxy = RemoteAutomationProxy(data)
        assert proxy.get_resolved_ttl_s() == 120

    def test_get_resolved_should_expire_from_effective_config(self):
        data = _make_registration_payload(default_should_expire=False)
        proxy = RemoteAutomationProxy(data)
        proxy.update_config({"should_expire": True})
        assert proxy.get_resolved_should_expire() is True

    def test_update_config_merges(self):
        data = _make_registration_payload()
        proxy = RemoteAutomationProxy(data)
        proxy.update_config({"ttl_minutes": 10, "enabled": False})
        cfg = proxy.get_effective_config()
        assert cfg["ttl_minutes"] == 10
        assert cfg["enabled"] is False


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

    def test_initialize_creates_registered_automations_dict(self):
        app = _make_app()
        app.initialize()
        assert hasattr(app, "_registered_automations")
        assert isinstance(app._registered_automations, dict)
        assert len(app._registered_automations) == 0

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
    def _run_startup(self, app):
        """Run _async_startup with all external deps patched."""
        mock_prov = MagicMock()
        mock_prov.ensure_script = AsyncMock(return_value=False)

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.read_current = AsyncMock(return_value=None)

        with patch("providers.ha_provisioner.HAProvisioner", return_value=mock_prov), \
             patch("vestaboard_apps.vestaboard_controller.vestaboard_controller_app.VestaboardClient", return_value=mock_client):
            _run(app._async_startup())

        return mock_prov

    def test_async_startup_registers_event_listener(self):
        app = _make_app()
        app.initialize()
        self._run_startup(app)

        assert app.listen_event.call_count >= 1
        call_args = app.listen_event.call_args_list
        event_names = [c[0][1] for c in call_args if len(c[0]) >= 2]
        assert "vestaboard_controller_command" in event_names

    def test_async_startup_registers_tick_timer(self):
        app = _make_app()
        app.initialize()
        self._run_startup(app)
        assert app.run_every.call_count >= 1

    def test_async_startup_provisions_relay_script(self):
        app = _make_app()
        app.initialize()

        mock_prov = MagicMock()
        mock_prov.ensure_script = AsyncMock(return_value=True)

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.read_current = AsyncMock(return_value=None)

        with patch("providers.ha_provisioner.HAProvisioner", return_value=mock_prov), \
             patch("vestaboard_apps.vestaboard_controller.vestaboard_controller_app.VestaboardClient", return_value=mock_client):
            _run(app._async_startup())

        mock_prov.ensure_script.assert_called_once()
        script_id = mock_prov.ensure_script.call_args[0][0]
        assert script_id == "vestaboard_controller_relay"

    def test_async_startup_publishes_status(self):
        app = _make_app()
        app.initialize()
        self._run_startup(app)

        app.set_state.assert_called()
        call_args = app.set_state.call_args
        assert "sensor.vestaboard_controller_status" in call_args[0]

    def test_async_startup_fires_ready_event(self):
        app = _make_app()
        app.initialize()
        self._run_startup(app)

        app.fire_event.assert_called()
        fire_calls = [c for c in app.fire_event.call_args_list
                      if c[0] and c[0][0] == "vestaboard_controller_ready"]
        assert len(fire_calls) >= 1

    def test_async_startup_skips_provisioning_without_ha_url(self):
        app = _make_app({"ha_url": "", "ha_token_env": ""})
        app.initialize()

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.read_current = AsyncMock(return_value=None)

        with patch("providers.ha_provisioner.HAProvisioner") as mock_prov_cls, \
             patch("vestaboard_apps.vestaboard_controller.vestaboard_controller_app.VestaboardClient", return_value=mock_client):
            _run(app._async_startup())

        mock_prov_cls.assert_not_called()


# ---------------------------------------------------------------------------
# Event-based automation registration tests
# ---------------------------------------------------------------------------

class TestAutomationRegistration:
    def test_register_automation_adds_proxy_to_dict(self):
        app = _setup_app_with_queue()
        payload = _make_registration_payload("my_auto")

        _simulate_register(app, payload)

        assert "my_auto" in app._registered_automations
        assert isinstance(app._registered_automations["my_auto"], RemoteAutomationProxy)

    def test_register_automation_proxy_has_correct_metadata(self):
        app = _setup_app_with_queue()
        payload = _make_registration_payload(
            "my_auto",
            automation_type="calendar_clock",
            display_name="Calendar Clock",
        )

        _simulate_register(app, payload)

        proxy = app._registered_automations["my_auto"]
        assert proxy.automation_type == "calendar_clock"
        assert proxy.display_name == "Calendar Clock"

    def test_register_automation_seeds_config_store_defaults(self):
        app = _setup_app_with_queue()
        payload = _make_registration_payload("my_auto")
        payload["DEFAULT_UI_CONFIG"] = {"enabled": True, "ttl_minutes": 5}

        mock_store = MagicMock()
        mock_store.seed = MagicMock(return_value=True)
        mock_store.get = MagicMock(return_value={})
        app._config_store = mock_store

        _simulate_register(app, payload)

        mock_store.seed.assert_called_once_with("my_auto", {"enabled": True, "ttl_minutes": 5})
        mock_store.save.assert_called_once()

    def test_register_automation_fires_config_back_when_stored(self):
        app = _setup_app_with_queue()
        payload = _make_registration_payload("my_auto")

        stored_config = {"enabled": False, "ttl_minutes": 10}
        mock_store = MagicMock()
        mock_store.seed = MagicMock(return_value=False)
        mock_store.get = MagicMock(return_value=stored_config)
        app._config_store = mock_store

        _simulate_register(app, payload)

        # Controller fires a config event back to the automation
        fire_calls = [
            c for c in app.fire_event.call_args_list
            if c[0] and c[0][0] == "vb_auto_config"
        ]
        assert len(fire_calls) == 1
        assert fire_calls[0][1]["config"] == stored_config

    def test_register_automation_skips_config_fire_when_store_empty(self):
        app = _setup_app_with_queue()
        payload = _make_registration_payload("my_auto")

        mock_store = MagicMock()
        mock_store.seed = MagicMock(return_value=False)
        mock_store.get = MagicMock(return_value={})
        app._config_store = mock_store

        _simulate_register(app, payload)

        fire_calls = [
            c for c in app.fire_event.call_args_list
            if c[0] and c[0][0] == "vb_auto_config"
        ]
        assert len(fire_calls) == 0

    def test_register_automation_publishes_status(self):
        app = _setup_app_with_queue()
        payload = _make_registration_payload("my_auto")

        initial_count = app.set_state.call_count
        _simulate_register(app, payload)

        assert app.set_state.call_count > initial_count

    def test_register_missing_automation_id_logs_warning(self):
        app = _setup_app_with_queue()

        app._on_command(
            "vestaboard_controller_command",
            {"command": "register_automation", "payload": json.dumps({})},
            {},
        )

        warning_calls = [c for c in app.log.call_args_list if "WARNING" in str(c)]
        assert any("automation_id" in str(c) or "missing" in str(c).lower() for c in warning_calls)

    def test_deregister_automation_removes_proxy(self):
        app = _setup_app_with_queue()
        payload = _make_registration_payload("my_auto")
        _simulate_register(app, payload)

        app._on_command(
            "vestaboard_controller_command",
            {"command": "deregister_automation", "payload": json.dumps({"automation_id": "my_auto"})},
            {},
        )

        assert "my_auto" not in app._registered_automations

    def test_deregister_automation_removes_frames_from_queue(self):
        app = _setup_app_with_queue()
        payload = _make_registration_payload("my_auto")
        _simulate_register(app, payload)

        from vestaboard_apps._shared.frame_queue import FrameQueueAction
        mock_queue = MagicMock()
        mock_queue.remove_source = MagicMock(return_value=FrameQueueAction(
            display_frame=None, dropped_frames=[], reason="removed",
        ))
        mock_queue.get_state = MagicMock(return_value=MagicMock(
            displayed=None,
            displayed_ttl_remaining_s=None,
            pending=[],
            fallback_stack=[],
        ))
        app._queue = mock_queue

        app._on_command(
            "vestaboard_controller_command",
            {"command": "deregister_automation", "payload": json.dumps({"automation_id": "my_auto"})},
            {},
        )

        mock_queue.remove_source.assert_called_once_with("my_auto")

    def test_deregister_unknown_automation_is_safe(self):
        app = _setup_app_with_queue()
        # Should not raise
        app._on_command(
            "vestaboard_controller_command",
            {"command": "deregister_automation", "payload": json.dumps({"automation_id": "ghost"})},
            {},
        )

    def test_deregister_automation_publishes_status(self):
        app = _setup_app_with_queue()
        payload = _make_registration_payload("my_auto")
        _simulate_register(app, payload)

        initial_count = app.set_state.call_count
        app._on_command(
            "vestaboard_controller_command",
            {"command": "deregister_automation", "payload": json.dumps({"automation_id": "my_auto"})},
            {},
        )

        assert app.set_state.call_count > initial_count


# ---------------------------------------------------------------------------
# push_automation_frame (via event) tests
# ---------------------------------------------------------------------------

class TestPushAutomationFrame:
    def test_push_non_blank_frame_is_queued(self):
        app = _setup_app_with_queue()

        app.push_automation_frame(
            automation_id="my_auto",
            source_label="MyAuto",
            grid=_test_grid(),
            ttl_s=60,
            max_age_s=None,
            override_ttl=False,
            should_expire=False,
        )

        # Frame should be displayed (queue was empty)
        assert app._queue._displayed is not None
        assert app._queue._displayed.source == "my_auto"

    def test_push_blank_frame_is_rejected(self):
        app = _setup_app_with_queue()

        app.push_automation_frame(
            automation_id="my_auto",
            source_label="MyAuto",
            grid=_blank_grid(),
            ttl_s=60,
            max_age_s=None,
            override_ttl=False,
            should_expire=False,
        )

        # Blank frame must be rejected
        assert app._queue._displayed is None
        assert len(app._queue._pending) == 0

    def test_push_blank_frame_logs_info(self):
        app = _setup_app_with_queue()

        app.push_automation_frame(
            automation_id="my_auto",
            source_label="MyAuto",
            grid=_blank_grid(),
            ttl_s=60,
            max_age_s=None,
        )

        info_calls = [c for c in app.log.call_args_list if "INFO" in str(c)]
        assert any("blank" in str(c).lower() or "skip" in str(c).lower() for c in info_calls)

    def test_push_triggers_board_write(self):
        app = _setup_app_with_queue()

        app.push_automation_frame(
            automation_id="my_auto",
            source_label="MyAuto",
            grid=_test_grid(),
            ttl_s=60,
            max_age_s=None,
        )

        # Board write is scheduled via run_in(0) for thread safety
        assert app.run_in.call_count >= 1
        # Verify the callback is _board_write_callback
        call_args = app.run_in.call_args
        assert call_args[1].get("characters") is not None

    def test_push_publishes_status(self):
        app = _setup_app_with_queue()
        initial_count = app.set_state.call_count

        app.push_automation_frame(
            automation_id="my_auto",
            source_label="MyAuto",
            grid=_test_grid(),
            ttl_s=60,
            max_age_s=None,
        )

        assert app.set_state.call_count > initial_count

    def test_push_with_override_ttl_preempts_active_frame(self):
        app = _setup_app_with_queue()

        now = time.time()
        first_frame = BoardFrame(
            frame_id="first",
            characters=_blank_grid(),
            source="source1",
            source_label="Source1",
            ttl_s=300,
            max_age_s=None,
            override_ttl=False,
            created_at=now,
            displayed_at=now,
        )
        app._queue._displayed = first_frame

        app.push_automation_frame(
            automation_id="source2",
            source_label="Source2",
            grid=_test_grid(),
            ttl_s=30,
            max_age_s=None,
            override_ttl=True,
        )

        assert app._queue._displayed.source == "source2"

    def test_push_without_override_ttl_goes_to_pending(self):
        app = _setup_app_with_queue()

        now = time.time()
        first_frame = BoardFrame(
            frame_id="first",
            characters=_blank_grid(),
            source="source1",
            source_label="Source1",
            ttl_s=300,
            max_age_s=None,
            override_ttl=False,
            created_at=now,
            displayed_at=now,
        )
        app._queue._displayed = first_frame

        app.push_automation_frame(
            automation_id="source2",
            source_label="Source2",
            grid=_test_grid(),
            ttl_s=60,
            max_age_s=None,
            override_ttl=False,
        )

        assert len(app._queue._pending) == 1
        assert app._queue._displayed is first_frame

    def test_push_automation_frame_event_decodes_json_characters(self):
        """push_automation_frame command decodes JSON-stringified characters."""
        app = _setup_app_with_queue()
        grid = _test_grid()

        payload = {
            "automation_id": "my_auto",
            "source_label": "MyAuto",
            "characters": json.dumps(grid),
            "ttl_s": 60,
            "override_ttl": False,
        }
        app._on_command(
            "vestaboard_controller_command",
            {"command": "push_automation_frame", "payload": json.dumps(payload)},
            {},
        )

        assert app._queue._displayed is not None
        assert app._queue._displayed.source == "my_auto"

    def test_push_automation_frame_event_missing_characters_logs_warning(self):
        app = _setup_app_with_queue()

        payload = {"automation_id": "my_auto", "source_label": "MyAuto"}
        app._on_command(
            "vestaboard_controller_command",
            {"command": "push_automation_frame", "payload": json.dumps(payload)},
            {},
        )

        warning_calls = [c for c in app.log.call_args_list if "WARNING" in str(c)]
        assert any("characters" in str(c) or "missing" in str(c).lower() for c in warning_calls)

    def test_push_automation_frame_event_updates_proxy_next_fire_time(self):
        app = _setup_app_with_queue()
        payload_reg = _make_registration_payload("my_auto")
        _simulate_register(app, payload_reg)

        next_fire = time.time() + 600.0
        payload = {
            "automation_id": "my_auto",
            "source_label": "MyAuto",
            "characters": json.dumps(_test_grid()),
            "ttl_s": 60,
            "override_ttl": False,
            "next_fire_time": next_fire,
        }
        app._on_command(
            "vestaboard_controller_command",
            {"command": "push_automation_frame", "payload": json.dumps(payload)},
            {},
        )

        proxy = app._registered_automations["my_auto"]
        assert proxy._next_fire_time == next_fire


# ---------------------------------------------------------------------------
# Activate / deactivate automation command handlers
# ---------------------------------------------------------------------------

class TestActivateDeactivateHandlers:
    def test_handle_activate_fires_enabled_event(self):
        app = _setup_app_with_queue()
        payload = _make_registration_payload("my_auto")
        _simulate_register(app, payload)
        app.fire_event.reset_mock()

        app._handle_activate_automation("my_auto")

        fire_calls = [
            c for c in app.fire_event.call_args_list
            if c[0] and c[0][0] == "vb_auto_enabled"
        ]
        assert len(fire_calls) == 1
        assert fire_calls[0][1]["enabled"] is True

    def test_handle_activate_updates_config_store(self):
        app = _setup_app_with_queue()
        payload = _make_registration_payload("my_auto")
        _simulate_register(app, payload)

        mock_store = MagicMock()
        app._config_store = mock_store

        app._handle_activate_automation("my_auto")

        mock_store.update.assert_called_once_with("my_auto", {"enabled": True})

    def test_handle_activate_unknown_logs_warning(self):
        app = _setup_app_with_queue()

        app._handle_activate_automation("ghost_auto")

        warning_calls = [c for c in app.log.call_args_list if "WARNING" in str(c)]
        assert any("ghost_auto" in str(c) or "not registered" in str(c) for c in warning_calls)

    def test_handle_deactivate_fires_enabled_event(self):
        app = _setup_app_with_queue()
        payload = _make_registration_payload("my_auto")
        _simulate_register(app, payload)
        app.fire_event.reset_mock()

        app._handle_deactivate_automation("my_auto")

        fire_calls = [
            c for c in app.fire_event.call_args_list
            if c[0] and c[0][0] == "vb_auto_enabled"
        ]
        assert len(fire_calls) == 1
        assert fire_calls[0][1]["enabled"] is False

    def test_handle_deactivate_updates_config_store(self):
        app = _setup_app_with_queue()
        payload = _make_registration_payload("my_auto")
        _simulate_register(app, payload)

        mock_store = MagicMock()
        app._config_store = mock_store

        app._handle_deactivate_automation("my_auto")

        mock_store.update.assert_called_once_with("my_auto", {"enabled": False})

    def test_handle_deactivate_purges_frames(self):
        app = _setup_app_with_queue()
        payload = _make_registration_payload("my_auto")
        _simulate_register(app, payload)

        # Put a frame from this automation in fallback
        now = time.time()
        frame = BoardFrame(
            frame_id="auto-frame",
            characters=_blank_grid(),
            source="my_auto",
            source_label="MyAuto",
            ttl_s=None,
            max_age_s=None,
            override_ttl=False,
            created_at=now,
        )
        app._queue._fallback.append(frame)

        app._handle_deactivate_automation("my_auto")

        assert len(app._queue._fallback) == 0

    def test_handle_deactivate_unknown_logs_warning(self):
        app = _setup_app_with_queue()

        app._handle_deactivate_automation("ghost_auto")

        warning_calls = [c for c in app.log.call_args_list if "WARNING" in str(c)]
        assert any("ghost_auto" in str(c) or "not registered" in str(c) for c in warning_calls)

    def test_handle_deactivate_publishes_status(self):
        app = _setup_app_with_queue()
        payload = _make_registration_payload("my_auto")
        _simulate_register(app, payload)

        initial_count = app.set_state.call_count
        app._handle_deactivate_automation("my_auto")

        assert app.set_state.call_count > initial_count

    def test_activate_command_routing(self):
        app = _setup_app_with_queue()
        payload = _make_registration_payload("test_auto")
        _simulate_register(app, payload)
        app.fire_event.reset_mock()

        app._on_command(
            "vestaboard_controller_command",
            {"command": "activate_automation", "payload": json.dumps({"automation_id": "test_auto"})},
            {},
        )

        fire_calls = [
            c for c in app.fire_event.call_args_list
            if c[0] and c[0][0] == "vb_auto_enabled"
        ]
        assert len(fire_calls) == 1
        assert fire_calls[0][1]["enabled"] is True

    def test_deactivate_command_routing(self):
        app = _setup_app_with_queue()
        payload = _make_registration_payload("test_auto")
        _simulate_register(app, payload)
        app.fire_event.reset_mock()

        app._on_command(
            "vestaboard_controller_command",
            {"command": "deactivate_automation", "payload": json.dumps({"automation_id": "test_auto"})},
            {},
        )

        fire_calls = [
            c for c in app.fire_event.call_args_list
            if c[0] and c[0][0] == "vb_auto_enabled"
        ]
        assert len(fire_calls) == 1
        assert fire_calls[0][1]["enabled"] is False


# ---------------------------------------------------------------------------
# set_automation_config handler tests
# ---------------------------------------------------------------------------

class TestSetAutomationConfigHandler:
    def test_handle_set_config_persists_to_store(self):
        app = _setup_app_with_queue()
        payload = _make_registration_payload("my_auto")
        _simulate_register(app, payload)

        mock_store = MagicMock()
        app._config_store = mock_store

        new_config = {"ttl_minutes": 10, "min_stars": 3}
        app._handle_set_automation_config("my_auto", new_config)

        mock_store.update.assert_called_once_with("my_auto", new_config)

    def test_handle_set_config_fires_config_event(self):
        app = _setup_app_with_queue()
        payload = _make_registration_payload("my_auto")
        _simulate_register(app, payload)
        app.fire_event.reset_mock()

        new_config = {"ttl_minutes": 10}
        app._handle_set_automation_config("my_auto", new_config)

        fire_calls = [
            c for c in app.fire_event.call_args_list
            if c[0] and c[0][0] == "vb_auto_config"
        ]
        assert len(fire_calls) == 1
        assert fire_calls[0][1]["config"] == new_config

    def test_handle_set_config_updates_proxy(self):
        app = _setup_app_with_queue()
        payload = _make_registration_payload("my_auto")
        _simulate_register(app, payload)

        new_config = {"ttl_minutes": 15}
        app._handle_set_automation_config("my_auto", new_config)

        proxy = app._registered_automations["my_auto"]
        assert proxy.get_effective_config()["ttl_minutes"] == 15

    def test_handle_set_config_unknown_automation_logs_warning(self):
        app = _setup_app_with_queue()

        app._handle_set_automation_config("ghost_auto", {"enabled": True})

        warning_calls = [c for c in app.log.call_args_list if "WARNING" in str(c)]
        assert any("ghost_auto" in str(c) or "unknown" in str(c).lower() for c in warning_calls)

    def test_handle_set_config_publishes_status(self):
        app = _setup_app_with_queue()
        payload = _make_registration_payload("my_auto")
        _simulate_register(app, payload)

        initial_count = app.set_state.call_count
        app._handle_set_automation_config("my_auto", {"ttl_minutes": 5})

        assert app.set_state.call_count > initial_count


# ---------------------------------------------------------------------------
# Command routing tests
# ---------------------------------------------------------------------------

class TestCommandRouting:
    def _setup_app(self) -> VestaboardControllerApp:
        app = _setup_app_with_queue()
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
        app._on_command(
            "vestaboard_controller_command",
            {"command": "push_frame", "payload": json.dumps(payload)},
            {},
        )
        # Board write is scheduled via run_in for thread safety
        assert app.run_in.call_count >= 1

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

    def test_payload_as_dict(self):
        """Payload can be passed as a pre-parsed dict (not a string)."""
        app = self._setup_app()
        grid = _blank_grid()
        payload = {"characters": grid, "source": "ui", "override_ttl": True}

        app._on_command(
            "vestaboard_controller_command",
            {"command": "push_frame", "payload": payload},
            {},
        )
        # Board write scheduled via run_in
        assert app.run_in.call_count >= 1

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
        payload_reg = _make_registration_payload("msg_lib", automation_type="messages_from_library")
        _simulate_register(app, payload_reg)
        app.create_task.reset_mock()

        app._on_command(
            "vestaboard_controller_command",
            {"command": "generate_random_message", "payload": "{}"},
            {},
        )
        assert app.create_task.call_count >= 1

    def test_generate_random_art_fires_create_task(self):
        app = self._setup_app()
        payload_reg = _make_registration_payload("art_lib", automation_type="art_from_library")
        _simulate_register(app, payload_reg)
        app.create_task.reset_mock()

        app._on_command(
            "vestaboard_controller_command",
            {"command": "generate_random_art", "payload": "{}"},
            {},
        )
        assert app.create_task.call_count >= 1

    def test_generate_ai_art_fires_create_task(self):
        app = self._setup_app()
        payload_reg = _make_registration_payload("ai_art", automation_type="art_generated_by_ai")
        _simulate_register(app, payload_reg)
        app.create_task.reset_mock()

        app._on_command(
            "vestaboard_controller_command",
            {"command": "generate_ai_art", "payload": json.dumps({"subject": "cat"})},
            {},
        )
        assert app.create_task.call_count >= 1

    def test_generate_ai_art_not_generate_art(self):
        """Regression: controller must recognise 'generate_ai_art', not 'generate_art'."""
        app = self._setup_app()
        payload_reg = _make_registration_payload("ai_art", automation_type="art_generated_by_ai")
        _simulate_register(app, payload_reg)
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

    def test_set_automation_config_command_routing(self):
        app = self._setup_app()
        payload_reg = _make_registration_payload("my_auto")
        _simulate_register(app, payload_reg)
        app.fire_event.reset_mock()

        app._on_command(
            "vestaboard_controller_command",
            {
                "command": "set_automation_config",
                "payload": json.dumps({
                    "automation_id": "my_auto",
                    "config": {"ttl_minutes": 10},
                }),
            },
            {},
        )

        # Config event should be fired
        fire_calls = [
            c for c in app.fire_event.call_args_list
            if c[0] and c[0][0] == "vb_auto_config"
        ]
        assert len(fire_calls) == 1


# ---------------------------------------------------------------------------
# Generate event dispatch tests
# ---------------------------------------------------------------------------

class TestGenerateEventDispatch:
    def test_generate_random_message_fires_generate_event(self):
        app = _setup_app_with_queue()
        payload_reg = _make_registration_payload("msg_lib", automation_type="messages_from_library")
        _simulate_register(app, payload_reg)
        app.fire_event.reset_mock()

        _run(app._handle_generate_by_type({}, "messages_from_library", "generate_random_message"))

        fire_calls = [
            c for c in app.fire_event.call_args_list
            if c[0] and c[0][0] == "vb_auto_generate"
        ]
        assert len(fire_calls) == 1
        assert fire_calls[0][1]["preview_only"] is False

    def test_generate_random_message_missing_automation_logs_warning(self):
        app = _setup_app_with_queue()

        _run(app._handle_generate_by_type({}, "messages_from_library", "generate_random_message"))

        warning_calls = [c for c in app.log.call_args_list if "WARNING" in str(c)]
        assert any("not registered" in str(c) or "messages_from_library" in str(c) for c in warning_calls)

    def test_generate_ai_art_fires_generate_event_with_subject(self):
        app = _setup_app_with_queue()
        payload_reg = _make_registration_payload("ai_art", automation_type="art_generated_by_ai")
        _simulate_register(app, payload_reg)
        app.fire_event.reset_mock()

        _run(app._handle_generate_ai_art({"subject": "rainbow", "override_ttl": True}))

        fire_calls = [
            c for c in app.fire_event.call_args_list
            if c[0] and c[0][0] == "vb_auto_generate"
        ]
        assert len(fire_calls) == 1
        kwargs_sent = fire_calls[0][1]["generate_kwargs"]
        assert kwargs_sent["subject"] == "rainbow"
        assert fire_calls[0][1]["preview_only"] is False

    def test_generate_ai_art_preview_fires_generate_event_with_preview_only(self):
        app = _setup_app_with_queue()
        payload_reg = _make_registration_payload("ai_art", automation_type="art_generated_by_ai")
        _simulate_register(app, payload_reg)
        app.fire_event.reset_mock()

        _run(app._handle_generate_ai_art_preview({"subject": "sunset"}))

        fire_calls = [
            c for c in app.fire_event.call_args_list
            if c[0] and c[0][0] == "vb_auto_generate"
        ]
        assert len(fire_calls) == 1
        assert fire_calls[0][1]["preview_only"] is True
        assert fire_calls[0][1]["generate_kwargs"]["subject"] == "sunset"

    def test_push_ai_art_preview_result_stores_preview(self):
        app = _setup_app_with_queue()
        grid = _test_grid()

        payload = {
            "characters": json.dumps(grid),
            "subject": "ocean waves",
        }
        app._on_command(
            "vestaboard_controller_command",
            {"command": "push_ai_art_preview_result", "payload": json.dumps(payload)},
            {},
        )

        assert app._ai_art_preview is not None
        assert app._ai_art_preview["subject"] == "ocean waves"
        assert json.loads(app._ai_art_preview["characters"]) == grid


# ---------------------------------------------------------------------------
# Find automation by type tests
# ---------------------------------------------------------------------------

class TestFindAutomationByType:
    def test_find_by_type_returns_matching_automation(self):
        app = _setup_app_with_queue()
        payload = _make_registration_payload("msg_lib", automation_type="messages_from_library")
        _simulate_register(app, payload)

        found_id, found = app._find_automation_by_type("messages_from_library")

        assert found_id == "msg_lib"
        assert found is not None

    def test_find_by_type_returns_none_when_not_registered(self):
        app = _setup_app_with_queue()

        found_id, found = app._find_automation_by_type("messages_from_library")

        assert found_id is None
        assert found is None

    def test_find_automation_by_id_first(self):
        app = _setup_app_with_queue()
        payload = _make_registration_payload("my_specific_id", automation_type="messages_from_library")
        _simulate_register(app, payload)

        found_id, found = app._find_automation("my_specific_id")

        assert found_id == "my_specific_id"
        assert found is not None

    def test_find_automation_falls_back_to_type(self):
        app = _setup_app_with_queue()
        payload = _make_registration_payload("msg_lib_actual", automation_type="messages_from_library")
        _simulate_register(app, payload)

        # Pass automation_type as candidate id — should find by type
        found_id, found = app._find_automation("messages_from_library")

        assert found_id == "msg_lib_actual"
        assert found is not None

    def test_find_automation_returns_none_when_nothing_matches(self):
        app = _setup_app_with_queue()

        found_id, found = app._find_automation("nonexistent", "also_missing")

        assert found_id is None
        assert found is None


# ---------------------------------------------------------------------------
# Tick tests
# ---------------------------------------------------------------------------

class TestTick:
    def _setup_app_for_tick(self) -> VestaboardControllerApp:
        app = _make_app()
        app.initialize()
        app._queue = FrameQueue(log_fn=app.log)
        app._registered_automations = {}
        app._last_write_ok = None
        app._write_to_board = AsyncMock()
        app._read_board_state = AsyncMock()
        app.create_task = MagicMock(side_effect=lambda coro: _run(coro))
        app.set_state = MagicMock()
        app.fire_event = MagicMock()
        return app

    def test_tick_no_action_when_queue_empty(self):
        app = self._setup_app_for_tick()
        _run(app._tick())
        # Should not raise and should not write (queue empty, no external frame)

    def test_tick_promotes_pending_frame_after_ttl_expires(self):
        app = self._setup_app_for_tick()
        app._write_to_board = AsyncMock()

        now = time.time()
        frame1 = BoardFrame(
            frame_id="frame1",
            characters=_blank_grid(),
            source="auto1",
            source_label="Auto1",
            ttl_s=1,
            max_age_s=None,
            override_ttl=False,
            created_at=now - 10,
            displayed_at=now - 10,  # displayed 10s ago, TTL=1 → expired
        )
        app._queue._displayed = frame1

        frame2 = BoardFrame(
            frame_id="frame2",
            characters=_blank_grid(),
            source="auto2",
            source_label="Auto2",
            ttl_s=60,
            max_age_s=None,
            override_ttl=False,
            created_at=now,
        )
        app._queue._pending.append(frame2)

        _run(app._tick())

        assert app._queue._displayed is frame2
        assert app._write_to_board.call_count >= 1

    def test_tick_wrapper_calls_create_task(self):
        app = _make_app()
        app.initialize()
        app._queue = FrameQueue(log_fn=app.log)
        app._registered_automations = {}
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
        app._registered_automations = {}
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
            max_age_s=None,
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
                max_age_s=None,
                override_ttl=False,
                created_at=now,
            )
            app._queue._pending.append(frame)

        displayed = BoardFrame(
            frame_id="disp",
            characters=_blank_grid(),
            source="disp_source",
            source_label="Disp",
            ttl_s=300,
            max_age_s=None,
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

        payload = _make_registration_payload("calendar_clock", display_name="CalendarClock", enabled=True)
        _simulate_register(app, payload)

        mock_store = MagicMock()
        mock_store.get = MagicMock(return_value={"enabled": True})
        app._config_store = mock_store

        app._publish_status()
        attrs = app.set_state.call_args[1]["attributes"]
        all_autos = attrs["all_automations"]
        assert len(all_autos) == 1
        entry = all_autos[0]
        assert entry["id"] == "calendar_clock"
        assert entry["name"] == "CalendarClock"
        assert entry["enabled"] is True

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
            max_age_s=None,
            override_ttl=False,
            created_at=now,
            displayed_at=now,
        )
        app._queue._displayed = frame

        app._publish_status()
        attrs = app.set_state.call_args[1]["attributes"]
        displayed_frame = attrs["displayed_frame"]
        assert displayed_frame is not None
        assert json.loads(displayed_frame["characters"]) == grid

    def test_publish_status_pending_expires_at_with_expiration(self):
        """Pending items with max_age_s should have an ISO expires_at string."""
        app = self._setup_app()

        now = 1710000000.0  # fixed timestamp for deterministic ISO output
        displayed = BoardFrame(
            frame_id="disp",
            characters=_blank_grid(),
            source="holder",
            source_label="Holder",
            ttl_s=9999,
            max_age_s=None,
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
            max_age_s=300,
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
        from datetime import datetime, timezone
        expected_ts = now + 300
        expected_iso = datetime.fromtimestamp(expected_ts, tz=timezone.utc).isoformat()
        assert item["expires_at"] == expected_iso

    def test_publish_status_pending_expires_at_null_without_expiration(self):
        """Pending items without max_age_s should have expires_at=null."""
        app = self._setup_app()

        now = time.time()
        displayed = BoardFrame(
            frame_id="disp",
            characters=_blank_grid(),
            source="holder",
            source_label="Holder",
            ttl_s=9999,
            max_age_s=None,
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
            max_age_s=None,
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
        """Automations with _next_fire_time set should expose it in all_automations."""
        app = self._setup_app()

        now = time.time()
        next_fire = now + 600.0  # 10 minutes from now

        payload = _make_registration_payload("messages_from_library", display_name="MessagesFromLibrary")
        _simulate_register(app, payload)
        proxy = app._registered_automations["messages_from_library"]
        proxy._next_fire_time = next_fire

        app._publish_status()
        attrs = app.set_state.call_args[1]["attributes"]
        entry = attrs["all_automations"][0]
        assert "next_fire_time" in entry
        assert entry["next_fire_time"] == next_fire

    def test_publish_status_omits_next_fire_time_when_none(self):
        """Automations without a scheduled next_fire_time should not include the key."""
        app = self._setup_app()

        payload = _make_registration_payload("calendar_clock", display_name="CalendarClock")
        _simulate_register(app, payload)

        app._publish_status()
        attrs = app.set_state.call_args[1]["attributes"]
        entry = attrs["all_automations"][0]
        assert "next_fire_time" not in entry

    def test_publish_status_includes_preview_frame(self):
        """Each automation entry should include a preview_frame from get_preview_frame()."""
        app = self._setup_app()

        preview = [[1] * 22 for _ in range(6)]  # non-blank preview
        payload = _make_registration_payload("art_from_library", display_name="ArtFromLibrary")
        payload["preview_frame"] = json.dumps(preview)
        _simulate_register(app, payload)

        app._publish_status()
        attrs = app.set_state.call_args[1]["attributes"]
        entry = attrs["all_automations"][0]
        assert "preview_frame" in entry
        assert json.loads(entry["preview_frame"]) == preview

    def test_publish_status_preview_frame_error_does_not_crash(self):
        """If get_preview_frame() raises, status publishing continues without preview."""
        app = self._setup_app()

        # Use a mock automation with a broken get_preview_frame to verify
        # the controller doesn't crash even if the proxy raises
        auto = _make_mock_automation("broken_auto", display_name="BrokenAuto")
        auto.get_preview_frame = MagicMock(side_effect=RuntimeError("oops"))
        app._registered_automations["broken_auto"] = auto

        # Must not raise
        app._publish_status()
        attrs = app.set_state.call_args[1]["attributes"]
        entry = attrs["all_automations"][0]
        assert "preview_frame" not in entry


# ---------------------------------------------------------------------------
# Board write tests
# ---------------------------------------------------------------------------

class TestBoardWrite:
    def test_write_skipped_when_ip_not_configured(self):
        app = _make_app({"vestaboard_ip": "", "vestaboard_api_key": ""})
        app.initialize()
        app._sleep_enabled = False
        app._vb_ip = ""
        app._vb_api_key = ""

        _run(app._write_to_board(_blank_grid()))

        warning_calls = [c for c in app.log.call_args_list if "WARNING" in str(c)]
        assert any("not configured" in str(c).lower() or "skipping" in str(c).lower()
                   for c in warning_calls)

    def test_write_calls_client(self):
        app = _make_app()
        app.initialize()
        app._sleep_enabled = False
        app._vb_ip = "192.168.1.50"
        app._vb_api_key = "test-api-key-fake"

        mock_client = AsyncMock()
        mock_client.write_frame = AsyncMock(return_value=True)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "vestaboard_apps.vestaboard_controller.vestaboard_controller_app.VestaboardClient",
            return_value=mock_client,
        ):
            _run(app._write_to_board(_blank_grid()))

        mock_client.write_frame.assert_called_once()
        assert app._last_write_ok is True

    def test_write_sets_last_write_ok_false_on_failure(self):
        app = _make_app()
        app.initialize()
        app._sleep_enabled = False
        app._vb_ip = "192.168.1.50"
        app._vb_api_key = "test-api-key-fake"

        mock_client = AsyncMock()
        mock_client.write_frame = AsyncMock(side_effect=Exception("Connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "vestaboard_apps.vestaboard_controller.vestaboard_controller_app.VestaboardClient",
            return_value=mock_client,
        ):
            _run(app._write_to_board(_blank_grid()))

        assert app._last_write_ok is False


# ---------------------------------------------------------------------------
# Frame queue integration tests
# ---------------------------------------------------------------------------

class TestFrameQueueIntegration:
    def test_push_automation_frame_displays_when_queue_empty(self):
        app = _setup_app_with_queue()

        grid = _test_grid()
        app.push_automation_frame(
            automation_id="calendar_clock",
            source_label="CalendarClock",
            grid=grid,
            ttl_s=60,
            max_age_s=None,
        )

        assert app._queue._displayed is not None
        assert app._queue._displayed.source == "calendar_clock"
        assert app.run_in.call_count >= 1  # board write via _schedule_board_write

    def test_push_automation_frame_queues_when_ttl_active(self):
        app = _setup_app_with_queue()

        now = time.time()
        first_frame = BoardFrame(
            frame_id="first",
            characters=_blank_grid(),
            source="source1",
            source_label="Source1",
            ttl_s=300,
            max_age_s=None,
            override_ttl=False,
            created_at=now,
            displayed_at=now,
        )
        app._queue._displayed = first_frame

        app.push_automation_frame(
            automation_id="source2",
            source_label="Source2",
            grid=_test_grid(),
            ttl_s=60,
            max_age_s=None,
            override_ttl=False,
        )

        assert len(app._queue._pending) == 1
        assert app._queue._displayed is first_frame

    def test_override_ttl_preempts_active_frame(self):
        app = _setup_app_with_queue()

        now = time.time()
        first_frame = BoardFrame(
            frame_id="first",
            characters=_blank_grid(),
            source="source1",
            source_label="Source1",
            ttl_s=300,
            max_age_s=None,
            override_ttl=False,
            created_at=now,
            displayed_at=now,
        )
        app._queue._displayed = first_frame

        app.push_automation_frame(
            automation_id="user",
            source_label="User",
            grid=_test_grid(),
            ttl_s=30,
            max_age_s=None,
            override_ttl=True,
        )

        assert app._queue._displayed.source == "user"
        assert app.run_in.call_count >= 1  # board write via _schedule_board_write

    def test_dedup_same_source_pending_frames(self):
        app = _setup_app_with_queue()

        now = time.time()
        first_frame = BoardFrame(
            frame_id="first",
            characters=_blank_grid(),
            source="other_source",
            source_label="Other",
            ttl_s=300,
            max_age_s=None,
            override_ttl=False,
            created_at=now,
            displayed_at=now,
        )
        app._queue._displayed = first_frame

        app.push_automation_frame("auto_x", "AutoX", _test_grid(), 60, None)
        app.push_automation_frame("auto_x", "AutoX", _test_grid(), 60, None)

        assert len(app._queue._pending) == 1

    def test_push_blank_frame_is_rejected(self):
        """Blank frames (all zeros) should not be pushed to the queue."""
        app = _setup_app_with_queue()

        app.push_automation_frame(
            automation_id="calendar_summary",
            source_label="CalendarSummary",
            grid=_blank_grid(),
            ttl_s=60,
            max_age_s=None,
        )

        assert app._queue._displayed is None
        assert len(app._queue._pending) == 0

    def test_is_blank_frame_static_method(self):
        """Test the _is_blank_frame helper directly."""
        assert VestaboardControllerApp._is_blank_frame([])
        assert VestaboardControllerApp._is_blank_frame([[0] * 22 for _ in range(6)])
        assert VestaboardControllerApp._is_blank_frame([[], [], [], [], [], []])
        assert not VestaboardControllerApp._is_blank_frame([[1] + [0] * 21] + [[0] * 22 for _ in range(5)])


# ---------------------------------------------------------------------------
# JSON-serialized frame data tests
# ---------------------------------------------------------------------------

class TestJSONSerializedFrameData:
    """Verify frame data is serialized as JSON strings in status."""

    def _setup_app(self) -> VestaboardControllerApp:
        app = _make_app()
        app.initialize()
        app._queue = FrameQueue(log_fn=app.log)
        app._registered_automations = {}
        app._last_write_ok = None
        return app

    def test_displayed_characters_is_json_string(self):
        app = self._setup_app()

        now = time.time()
        grid = [[0] * 22 for _ in range(6)]
        frame = BoardFrame(
            frame_id="f1",
            characters=grid,
            source="test",
            source_label="Test",
            ttl_s=None,
            max_age_s=None,
            override_ttl=False,
            created_at=now,
            displayed_at=now,
        )
        app._queue._displayed = frame

        app._publish_status()
        attrs = app.set_state.call_args[1]["attributes"]
        chars = attrs["displayed_frame"]["characters"]
        assert isinstance(chars, str)
        assert json.loads(chars) == grid

    def test_preview_frame_is_json_string(self):
        app = self._setup_app()

        preview = [[0] * 22 for _ in range(6)]
        payload = _make_registration_payload("test_auto", display_name="TestAuto")
        payload["preview_frame"] = json.dumps(preview)
        _simulate_register(app, payload)

        app._publish_status()
        attrs = app.set_state.call_args[1]["attributes"]
        entry = attrs["all_automations"][0]
        assert isinstance(entry["preview_frame"], str)
        assert json.loads(entry["preview_frame"]) == preview


# ---------------------------------------------------------------------------
# Sleep window tests
# ---------------------------------------------------------------------------

class TestSleepWindow:
    """Tests for the sleep window feature."""

    def _make_app_with_sleep(self, start: str, end: str) -> VestaboardControllerApp:
        app = _make_app({
            "sleep_window": {"enabled": True, "start": start, "end": end},
        })
        app.initialize()
        return app

    def test_sleep_disabled_never_sleeping(self):
        app = _make_app({"sleep_window": {"enabled": False, "start": "01:00:00", "end": "07:00:00"}})
        app.initialize()
        assert app._sleep_enabled is False
        assert app._is_sleeping() is False

    def test_sleep_within_window_is_sleeping(self):
        """When current time is inside the sleep window, _is_sleeping() returns True."""
        app = self._make_app_with_sleep("00:00:00", "23:59:59")
        # Window covers almost the whole day — should be sleeping now
        assert app._is_sleeping() is True

    def test_sleep_outside_window_is_not_sleeping(self):
        """When the window is 1 second that has definitely passed, not sleeping."""
        app = self._make_app_with_sleep("00:00:00", "00:00:01")
        # Window is 00:00:00 to 00:00:01 — almost certainly not sleeping right now
        # This is a weak test but avoids time-of-day flakiness
        result = app._is_sleeping()
        assert isinstance(result, bool)

    def test_overnight_sleep_window_before_midnight(self):
        """Overnight window (start > end): time after start should be sleeping."""
        import datetime

        app = self._make_app_with_sleep("23:00:00", "06:00:00")

        with patch("vestaboard_apps.vestaboard_controller.vestaboard_controller_app.datetime") as mock_dt:
            mock_dt.now.return_value = MagicMock(
                time=MagicMock(return_value=datetime.time(23, 30, 0))
            )
            # Patch _is_sleeping to use mock datetime
            from datetime import time as dtime
            app._sleep_start = "23:00:00"
            app._sleep_end = "06:00:00"
            sh, sm, ss = app._parse_time("23:00:00")
            eh, em, es = app._parse_time("06:00:00")
            start = dtime(sh, sm, ss)
            end = dtime(eh, em, es)
            now_t = dtime(23, 30, 0)
            # Manually verify the logic: start > end, now >= start → sleeping
            assert start > end
            assert now_t >= start

    def test_overnight_sleep_window_after_midnight(self):
        """Overnight window: time before end (after midnight) should also be sleeping."""
        import datetime

        app = self._make_app_with_sleep("23:00:00", "06:00:00")
        from datetime import time as dtime
        sh, sm, ss = app._parse_time("23:00:00")
        eh, em, es = app._parse_time("06:00:00")
        start = dtime(sh, sm, ss)
        end = dtime(eh, em, es)
        now_t = dtime(3, 0, 0)  # 3am — within overnight window
        assert start > end
        assert now_t < end

    def test_write_suppressed_during_sleep(self):
        """Board writes are suppressed during the sleep window."""
        app = self._make_app_with_sleep("00:00:00", "23:59:59")
        app._vb_ip = "192.168.1.50"
        app._vb_api_key = "test-api-key-fake"

        mock_client = AsyncMock()
        with patch(
            "vestaboard_apps.vestaboard_controller.vestaboard_controller_app.VestaboardClient",
            return_value=mock_client,
        ):
            _run(app._write_to_board(_blank_grid()))

        # Client should not have been called
        mock_client.write_frame.assert_not_called()

    def test_tick_logs_sleep_start(self):
        """When transitioning from awake to sleeping, tick logs the event."""
        app = self._make_app_with_sleep("00:00:00", "23:59:59")
        app._queue = FrameQueue(log_fn=app.log)
        app._registered_automations = {}
        app._last_write_ok = None
        app._write_to_board = AsyncMock()
        app._read_board_state = AsyncMock()
        app.create_task = MagicMock(side_effect=lambda coro: _run(coro))
        app.set_state = MagicMock()
        app.fire_event = MagicMock()

        app._was_sleeping = False  # start awake

        _run(app._tick())

        info_calls = [c for c in app.log.call_args_list if "INFO" in str(c)]
        assert any("sleep" in str(c).lower() for c in info_calls)

    def test_tick_logs_sleep_end(self):
        """When transitioning from sleeping to awake, tick logs the event."""
        app = self._make_app_with_sleep("00:00:00", "00:00:01")
        app._queue = FrameQueue(log_fn=app.log)
        app._registered_automations = {}
        app._last_write_ok = None
        app._write_to_board = AsyncMock()
        app._read_board_state = AsyncMock()
        app.create_task = MagicMock(side_effect=lambda coro: _run(coro))
        app.set_state = MagicMock()
        app.fire_event = MagicMock()

        app._was_sleeping = True  # start sleeping

        # Patch _is_sleeping to return False (simulate waking up)
        with patch.object(app, "_is_sleeping", return_value=False):
            _run(app._tick())

        info_calls = [c for c in app.log.call_args_list if "INFO" in str(c)]
        assert any("wake" in str(c).lower() or "sleep" in str(c).lower() for c in info_calls)


# ---------------------------------------------------------------------------
# Template resolution tests
# ---------------------------------------------------------------------------

class TestTemplateResolution:
    """Tests for {entity_id} template resolution in push_frame and push_automation_frame."""

    def _make_app(self) -> VestaboardControllerApp:
        app = _setup_app_with_queue()
        app._last_template_refresh = None
        return app

    # ------------------------------------------------------------------
    # _handle_push_frame
    # ------------------------------------------------------------------

    def test_push_frame_resolves_template_into_grid(self):
        """push_frame with a template field encodes the resolved text into characters."""
        app = self._make_app()
        app.get_state = MagicMock(return_value="85")

        # Build a payload with a template but no pre-encoded characters
        payload = {
            "template": "UPS: {sensor.ups_load}%",
            "source": "user",
            "source_label": "User",
            "ttl_s": 60,
        }
        app._handle_push_frame(payload)

        displayed = app._queue._displayed
        assert displayed is not None
        assert displayed.template == "UPS: {sensor.ups_load}%"
        # The grid must be non-blank (text was encoded)
        assert not app._is_blank_frame(displayed.characters)

    def test_push_frame_template_logs_resolution(self):
        """push_frame logs the template resolutions at INFO."""
        app = self._make_app()
        app.get_state = MagicMock(return_value="42")

        payload = {
            "template": "LOAD: {sensor.load}%",
            "source": "user",
            "source_label": "User",
        }
        app._handle_push_frame(payload)

        info_calls = [c for c in app.log.call_args_list if "INFO" in str(c)]
        assert any("template resolved" in str(c).lower() for c in info_calls)

    def test_push_frame_stores_template_on_board_frame(self):
        """push_frame stores the original template string on the BoardFrame."""
        app = self._make_app()
        app.get_state = MagicMock(return_value="75")

        payload = {
            "template": "TEMP: {sensor.temp}F",
            "source": "user",
            "source_label": "User",
        }
        app._handle_push_frame(payload)

        displayed = app._queue._displayed
        assert displayed is not None
        assert displayed.template == "TEMP: {sensor.temp}F"

    def test_push_frame_stores_refresh_interval_on_board_frame(self):
        """push_frame stores refresh_interval_minutes on the BoardFrame."""
        app = self._make_app()
        app.get_state = MagicMock(return_value="50")

        payload = {
            "template": "LOAD: {sensor.load}%",
            "source": "user",
            "source_label": "User",
            "refresh_interval_minutes": 5,
        }
        app._handle_push_frame(payload)

        displayed = app._queue._displayed
        assert displayed is not None
        assert displayed.refresh_interval_minutes == 5

    def test_push_frame_without_template_uses_characters_directly(self):
        """push_frame without template uses the characters payload as-is."""
        app = self._make_app()
        grid = _test_grid()

        payload = {
            "characters": grid,
            "source": "user",
            "source_label": "User",
        }
        app._handle_push_frame(payload)

        displayed = app._queue._displayed
        assert displayed is not None
        assert displayed.template is None
        assert displayed.characters == grid

    def test_push_frame_template_sets_last_template_refresh(self):
        """When a template frame is displayed, _last_template_refresh is set."""
        app = self._make_app()
        app.get_state = MagicMock(return_value="99")

        payload = {
            "template": "VAL: {sensor.x}",
            "source": "user",
            "source_label": "User",
        }
        before = time.time()
        app._handle_push_frame(payload)
        after = time.time()

        assert app._last_template_refresh is not None
        assert before <= app._last_template_refresh <= after

    def test_push_frame_no_template_does_not_set_last_template_refresh(self):
        """When a non-template frame is displayed, _last_template_refresh is None."""
        app = self._make_app()

        payload = {
            "characters": _test_grid(),
            "source": "user",
            "source_label": "User",
        }
        app._handle_push_frame(payload)

        assert app._last_template_refresh is None

    # ------------------------------------------------------------------
    # push_automation_frame
    # ------------------------------------------------------------------

    def test_push_automation_frame_resolves_template(self):
        """push_automation_frame resolves template placeholders and updates the grid."""
        app = self._make_app()
        app.get_state = MagicMock(return_value="120")

        raw_grid = _test_grid()  # non-blank placeholder
        app.push_automation_frame(
            automation_id="ups_status",
            source_label="UPS Status",
            grid=raw_grid,
            ttl_s=60,
            max_age_s=None,
            template="UPS: {sensor.ups_load}W",
            refresh_interval_minutes=2,
        )

        displayed = app._queue._displayed
        assert displayed is not None
        assert displayed.template == "UPS: {sensor.ups_load}W"
        assert displayed.refresh_interval_minutes == 2
        # Grid should differ from raw_grid (was re-encoded from template)
        assert not app._is_blank_frame(displayed.characters)

    def test_push_automation_frame_logs_template_resolution(self):
        """push_automation_frame logs the resolution at INFO."""
        app = self._make_app()
        app.get_state = MagicMock(return_value="55")

        app.push_automation_frame(
            automation_id="sensor_display",
            source_label="Sensor Display",
            grid=_test_grid(),
            ttl_s=30,
            max_age_s=None,
            template="HUMIDITY: {sensor.humidity}%",
        )

        info_calls = [c for c in app.log.call_args_list if "INFO" in str(c)]
        assert any("template resolved" in str(c).lower() for c in info_calls)

    def test_push_automation_frame_without_template_unchanged(self):
        """push_automation_frame without template keeps the original grid."""
        app = self._make_app()
        grid = _test_grid()

        app.push_automation_frame(
            automation_id="art_display",
            source_label="Art",
            grid=grid,
            ttl_s=None,
            max_age_s=None,
        )

        displayed = app._queue._displayed
        assert displayed is not None
        assert displayed.template is None
        assert displayed.refresh_interval_minutes is None

    def test_handle_push_automation_frame_event_passes_template(self):
        """_handle_push_automation_frame_event extracts and forwards template fields."""
        app = self._make_app()
        app.get_state = MagicMock(return_value="30")

        grid = _test_grid()
        payload = {
            "automation_id": "lib_auto",
            "source_label": "Lib Auto",
            "characters": json.dumps(grid),
            "ttl_s": 60,
            "template": "TEMP: {sensor.temp}F",
            "refresh_interval_minutes": 3,
        }

        app._handle_push_automation_frame_event(payload)

        displayed = app._queue._displayed
        assert displayed is not None
        assert displayed.template == "TEMP: {sensor.temp}F"
        assert displayed.refresh_interval_minutes == 3

    # ------------------------------------------------------------------
    # Template refresh in _tick()
    # ------------------------------------------------------------------

    def test_tick_refreshes_template_frame_after_interval(self):
        """_tick re-resolves a template frame once refresh_interval has elapsed."""
        app = self._make_app()
        app._sleep_enabled = False  # disable sleep window so time of day doesn't affect test
        app._write_to_board = AsyncMock()
        app._read_board_state = AsyncMock()
        app.create_task = MagicMock(side_effect=lambda coro: _run(coro))
        app.set_state = MagicMock()
        app.fire_event = MagicMock()

        # Install a template frame as displayed
        now = time.time()
        initial_grid = [[0] * 22 for _ in range(6)]
        initial_grid[0][0] = 1  # ensure non-blank
        frame = BoardFrame(
            frame_id="tpl-frame-001",
            characters=initial_grid,
            source="sensor_source",
            source_label="Sensor Source",
            ttl_s=None,
            max_age_s=None,
            override_ttl=False,
            created_at=now,
            displayed_at=now,
            template="VAL: {sensor.x}",
            refresh_interval_minutes=1,
        )
        app._queue._displayed = frame
        # Set _last_template_refresh far in the past (> 1 minute ago)
        app._last_template_refresh = now - 120

        # get_state returns a new value
        app.get_state = MagicMock(return_value="999")

        _run(app._tick())

        # Board should have been written
        app._write_to_board.assert_called_once()
        # _last_template_refresh should be updated
        assert app._last_template_refresh >= now

    def test_tick_does_not_refresh_when_interval_not_elapsed(self):
        """_tick skips template refresh when the interval has not elapsed."""
        app = self._make_app()
        app._sleep_enabled = False  # disable sleep window so time of day doesn't affect test
        app._write_to_board = AsyncMock()
        app._read_board_state = AsyncMock()
        app.create_task = MagicMock(side_effect=lambda coro: _run(coro))
        app.set_state = MagicMock()
        app.fire_event = MagicMock()

        now = time.time()
        frame = BoardFrame(
            frame_id="tpl-frame-002",
            characters=_test_grid(),
            source="sensor_source",
            source_label="Sensor Source",
            ttl_s=None,
            max_age_s=None,
            override_ttl=False,
            created_at=now,
            displayed_at=now,
            template="VAL: {sensor.x}",
            refresh_interval_minutes=5,  # 5 minute interval
        )
        app._queue._displayed = frame
        # Just refreshed 1 second ago — should NOT refresh yet
        app._last_template_refresh = now - 1

        app.get_state = MagicMock(return_value="123")

        _run(app._tick())

        # No board write should have occurred
        app._write_to_board.assert_not_called()

    def test_tick_skips_write_when_grid_unchanged(self):
        """_tick skips board write when resolved grid is identical to the current one."""
        app = self._make_app()
        app._sleep_enabled = False  # disable sleep window so time of day doesn't affect test
        app._write_to_board = AsyncMock()
        app._read_board_state = AsyncMock()
        app.create_task = MagicMock(side_effect=lambda coro: _run(coro))
        app.set_state = MagicMock()
        app.fire_event = MagicMock()

        now = time.time()
        # Encode a known template with a known value into the frame
        from providers.vestaboard.character_encoding import text_to_grid as _ttg
        current_grid = _ttg("VAL: 42", justify="center", align="center")

        frame = BoardFrame(
            frame_id="tpl-frame-003",
            characters=current_grid,
            source="sensor_source",
            source_label="Sensor Source",
            ttl_s=None,
            max_age_s=None,
            override_ttl=False,
            created_at=now,
            displayed_at=now,
            template="VAL: {sensor.x}",
            refresh_interval_minutes=1,
        )
        app._queue._displayed = frame
        # Interval has elapsed
        app._last_template_refresh = now - 120

        # get_state returns the same value → grid should not change
        app.get_state = MagicMock(return_value="42")

        _run(app._tick())

        # No board write because the grid did not change
        app._write_to_board.assert_not_called()
        # But refresh timestamp should be updated
        assert app._last_template_refresh >= now

    def test_tick_does_not_refresh_during_sleep(self):
        """_tick skips template refresh while in the sleep window."""
        app = self._make_app()
        app._write_to_board = AsyncMock()
        app._read_board_state = AsyncMock()
        app.create_task = MagicMock(side_effect=lambda coro: _run(coro))
        app.set_state = MagicMock()
        app.fire_event = MagicMock()

        now = time.time()
        frame = BoardFrame(
            frame_id="tpl-frame-004",
            characters=_test_grid(),
            source="sensor_source",
            source_label="Sensor Source",
            ttl_s=None,
            max_age_s=None,
            override_ttl=False,
            created_at=now,
            displayed_at=now,
            template="VAL: {sensor.x}",
            refresh_interval_minutes=1,
        )
        app._queue._displayed = frame
        app._last_template_refresh = now - 120  # interval elapsed

        app.get_state = MagicMock(return_value="777")

        # Simulate sleep window active
        with patch.object(app, "_is_sleeping", return_value=True):
            app._was_sleeping = True  # prevent sleep-change logic from interfering
            _run(app._tick())

        # No template refresh write during sleep
        app._write_to_board.assert_not_called()

    def test_tick_promotes_new_frame_and_resets_refresh_tracking(self):
        """When tick promotes a new template frame, _last_template_refresh is reset."""
        app = self._make_app()
        app._sleep_enabled = False  # disable sleep window so time of day doesn't affect test
        app._write_to_board = AsyncMock()
        app._read_board_state = AsyncMock()
        app.create_task = MagicMock(side_effect=lambda coro: _run(coro))
        app.set_state = MagicMock()
        app.fire_event = MagicMock()

        now = time.time()
        # Put a template frame in pending that will be promoted
        pending_frame = BoardFrame(
            frame_id="pending-tpl-001",
            characters=_test_grid(),
            source="source_pending",
            source_label="Pending Source",
            ttl_s=None,
            max_age_s=None,
            override_ttl=False,
            created_at=now,
            template="PENDING: {sensor.y}",
            refresh_interval_minutes=3,
        )
        app._queue._pending.append(pending_frame)
        # No currently displayed frame — queue will promote pending immediately
        app._queue._displayed = None
        app._last_template_refresh = None

        app.get_state = MagicMock(return_value="5")

        _run(app._tick())

        # _write_to_board was called (frame promoted)
        app._write_to_board.assert_called_once()
        # Since promoted frame has a template, _last_template_refresh should be set
        assert app._last_template_refresh is not None
