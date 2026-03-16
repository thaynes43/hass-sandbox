"""Weather Schedule automation app — displays weather at configured daily times."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[4]))  # adds appdaemon/

import hassapi as hass

from providers.vestaboard.character_encoding import (
    CHAR_TO_CODE,
    COLOR_CODES,
    COLS,
    ROWS,
    blank_grid,
)

from vestaboard_apps._shared.base import VestaboardAutomation

# Weather condition → color code mapping for the top accent bar.
_CONDITION_COLORS: dict[str, int] = {
    "sunny": COLOR_CODES["yellow"],
    "clear-night": COLOR_CODES["blue"],
    "clear": COLOR_CODES["yellow"],
    "partlycloudy": COLOR_CODES["white"],
    "cloudy": COLOR_CODES["white"],
    "rainy": COLOR_CODES["blue"],
    "pouring": COLOR_CODES["blue"],
    "snowy": COLOR_CODES["white"],
    "snowy-rainy": COLOR_CODES["violet"],
    "fog": COLOR_CODES["white"],
    "hail": COLOR_CODES["white"],
    "lightning": COLOR_CODES["yellow"],
    "lightning-rainy": COLOR_CODES["yellow"],
    "windy": COLOR_CODES["green"],
    "windy-variant": COLOR_CODES["green"],
    "exceptional": COLOR_CODES["red"],
}

# Human-readable condition labels (uppercased for the board).
_CONDITION_LABELS: dict[str, str] = {
    "sunny": "SUNNY",
    "clear-night": "CLEAR NIGHT",
    "clear": "CLEAR",
    "partlycloudy": "PARTLY CLOUDY",
    "cloudy": "CLOUDY",
    "rainy": "RAINY",
    "pouring": "HEAVY RAIN",
    "snowy": "SNOWY",
    "snowy-rainy": "SNOW AND RAIN",
    "fog": "FOGGY",
    "hail": "HAIL",
    "lightning": "THUNDERSTORM",
    "lightning-rainy": "THUNDERSTORM",
    "windy": "WINDY",
    "windy-variant": "WINDY",
    "exceptional": "SEVERE WEATHER",
}


def _encode_char(ch: str) -> int:
    if ch == " ":
        return 0
    return CHAR_TO_CODE.get(ch.upper(), 0)


def _center_text_row(text: str, width: int = COLS) -> list[int]:
    padded = text[:width].center(width)
    return [_encode_char(ch) for ch in padded]


class WeatherScheduleApp(hass.Hass, VestaboardAutomation):
    """Automation that displays weather information at configured daily times.

    Reads weather data from a Home Assistant weather entity and displays
    temperature, condition, and humidity on the Vestaboard.

    Config keys:
    - weather_entity: HA weather entity ID (e.g. "weather.first_floor_ecobee").
    - time_list: List of "HH:MM:SS" strings for daily display times.
    - force_push: If True, override active TTL when pushing.
    """

    automation_type = "weather_schedule"
    display_name = "Weather Schedule"
    display_description = "Displays weather conditions at scheduled times."
    default_ttl_s = 3600  # 1 hour
    default_max_age_s = None
    default_should_expire = True

    DEFAULT_UI_CONFIG = {
        "enabled": True,
        "ttl_minutes": 60,
        "should_expire": True,
        "force_push": False,
    }

    _daily_handles: list = []

    @classmethod
    def get_config_schema(cls) -> dict:
        return {
            "enabled": {"type": "bool", "label": "Enabled", "default": True},
            "ttl_minutes": {"type": "int", "label": "TTL (minutes)", "min": 1, "max": 1440, "default": 60},
            "should_expire": {"type": "bool", "label": "Should Expire", "default": True},
            "force_push": {
                "type": "bool",
                "label": "Force Push Frame",
                "description": "Override active TTL to display immediately",
                "default": False,
            },
            "time_list": {
                "type": "time_list",
                "label": "Display Times",
                "description": "HH:MM:SS local times to push weather",
                "default": ["07:30:00", "15:00:00"],
            },
        }

    def get_preview_frame(self) -> list[list[int]]:
        """Show a sample weather frame."""
        color = COLOR_CODES["yellow"]
        grid = blank_grid()
        grid[0] = [color] * COLS
        grid[1] = _center_text_row("SUNNY")
        grid[2] = _center_text_row("72 F")
        grid[3] = _center_text_row("FEELS LIKE 75")
        grid[5] = _center_text_row("7:30 AM")
        return grid

    def initialize(self) -> None:
        self._daily_handles = []
        self.register_with_controller()
        self._register_daily_timers()

    def terminate(self) -> None:
        self._cancel_daily_timers()
        self.deregister_from_controller()

    def set_enabled(self, enabled: bool) -> None:
        if enabled:
            self._register_daily_timers()
        else:
            self._cancel_daily_timers()

    def on_config_updated(self, config: dict[str, Any]) -> None:
        super().on_config_updated(config)
        if "time_list" in config:
            self._cancel_daily_timers()
            self._register_daily_timers()

    def _register_daily_timers(self) -> None:
        """Register run_daily timers for each time in time_list."""
        self._cancel_daily_timers()

        cfg = self.args or {}
        time_list = cfg.get("time_list", self.DEFAULT_UI_CONFIG.get("time_list", []))
        if not isinstance(time_list, list):
            time_list = []

        for time_str in time_list:
            try:
                parts = str(time_str).split(":")
                h = int(parts[0]) if len(parts) > 0 else 7
                m = int(parts[1]) if len(parts) > 1 else 30
                s = int(parts[2]) if len(parts) > 2 else 0
                from datetime import time as dtime
                run_time = dtime(h, m, s)
                handle = self.run_daily(self._on_daily_fire, run_time)
                self._daily_handles.append(handle)
                self.log(f"Weather scheduled at {time_str}", level="INFO")
            except Exception as exc:
                self.log(f"Failed to schedule weather at {time_str!r}: {exc!r}", level="WARNING")

    def _cancel_daily_timers(self) -> None:
        """Cancel all daily timers."""
        for handle in self._daily_handles:
            try:
                self.cancel_timer(handle)
            except Exception:
                pass
        self._daily_handles = []

    def _on_daily_fire(self, kwargs: dict) -> None:
        """Daily timer callback — generate and push weather frame."""
        self.create_task(self._generate_and_push_weather())

    async def _generate_and_push_weather(self) -> None:
        """Generate weather frame and push with force_push support."""
        try:
            grid = await self.generate_frame()
            if grid and any(any(cell != 0 for cell in row) for row in grid):
                cfg = self.args or {}
                force_push = bool(cfg.get("force_push", False))
                self.push_frame(
                    grid,
                    ttl_s=self.get_resolved_ttl_s(),
                    max_age_s=self.default_max_age_s,
                    override_ttl=force_push,
                    should_expire=self.get_resolved_should_expire(),
                )
            else:
                self.log("Weather frame is blank — skipping push", level="DEBUG")
        except Exception as exc:
            self.log(f"Weather generate_and_push failed: {exc!r}", level="ERROR")

    async def generate_frame(self, **kwargs) -> list[list[int]]:
        """Read weather entity and build a 6x22 grid.

        Layout:
        - Row 0: colored bar (weather-condition-based color)
        - Row 1: condition (e.g., "SUNNY") centered
        - Row 2: temperature (e.g., "72 F") centered
        - Row 3: "FEELS LIKE 75" or humidity centered
        - Row 4: blank
        - Row 5: time of reading
        """
        cfg = self.args or {}
        weather_entity = str(cfg.get("weather_entity", ""))
        if not weather_entity:
            self.log("No weather_entity configured", level="WARNING")
            return blank_grid()

        try:
            state = self.get_state(weather_entity, attribute="all")
            if hasattr(state, "__await__"):
                state = await state
        except Exception as exc:
            self.log(f"Failed to read weather entity: {exc!r}", level="ERROR")
            return blank_grid()

        if state is None:
            self.log(f"Weather entity {weather_entity!r} not found", level="WARNING")
            return blank_grid()

        attrs = state.get("attributes", {}) if isinstance(state, dict) else {}
        condition = state.get("state", "unknown") if isinstance(state, dict) else str(state)

        temperature = attrs.get("temperature")
        humidity = attrs.get("humidity")
        apparent_temp = attrs.get("apparent_temperature")
        temp_unit = attrs.get("temperature_unit", "F")

        # Build condition label
        condition_label = _CONDITION_LABELS.get(condition, condition.upper().replace("-", " ").replace("_", " "))

        # Build temperature string
        if temperature is not None:
            temp_str = f"{int(round(float(temperature)))} {temp_unit.replace('°', '')}"
        else:
            temp_str = ""

        # Build detail line
        if apparent_temp is not None:
            detail_str = f"FEELS LIKE {int(round(float(apparent_temp)))}"
        elif humidity is not None:
            detail_str = f"HUMIDITY {int(round(float(humidity)))}%"
        else:
            detail_str = ""

        # Time of reading
        now = datetime.now()
        time_str = now.strftime("%I:%M %p").lstrip("0")

        # Color bar
        bar_color = _CONDITION_COLORS.get(condition, COLOR_CODES["white"])

        grid = blank_grid()
        grid[0] = [bar_color] * COLS
        grid[1] = _center_text_row(condition_label)
        grid[2] = _center_text_row(temp_str)
        grid[3] = _center_text_row(detail_str)
        # Row 4: blank
        grid[5] = _center_text_row(time_str)

        self.log(
            f"Weather frame: {condition_label} {temp_str} {detail_str} at {time_str}",
            level="INFO",
        )
        return grid
