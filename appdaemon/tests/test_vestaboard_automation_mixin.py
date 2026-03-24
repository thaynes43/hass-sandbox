"""Tests for VestaboardAutomation mixin (event-based communication)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, call, patch

# Mock hassapi before importing
mock_hass = MagicMock()
mock_hass.Hass = type("_MockHass", (), {"__init__": lambda self, *a, **kw: None})
sys.modules["hassapi"] = mock_hass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))

from vestaboard_apps._shared.base import VestaboardAutomation


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _blank_grid() -> list[list[int]]:
    return [[0] * 22 for _ in range(6)]


def _test_grid() -> list[list[int]]:
    grid = [[0] * 22 for _ in range(6)]
    grid[0][0] = 1
    return grid


class ConcreteAutomation(mock_hass.Hass, VestaboardAutomation):
    """Minimal concrete subclass for testing the mixin."""

    automation_type = "test_type"
    display_name = "Test Automation"
    display_description = "A test automation."
    default_ttl_s = 60
    default_max_age_s = None
    default_should_expire = False
    DEFAULT_UI_CONFIG = {"enabled": True, "ttl_minutes": 1}

    @classmethod
    def get_config_schema(cls) -> dict:
        return {"enabled": {"type": "bool"}}

    def get_preview_frame(self) -> list[list[int]]:
        return _test_grid()

    async def generate_frame(self, **kwargs) -> list[list[int]]:
        return _test_grid()


def _make_automation(
    name: str = "my_automation",
    extra_args: dict | None = None,
) -> ConcreteAutomation:
    """Create a minimal ConcreteAutomation with AppDaemon methods mocked."""
    auto = ConcreteAutomation.__new__(ConcreteAutomation)

    auto.name = name
    auto.args = extra_args or {}

    auto.log = MagicMock()
    auto.fire_event = MagicMock()
    auto.listen_event = MagicMock(return_value="event-handle")
    auto.cancel_listen_event = MagicMock()
    auto.run_in = MagicMock(return_value="timer-handle")
    auto.cancel_timer = MagicMock()
    auto.create_task = MagicMock(side_effect=lambda coro: _run(coro))
    auto.set_state = MagicMock()

    # Reset mixin state
    auto._event_handles = []
    auto._random_interval_handle = None
    auto._next_fire_time = None

    return auto


# ---------------------------------------------------------------------------
# Registration tests
# ---------------------------------------------------------------------------

class TestRegisterWithController:
    def test_register_fires_register_automation_event(self):
        auto = _make_automation()
        auto.register_with_controller()

        fire_calls = [
            c for c in auto.fire_event.call_args_list
            if c[0] and c[0][0] == "vestaboard_controller_command"
        ]
        assert len(fire_calls) == 1
        assert fire_calls[0][1]["command"] == "register_automation"

    def test_register_payload_contains_metadata(self):
        auto = _make_automation("calendar_clock_dev")
        auto.register_with_controller()

        fire_call = auto.fire_event.call_args_list[-1]
        raw_payload = fire_call[1]["payload"]
        payload = json.loads(raw_payload)

        assert payload["automation_id"] == "calendar_clock_dev"
        assert payload["automation_type"] == "test_type"
        assert payload["display_name"] == "Test Automation"
        assert payload["display_description"] == "A test automation."
        assert payload["default_ttl_s"] == 60
        assert payload["default_should_expire"] is False
        assert payload["DEFAULT_UI_CONFIG"] == {"enabled": True, "ttl_minutes": 1}

    def test_register_payload_preview_frame_is_json_string(self):
        auto = _make_automation()
        auto.register_with_controller()

        fire_call = auto.fire_event.call_args_list[-1]
        payload = json.loads(fire_call[1]["payload"])
        # preview_frame must be a JSON string (not a raw list) to avoid HA zero-stripping
        assert isinstance(payload["preview_frame"], str)
        assert json.loads(payload["preview_frame"]) == _test_grid()

    def test_register_payload_config_schema_included(self):
        auto = _make_automation()
        auto.register_with_controller()

        fire_call = auto.fire_event.call_args_list[-1]
        payload = json.loads(fire_call[1]["payload"])
        assert "config_schema" in payload
        assert payload["config_schema"] == {"enabled": {"type": "bool"}}

    def test_register_sets_up_four_event_listeners(self):
        auto = _make_automation("my_automation")
        auto.register_with_controller()

        # Should have registered 4 listeners:
        # config, enabled, generate, controller_ready
        assert auto.listen_event.call_count == 4
        event_names = [c[0][1] for c in auto.listen_event.call_args_list]
        assert "vb_auto_config" in event_names
        assert "vb_auto_enabled" in event_names
        assert "vb_auto_generate" in event_names
        assert "vestaboard_controller_ready" in event_names

    def test_register_stores_event_handles(self):
        auto = _make_automation()
        auto.register_with_controller()

        # 4 listeners registered → 4 handles stored
        assert len(auto._event_handles) == 4

    def test_register_logs_info(self):
        auto = _make_automation("test_auto")
        auto.register_with_controller()

        info_calls = [c for c in auto.log.call_args_list if "INFO" in str(c)]
        assert any("test_auto" in str(c) and "register" in str(c).lower() for c in info_calls)


# ---------------------------------------------------------------------------
# Deregistration tests
# ---------------------------------------------------------------------------

class TestDeregisterFromController:
    def test_deregister_fires_deregister_event(self):
        auto = _make_automation("my_automation")
        auto.register_with_controller()
        auto.fire_event.reset_mock()

        auto.deregister_from_controller()

        fire_calls = [
            c for c in auto.fire_event.call_args_list
            if c[0] and c[0][0] == "vestaboard_controller_command"
        ]
        assert len(fire_calls) == 1
        assert fire_calls[0][1]["command"] == "deregister_automation"

    def test_deregister_payload_contains_automation_id(self):
        auto = _make_automation("my_automation")
        auto.register_with_controller()
        auto.fire_event.reset_mock()

        auto.deregister_from_controller()

        fire_call = auto.fire_event.call_args
        payload = json.loads(fire_call[1]["payload"])
        assert payload["automation_id"] == "my_automation"

    def test_deregister_cancels_all_listeners(self):
        auto = _make_automation()
        auto.register_with_controller()
        assert len(auto._event_handles) == 4

        auto.deregister_from_controller()

        assert auto.cancel_listen_event.call_count == 4
        assert auto._event_handles == []

    def test_deregister_logs_info(self):
        auto = _make_automation("my_automation")
        auto.register_with_controller()
        auto.log.reset_mock()

        auto.deregister_from_controller()

        info_calls = [c for c in auto.log.call_args_list if "INFO" in str(c)]
        assert any("my_automation" in str(c) and "deregist" in str(c).lower() for c in info_calls)

    def test_deregister_without_prior_register_does_not_raise(self):
        auto = _make_automation()
        # No register call — deregister should not raise
        auto.deregister_from_controller()


# ---------------------------------------------------------------------------
# push_frame tests
# ---------------------------------------------------------------------------

class TestPushFrame:
    def test_push_frame_fires_push_automation_frame_command(self):
        auto = _make_automation("my_automation")
        auto.push_frame(_test_grid(), ttl_s=60, should_expire=True)

        fire_calls = [
            c for c in auto.fire_event.call_args_list
            if c[0] and c[0][0] == "vestaboard_controller_command"
        ]
        assert len(fire_calls) == 1
        assert fire_calls[0][1]["command"] == "push_automation_frame"

    def test_push_frame_payload_has_correct_fields(self):
        auto = _make_automation("my_automation")
        grid = _test_grid()
        auto.push_frame(grid, ttl_s=90, max_age_s=300, override_ttl=True, should_expire=True)

        fire_call = auto.fire_event.call_args
        payload = json.loads(fire_call[1]["payload"])

        assert payload["automation_id"] == "my_automation"
        assert payload["source_label"] == "Test Automation"
        assert payload["ttl_s"] == 90
        assert payload["max_age_s"] == 300
        assert payload["override_ttl"] is True
        assert payload["should_expire"] is True

    def test_push_frame_characters_is_json_string(self):
        """Grid must be JSON-stringified to avoid HA zero-stripping."""
        auto = _make_automation("my_automation")
        grid = _test_grid()
        auto.push_frame(grid)

        fire_call = auto.fire_event.call_args
        payload = json.loads(fire_call[1]["payload"])

        assert isinstance(payload["characters"], str)
        assert json.loads(payload["characters"]) == grid

    def test_push_frame_includes_next_fire_time_when_set(self):
        auto = _make_automation("my_automation")
        auto._next_fire_time = 12345.0
        auto.push_frame(_test_grid())

        fire_call = auto.fire_event.call_args
        payload = json.loads(fire_call[1]["payload"])
        assert payload["next_fire_time"] == 12345.0

    def test_push_frame_omits_next_fire_time_when_none(self):
        auto = _make_automation("my_automation")
        auto._next_fire_time = None
        auto.push_frame(_test_grid())

        fire_call = auto.fire_event.call_args
        payload = json.loads(fire_call[1]["payload"])
        assert "next_fire_time" not in payload

    def test_push_frame_logs_info(self):
        auto = _make_automation("my_automation")
        auto.push_frame(_test_grid(), ttl_s=60)

        info_calls = [c for c in auto.log.call_args_list if "INFO" in str(c)]
        assert any("my_automation" in str(c) and "push_frame" in str(c).lower() for c in info_calls)


# ---------------------------------------------------------------------------
# Config event handler tests
# ---------------------------------------------------------------------------

class TestOnConfigEvent:
    def test_on_config_event_calls_on_config_updated(self):
        auto = _make_automation()
        auto.on_config_updated = MagicMock()

        config = {"ttl_minutes": 10, "enabled": False}
        auto._on_config_event("vb_auto_config", {"automation_id": "my_automation", "config": config}, {})

        auto.on_config_updated.assert_called_once_with(config)

    def test_on_config_event_updates_args(self):
        auto = _make_automation(extra_args={"enabled": True})
        config = {"enabled": False, "ttl_minutes": 20}
        auto._on_config_event("vb_auto_config", {"automation_id": "my_automation", "config": config}, {})

        assert auto.args["enabled"] is False
        assert auto.args["ttl_minutes"] == 20

    def test_on_config_event_handles_missing_config_key(self):
        auto = _make_automation()
        auto.on_config_updated = MagicMock()

        # Data with no 'config' key
        auto._on_config_event("vb_auto_config", {"automation_id": "my_automation"}, {})

        auto.on_config_updated.assert_called_once_with({})


# ---------------------------------------------------------------------------
# Enabled event handler tests
# ---------------------------------------------------------------------------

class TestOnEnabledEvent:
    def test_on_enabled_event_calls_set_enabled_true(self):
        auto = _make_automation()
        auto.set_enabled = MagicMock()

        auto._on_enabled_event("vb_auto_enabled", {"automation_id": "my_automation", "enabled": True}, {})

        auto.set_enabled.assert_called_once_with(True)

    def test_on_enabled_event_calls_set_enabled_false(self):
        auto = _make_automation()
        auto.set_enabled = MagicMock()

        auto._on_enabled_event("vb_auto_enabled", {"automation_id": "my_automation", "enabled": False}, {})

        auto.set_enabled.assert_called_once_with(False)

    def test_on_enabled_event_defaults_to_true(self):
        auto = _make_automation()
        auto.set_enabled = MagicMock()

        # No 'enabled' key → defaults to True
        auto._on_enabled_event("vb_auto_enabled", {"automation_id": "my_automation"}, {})

        auto.set_enabled.assert_called_once_with(True)

    def test_on_enabled_event_logs_info(self):
        auto = _make_automation("my_automation")
        auto.set_enabled = MagicMock()
        auto._on_enabled_event("vb_auto_enabled", {"automation_id": "my_automation", "enabled": True}, {})

        info_calls = [c for c in auto.log.call_args_list if "INFO" in str(c)]
        assert any("enabled" in str(c).lower() for c in info_calls)


# ---------------------------------------------------------------------------
# Generate event handler tests
# ---------------------------------------------------------------------------

class TestOnGenerateEvent:
    def test_on_generate_event_calls_generate_frame(self):
        auto = _make_automation("my_automation")
        auto.generate_frame = AsyncMock(return_value=_test_grid())
        auto.push_frame = MagicMock()

        auto._on_generate_event(
            "vb_auto_generate",
            {"automation_id": "my_automation", "generate_kwargs": {}, "preview_only": False},
            {},
        )

        auto.generate_frame.assert_called_once()

    def test_on_generate_event_pushes_frame_when_not_preview(self):
        auto = _make_automation("my_automation")
        auto.generate_frame = AsyncMock(return_value=_test_grid())
        auto.push_frame = MagicMock()

        auto._on_generate_event(
            "vb_auto_generate",
            {"automation_id": "my_automation", "generate_kwargs": {"override_ttl": True}, "preview_only": False},
            {},
        )

        auto.push_frame.assert_called_once()

    def test_on_generate_event_fires_preview_result_when_preview_only(self):
        auto = _make_automation("my_automation")
        auto.generate_frame = AsyncMock(return_value=_test_grid())
        auto.push_frame = MagicMock()

        auto._on_generate_event(
            "vb_auto_generate",
            {"automation_id": "my_automation", "generate_kwargs": {"subject": "cats"}, "preview_only": True},
            {},
        )

        # Should not push to board
        auto.push_frame.assert_not_called()

        # Should fire preview result event
        fire_calls = [
            c for c in auto.fire_event.call_args_list
            if c[0] and c[0][0] == "vestaboard_controller_command"
        ]
        assert len(fire_calls) == 1
        assert fire_calls[0][1]["command"] == "push_ai_art_preview_result"

    def test_on_generate_event_preview_payload_has_json_characters(self):
        auto = _make_automation("my_automation")
        grid = _test_grid()
        auto.generate_frame = AsyncMock(return_value=grid)
        auto.push_frame = MagicMock()

        auto._on_generate_event(
            "vb_auto_generate",
            {"automation_id": "my_automation", "generate_kwargs": {"subject": "stars"}, "preview_only": True},
            {},
        )

        fire_call = auto.fire_event.call_args
        payload = json.loads(fire_call[1]["payload"])
        assert isinstance(payload["characters"], str)
        assert json.loads(payload["characters"]) == grid
        assert payload["subject"] == "stars"

    def test_on_generate_event_skips_blank_grid_push(self):
        auto = _make_automation("my_automation")
        auto.generate_frame = AsyncMock(return_value=_blank_grid())
        auto.push_frame = MagicMock()

        auto._on_generate_event(
            "vb_auto_generate",
            {"automation_id": "my_automation", "generate_kwargs": {}, "preview_only": False},
            {},
        )

        auto.push_frame.assert_not_called()

    def test_on_generate_event_handles_generate_frame_error(self):
        auto = _make_automation("my_automation")
        auto.generate_frame = AsyncMock(side_effect=RuntimeError("AI call failed"))
        auto.push_frame = MagicMock()

        # Should not raise
        auto._on_generate_event(
            "vb_auto_generate",
            {"automation_id": "my_automation", "generate_kwargs": {}, "preview_only": False},
            {},
        )

        auto.push_frame.assert_not_called()
        error_calls = [c for c in auto.log.call_args_list if "ERROR" in str(c)]
        assert len(error_calls) >= 1

    def test_on_generate_event_passes_kwargs_to_generate_frame(self):
        auto = _make_automation("my_automation")
        auto.generate_frame = AsyncMock(return_value=_test_grid())
        auto.push_frame = MagicMock()

        auto._on_generate_event(
            "vb_auto_generate",
            {"automation_id": "my_automation", "generate_kwargs": {"subject": "ocean"}, "preview_only": False},
            {},
        )

        auto.generate_frame.assert_called_once_with(subject="ocean")


# ---------------------------------------------------------------------------
# Controller ready event handler tests
# ---------------------------------------------------------------------------

class TestOnControllerReady:
    def test_on_controller_ready_re_fires_register_event(self):
        auto = _make_automation("my_automation")
        auto.register_with_controller()
        auto.fire_event.reset_mock()

        auto._on_controller_ready("vestaboard_controller_ready", {}, {})

        fire_calls = [
            c for c in auto.fire_event.call_args_list
            if c[0] and c[0][0] == "vestaboard_controller_command"
            and c[1].get("command") == "register_automation"
        ]
        assert len(fire_calls) == 1

    def test_on_controller_ready_logs_info(self):
        auto = _make_automation("my_automation")
        auto.register_with_controller()
        auto.log.reset_mock()

        auto._on_controller_ready("vestaboard_controller_ready", {}, {})

        info_calls = [c for c in auto.log.call_args_list if "INFO" in str(c)]
        assert any("my_automation" in str(c) and "re-register" in str(c).lower() for c in info_calls)


# ---------------------------------------------------------------------------
# Config helpers tests
# ---------------------------------------------------------------------------

class TestConfigHelpers:
    def test_get_resolved_ttl_s_from_args(self):
        auto = _make_automation(extra_args={"ttl_minutes": 5})
        assert auto.get_resolved_ttl_s() == 300

    def test_get_resolved_ttl_s_falls_back_to_default(self):
        auto = _make_automation()
        assert auto.get_resolved_ttl_s() == 60  # default_ttl_s = 60

    def test_get_resolved_should_expire_from_args(self):
        auto = _make_automation(extra_args={"should_expire": True})
        assert auto.get_resolved_should_expire() is True

    def test_get_resolved_should_expire_falls_back_to_default(self):
        auto = _make_automation()
        assert auto.get_resolved_should_expire() is False

    def test_get_effective_config_strips_internal_keys(self):
        auto = _make_automation(extra_args={
            "module": "some.module",
            "class": "SomeClass",
            "dependencies": ["other_app"],
            "controller_app": "vestaboard_controller",
            "ttl_minutes": 5,
        })
        cfg = auto.get_effective_config()
        assert "module" not in cfg
        assert "class" not in cfg
        assert "dependencies" not in cfg
        assert "controller_app" not in cfg
        assert cfg["ttl_minutes"] == 5

    def test_on_config_updated_merges_into_args(self):
        auto = _make_automation(extra_args={"enabled": True, "ttl_minutes": 1})
        auto.on_config_updated({"ttl_minutes": 5, "should_expire": True})

        assert auto.args["ttl_minutes"] == 5
        assert auto.args["should_expire"] is True
        assert auto.args["enabled"] is True  # unchanged

    def test_on_config_updated_no_op_when_args_not_dict(self):
        auto = _make_automation()
        auto.args = None
        # Should not raise
        auto.on_config_updated({"ttl_minutes": 5})


# ---------------------------------------------------------------------------
# _generate_and_push helper tests
# ---------------------------------------------------------------------------

class TestGenerateAndPush:
    def test_generate_and_push_calls_push_frame(self):
        auto = _make_automation("my_automation")
        auto.generate_frame = AsyncMock(return_value=_test_grid())
        auto.push_frame = MagicMock()

        _run(auto._generate_and_push())

        auto.push_frame.assert_called_once()

    def test_generate_and_push_skips_blank_grid(self):
        auto = _make_automation("my_automation")
        auto.generate_frame = AsyncMock(return_value=_blank_grid())
        auto.push_frame = MagicMock()

        _run(auto._generate_and_push())

        auto.push_frame.assert_not_called()

    def test_generate_and_push_handles_exception(self):
        auto = _make_automation("my_automation")
        auto.generate_frame = AsyncMock(side_effect=RuntimeError("something broke"))
        auto.push_frame = MagicMock()

        # Must not raise
        _run(auto._generate_and_push())

        auto.push_frame.assert_not_called()
        error_calls = [c for c in auto.log.call_args_list if "ERROR" in str(c)]
        assert len(error_calls) >= 1
