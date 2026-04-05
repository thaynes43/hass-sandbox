"""Unit tests for CountdownApp.

Mocks AppDaemon methods and all external providers — no real network or HA
access required.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Mock hassapi before importing the app
# ---------------------------------------------------------------------------
mock_hass = MagicMock()
mock_hass.Hass = type("_MockHass", (), {"__init__": lambda self, *a, **kw: None})
sys.modules["hassapi"] = mock_hass

_repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo_root / "apps"))
sys.path.insert(0, str(_repo_root))

from countdown_app.countdown_app import (
    CountdownApp,
    SENSOR_STATUS,
    RELAY_SCRIPT_ID,
    COMMAND_EVENT,
    _DEFAULT_TEXT_STYLE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_countdown(
    id="test-id-1",
    title="Test Event",
    subtitle="Test Subtitle",
    target_datetime=None,
    image_prompt="test prompt",
    image_filename="",
    text_style=None,
    hidden=False,
):
    if target_datetime is None:
        target_datetime = (
            datetime.datetime.now() + datetime.timedelta(days=15)
        ).isoformat()
    return {
        "id": id,
        "title": title,
        "subtitle": subtitle,
        "target_datetime": target_datetime,
        "image_prompt": image_prompt,
        "image_filename": image_filename,
        "text_style": dict(text_style or _DEFAULT_TEXT_STYLE),
        "created_at": datetime.datetime.now().isoformat(),
        "hidden": hidden,
    }


def _fire_command(app, cmd, payload=None):
    data = {"command": cmd, "payload": json.dumps(payload or {})}
    app._on_command("countdown_command", data, {})


def _run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

_DEFAULT_ARGS = {
    "ha_url": "http://ha:8123",
    "ha_token_env": "TOKEN",
    "ai_provider_conf": {"image": "openai-default"},
    "image_sync_shell_command": "countdown_sync_images",
    "rotation_interval_s": 15,
    "countdown_refresh_s": 60,
}


def _make_app(extra_args=None, tmpdir=None):
    td = tmpdir or tempfile.mkdtemp(prefix="cd_test_")
    ad = MagicMock()
    app = CountdownApp(ad, MagicMock())

    args = dict(_DEFAULT_ARGS)
    args["media_fs_root"] = td
    args["media_subdir"] = "countdown-app"
    args["www_subdir"] = "countdown-app"
    if extra_args:
        args.update(extra_args)

    app.args = args
    app.get_state = MagicMock(return_value=None)
    app.set_state = MagicMock()
    app.call_service = MagicMock()
    app.listen_state = MagicMock()
    app.listen_event = MagicMock()
    app.fire_event = MagicMock()
    app.run_in = MagicMock()
    app.run_every = MagicMock()
    app.run_daily = MagicMock()
    app.cancel_timer = MagicMock()
    app.log = MagicMock()
    app.create_task = MagicMock()
    app.name = "countdown_app"

    with patch("providers.secrets.resolve_arg_secret", side_effect=lambda cfg, key, **kw: cfg.get(key, kw.get("default", ""))):
        app.initialize()

    return app


# ---------------------------------------------------------------------------
# Lifecycle Tests
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_initialize_creates_timers(self):
        app = _make_app()
        # run_in should be called once for startup callback
        app.run_in.assert_called_once()
        call_args = app.run_in.call_args
        assert call_args[0][0] == app._on_startup
        assert call_args[0][1] == 0

    def test_initialize_sets_countdowns_and_active_index(self):
        app = _make_app()
        assert isinstance(app._countdowns, list)
        assert isinstance(app._active_index, int)

    def test_initialize_creates_media_dir(self):
        td = tempfile.mkdtemp(prefix="cd_test_dir_")
        app = _make_app(tmpdir=td)
        expected_dir = os.path.join(td, "countdown-app")
        assert os.path.isdir(expected_dir)


# ---------------------------------------------------------------------------
# State Persistence Tests
# ---------------------------------------------------------------------------


class TestStatePersistence:
    def test_save_and_load_round_trip(self):
        td = tempfile.mkdtemp(prefix="cd_test_rt_")
        app = _make_app(tmpdir=td)

        cd = _make_countdown()
        app._countdowns = [cd]
        app._active_index = 0
        app._save_state()

        # New app instance with same tmpdir — should load the saved state
        app2 = _make_app(tmpdir=td)
        assert len(app2._countdowns) == 1
        assert app2._countdowns[0]["id"] == cd["id"]
        assert app2._countdowns[0]["title"] == cd["title"]
        assert app2._active_index == 0

    def test_load_state_missing_file(self):
        td = tempfile.mkdtemp(prefix="cd_test_missing_")
        app = _make_app(tmpdir=td)
        assert app._countdowns == []
        assert app._active_index == 0

    def test_save_state_atomic(self):
        td = tempfile.mkdtemp(prefix="cd_test_atomic_")
        app = _make_app(tmpdir=td)

        cd = _make_countdown()
        app._countdowns = [cd]
        app._save_state()

        state_file = app._state_file_path()
        assert os.path.isfile(state_file)

        with open(state_file, "r", encoding="utf-8") as fh:
            data = json.load(fh)

        assert "countdowns" in data
        assert "active_index" in data
        assert len(data["countdowns"]) == 1
        assert data["countdowns"][0]["id"] == cd["id"]


# ---------------------------------------------------------------------------
# Countdown Text Tests
# ---------------------------------------------------------------------------


class TestCountdownText:
    def test_countdown_text_future(self):
        app = _make_app()
        target = (datetime.datetime.now() + datetime.timedelta(days=15, hours=3, minutes=30)).isoformat()
        cd = _make_countdown(target_datetime=target)
        text = app._compute_countdown_text(cd)
        # Should look like "15D XH YM"
        assert "D" in text
        assert "H" in text
        assert "M" in text
        assert text.startswith("15D")

    def test_countdown_text_now(self):
        app = _make_app()
        # One hour ago — within 24h window
        target = (datetime.datetime.now() - datetime.timedelta(hours=1)).isoformat()
        cd = _make_countdown(target_datetime=target)
        text = app._compute_countdown_text(cd)
        assert text == "NOW"

    def test_countdown_text_expired(self):
        app = _make_app()
        # 48 hours ago — more than 24h past target
        target = (datetime.datetime.now() - datetime.timedelta(hours=48)).isoformat()
        cd = _make_countdown(target_datetime=target)
        text = app._compute_countdown_text(cd)
        assert text == ""

    def test_countdown_text_invalid_datetime(self):
        app = _make_app()
        cd = _make_countdown(target_datetime="not-a-date")
        text = app._compute_countdown_text(cd)
        assert text == ""
        # Verify warning was logged for invalid datetime
        warning_calls = [
            c for c in app.log.call_args_list if c[1].get("level") == "WARNING"
        ]
        assert len(warning_calls) >= 1

    def test_countdown_text_empty_target(self):
        app = _make_app()
        cd = _make_countdown(target_datetime="")
        text = app._compute_countdown_text(cd)
        assert text == ""


# ---------------------------------------------------------------------------
# Visible Countdowns Tests
# ---------------------------------------------------------------------------


class TestVisibleCountdowns:
    def test_visible_excludes_hidden(self):
        app = _make_app()
        app._countdowns = [_make_countdown(hidden=True)]
        visible = app._visible_countdowns()
        assert visible == []

    def test_visible_excludes_expired(self):
        app = _make_app()
        expired_target = (datetime.datetime.now() - datetime.timedelta(hours=48)).isoformat()
        app._countdowns = [_make_countdown(target_datetime=expired_target)]
        visible = app._visible_countdowns()
        assert visible == []

    def test_visible_includes_active(self):
        app = _make_app()
        future_target = (datetime.datetime.now() + datetime.timedelta(days=5)).isoformat()
        cd = _make_countdown(target_datetime=future_target)
        app._countdowns = [cd]
        visible = app._visible_countdowns()
        assert len(visible) == 1
        assert visible[0]["id"] == cd["id"]

    def test_visible_includes_now(self):
        app = _make_app()
        # One hour ago — in "NOW" window
        recent_target = (datetime.datetime.now() - datetime.timedelta(hours=1)).isoformat()
        cd = _make_countdown(target_datetime=recent_target)
        app._countdowns = [cd]
        visible = app._visible_countdowns()
        assert len(visible) == 1
        assert visible[0]["id"] == cd["id"]


# ---------------------------------------------------------------------------
# Sensor Publishing Tests
# ---------------------------------------------------------------------------


class TestSensorPublishing:
    def test_publish_sensor_with_countdowns(self):
        app = _make_app()
        cd = _make_countdown()
        app._countdowns = [cd]
        app._active_index = 0
        app.set_state.reset_mock()

        app._publish_sensor()

        app.set_state.assert_called_once()
        call_kwargs = app.set_state.call_args
        assert call_kwargs[0][0] == SENSOR_STATUS
        assert call_kwargs[1]["state"] != "idle"

    def test_publish_sensor_empty(self):
        app = _make_app()
        app._countdowns = []
        app.set_state.reset_mock()

        app._publish_sensor()

        app.set_state.assert_called_once()
        call_kwargs = app.set_state.call_args
        assert call_kwargs[1]["state"] == "idle"
        assert call_kwargs[1]["attributes"]["active_countdown"] == {}

    def test_publish_sensor_attributes_format(self):
        app = _make_app()
        cd = _make_countdown()
        app._countdowns = [cd]
        app._active_index = 0
        app.set_state.reset_mock()

        app._publish_sensor()

        attrs = app.set_state.call_args[1]["attributes"]
        assert "countdowns" in attrs
        assert len(attrs["countdowns"]) == 1

        serialized = attrs["countdowns"][0]
        assert "countdown_text" in serialized
        assert "image_url" in serialized
        assert "image_version" in serialized


# ---------------------------------------------------------------------------
# Command Routing Tests
# ---------------------------------------------------------------------------


class TestCommandRouting:
    def test_save_countdown_new(self):
        app = _make_app()
        app._countdowns = []

        _fire_command(app, "save_countdown", {
            "title": "New Year",
            "subtitle": "The new year",
            "target_datetime": (datetime.datetime.now() + datetime.timedelta(days=10)).isoformat(),
            "image_prompt": "fireworks",
        })

        assert len(app._countdowns) == 1
        assert app._countdowns[0]["title"] == "New Year"
        assert app._countdowns[0]["id"]  # should have a generated id

    def test_save_countdown_update(self):
        app = _make_app()
        cd = _make_countdown(id="existing-id", title="Old Title")
        app._countdowns = [cd]

        _fire_command(app, "save_countdown", {
            "id": "existing-id",
            "title": "New Title",
        })

        assert len(app._countdowns) == 1
        assert app._countdowns[0]["title"] == "New Title"

    def test_save_countdown_update_partial(self):
        app = _make_app()
        original_subtitle = "Keep this subtitle"
        cd = _make_countdown(id="existing-id", title="Old Title", subtitle=original_subtitle)
        app._countdowns = [cd]

        _fire_command(app, "save_countdown", {
            "id": "existing-id",
            "title": "Updated Title",
        })

        assert app._countdowns[0]["title"] == "Updated Title"
        assert app._countdowns[0]["subtitle"] == original_subtitle

    def test_delete_countdown(self):
        app = _make_app()
        cd = _make_countdown(id="del-id")
        app._countdowns = [cd]

        _fire_command(app, "delete_countdown", {"id": "del-id"})

        assert len(app._countdowns) == 0

    def test_delete_countdown_removes_image(self):
        td = tempfile.mkdtemp(prefix="cd_del_img_")
        app = _make_app(tmpdir=td)

        # Create a real image file
        img_filename = "del-id.png"
        img_path = os.path.join(app._media_dir, img_filename)
        os.makedirs(app._media_dir, exist_ok=True)
        with open(img_path, "wb") as fh:
            fh.write(b"fake png data")

        cd = _make_countdown(id="del-id", image_filename=img_filename)
        app._countdowns = [cd]

        _fire_command(app, "delete_countdown", {"id": "del-id"})

        assert not os.path.exists(img_path)

    def test_delete_countdown_adjusts_active_index(self):
        app = _make_app()
        cd1 = _make_countdown(id="id-1", title="First")
        cd2 = _make_countdown(id="id-2", title="Second")
        cd3 = _make_countdown(id="id-3", title="Third")
        app._countdowns = [cd1, cd2, cd3]
        app._active_index = 2  # pointing at cd3

        _fire_command(app, "delete_countdown", {"id": "id-3"})

        # After deleting the 3rd item, active_index should be capped to 1
        assert app._active_index <= len(app._countdowns) - 1

    def test_delete_countdown_missing_id(self):
        app = _make_app()
        cd = _make_countdown(id="some-id")
        app._countdowns = [cd]
        app.log.reset_mock()

        _fire_command(app, "delete_countdown", {})

        # No countdown should be removed
        assert len(app._countdowns) == 1
        warning_calls = [
            c for c in app.log.call_args_list if c[1].get("level") == "WARNING"
        ]
        assert len(warning_calls) >= 1

    def test_update_style(self):
        app = _make_app()
        cd = _make_countdown(id="style-id")
        app._countdowns = [cd]

        _fire_command(app, "update_style", {
            "id": "style-id",
            "text_style": {"font_size": 72, "color": "#ffffff"},
        })

        updated = app._countdowns[0]
        assert updated["text_style"]["font_size"] == 72
        assert updated["text_style"]["color"] == "#ffffff"
        # Other default fields should be preserved via merge
        assert "position_y" in updated["text_style"]

    def test_set_active(self):
        app = _make_app()
        cd1 = _make_countdown(id="id-1")
        cd2 = _make_countdown(id="id-2")
        app._countdowns = [cd1, cd2]
        app._active_index = 0

        _fire_command(app, "set_active", {"index": 1})

        assert app._active_index == 1

    def test_unknown_command(self):
        app = _make_app()
        app.log.reset_mock()

        _fire_command(app, "totally_unknown_command", {})

        warning_calls = [
            c for c in app.log.call_args_list if c[1].get("level") == "WARNING"
        ]
        assert len(warning_calls) >= 1

    def test_invalid_payload(self):
        app = _make_app()
        app.log.reset_mock()

        # Pass a non-JSON string directly as payload
        data = {"command": "save_countdown", "payload": "not-valid-json{{{"}
        app._on_command("countdown_command", data, {})

        warning_calls = [
            c for c in app.log.call_args_list if c[1].get("level") == "WARNING"
        ]
        assert len(warning_calls) >= 1


# ---------------------------------------------------------------------------
# Image Generation Tests
# ---------------------------------------------------------------------------


class TestImageGeneration:
    def _make_app_with_countdown(self, cd=None, tmpdir=None):
        td = tmpdir or tempfile.mkdtemp(prefix="cd_gen_")
        app = _make_app(tmpdir=td)
        if cd is None:
            cd = _make_countdown(id="gen-id", image_prompt="a beautiful sunset")
        app._countdowns = [cd]
        return app, td

    def _make_mock_provider(self):
        mock_provider = MagicMock()
        mock_provider.edit_image = MagicMock(return_value={"status": "ok"})
        return mock_provider

    def test_generate_image_calls_provider(self):
        app, _ = self._make_app_with_countdown()
        mock_provider = self._make_mock_provider()

        with patch("providers.ai_providers.registry.provider_config_from_appdaemon_args") as mock_cfg, \
             patch("providers.ai_providers.registry.build_image_provider") as mock_build:
            mock_build.return_value = mock_provider

            _run_async(app._handle_generate_image({"id": "gen-id", "prompt": "a beautiful sunset"}))

        mock_provider.edit_image.assert_called_once()
        call_kwargs = mock_provider.edit_image.call_args
        assert call_kwargs[1]["prompt"] == "a beautiful sunset" or call_kwargs[0][1] == "a beautiful sunset" \
               or "a beautiful sunset" in str(call_kwargs)

    def test_generate_image_sets_filename(self):
        app, _ = self._make_app_with_countdown()
        mock_provider = self._make_mock_provider()

        with patch("providers.ai_providers.registry.provider_config_from_appdaemon_args"), \
             patch("providers.ai_providers.registry.build_image_provider") as mock_build:
            mock_build.return_value = mock_provider

            _run_async(app._handle_generate_image({"id": "gen-id", "prompt": "test prompt"}))

        assert app._countdowns[0]["image_filename"] == "gen-id.png"

    def test_generate_image_calls_sync_shell_command(self):
        app, _ = self._make_app_with_countdown()
        mock_provider = self._make_mock_provider()
        app.call_service.reset_mock()

        with patch("providers.ai_providers.registry.provider_config_from_appdaemon_args"), \
             patch("providers.ai_providers.registry.build_image_provider") as mock_build:
            mock_build.return_value = mock_provider

            _run_async(app._handle_generate_image({"id": "gen-id", "prompt": "test prompt"}))

        app.call_service.assert_called()
        service_calls = [str(c) for c in app.call_service.call_args_list]
        assert any("countdown_sync_images" in c for c in service_calls)

    def test_generate_image_sets_generating_flag(self):
        app, _ = self._make_app_with_countdown()
        generating_states = []

        def capture_publish():
            state = app._countdowns[0].get("generating", False)
            generating_states.append(state)

        original_publish = app._publish_sensor
        app._publish_sensor = MagicMock(side_effect=lambda: generating_states.append(
            app._countdowns[0].get("generating", False)
        ))

        mock_provider = self._make_mock_provider()

        with patch("providers.ai_providers.registry.provider_config_from_appdaemon_args"), \
             patch("providers.ai_providers.registry.build_image_provider") as mock_build:
            mock_build.return_value = mock_provider

            _run_async(app._handle_generate_image({"id": "gen-id", "prompt": "test prompt"}))

        # First publish call should have generating=True; last call should not
        assert generating_states[0] is True
        assert generating_states[-1] is not True

    def test_generate_image_handles_provider_error(self):
        app, _ = self._make_app_with_countdown()

        mock_provider = MagicMock()
        mock_provider.edit_image = MagicMock(side_effect=RuntimeError("provider boom"))

        with patch("providers.ai_providers.registry.provider_config_from_appdaemon_args"), \
             patch("providers.ai_providers.registry.build_image_provider") as mock_build:
            mock_build.return_value = mock_provider

            _run_async(app._handle_generate_image({"id": "gen-id", "prompt": "test prompt"}))

        # generating flag should be cleared even on error
        assert not app._countdowns[0].get("generating", False)

        error_calls = [
            c for c in app.log.call_args_list if c[1].get("level") == "ERROR"
        ]
        assert len(error_calls) >= 1

    def test_generate_image_missing_id(self):
        app, _ = self._make_app_with_countdown()
        app.log.reset_mock()

        _run_async(app._handle_generate_image({"prompt": "test prompt"}))

        warning_calls = [
            c for c in app.log.call_args_list if c[1].get("level") == "WARNING"
        ]
        assert len(warning_calls) >= 1

    def test_generate_image_missing_prompt(self):
        app, _ = self._make_app_with_countdown(
            cd=_make_countdown(id="noprompt-id", image_prompt="")
        )
        app.log.reset_mock()

        _run_async(app._handle_generate_image({"id": "noprompt-id"}))

        warning_calls = [
            c for c in app.log.call_args_list if c[1].get("level") == "WARNING"
        ]
        assert len(warning_calls) >= 1


# ---------------------------------------------------------------------------
# Rotation Tests
# ---------------------------------------------------------------------------


class TestRotation:
    def test_rotation_tick_advances_index(self):
        app = _make_app()
        future = (datetime.datetime.now() + datetime.timedelta(days=5)).isoformat()
        cd1 = _make_countdown(id="id-1", target_datetime=future)
        cd2 = _make_countdown(id="id-2", target_datetime=future)
        app._countdowns = [cd1, cd2]
        app._active_index = 0

        app._on_rotation_tick({})

        assert app._active_index == 1

    def test_rotation_tick_wraps_around(self):
        app = _make_app()
        future = (datetime.datetime.now() + datetime.timedelta(days=5)).isoformat()
        cd1 = _make_countdown(id="id-1", target_datetime=future)
        cd2 = _make_countdown(id="id-2", target_datetime=future)
        app._countdowns = [cd1, cd2]
        app._active_index = 1  # already at last

        app._on_rotation_tick({})

        assert app._active_index == 0

    def test_rotation_empty_countdowns(self):
        app = _make_app()
        app._countdowns = []
        app._active_index = 0

        # Should not raise
        app._on_rotation_tick({})

        assert app._active_index == 0
