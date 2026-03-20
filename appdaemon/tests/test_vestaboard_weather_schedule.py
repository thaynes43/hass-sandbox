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
    _build_temp_row,
    _build_detail_row,
    _temp_color,
    _wind_tiles,
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
        "weather_entity": "weather.test_forecast",
        "ha_url_env": "HA_URL",
        "ha_token_env": "TOKEN",
        "time_list": ["07:30:00", "15:00:00"],
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


def _mock_weather_state(condition="sunny", temperature=72, humidity=45, wind_speed=8):
    return {
        "state": condition,
        "attributes": {
            "temperature": temperature,
            "humidity": humidity,
            "wind_speed": wind_speed,
            "temperature_unit": "°F",
        },
    }


# ---------------------------------------------------------------------------
# Tests — grid helpers
# ---------------------------------------------------------------------------

class TestTempColor:
    def test_freezing(self):
        assert _temp_color(20) == COLOR_CODES["blue"]

    def test_cold(self):
        assert _temp_color(40) == COLOR_CODES["blue"]

    def test_mild(self):
        assert _temp_color(55) == COLOR_CODES["yellow"]

    def test_warm(self):
        assert _temp_color(75) == COLOR_CODES["orange"]

    def test_hot(self):
        assert _temp_color(95) == COLOR_CODES["red"]


class TestWindTiles:
    def test_calm(self):
        tiles = _wind_tiles(3)
        assert len(tiles) == 1
        assert tiles[0] == COLOR_CODES["white"]

    def test_breeze(self):
        tiles = _wind_tiles(12)
        assert len(tiles) == 2
        assert all(t == COLOR_CODES["yellow"] for t in tiles)

    def test_moderate(self):
        tiles = _wind_tiles(20)
        assert len(tiles) == 3
        assert all(t == COLOR_CODES["orange"] for t in tiles)

    def test_high_wind(self):
        tiles = _wind_tiles(30)
        assert len(tiles) == 4
        assert all(t == COLOR_CODES["red"] for t in tiles)


class TestBuildTempRow:
    def test_all_temps(self):
        row = _build_temp_row(72, 55, 68)
        assert len(row) == COLS
        assert any(cell != 0 for cell in row)

    def test_none_temps(self):
        row = _build_temp_row(None, None, None)
        assert len(row) == COLS
        assert all(cell == 0 for cell in row)

    def test_partial_temps(self):
        row = _build_temp_row(72, None, 68)
        assert len(row) == COLS
        assert any(cell != 0 for cell in row)


class TestBuildDetailRow:
    def test_humidity_and_wind(self):
        row = _build_detail_row(50, 12.0)
        assert len(row) == COLS
        assert any(cell != 0 for cell in row)

    def test_humidity_only(self):
        row = _build_detail_row(50, None)
        assert len(row) == COLS
        assert any(cell != 0 for cell in row)

    def test_wind_only(self):
        row = _build_detail_row(None, 12.0)
        assert len(row) == COLS
        assert any(cell != 0 for cell in row)

    def test_nothing(self):
        row = _build_detail_row(None, None)
        assert len(row) == COLS
        assert all(cell == 0 for cell in row)


# ---------------------------------------------------------------------------
# Tests — generate_frame
# ---------------------------------------------------------------------------

class TestGenerateFrame:
    def _patch_forecast(self, app):
        """Patch _fetch_daily_high_low to avoid real REST calls."""
        app._fetch_daily_high_low = AsyncMock(return_value=(80, 55))

    def test_generates_weather_grid(self):
        app = _make_app()
        app.get_state = MagicMock(return_value=_mock_weather_state())
        self._patch_forecast(app)

        grid = _run(app.generate_frame())
        assert len(grid) == 6
        assert all(len(row) == 22 for row in grid)

    def test_row0_is_color_bar(self):
        app = _make_app()
        app.get_state = MagicMock(return_value=_mock_weather_state("sunny"))
        self._patch_forecast(app)

        grid = _run(app.generate_frame())
        expected_color = _CONDITION_COLORS["sunny"]
        assert all(cell == expected_color for cell in grid[0])

    def test_row1_has_condition_label(self):
        app = _make_app()
        app.get_state = MagicMock(return_value=_mock_weather_state("cloudy"))
        self._patch_forecast(app)

        grid = _run(app.generate_frame())
        assert any(cell != 0 for cell in grid[1])

    def test_row2_has_temps_with_color_tiles(self):
        app = _make_app()
        app.get_state = MagicMock(return_value=_mock_weather_state(temperature=72))
        self._patch_forecast(app)

        grid = _run(app.generate_frame())
        assert any(cell != 0 for cell in grid[2])
        # Should contain color tile codes (63-70 range)
        has_color = any(63 <= cell <= 70 for cell in grid[2])
        assert has_color

    def test_row3_has_humidity_and_wind(self):
        app = _make_app()
        app.get_state = MagicMock(return_value=_mock_weather_state(humidity=60, wind_speed=12))
        self._patch_forecast(app)

        grid = _run(app.generate_frame())
        assert any(cell != 0 for cell in grid[3])

    def test_row4_is_blank(self):
        app = _make_app()
        app.get_state = MagicMock(return_value=_mock_weather_state())
        self._patch_forecast(app)

        grid = _run(app.generate_frame())
        assert all(cell == 0 for cell in grid[4])

    def test_row5_has_time(self):
        app = _make_app()
        app.get_state = MagicMock(return_value=_mock_weather_state())
        self._patch_forecast(app)

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

    def test_rainy_condition_uses_blue_bar(self):
        app = _make_app()
        app.get_state = MagicMock(return_value=_mock_weather_state("rainy"))
        self._patch_forecast(app)

        grid = _run(app.generate_frame())
        assert all(cell == COLOR_CODES["blue"] for cell in grid[0])

    def test_no_forecast_still_shows_current(self):
        """When daily forecast fails, still show current temp without high/low."""
        app = _make_app()
        app.get_state = MagicMock(return_value=_mock_weather_state(temperature=45))
        app._fetch_daily_high_low = AsyncMock(return_value=(None, None))

        grid = _run(app.generate_frame())
        assert any(cell != 0 for cell in grid[2])


# ---------------------------------------------------------------------------
# Tests — daily scheduling
# ---------------------------------------------------------------------------

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
        app.args["enabled"] = True
        app._daily_handles = ["h1"]
        app._register_daily_timers = MagicMock()
        app._cancel_daily_timers = MagicMock()

        app.on_config_updated({"time_list": ["08:00:00"]})
        app._cancel_daily_timers.assert_called_once()
        app._register_daily_timers.assert_called_once()


# ---------------------------------------------------------------------------
# Tests — config schema
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Tests — preview frame
# ---------------------------------------------------------------------------

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

    def test_preview_has_temp_tiles(self):
        app = _make_app()
        grid = app.get_preview_frame()
        # Row 2 should have color tiles
        has_color = any(63 <= cell <= 70 for cell in grid[2])
        assert has_color


# ---------------------------------------------------------------------------
# Tests — condition mappings
# ---------------------------------------------------------------------------

class TestConditionMappings:
    def test_all_conditions_have_colors(self):
        for condition in _CONDITION_COLORS:
            assert isinstance(_CONDITION_COLORS[condition], int)

    def test_all_conditions_have_labels(self):
        for condition in _CONDITION_LABELS:
            assert isinstance(_CONDITION_LABELS[condition], str)
            assert len(_CONDITION_LABELS[condition]) > 0
