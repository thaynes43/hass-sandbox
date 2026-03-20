"""Tests for AiMessageGeneratorApp prompt_data_bundles feature."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Mock hassapi before any app imports
mock_hass = MagicMock()
mock_hass.Hass = type("_MockHass", (), {"__init__": lambda self, *a, **kw: None})
sys.modules["hassapi"] = mock_hass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _blank_grid():
    return [[0] * 22 for _ in range(6)]


def _make_app(args: dict | None = None):
    """Create an AiMessageGeneratorApp instance with AppDaemon methods mocked."""
    from vestaboard_apps.automations.ai_message_generator.ai_message_generator_app import (
        AiMessageGeneratorApp,
    )

    app = AiMessageGeneratorApp.__new__(AiMessageGeneratorApp)
    app.name = "test_ai_message_generator"
    app.args = args or {}
    app.log = MagicMock()
    app.fire_event = MagicMock()
    app.listen_event = MagicMock(return_value="handle")
    app.cancel_listen_event = MagicMock()
    app.run_in = MagicMock(return_value="timer-handle")
    app.get_state = MagicMock(return_value=None)
    app.create_task = MagicMock()
    # Initialize bundle list the same way initialize() does
    app._prompt_data_bundles = app.args.get("prompt_data_bundles", [])
    # Stub the random-interval helpers used by the mixin
    app._schedule_random_interval = MagicMock()
    app._cancel_random_interval = MagicMock()
    app._clear_random_interval_handle = MagicMock()
    return app


# ---------------------------------------------------------------------------
# _pick_bundle
# ---------------------------------------------------------------------------


class TestPickBundle:
    def test_returns_none_when_no_bundles(self):
        app = _make_app({})
        assert app._pick_bundle() is None

    def test_returns_none_when_bundles_empty_list(self):
        app = _make_app({"prompt_data_bundles": []})
        assert app._pick_bundle() is None

    def test_returns_single_bundle_when_only_one(self):
        bundle = {"description": "energy", "entities": []}
        app = _make_app({"prompt_data_bundles": [bundle]})
        result = app._pick_bundle()
        assert result == bundle

    def test_returns_one_of_multiple_bundles(self):
        bundles = [
            {"description": "energy", "entities": []},
            {"description": "security", "entities": []},
            {"description": "weather", "entities": []},
        ]
        app = _make_app({"prompt_data_bundles": bundles})
        # Run many times — always returns a member of the list
        for _ in range(50):
            result = app._pick_bundle()
            assert result in bundles


# ---------------------------------------------------------------------------
# _build_bundle_prompt_section
# ---------------------------------------------------------------------------


class TestBuildBundlePromptSection:
    def test_formats_single_entity(self):
        app = _make_app()
        app.get_state = MagicMock(return_value="85")

        bundle = {
            "description": "home energy usage",
            "entities": [
                {"entity_id": "sensor.ups_load", "description": "UPS load percentage"}
            ],
        }

        section = _run(app._build_bundle_prompt_section(bundle))

        assert "For this message, write about: home energy usage." in section
        assert "- UPS load percentage: 85" in section

    def test_formats_multiple_entities(self):
        app = _make_app()
        app.get_state = MagicMock(side_effect=lambda eid: {
            "binary_sensor.front_door_motion": "on",
            "binary_sensor.garage_motion": "off",
        }.get(eid))

        bundle = {
            "description": "home security",
            "entities": [
                {"entity_id": "binary_sensor.front_door_motion", "description": "front door motion"},
                {"entity_id": "binary_sensor.garage_motion", "description": "garage motion"},
            ],
        }

        section = _run(app._build_bundle_prompt_section(bundle))

        assert "home security" in section
        assert "- front door motion: on" in section
        assert "- garage motion: off" in section

    def test_unavailable_entity_shows_na(self):
        app = _make_app()
        app.get_state = MagicMock(return_value="unavailable")

        bundle = {
            "description": "sensors",
            "entities": [
                {"entity_id": "sensor.broken", "description": "broken sensor"}
            ],
        }

        section = _run(app._build_bundle_prompt_section(bundle))
        assert "- broken sensor: N/A" in section

    def test_none_entity_state_shows_na(self):
        app = _make_app()
        app.get_state = MagicMock(return_value=None)

        bundle = {
            "description": "sensors",
            "entities": [
                {"entity_id": "sensor.missing", "description": "missing sensor"}
            ],
        }

        section = _run(app._build_bundle_prompt_section(bundle))
        assert "- missing sensor: N/A" in section

    def test_falls_back_to_entity_id_when_no_description(self):
        app = _make_app()
        app.get_state = MagicMock(return_value="42")

        bundle = {
            "description": "test",
            "entities": [{"entity_id": "sensor.no_desc"}],
        }

        section = _run(app._build_bundle_prompt_section(bundle))
        assert "- sensor.no_desc: 42" in section

    def test_logs_bundle_selection_at_info(self):
        app = _make_app()
        app.get_state = MagicMock(return_value="72")

        bundle = {
            "description": "indoor temp",
            "entities": [{"entity_id": "sensor.temp", "description": "temperature"}],
        }

        _run(app._build_bundle_prompt_section(bundle))

        app.log.assert_called_once()
        call_args = app.log.call_args
        assert call_args.kwargs.get("level") == "INFO" or call_args[1].get("level") == "INFO"
        log_msg = call_args[0][0]
        assert "indoor temp" in log_msg
        assert "sensor.temp" in log_msg

    def test_empty_entities_list(self):
        app = _make_app()
        bundle = {"description": "nothing here", "entities": []}
        section = _run(app._build_bundle_prompt_section(bundle))
        assert "For this message, write about: nothing here." in section
        # No data lines
        assert "- " not in section


# ---------------------------------------------------------------------------
# generate_frame with bundles
# ---------------------------------------------------------------------------


class TestGenerateFrameWithBundles:
    def test_no_ai_provider_conf_uses_fallback_regardless_of_bundles(self):
        """When no ai_provider_conf, always use fallback (no AI call)."""
        bundle = {"description": "energy", "entities": []}
        app = _make_app({"prompt_data_bundles": [bundle]})
        grid = _run(app.generate_frame())
        assert len(grid) == 6
        assert all(len(row) == 22 for row in grid)

    def test_with_ai_provider_calls_generate_with_bundle_input(self):
        """When bundles configured, input_text includes bundle section."""
        bundle = {
            "description": "home energy usage",
            "entities": [
                {"entity_id": "sensor.ups_load", "description": "UPS load percentage"}
            ],
        }
        app = _make_app(
            {
                "ai_provider_conf": {"simple_text": "openai-budget"},
                "prompt_data_bundles": [bundle],
            }
        )
        app.get_state = MagicMock(return_value="80")

        fake_grid = [[1] * 22 for _ in range(6)]

        with patch.object(app, "_generate_ai_frame", new=AsyncMock(return_value=fake_grid)) as mock_ai:
            with patch.object(app, "_pick_bundle", return_value=bundle):
                result = _run(app.generate_frame())

        mock_ai.assert_called_once_with({"simple_text": "openai-budget"}, bundle)
        assert result == fake_grid

    def test_without_bundles_passes_none_to_ai_frame(self):
        """With no bundles, bundle=None is passed to _generate_ai_frame."""
        app = _make_app({"ai_provider_conf": {"simple_text": "openai-budget"}})
        fake_grid = [[2] * 22 for _ in range(6)]

        with patch.object(app, "_generate_ai_frame", new=AsyncMock(return_value=fake_grid)) as mock_ai:
            result = _run(app.generate_frame())

        mock_ai.assert_called_once_with({"simple_text": "openai-budget"}, None)
        assert result == fake_grid

    def test_ai_failure_falls_back_to_fallback_frame(self):
        """If AI throws, fallback message grid is returned."""
        bundle = {"description": "energy", "entities": []}
        app = _make_app(
            {
                "ai_provider_conf": {"simple_text": "openai-budget"},
                "prompt_data_bundles": [bundle],
            }
        )

        with patch.object(app, "_generate_ai_frame", new=AsyncMock(side_effect=RuntimeError("boom"))):
            result = _run(app.generate_frame())

        # Should return a 6×22 grid from fallback
        assert len(result) == 6
        assert all(len(row) == 22 for row in result)
        # Warning logged
        app.log.assert_called()
        warning_call = next(
            (c for c in app.log.call_args_list if "WARNING" in str(c)),
            None,
        )
        assert warning_call is not None


# ---------------------------------------------------------------------------
# _generate_ai_frame — bundle vs no-bundle input_text
# ---------------------------------------------------------------------------


class TestGenerateAiFrameInputText:
    """Verify _generate_ai_frame passes the right input_text to the provider."""

    def _make_mock_provider(self, message_text: str):
        provider = MagicMock()
        provider.generate_from_text.return_value = {"message": message_text}
        return provider

    def _patch_registry(self, mock_provider):
        """Patch the registry imports used inside _generate_ai_frame at their source."""
        return [
            patch(
                "providers.ai_providers.registry.build_simple_text_provider",
                return_value=mock_provider,
            ),
            patch(
                "providers.ai_providers.registry.simple_text_config_from_appdaemon_args",
                return_value={},
            ),
        ]

    def test_with_bundle_includes_bundle_section_in_input(self):
        app = _make_app({"ai_provider_conf": {"simple_text": "openai-budget"}})
        app.get_state = MagicMock(return_value="55")
        bundle = {
            "description": "UPS health",
            "entities": [{"entity_id": "sensor.ups_load", "description": "UPS load"}],
        }
        mock_provider = self._make_mock_provider("HELLO WORLD")

        # Patch at the registry module level; _generate_ai_frame imports lazily from there
        import providers.ai_providers.registry as reg
        with patch.object(reg, "build_simple_text_provider", return_value=mock_provider):
            with patch.object(reg, "simple_text_config_from_appdaemon_args", return_value={}):
                _run(app._generate_ai_frame({"simple_text": "openai-budget"}, bundle))

        call_kwargs = mock_provider.generate_from_text.call_args
        input_text = call_kwargs[1]["input_text"] if call_kwargs[1] else call_kwargs[0][0]
        assert "UPS health" in input_text
        assert "UPS load: 55" in input_text

    def test_without_bundle_uses_plain_input(self):
        app = _make_app({"ai_provider_conf": {"simple_text": "openai-budget"}})
        mock_provider = self._make_mock_provider("STAY CURIOUS")

        import providers.ai_providers.registry as reg
        with patch.object(reg, "build_simple_text_provider", return_value=mock_provider):
            with patch.object(reg, "simple_text_config_from_appdaemon_args", return_value={}):
                _run(app._generate_ai_frame({"simple_text": "openai-budget"}, None))

        call_kwargs = mock_provider.generate_from_text.call_args
        input_text = call_kwargs[1]["input_text"] if call_kwargs[1] else call_kwargs[0][0]
        assert input_text == "Generate a witty board message."

    def test_with_bundle_logs_bundle_generation_message(self):
        app = _make_app({"ai_provider_conf": {"simple_text": "openai-budget"}})
        app.get_state = MagicMock(return_value="10")
        bundle = {
            "description": "camera status",
            "entities": [{"entity_id": "binary_sensor.cam", "description": "camera"}],
        }
        mock_provider = self._make_mock_provider("TEST")

        import providers.ai_providers.registry as reg
        with patch.object(reg, "build_simple_text_provider", return_value=mock_provider):
            with patch.object(reg, "simple_text_config_from_appdaemon_args", return_value={}):
                _run(app._generate_ai_frame({"simple_text": "openai-budget"}, bundle))

        log_messages = [str(c) for c in app.log.call_args_list]
        assert any("data bundle" in m for m in log_messages)

    def test_without_bundle_logs_plain_generation_message(self):
        app = _make_app({"ai_provider_conf": {"simple_text": "openai-budget"}})
        mock_provider = self._make_mock_provider("HELLO")

        import providers.ai_providers.registry as reg
        with patch.object(reg, "build_simple_text_provider", return_value=mock_provider):
            with patch.object(reg, "simple_text_config_from_appdaemon_args", return_value={}):
                _run(app._generate_ai_frame({"simple_text": "openai-budget"}, None))

        log_messages = [str(c) for c in app.log.call_args_list]
        assert any("Generating AI message..." in m for m in log_messages)
