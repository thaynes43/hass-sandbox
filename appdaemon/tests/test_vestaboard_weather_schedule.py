"""Tests for WeatherScheduleApp — frame generation, daily scheduling, config updates."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Mock hassapi before importing
mock_hass_module = MagicMock()
mock_hass_module.Hass = type("_MockHass", (), {"__init__": lambda self, *a, **kw: None})
sys.modules["hassapi"] = mock_hass_module

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))

from vestaboard_apps.automations.weather_schedule.weather_schedule_app import (
    WeatherScheduleApp,
    _center_text_row,
    _CONDITION_COLORS,
    _CONDITION_LABELS,
)
from providers.vestaboard.character_encoding import COLOR_CODES, COLS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_app(extra_args=None):
    ad = MagicMock()
    app = WeatherScheduleApp(ad, MagicMock())

    base_args = {
        "weather_entity": "weather.test_ecobee",
        "time_list": ["07:30:00", "15:00:00"],
        "controller_app": "vestaboard_controller",
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
    app.run_daily = MagicMock(return_value="handle-daily")
    app.run_in = MagicMock()
    app.cancel_timer = MagicMock()
    app.log = MagicMock()
    app.create_task = MagicMock(side_effect=lambda coro: _run(coro))
    app.name = "weather_schedule"
    app.get_app = MagicMock(return_value=None)

    return app


def _mock_weather_state(condition="sunny", temperature=72, humidity=45, apparent_temp=75):
    return {
        "state": condition,
        "attributes": {
            "temperature": temperature,
            "humidity": humidity,
            "apparent_temperature": apparent_temp,
            "temperature_unit": "F",
        },
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestGenerateFrame:
    def test_generates_weather_grid(self):
        app = _make_app()
        app.get_state = MagicMock(return_value=_mock_weather_state())

        grid = _run(app.generate_frame())
        assert len(grid) == 6
        assert all(len(row) == 22 for row in grid)

    def test_row0_is_color_bar(self):
        app = _make_app()
        app.get_state = MagicMock(return_value=_mock_weather_state("sunny"))

        grid = _run(app.generate_frame())
        expected_color = _CONDITION_COLORS["sunny"]
        assert all(cell == expected_color for cell in grid[0])

    def test_row1_has_condition_label(self):
        app = _make_app()
        app.get_state = MagicMock(return_value=_mock_weather_state("cloudy"))

        grid = _run(app.generate_frame())
        # Row 1 should have non-zero cells (the condition text)
        assert any(cell != 0 for cell in grid[1])

    def test_row2_has_temperature(self):
        app = _make_app()
        app.get_state = MagicMock(return_value=_mock_weather_state(temperature=72))

        grid = _run(app.generate_frame())
        assert any(cell != 0 for cell in grid[2])

    def test_row3_has_feels_like(self):
        app = _make_app()
        app.get_state = MagicMock(return_value=_mock_weather_state(apparent_temp=75))

        grid = _run(app.generate_frame())
        assert any(cell != 0 for cell in grid[3])

    def test_row4_is_blank(self):
        app = _make_app()
        app.get_state = MagicMock(return_value=_mock_weather_state())

        grid = _run(app.generate_frame())
        assert all(cell == 0 for cell in grid[4])

    def test_row5_has_time(self):
        app = _make_app()
        app.get_state = MagicMock(return_value=_mock_weather_state())

        grid = _run(app.generate_frame())
        assert any(cell != 0 for cell in grid[5])

    def test_blank_grid_when_no_entity(self):
        app = _make_app({"weather_entity": ""})
        grid = _run(app.generate_frame())
        assert all(all(cell == 0 for cell in row) for row in grid)

    def test_blank_grid_when_state_none(self):
        app = _make_app()
        app.get_state = MagicMock(return_value=None)

        grid = _run(app.generate_frame())
        assert all(all(cell == 0 for cell in row) for row in grid)

    def test_humidity_fallback_when_no_apparent_temp(self):
        app = _make_app()
        state = _mock_weather_state(apparent_temp=None, humidity=60)
        state["attributes"].pop("apparent_temperature")
        app.get_state = MagicMock(return_value=state)

        grid = _run(app.generate_frame())
        # Row 3 should still have content (humidity)
        assert any(cell != 0 for cell in grid[3])

    def test_rainy_condition_uses_blue_bar(self):
        app = _make_app()
        app.get_state = MagicMock(return_value=_mock_weather_state("rainy"))

        grid = _run(app.generate_frame())
        assert all(cell == COLOR_CODES["blue"] for cell in grid[0])


class TestDailyScheduling:
    def test_register_daily_timers_creates_handles(self):
        app = _make_app()
        app._daily_handles = []
        app._register_daily_timers()
        assert app.run_daily.call_count == 2

    def test_cancel_daily_timers_clears_handles(self):
        app = _make_app()
        app._daily_handles = ["h1", "h2"]
        app._cancel_daily_timers()
        assert app._daily_handles == []
        assert app.cancel_timer.call_count == 2

    def test_on_config_updated_reschedules_timers(self):
        app = _make_app()
        app._daily_handles = ["h1"]
        app._register_daily_timers = MagicMock()
        app._cancel_daily_timers = MagicMock()

        app.on_config_updated({"time_list": ["08:00:00"]})
        app._cancel_daily_timers.assert_called_once()
        app._register_daily_timers.assert_called_once()


class TestConfigSchema:
    def test_schema_has_required_fields(self):
        schema = WeatherScheduleApp.get_config_schema()
        assert "enabled" in schema
        assert "ttl_minutes" in schema
        assert "force_push" in schema
        assert "time_list" in schema

    def test_time_list_type(self):
        schema = WeatherScheduleApp.get_config_schema()
        assert schema["time_list"]["type"] == "time_list"


class TestPreviewFrame:
    def test_preview_is_6x22(self):
        app = _make_app()
        grid = app.get_preview_frame()
        assert len(grid) == 6
        assert all(len(row) == 22 for row in grid)

    def test_preview_has_color_bar(self):
        app = _make_app()
        grid = app.get_preview_frame()
        assert all(cell == COLOR_CODES["yellow"] for cell in grid[0])


class TestConditionMappings:
    def test_all_conditions_have_colors(self):
        for condition in _CONDITION_COLORS:
            assert isinstance(_CONDITION_COLORS[condition], int)

    def test_all_conditions_have_labels(self):
        for condition in _CONDITION_LABELS:
            assert isinstance(_CONDITION_LABELS[condition], str)
            assert len(_CONDITION_LABELS[condition]) > 0
