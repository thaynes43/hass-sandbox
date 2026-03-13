"""Tests for VestaboardConfigurationApp — init, provisioning, commands, sensor."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

# ---------------------------------------------------------------------------
# Mock hassapi before importing the app module
# ---------------------------------------------------------------------------
mock_hass = MagicMock()
mock_hass.Hass = type("_MockHass", (), {"__init__": lambda self, *a, **kw: None})
sys.modules["hassapi"] = mock_hass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))

from vestaboard_configuration_app.vestaboard_configuration_app import (
    VestaboardConfigurationApp,
)
from vestaboard_configuration_app.frame_library import FrameLibrary, LibraryFrame


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(extra_args: dict | None = None, tmp_dir: str | None = None) -> VestaboardConfigurationApp:
    """Create a minimal VestaboardConfigurationApp with AppDaemon methods mocked."""
    if tmp_dir is None:
        tmp_dir = tempfile.mkdtemp(prefix="vbcfg_test_")

    ad = MagicMock()
    app = VestaboardConfigurationApp(ad, MagicMock())

    base_args: dict = {
        "ha_url": "http://ha:8123",
        "ha_token_env": "HA_TOKEN",
        "frame_library_path": os.path.join(tmp_dir, "frame-library.json"),
        "creators": ["Mom", "Tom", "Anonymous"],
    }
    if extra_args:
        base_args.update(extra_args)
    app.args = base_args

    app.get_state = MagicMock(return_value=None)
    app.set_state = MagicMock()
    app.call_service = MagicMock()
    app.listen_state = MagicMock()
    app.listen_event = MagicMock()
    app.run_in = MagicMock()
    app.cancel_timer = MagicMock()
    app.log = MagicMock()
    app.create_task = MagicMock()
    app.fire_event = MagicMock()
    app.name = "vestaboard_configuration_app"

    return app


def _run(coro):
    """Run a coroutine synchronously in the test event loop."""
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_mock_provisioner():
    mock_prov = MagicMock()
    mock_prov.ensure_script = AsyncMock(return_value=False)
    mock_prov.ensure_helper = AsyncMock(return_value=False)
    return mock_prov


def _make_dummy_frame() -> list[list[int]]:
    """Return a 6x22 blank grid for use as test frame data."""
    return [[0] * 22 for _ in range(6)]


# ---------------------------------------------------------------------------
# Initialization tests
# ---------------------------------------------------------------------------


class TestInitialization:
    def test_initialize_loads_library_and_seeds(self):
        """App should load the frame library and seed Hello World on first run."""
        app = _make_app()
        with patch("providers.ha_provisioner.HAProvisioner", return_value=_make_mock_provisioner()):
            app.initialize()

        # Library should have been created and seeded
        assert app._frame_library is not None
        frames = app._frame_library.list_frames()
        assert len(frames) == 1
        assert frames[0].name == "Hello World"
        assert frames[0].creator == "Tom"
        assert frames[0].rating == 3

    def test_initialize_does_not_re_seed_existing_library(self, tmp_path):
        """App must not add a duplicate Hello World if library already has frames."""
        lib_path = str(tmp_path / "frame-library.json")
        # Pre-populate the library
        lib = FrameLibrary(storage_path=lib_path)
        lib.load()
        lib.add_frame(
            LibraryFrame(
                frame_id=uuid.uuid4().hex,
                characters=_make_dummy_frame(),
                creator="Tom",
                created_at=time.time(),
                rating=2,
                name="Existing Frame",
            )
        )

        app = _make_app(extra_args={"frame_library_path": lib_path})
        with patch("providers.ha_provisioner.HAProvisioner", return_value=_make_mock_provisioner()):
            app.initialize()

        frames = app._frame_library.list_frames()
        # Should still be 1 — no duplicate seed
        assert len(frames) == 1
        assert frames[0].name == "Existing Frame"

    def test_initialize_schedules_async_startup(self):
        """initialize() must call run_in to defer async provisioning."""
        app = _make_app()
        with patch("providers.ha_provisioner.HAProvisioner", return_value=_make_mock_provisioner()):
            app.initialize()

        app.run_in.assert_called_once()
        callback_fn = app.run_in.call_args[0][0]
        delay = app.run_in.call_args[0][1]
        assert delay == 0
        assert callable(callback_fn)

    def test_initialize_registers_creators(self):
        """App should store the configured creators list."""
        app = _make_app()
        with patch("providers.ha_provisioner.HAProvisioner", return_value=_make_mock_provisioner()):
            app.initialize()

        assert "Mom" in app._creators
        assert "Tom" in app._creators
        assert "Anonymous" in app._creators


class TestAsyncStartup:
    def test_async_startup_registers_event_listener(self, tmp_path):
        """_async_startup should register a listener for the command event."""
        mock_prov = _make_mock_provisioner()
        app = _make_app(extra_args={"frame_library_path": str(tmp_path / "lib.json")})
        with patch("providers.ha_provisioner.HAProvisioner", return_value=mock_prov):
            app.initialize()
            _run(app._async_startup())

        app.listen_event.assert_called()
        event_names = [c[0][1] for c in app.listen_event.call_args_list]
        assert VestaboardConfigurationApp.COMMAND_EVENT in event_names

    def test_async_startup_listens_to_controller_status(self, tmp_path):
        """_async_startup should listen for controller status changes."""
        mock_prov = _make_mock_provisioner()
        app = _make_app(extra_args={"frame_library_path": str(tmp_path / "lib.json")})
        with patch("providers.ha_provisioner.HAProvisioner", return_value=mock_prov):
            app.initialize()
            _run(app._async_startup())

        app.listen_state.assert_called()
        watched_entities = [c[0][1] for c in app.listen_state.call_args_list]
        assert VestaboardConfigurationApp.CONTROLLER_STATUS_ENTITY in watched_entities

    def test_async_startup_provisions_relay_script(self, tmp_path):
        """_async_startup should call ensure_script for the relay."""
        mock_prov = _make_mock_provisioner()
        app = _make_app(extra_args={"frame_library_path": str(tmp_path / "lib.json")})
        with patch("providers.ha_provisioner.HAProvisioner", return_value=mock_prov):
            app.initialize()
            _run(app._async_startup())

        mock_prov.ensure_script.assert_called_once()
        script_id = mock_prov.ensure_script.call_args[0][0]
        assert script_id == VestaboardConfigurationApp.RELAY_SCRIPT_ID

    def test_async_startup_provisions_creator_input_select(self, tmp_path):
        """_async_startup should call ensure_helper for the creator input_select."""
        mock_prov = _make_mock_provisioner()
        app = _make_app(extra_args={"frame_library_path": str(tmp_path / "lib.json")})
        with patch("providers.ha_provisioner.HAProvisioner", return_value=mock_prov):
            app.initialize()
            _run(app._async_startup())

        mock_prov.ensure_helper.assert_called_once()
        helper_type = mock_prov.ensure_helper.call_args[0][0]
        helper_name = mock_prov.ensure_helper.call_args[0][1]
        assert helper_type == "input_select"
        assert "vestaboard" in helper_name.lower()

    def test_async_startup_skips_provisioning_when_no_credentials(self, tmp_path):
        """Provisioning should be skipped (with a warning) when ha_url is empty."""
        app = _make_app(
            extra_args={
                "ha_url": "",
                "ha_token_env": "",
                "frame_library_path": str(tmp_path / "lib.json"),
            }
        )
        app.initialize()
        _run(app._async_startup())

        # set_state should still have been called (publish status), but no provisioner
        app.log.assert_called()
        log_levels = [c[1].get("level", "INFO") for c in app.log.call_args_list]
        assert "WARNING" in log_levels

    def test_async_startup_performs_immediate_controller_sync(self, tmp_path):
        """_async_startup should immediately read controller state if it already exists."""
        mock_prov = _make_mock_provisioner()
        app = _make_app(extra_args={"frame_library_path": str(tmp_path / "lib.json")})

        # Simulate controller already having state (e.g. controller started first)
        controller_attrs = {
            "displayed_source": "calendar_clock",
            "queue_state": {"pending": [], "fallback": []},
            "all_automations": [],
        }
        app.get_state = MagicMock(return_value={"attributes": controller_attrs})

        with patch("providers.ha_provisioner.HAProvisioner", return_value=mock_prov):
            app.initialize()
            _run(app._async_startup())

        # Controller attrs should be loaded immediately (not waiting for 5s deferred sync)
        assert app._controller_attrs.get("displayed_source") == "calendar_clock"

    def test_async_startup_immediate_sync_tolerates_missing_controller(self, tmp_path):
        """_async_startup immediate sync should not crash when controller has no state yet."""
        mock_prov = _make_mock_provisioner()
        app = _make_app(extra_args={"frame_library_path": str(tmp_path / "lib.json")})

        # Controller returns None (not yet started)
        app.get_state = MagicMock(return_value=None)

        with patch("providers.ha_provisioner.HAProvisioner", return_value=mock_prov):
            app.initialize()
            _run(app._async_startup())

        # Should complete without error; controller attrs remain empty
        assert app._controller_attrs == {}


# ---------------------------------------------------------------------------
# Command handling tests
# ---------------------------------------------------------------------------


class TestSaveFrame:
    def test_save_frame_command_adds_to_library(self, tmp_path):
        """save_frame should create a new LibraryFrame and persist it."""
        app = _make_app(extra_args={"frame_library_path": str(tmp_path / "lib.json")})
        app.initialize()

        frame_data = _make_dummy_frame()
        payload = json.dumps({
            "frame": frame_data,
            "name": "My Test Frame",
            "creator": "Tom",
            "rating": 4,
        })
        app._on_command("vestaboard_configuration_command", {"command": "save_frame", "payload": payload}, {})

        frames = app._frame_library.list_frames()
        # Hello World seed + our new frame
        assert len(frames) == 2
        names = {f.name for f in frames}
        assert "My Test Frame" in names

    def test_save_frame_publishes_status(self, tmp_path):
        """save_frame should call set_state after saving."""
        app = _make_app(extra_args={"frame_library_path": str(tmp_path / "lib.json")})
        app.initialize()
        app.set_state.reset_mock()

        payload = json.dumps({"frame": _make_dummy_frame(), "name": "X", "creator": "Tom", "rating": 1})
        app._on_command("vestaboard_configuration_command", {"command": "save_frame", "payload": payload}, {})

        app.set_state.assert_called()
        entity = app.set_state.call_args[0][0]
        assert entity == VestaboardConfigurationApp.SENSOR_ENTITY

    def test_save_frame_missing_frame_logs_warning(self, tmp_path):
        """save_frame with no 'frame' key should log a warning and not modify library."""
        app = _make_app(extra_args={"frame_library_path": str(tmp_path / "lib.json")})
        app.initialize()
        initial_count = len(app._frame_library.list_frames())

        payload = json.dumps({"name": "No Frame"})
        app._on_command("vestaboard_configuration_command", {"command": "save_frame", "payload": payload}, {})

        assert len(app._frame_library.list_frames()) == initial_count
        levels = [c[1].get("level", "INFO") for c in app.log.call_args_list]
        assert "WARNING" in levels


class TestUpdateFrame:
    def test_update_frame_command_updates_library(self, tmp_path):
        """update_frame should update mutable fields on an existing frame."""
        app = _make_app(extra_args={"frame_library_path": str(tmp_path / "lib.json")})
        app.initialize()

        frames = app._frame_library.list_frames()
        frame_id = frames[0].frame_id

        payload = json.dumps({"frame_id": frame_id, "name": "Updated Name", "rating": 5})
        app._on_command("vestaboard_configuration_command", {"command": "update_frame", "payload": payload}, {})

        updated = app._frame_library.get_frame(frame_id)
        assert updated is not None
        assert updated.name == "Updated Name"
        assert updated.rating == 5

    def test_update_frame_missing_frame_id_logs_warning(self, tmp_path):
        """update_frame with no 'frame_id' should log a warning."""
        app = _make_app(extra_args={"frame_library_path": str(tmp_path / "lib.json")})
        app.initialize()
        app.log.reset_mock()

        payload = json.dumps({"name": "no id"})
        app._on_command("vestaboard_configuration_command", {"command": "update_frame", "payload": payload}, {})

        levels = [c[1].get("level", "INFO") for c in app.log.call_args_list]
        assert "WARNING" in levels

    def test_update_frame_not_found_logs_warning(self, tmp_path):
        """update_frame on a nonexistent frame_id should log a warning."""
        app = _make_app(extra_args={"frame_library_path": str(tmp_path / "lib.json")})
        app.initialize()
        app.log.reset_mock()

        payload = json.dumps({"frame_id": "nonexistent-id", "rating": 3})
        app._on_command("vestaboard_configuration_command", {"command": "update_frame", "payload": payload}, {})

        levels = [c[1].get("level", "INFO") for c in app.log.call_args_list]
        assert "WARNING" in levels


class TestDeleteFrame:
    def test_delete_frame_command_removes_from_library(self, tmp_path):
        """delete_frame should remove the specified frame."""
        app = _make_app(extra_args={"frame_library_path": str(tmp_path / "lib.json")})
        app.initialize()

        frames = app._frame_library.list_frames()
        assert len(frames) == 1
        frame_id = frames[0].frame_id

        payload = json.dumps({"frame_id": frame_id})
        app._on_command("vestaboard_configuration_command", {"command": "delete_frame", "payload": payload}, {})

        assert app._frame_library.get_frame(frame_id) is None
        assert len(app._frame_library.list_frames()) == 0

    def test_delete_frame_missing_frame_id_logs_warning(self, tmp_path):
        """delete_frame with no frame_id should log a warning."""
        app = _make_app(extra_args={"frame_library_path": str(tmp_path / "lib.json")})
        app.initialize()
        app.log.reset_mock()

        payload = json.dumps({})
        app._on_command("vestaboard_configuration_command", {"command": "delete_frame", "payload": payload}, {})

        levels = [c[1].get("level", "INFO") for c in app.log.call_args_list]
        assert "WARNING" in levels


class TestPushFrame:
    def test_push_frame_forwards_to_controller(self, tmp_path):
        """push_frame should fire the controller command event."""
        app = _make_app(extra_args={"frame_library_path": str(tmp_path / "lib.json")})
        app.initialize()

        frame_data = _make_dummy_frame()
        payload = json.dumps({"frame": frame_data, "ttl_minutes": 30})
        app._on_command("vestaboard_configuration_command", {"command": "push_frame", "payload": payload}, {})

        app.fire_event.assert_called_once()
        event_name = app.fire_event.call_args[0][0]
        assert event_name == VestaboardConfigurationApp.CONTROLLER_COMMAND_EVENT
        fired_command = app.fire_event.call_args[1]["command"]
        assert fired_command == "push_frame"

    def test_push_frame_sets_respect_ttl_false(self, tmp_path):
        """push_frame should always send respect_ttl=False."""
        app = _make_app(extra_args={"frame_library_path": str(tmp_path / "lib.json")})
        app.initialize()

        payload = json.dumps({"frame": _make_dummy_frame()})
        app._on_command("vestaboard_configuration_command", {"command": "push_frame", "payload": payload}, {})

        fired_payload = json.loads(app.fire_event.call_args[1]["payload"])
        assert fired_payload["respect_ttl"] is False



class TestPushLibraryFrame:
    def test_push_library_frame_forwards_correct_characters(self, tmp_path):
        """push_library_frame should resolve the frame and forward its characters."""
        app = _make_app(extra_args={"frame_library_path": str(tmp_path / "lib.json")})
        app.initialize()

        # The seeded Hello World frame
        frames = app._frame_library.list_frames()
        frame_id = frames[0].frame_id
        expected_chars = frames[0].characters

        payload = json.dumps({"frame_id": frame_id, "ttl_minutes": 10})
        app._on_command("vestaboard_configuration_command", {"command": "push_library_frame", "payload": payload}, {})

        app.fire_event.assert_called_once()
        fired_payload = json.loads(app.fire_event.call_args[1]["payload"])
        assert fired_payload["frame"] == expected_chars

    def test_push_library_frame_includes_should_expire(self, tmp_path):
        """push_library_frame should include should_expire=True in controller payload."""
        app = _make_app(extra_args={"frame_library_path": str(tmp_path / "lib.json")})
        app.initialize()

        frames = app._frame_library.list_frames()
        frame_id = frames[0].frame_id

        payload = json.dumps({"frame_id": frame_id})
        app._on_command("vestaboard_configuration_command", {"command": "push_library_frame", "payload": payload}, {})

        fired_payload = json.loads(app.fire_event.call_args[1]["payload"])
        assert fired_payload["should_expire"] is True

    def test_push_library_frame_missing_frame_logs_warning(self, tmp_path):
        """push_library_frame with an unknown frame_id should log a warning."""
        app = _make_app(extra_args={"frame_library_path": str(tmp_path / "lib.json")})
        app.initialize()
        app.log.reset_mock()
        app.fire_event.reset_mock()

        payload = json.dumps({"frame_id": "does-not-exist"})
        app._on_command("vestaboard_configuration_command", {"command": "push_library_frame", "payload": payload}, {})

        app.fire_event.assert_not_called()
        levels = [c[1].get("level", "INFO") for c in app.log.call_args_list]
        assert "WARNING" in levels


class TestToggleAutomation:
    def test_toggle_automation_forwards_to_controller(self, tmp_path):
        """toggle_automation should fire the controller command event."""
        app = _make_app(extra_args={"frame_library_path": str(tmp_path / "lib.json")})
        app.initialize()

        payload = json.dumps({"automation_id": "calendar_clock", "enabled": True})
        app._on_command("vestaboard_configuration_command", {"command": "toggle_automation", "payload": payload}, {})

        app.fire_event.assert_called_once()
        event = app.fire_event.call_args[0][0]
        assert event == VestaboardConfigurationApp.CONTROLLER_COMMAND_EVENT
        fired_command = app.fire_event.call_args[1]["command"]
        assert fired_command == "activate_automation"

        fired_payload = json.loads(app.fire_event.call_args[1]["payload"])
        assert fired_payload["automation_id"] == "calendar_clock"

    def test_toggle_automation_missing_id_logs_warning(self, tmp_path):
        """toggle_automation with no automation_id should log a warning."""
        app = _make_app(extra_args={"frame_library_path": str(tmp_path / "lib.json")})
        app.initialize()
        app.log.reset_mock()

        payload = json.dumps({"enabled": False})
        app._on_command("vestaboard_configuration_command", {"command": "toggle_automation", "payload": payload}, {})

        levels = [c[1].get("level", "INFO") for c in app.log.call_args_list]
        assert "WARNING" in levels


class TestRefreshStatus:
    def test_refresh_status_publishes_state(self, tmp_path):
        """refresh_status command should call set_state."""
        app = _make_app(extra_args={"frame_library_path": str(tmp_path / "lib.json")})
        app.initialize()
        app.set_state.reset_mock()

        payload = json.dumps({})
        app._on_command("vestaboard_configuration_command", {"command": "refresh_status", "payload": payload}, {})

        app.set_state.assert_called()
        entity = app.set_state.call_args[0][0]
        assert entity == VestaboardConfigurationApp.SENSOR_ENTITY


class TestAddCreator:
    def test_add_creator_adds_to_list(self, tmp_path):
        """add_creator should append the name to _creators."""
        app = _make_app(extra_args={"frame_library_path": str(tmp_path / "lib.json")})
        app.initialize()

        assert "NewPerson" not in app._creators

        payload = json.dumps({"name": "NewPerson"})
        app._on_command("vestaboard_configuration_command", {"command": "add_creator", "payload": payload}, {})

        assert "NewPerson" in app._creators

    def test_add_creator_no_duplicates(self, tmp_path):
        """add_creator should not add the same name twice."""
        app = _make_app(extra_args={"frame_library_path": str(tmp_path / "lib.json")})
        app.initialize()
        initial_count = len(app._creators)

        payload = json.dumps({"name": "Tom"})
        app._on_command("vestaboard_configuration_command", {"command": "add_creator", "payload": payload}, {})
        app._on_command("vestaboard_configuration_command", {"command": "add_creator", "payload": payload}, {})

        assert app._creators.count("Tom") == 1
        # Total count should not have grown
        assert len(app._creators) == initial_count

    def test_add_creator_updates_input_select(self, tmp_path):
        """add_creator should call call_service to update the input_select options."""
        app = _make_app(extra_args={"frame_library_path": str(tmp_path / "lib.json")})
        app.initialize()
        app.call_service.reset_mock()

        payload = json.dumps({"name": "AnotherPerson"})
        app._on_command("vestaboard_configuration_command", {"command": "add_creator", "payload": payload}, {})

        app.call_service.assert_called()
        service_call = app.call_service.call_args[0][0]
        assert "input_select" in service_call


class TestUnknownCommand:
    def test_unknown_command_logged_as_warning(self, tmp_path):
        """An unrecognised command should log at WARNING level."""
        app = _make_app(extra_args={"frame_library_path": str(tmp_path / "lib.json")})
        app.initialize()
        app.log.reset_mock()

        payload = json.dumps({})
        app._on_command("vestaboard_configuration_command", {"command": "totally_unknown_cmd", "payload": payload}, {})

        levels = [c[1].get("level", "INFO") for c in app.log.call_args_list]
        assert "WARNING" in levels


class TestStatusPublish:
    def test_status_publish_attributes(self, tmp_path):
        """_publish_status should call set_state with expected sensor entity and attributes."""
        app = _make_app(extra_args={"frame_library_path": str(tmp_path / "lib.json")})
        app.initialize()
        app.set_state.reset_mock()

        app._publish_status()

        app.set_state.assert_called_once()
        entity = app.set_state.call_args[0][0]
        assert entity == VestaboardConfigurationApp.SENSOR_ENTITY

        kwargs = app.set_state.call_args[1]
        assert kwargs["state"] == "ok"
        attrs = kwargs["attributes"]
        assert "library" in attrs
        assert "creators" in attrs
        assert "status" in attrs
        assert attrs["status"] == "ok"
        assert isinstance(attrs["creators"], list)

    def test_status_publish_includes_library_json(self, tmp_path):
        """library attribute should be a JSON string of all frames."""
        app = _make_app(extra_args={"frame_library_path": str(tmp_path / "lib.json")})
        app.initialize()
        app.set_state.reset_mock()

        app._publish_status()

        attrs = app.set_state.call_args[1]["attributes"]
        library_data = json.loads(attrs["library"])
        assert isinstance(library_data, list)
        # Hello World was seeded
        assert len(library_data) == 1
        assert library_data[0]["name"] == "Hello World"

    def test_status_merges_controller_attrs(self, tmp_path):
        """When controller attrs are cached, they should appear in our published status."""
        app = _make_app(extra_args={"frame_library_path": str(tmp_path / "lib.json")})
        app.initialize()

        # Simulate controller status being cached (using controller attr names)
        app._controller_attrs = {
            "displayed_frame": {"characters": [[0]*22]*6, "source": "calendar_clock"},
            "displayed_source": "calendar_clock",
            "displayed_ttl_remaining_s": 30,
            "queue_state": {
                "pending": [{"source": "garage_door"}],
                "fallback": [{"source": "user"}],
            },
            "all_automations": [{"id": "calendar_clock", "enabled": True}],
        }
        app.set_state.reset_mock()

        app._publish_status()

        attrs = app.set_state.call_args[1]["attributes"]
        assert attrs["current_source"] == "calendar_clock"
        assert attrs["current_frame"] == [[0]*22]*6
        assert attrs["current_ttl_expires"] is not None
        assert len(attrs["queue"]) == 1
        assert attrs["fallback_source"] == "user"
        assert len(attrs["automations"]) == 1


class TestControllerStatusMirroring:
    def test_on_controller_status_republishes(self, tmp_path):
        """_on_controller_status should update cached attrs and call set_state."""
        app = _make_app(extra_args={"frame_library_path": str(tmp_path / "lib.json")})
        app.initialize()

        # Mock get_state to return controller attributes
        app.get_state = MagicMock(
            return_value={
                "attributes": {
                    "displayed_source": "random_message",
                    "queue_state": {"pending": [], "fallback": []},
                }
            }
        )
        app.set_state.reset_mock()

        app._on_controller_status(
            VestaboardConfigurationApp.CONTROLLER_STATUS_ENTITY,
            "all",
            None,
            "ok",
            {},
        )

        app.set_state.assert_called()
        assert app._controller_attrs.get("displayed_source") == "random_message"


class TestGenerateArt:
    def test_generate_art_forwards_to_controller(self, tmp_path):
        """generate_art should fire the controller command event with subject as generate_ai_art."""
        app = _make_app(extra_args={"frame_library_path": str(tmp_path / "lib.json")})
        app.initialize()

        payload = json.dumps({"subject": "a rocket ship"})
        app._on_command("vestaboard_configuration_command", {"command": "generate_art", "payload": payload}, {})

        app.fire_event.assert_called_once()
        fired_command = app.fire_event.call_args[1]["command"]
        assert fired_command == "generate_ai_art"
        fired_payload = json.loads(app.fire_event.call_args[1]["payload"])
        assert fired_payload["subject"] == "a rocket ship"


class TestSaveArtToLibrary:
    def test_save_art_to_library_adds_frame(self, tmp_path):
        """save_art_to_library should add a frame with creator='AI' by default."""
        app = _make_app(extra_args={"frame_library_path": str(tmp_path / "lib.json")})
        app.initialize()
        initial_count = len(app._frame_library.list_frames())

        payload = json.dumps({"frame": _make_dummy_frame(), "name": "Cool Art"})
        app._on_command("vestaboard_configuration_command", {"command": "save_art_to_library", "payload": payload}, {})

        frames = app._frame_library.list_frames()
        assert len(frames) == initial_count + 1
        art_frames = [f for f in frames if f.name == "Cool Art"]
        assert len(art_frames) == 1
        assert art_frames[0].creator == "AI"


class TestInvalidPayload:
    def test_invalid_json_payload_logs_error(self, tmp_path):
        """A non-JSON payload string should log an error and not crash."""
        app = _make_app(extra_args={"frame_library_path": str(tmp_path / "lib.json")})
        app.initialize()
        app.log.reset_mock()

        app._on_command(
            "vestaboard_configuration_command",
            {"command": "save_frame", "payload": "not-valid-json!!!"},
            {},
        )

        levels = [c[1].get("level", "INFO") for c in app.log.call_args_list]
        assert "ERROR" in levels


# ---------------------------------------------------------------------------
# Status passthrough — next_fire_time and preview_frame
# ---------------------------------------------------------------------------


class TestStatusPassthrough:
    """Verify that controller automation fields are passed through unchanged."""

    def test_publish_status_passes_next_fire_time_through(self, tmp_path):
        """next_fire_time from controller all_automations is passed to card unchanged."""
        app = _make_app(extra_args={"frame_library_path": str(tmp_path / "lib.json")})
        app.initialize()

        next_fire = time.time() + 600.0
        app._controller_attrs = {
            "displayed_frame": None,
            "displayed_source": None,
            "displayed_ttl_remaining_s": None,
            "queue_state": {"pending": [], "fallback": []},
            "all_automations": [
                {
                    "id": "messages_from_library",
                    "name": "Messages From Library",
                    "enabled": True,
                    "next_fire_time": next_fire,
                }
            ],
        }
        app.set_state.reset_mock()

        app._publish_status()

        attrs = app.set_state.call_args[1]["attributes"]
        automations = attrs["automations"]
        assert len(automations) == 1
        assert automations[0]["next_fire_time"] == next_fire

    def test_publish_status_passes_preview_frame_through(self, tmp_path):
        """preview_frame from controller all_automations is passed to card unchanged."""
        app = _make_app(extra_args={"frame_library_path": str(tmp_path / "lib.json")})
        app.initialize()

        preview = [[5] * 22 for _ in range(6)]
        app._controller_attrs = {
            "displayed_frame": None,
            "displayed_source": None,
            "displayed_ttl_remaining_s": None,
            "queue_state": {"pending": [], "fallback": []},
            "all_automations": [
                {
                    "id": "art_from_library",
                    "name": "Art From Library",
                    "enabled": True,
                    "preview_frame": preview,
                }
            ],
        }
        app.set_state.reset_mock()

        app._publish_status()

        attrs = app.set_state.call_args[1]["attributes"]
        automations = attrs["automations"]
        assert len(automations) == 1
        assert automations[0]["preview_frame"] == preview

    def test_publish_status_passthrough_works_without_optional_fields(self, tmp_path):
        """automations without next_fire_time or preview_frame pass through cleanly."""
        app = _make_app(extra_args={"frame_library_path": str(tmp_path / "lib.json")})
        app.initialize()

        app._controller_attrs = {
            "displayed_frame": None,
            "displayed_source": None,
            "displayed_ttl_remaining_s": None,
            "queue_state": {"pending": [], "fallback": []},
            "all_automations": [
                {
                    "id": "calendar_clock",
                    "name": "Calendar Clock",
                    "enabled": True,
                    # no next_fire_time, no preview_frame
                }
            ],
        }
        app.set_state.reset_mock()

        app._publish_status()

        attrs = app.set_state.call_args[1]["attributes"]
        automations = attrs["automations"]
        assert len(automations) == 1
        assert "next_fire_time" not in automations[0]
        assert "preview_frame" not in automations[0]
