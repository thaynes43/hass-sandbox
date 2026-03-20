"""Tests for AiArtGeneratorApp art_prompt_bundles feature."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

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


def _make_app(args: dict | None = None):
    """Create an AiArtGeneratorApp instance with AppDaemon methods mocked."""
    from vestaboard_apps.automations.ai_art_generator.ai_art_generator_app import (
        AiArtGeneratorApp,
    )

    app = AiArtGeneratorApp.__new__(AiArtGeneratorApp)
    app.name = "test_ai_art_generator"
    app.args = args or {}
    app.log = MagicMock()
    app.fire_event = MagicMock()
    app.listen_event = MagicMock(return_value="handle")
    app.cancel_listen_event = MagicMock()
    app.run_in = MagicMock(return_value="timer-handle")
    app.get_state = MagicMock(return_value=None)
    app.create_task = MagicMock()
    # Initialize the same way initialize() does
    app._art_bundles_path = app.args.get("art_prompt_bundles_path")
    app._art_bundles_missing_warned = False
    # Stub the random-interval helpers used by the mixin
    app._schedule_random_interval = MagicMock()
    app._cancel_random_interval = MagicMock()
    app._clear_random_interval_handle = MagicMock()
    return app


_SAMPLE_ART_BUNDLES_YAML = """\
- subject: "a sunset over the ocean"
- subject: "a cat sleeping on a keyboard"
- subject: "weather-inspired pixel art"
  entities:
    - entity_id: "weather.forecast_home"
      description: "current weather condition and temperature"
