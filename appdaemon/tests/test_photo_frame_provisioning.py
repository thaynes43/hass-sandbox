"""Tests for PhotoFrameViewerApp provisioning startup.

Verifies that _provision_entities calls HAProvisioner correctly to create
the relay script and input_select picker helper.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Mock hassapi before importing the app
mock_hass = MagicMock()
mock_hass.Hass = type("_MockHass", (), {"__init__": lambda self, *a, **kw: None})
sys.modules["hassapi"] = mock_hass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))

from photo_frame_viewer.photo_frame_viewer_app import PhotoFrameViewerApp


def _make_app(extra_args: dict | None = None) -> PhotoFrameViewerApp:
    """Create a minimal PhotoFrameViewerApp with all AppDaemon methods mocked."""
    ad = MagicMock()
    app = PhotoFrameViewerApp(ad, MagicMock())

    td = tempfile.mkdtemp(prefix="pfv_prov_test_")
    base_args: dict = {
        "ha_url": "http://ha:8123",
        "ha_token_env": "TOKEN",
        "source_dir": td,
        "ha_local_url_base": "/local/photo-frame/live",
        "state_dir": td,
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

    return app


def _run(coro):
    """Run a coroutine synchronously in a test context."""
    return asyncio.get_event_loop().run_until_complete(coro)


class TestProvisionRelayScript:
    def test_provision_creates_relay_script(self):
        app = _make_app()
        app.initialize()

        mock_prov = MagicMock()
        mock_prov.ensure_script = AsyncMock(return_value=True)
        mock_prov.ensure_helper = AsyncMock(return_value=False)

        with patch("providers.ha_provisioner.HAProvisioner", return_value=mock_prov):
            _run(app._provision_entities())

        mock_prov.ensure_script.assert_called_once()
        call_args = mock_prov.ensure_script.call_args

        # First positional arg is the script_id
        assert call_args[0][0] == "wall_display_photo_frame_relay"

        # Script config has correct event name
        script_config = call_args[0][1]
        assert script_config["sequence"][0]["event"] == "wall_display_photo_frame_command"
        assert script_config["mode"] == "queued"
        assert "command" in script_config["fields"]
        assert "payload" in script_config["fields"]

    def test_provision_relay_script_logs_created(self):
        app = _make_app()
        app.initialize()

        mock_prov = MagicMock()
        mock_prov.ensure_script = AsyncMock(return_value=True)
        mock_prov.ensure_helper = AsyncMock(return_value=False)

        with patch("providers.ha_provisioner.HAProvisioner", return_value=mock_prov):
            _run(app._provision_entities())

        log_messages = [str(c) for c in app.log.call_args_list]
        assert any("created" in m.lower() and "wall_display_photo_frame_relay" in m
                   for m in log_messages)

    def test_provision_relay_script_logs_existing(self):
        app = _make_app()
        app.initialize()

        mock_prov = MagicMock()
        mock_prov.ensure_script = AsyncMock(return_value=False)
        mock_prov.ensure_helper = AsyncMock(return_value=False)

        with patch("providers.ha_provisioner.HAProvisioner", return_value=mock_prov):
            _run(app._provision_entities())

        log_messages = [str(c) for c in app.log.call_args_list]
        assert any("already exists" in m.lower() for m in log_messages)


class TestProvisionPickerHelper:
    def test_provision_creates_input_select(self):
        app = _make_app()
        app.initialize()

        mock_prov = MagicMock()
        mock_prov.ensure_script = AsyncMock(return_value=False)
        mock_prov.ensure_helper = AsyncMock(return_value=True)

        with patch("providers.ha_provisioner.HAProvisioner", return_value=mock_prov):
            _run(app._provision_entities())

        mock_prov.ensure_helper.assert_called_once()
        call_args = mock_prov.ensure_helper.call_args
        assert call_args[0][0] == "input_select"
        # Name should reference the prefix
        helper_name = call_args[0][1]
        assert "Wall Display" in helper_name
        assert "Photo Frame Image" in helper_name

    def test_provision_input_select_logs_created(self):
        app = _make_app()
        app.initialize()

        mock_prov = MagicMock()
        mock_prov.ensure_script = AsyncMock(return_value=False)
        mock_prov.ensure_helper = AsyncMock(return_value=True)

        with patch("providers.ha_provisioner.HAProvisioner", return_value=mock_prov):
            _run(app._provision_entities())

        log_messages = [str(c) for c in app.log.call_args_list]
        assert any("created" in m.lower() and "picker" in m.lower()
                   for m in log_messages)


class TestProvisionWithoutCredentials:
    def test_skips_provisioning_when_no_ha_url(self):
        app = _make_app({"ha_url": "", "ha_token_env": "TOKEN"})
        app.initialize()

        with patch("providers.ha_provisioner.HAProvisioner") as MockClass:
            _run(app._provision_entities())

        MockClass.assert_not_called()
        log_messages = [str(c) for c in app.log.call_args_list]
        assert any("skipping" in m.lower() for m in log_messages)

    def test_skips_provisioning_when_no_ha_token(self):
        app = _make_app({"ha_url": "http://ha:8123", "ha_token_env": ""})
        app.initialize()

        with patch("providers.ha_provisioner.HAProvisioner") as MockClass:
            _run(app._provision_entities())

        MockClass.assert_not_called()

    def test_skips_provisioning_when_credentials_missing_entirely(self):
        app = _make_app()
        app.args.pop("ha_url", None)
        app.args.pop("ha_token_env", None)
        app.initialize()

        with patch("providers.ha_provisioner.HAProvisioner") as MockClass:
            _run(app._provision_entities())

        MockClass.assert_not_called()


class TestProvisionErrorHandling:
    def test_relay_script_failure_is_caught_and_logged(self):
        app = _make_app()
        app.initialize()

        mock_prov = MagicMock()
        mock_prov.ensure_script = AsyncMock(
            side_effect=Exception("Connection refused")
        )
        mock_prov.ensure_helper = AsyncMock(return_value=False)

        with patch("providers.ha_provisioner.HAProvisioner", return_value=mock_prov):
            # Must NOT raise — app continues even if provisioning fails
            _run(app._provision_entities())

        log_messages = [str(c) for c in app.log.call_args_list]
        assert any("error" in m.lower() or "failed" in m.lower()
                   for m in log_messages)

    def test_helper_failure_is_caught_and_logged(self):
        app = _make_app()
        app.initialize()

        mock_prov = MagicMock()
        mock_prov.ensure_script = AsyncMock(return_value=False)
        mock_prov.ensure_helper = AsyncMock(
            side_effect=RuntimeError("Flow aborted")
        )

        with patch("providers.ha_provisioner.HAProvisioner", return_value=mock_prov):
            _run(app._provision_entities())

        log_messages = [str(c) for c in app.log.call_args_list]
        assert any("error" in m.lower() or "failed" in m.lower()
                   for m in log_messages)


class TestEntityPrefixDerivation:
    def test_derives_prefix_from_instance_name(self):
        app = _make_app()
        app.name = "photo_frame_viewer_bedroom"
        app.initialize()

        assert app._entity_prefix == "bedroom"
        assert app._sensor_entity_id == "sensor.bedroom_photo_frame_status"
        assert app._relay_script_id == "bedroom_photo_frame_relay"
        assert app._command_event == "bedroom_photo_frame_command"
        assert app.picker_entity_id == "input_select.bedroom_photo_frame_image"

    def test_explicit_entity_prefix_overrides_name(self):
        app = _make_app({"entity_prefix": "living_room"})
        app.name = "photo_frame_viewer_wall_display"
        app.initialize()

        assert app._entity_prefix == "living_room"
        assert app._sensor_entity_id == "sensor.living_room_photo_frame_status"

    def test_falls_back_to_wall_display_when_name_has_no_marker(self):
        app = _make_app()
        app.name = "some_other_app"
        app.initialize()

        assert app._entity_prefix == "wall_display"

    def test_relay_event_name_matches_prefix(self):
        app = _make_app()
        app.name = "photo_frame_viewer_wall_display"
        app.initialize()

        assert app._command_event == "wall_display_photo_frame_command"

        mock_prov = MagicMock()
        mock_prov.ensure_script = AsyncMock(return_value=True)
        mock_prov.ensure_helper = AsyncMock(return_value=False)

        with patch("providers.ha_provisioner.HAProvisioner", return_value=mock_prov):
            _run(app._provision_entities())

        script_config = mock_prov.ensure_script.call_args[0][1]
        event_name = script_config["sequence"][0]["event"]
        assert event_name == "wall_display_photo_frame_command"