"""

_MALFORMED_ART_BUNDLES_YAML = """\
- subject: "valid subject"
- missing_subject_key: true
- 42
- subject: "another valid subject"
"""


# ---------------------------------------------------------------------------
# _load_art_bundles
# ---------------------------------------------------------------------------


class TestLoadArtBundles:
    def test_returns_empty_when_no_path_configured(self):
        app = _make_app({})
        assert app._load_art_bundles() == []

    def test_loads_valid_yaml_file(self):
        app = _make_app({"art_prompt_bundles_path": "/fake/art-bundles.yaml"})
        with patch("builtins.open", mock_open(read_data=_SAMPLE_ART_BUNDLES_YAML)):
            bundles = app._load_art_bundles()
        assert len(bundles) == 3
        assert bundles[0]["subject"] == "a sunset over the ocean"
        assert bundles[2]["subject"] == "weather-inspired pixel art"
        assert len(bundles[2]["entities"]) == 1

    def test_returns_empty_on_missing_file(self):
        app = _make_app({"art_prompt_bundles_path": "/fake/missing.yaml"})
        with patch("builtins.open", side_effect=FileNotFoundError):
            bundles = app._load_art_bundles()
        assert bundles == []
        assert any("not found" in str(c) for c in app.log.call_args_list)

    def test_missing_file_warns_only_once(self):
        app = _make_app({"art_prompt_bundles_path": "/fake/missing.yaml"})
        with patch("builtins.open", side_effect=FileNotFoundError):
            app._load_art_bundles()
            app._load_art_bundles()
        warning_calls = [c for c in app.log.call_args_list if "not found" in str(c)]
        assert len(warning_calls) == 1

    def test_returns_empty_when_yaml_is_not_a_list(self):
        app = _make_app({"art_prompt_bundles_path": "/fake/dict.yaml"})
        with patch("builtins.open", mock_open(read_data="key: value\n")):
            bundles = app._load_art_bundles()
        assert bundles == []
        assert any("not a YAML list" in str(c) for c in app.log.call_args_list)

    def test_skips_malformed_entries_keeps_valid(self):
        app = _make_app({"art_prompt_bundles_path": "/fake/mixed.yaml"})
        with patch("builtins.open", mock_open(read_data=_MALFORMED_ART_BUNDLES_YAML)):
            bundles = app._load_art_bundles()
        assert len(bundles) == 2
        assert bundles[0]["subject"] == "valid subject"
        assert bundles[1]["subject"] == "another valid subject"

    def test_logs_bundle_count_at_info(self):
        app = _make_app({"art_prompt_bundles_path": "/fake/art-bundles.yaml"})
        with patch("builtins.open", mock_open(read_data=_SAMPLE_ART_BUNDLES_YAML)):
            app._load_art_bundles()
        info_calls = [c for c in app.log.call_args_list if "INFO" in str(c)]
        assert any("3" in str(c) and "bundle" in str(c).lower() for c in info_calls)


# ---------------------------------------------------------------------------
# _pick_art_bundle
# ---------------------------------------------------------------------------


class TestPickArtBundle:
    def test_returns_none_when_no_path(self):
        app = _make_app({})
        assert app._pick_art_bundle() is None

    def test_returns_none_when_file_missing(self):
        app = _make_app({"art_prompt_bundles_path": "/fake/missing.yaml"})
        with patch("builtins.open", side_effect=FileNotFoundError):
            assert app._pick_art_bundle() is None

    def test_returns_single_bundle(self):
        yaml_data = '- subject: "a rocket"\n'
        app = _make_app({"art_prompt_bundles_path": "/fake/art-bundles.yaml"})
        with patch("builtins.open", mock_open(read_data=yaml_data)):
            result = app._pick_art_bundle()
        assert result["subject"] == "a rocket"

    def test_returns_one_of_multiple_bundles(self):
        app = _make_app({"art_prompt_bundles_path": "/fake/art-bundles.yaml"})
        with patch("builtins.open", mock_open(read_data=_SAMPLE_ART_BUNDLES_YAML)):
            results = set()
            for _ in range(50):
                result = app._pick_art_bundle()
                results.add(result["subject"])
            assert len(results) >= 1


# ---------------------------------------------------------------------------
# _build_art_subject
# ---------------------------------------------------------------------------


class TestBuildArtSubject:
    def test_subject_only_bundle(self):
        app = _make_app()
        bundle = {"subject": "a sunset over the ocean"}
        result = _run(app._build_art_subject(bundle))
        assert result == "a sunset over the ocean"

    def test_bundle_with_entities(self):
        app = _make_app()
        app.get_state = MagicMock(return_value="sunny")

        bundle = {
            "subject": "weather-inspired pixel art",
            "entities": [
                {"entity_id": "weather.forecast_home", "description": "current weather"}
            ],
        }

        result = _run(app._build_art_subject(bundle))
        assert "weather-inspired pixel art" in result
        assert "current weather: sunny" in result

    def test_bundle_with_unavailable_entity(self):
        app = _make_app()
        app.get_state = MagicMock(return_value=None)

        bundle = {
            "subject": "data art",
            "entities": [
                {"entity_id": "sensor.missing", "description": "missing sensor"}
            ],
        }

        result = _run(app._build_art_subject(bundle))
        assert "missing sensor: N/A" in result

    def test_logs_bundle_selection(self):
        app = _make_app()
        bundle = {"subject": "a cat"}
        _run(app._build_art_subject(bundle))
        assert any("a cat" in str(c) for c in app.log.call_args_list)


# ---------------------------------------------------------------------------
# _on_random_fire with bundles
# ---------------------------------------------------------------------------


class TestOnRandomFireWithBundles:
    def test_uses_bundle_when_available(self):
        app = _make_app({"art_prompt_bundles_path": "/fake/art-bundles.yaml"})
        bundle = {"subject": "a rocket"}
        with patch.object(app, "_pick_art_bundle", return_value=bundle):
            app._on_random_fire({})
        # Should call create_task with the bundle-based coroutine
        app.create_task.assert_called_once()

    def test_falls_back_to_abstract_art_when_no_bundles(self):
        app = _make_app({})
        with patch.object(app, "_pick_art_bundle", return_value=None):
            app._on_random_fire({})
        app.create_task.assert_called_once()


# ---------------------------------------------------------------------------
# generate_frame (existing behavior preserved)
# ---------------------------------------------------------------------------


class TestGenerateFrame:
    def test_no_ai_provider_returns_blank_grid(self):
        app = _make_app({})
        grid = _run(app.generate_frame())
        assert len(grid) == 6
        assert all(len(row) == 22 for row in grid)
        assert all(all(c == 0 for c in row) for row in grid)

    def test_subject_passed_through(self):
        app = _make_app({"ai_provider_conf": {"simple_text": "openai-pixel-art"}})
        valid_grid = [[66] * 22 for _ in range(6)]

        with patch.object(app, "_call_ai", new=AsyncMock(return_value=valid_grid)):
            result = _run(app.generate_frame(subject="a castle"))

        assert result == valid_grid
        assert any("a castle" in str(c) for c in app.log.call_args_list)
