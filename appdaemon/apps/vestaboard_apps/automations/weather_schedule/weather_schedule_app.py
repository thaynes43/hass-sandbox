"""Weather Schedule automation app — displays weather at configured daily times.

Shows current conditions with color-coded temperature tiles, humidity,
and a wind speed meter.  Re-fetches and re-pushes every 15 minutes
during the TTL window so the board stays current.
"""

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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_UPDATE_INTERVAL_S = 15 * 60  # 15 minutes

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


# ---------------------------------------------------------------------------
# Grid helpers
# ---------------------------------------------------------------------------

def _encode_char(ch: str) -> int:
    if ch == " ":
        return 0
    return CHAR_TO_CODE.get(ch.upper(), 0)


def _center_text_row(text: str, width: int = COLS) -> list[int]:
    padded = text[:width].center(width)
    return [_encode_char(ch) for ch in padded]


def _temp_color(temp: int) -> int:
    """Return a color tile code for a temperature value."""
    if temp <= 40:
        return COLOR_CODES["blue"]
    if temp <= 60:
        return COLOR_CODES["yellow"]
    if temp <= 80:
        return COLOR_CODES["orange"]
    return COLOR_CODES["red"]


def _wind_tiles(wind_speed: float) -> list[int]:
    """Return 1-4 color-coded tiles representing wind speed.

    0-7 mph  : 1 white tile   (calm)
    8-15 mph : 2 yellow tiles (breeze)
    16-25 mph: 3 orange tiles (moderate)
    26+ mph  : 4 red tiles    (high wind)
    """
    if wind_speed <= 7:
        return [COLOR_CODES["white"]]
    if wind_speed <= 15:
        return [COLOR_CODES["yellow"]] * 2
    if wind_speed <= 25:
        return [COLOR_CODES["orange"]] * 3
    return [COLOR_CODES["red"]] * 4


def _build_temp_row(high: Optional[int], low: Optional[int], current: Optional[int]) -> list[int]:
    """Build row 2: HI[tile]## LO[tile]## [tile]##

    Centered within 22 columns.
    """
    # Build segments as (codes) lists
    segments: list[list[int]] = []

    if high is not None:
        hi_text = f"HI"
        hi_codes = [_encode_char(c) for c in hi_text]
        hi_codes.append(_temp_color(high))
        hi_codes += [_encode_char(c) for c in str(high)]
        segments.append(hi_codes)

    if low is not None:
        lo_text = f"LO"
        lo_codes = [_encode_char(c) for c in lo_text]
        lo_codes.append(_temp_color(low))
        lo_codes += [_encode_char(c) for c in str(low)]
        segments.append(lo_codes)

    if current is not None:
        cur_codes = [_temp_color(current)]
        cur_codes += [_encode_char(c) for c in str(current)]
        segments.append(cur_codes)

    if not segments:
        return [0] * COLS

    # Join segments with 2-space gaps
    row: list[int] = []
    for i, seg in enumerate(segments):
        if i > 0:
            row += [0, 0]  # 2-space gap
        row += seg

    # Center within COLS
    total = len(row)
    if total < COLS:
        left_pad = (COLS - total) // 2
        right_pad = COLS - total - left_pad
        row = [0] * left_pad + row + [0] * right_pad
    else:
        row = row[:COLS]

    return row


def _build_detail_row(humidity: Optional[int], wind_speed: Optional[float]) -> list[int]:
    """Build row 3: RH ##%   WIND [tiles]

    Centered within 22 columns.
    """
    segments: list[list[int]] = []

    if humidity is not None:
        rh_text = f"RH {humidity}%"
        segments.append([_encode_char(c) for c in rh_text])

    if wind_speed is not None:
        wind_label = [_encode_char(c) for c in "WIND"]
        wind_label.append(0)  # space before tiles
        wind_label += _wind_tiles(wind_speed)
        segments.append(wind_label)

    if not segments:
        return [0] * COLS

    # Join with 3-space gap
    row: list[int] = []
    for i, seg in enumerate(segments):
        if i > 0:
            row += [0, 0, 0]
        row += seg

    # Center
    total = len(row)
    if total < COLS:
        left_pad = (COLS - total) // 2
        right_pad = COLS - total - left_pad
        row = [0] * left_pad + row + [0] * right_pad
    else:
        row = row[:COLS]

    return row


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class WeatherScheduleApp(hass.Hass, VestaboardAutomation):
    """Automation that displays weather at configured daily times.

    Reads current weather + daily forecast from a HA weather entity and
    displays condition, temperatures (high/low/current with color tiles),
    humidity, and a wind speed meter.  Re-fetches every 15 minutes during
    the TTL window.
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
    _update_handle = None
    _push_started_at: float = 0.0  # timestamp of initial push (for TTL window)

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
        """Show a sample weather frame with the new layout."""
        bar_color = COLOR_CODES["yellow"]
        grid = blank_grid()
        grid[0] = [bar_color] * COLS
        grid[1] = _center_text_row("SUNNY")
        grid[2] = _build_temp_row(72, 55, 68)
        grid[3] = _build_detail_row(45, 8)
        grid[5] = _center_text_row("7:30 AM")
        return grid

    def initialize(self) -> None:
        self._daily_handles = []
        self._update_handle = None
        self.register_with_controller()

    def terminate(self) -> None:
        self._cancel_daily_timers()
        self._cancel_update_timer()
        self.deregister_from_controller()

    def set_enabled(self, enabled: bool) -> None:
        if enabled:
            self._register_daily_timers()
        else:
            self._cancel_daily_timers()
            self._cancel_update_timer()

    def on_config_updated(self, config: dict[str, Any]) -> None:
        super().on_config_updated(config)
        if "enabled" in config:
            if config["enabled"]:
                self._register_daily_timers()
            else:
                self._cancel_daily_timers()
                self._cancel_update_timer()
        if "time_list" in config:
            self.log(
                f"time_list updated: {config['time_list']} — rescheduling daily timers",
                level="INFO",
            )
            if self.args.get("enabled", False):
                self._cancel_daily_timers()
                self._register_daily_timers()

    # ------------------------------------------------------------------
    # Daily schedule timers
    # ------------------------------------------------------------------

    def _register_daily_timers(self) -> None:
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
        for handle in self._daily_handles:
            try:
                self.cancel_timer(handle)
            except Exception:
                pass
        self._daily_handles = []

    # ------------------------------------------------------------------
    # Periodic update timer (re-fetch every 15 min during TTL)
    # ------------------------------------------------------------------

    def _cancel_update_timer(self) -> None:
        if self._update_handle is not None:
            try:
                self.cancel_timer(self._update_handle)
            except Exception:
                pass
            self._update_handle = None

    def _on_update_timer(self, kwargs: dict) -> None:
        self._update_handle = None
        self.create_task(self._update_weather())

    async def _schedule_update_timer(self) -> None:
        """Schedule the next 15-minute weather refresh."""
        self._cancel_update_timer()
        handle = self.run_in(self._on_update_timer, _UPDATE_INTERVAL_S)
        if hasattr(handle, "__await__"):
            handle = await handle
        self._update_handle = handle
        self.log(
            f"Weather update scheduled in {_UPDATE_INTERVAL_S // 60} min",
            level="DEBUG",
        )

    async def _update_weather(self) -> None:
        """Re-fetch weather and push updated frame (same-source replaces displayed).

        Stops updating once the original TTL window has elapsed so the
        weather frame can expire and pending frames can be promoted.
        Also stops if we are no longer the displayed source — pushing a
        refresh when another frame owns the board would queue a stale
        weather frame in pending that surfaces hours later.
        """
        import time as _time

        # Check if we're past the TTL window — stop updating
        ttl_s = self.get_resolved_ttl_s() or 3600
        elapsed = _time.time() - self._push_started_at
        if elapsed >= ttl_s:
            self.log(
                f"Weather TTL expired ({elapsed:.0f}s >= {ttl_s}s) — "
                f"stopping periodic updates",
                level="INFO",
            )
            return  # Don't schedule another update

        # Check if we're still the displayed source — if another frame
        # took the board, stop refreshing to avoid queuing stale frames
        try:
            status = self.get_state(
                "sensor.vestaboard_controller_status", attribute="all"
            )
            if status and isinstance(status, dict):
                displayed_source = status.get("attributes", {}).get(
                    "displayed_source", ""
                )
                if displayed_source and displayed_source != self.name:
                    self.log(
                        f"Weather no longer on board (current={displayed_source!r}) "
                        f"— stopping periodic updates",
                        level="INFO",
                    )
                    return  # Don't push or schedule another update
        except Exception:
            pass  # If we can't check, proceed with the update

        try:
            grid = await self.generate_frame()
            if grid and any(any(cell != 0 for cell in row) for row in grid):
                # Use remaining TTL, not full TTL, so the frame actually expires
                remaining_ttl = max(60, int(ttl_s - elapsed))
                self.push_frame(
                    grid,
                    ttl_s=remaining_ttl,
                    max_age_s=self.default_max_age_s,
                    override_ttl=False,
                    should_expire=self.get_resolved_should_expire(),
                )
                self.log(
                    f"Weather update pushed (periodic refresh, "
                    f"remaining_ttl={remaining_ttl}s)",
                    level="INFO",
                )
        except Exception as exc:
            self.log(f"Weather update failed: {exc!r}", level="ERROR")

        # Schedule next update
        await self._schedule_update_timer()

    # ------------------------------------------------------------------
    # Daily fire + initial push
    # ------------------------------------------------------------------

    def _on_daily_fire(self, kwargs: dict) -> None:
        self.create_task(self._generate_and_push_weather())

    async def _generate_and_push_weather(self) -> None:
        """Generate weather frame, push it, and start the update timer."""
        import time as _time
        self._push_started_at = _time.time()
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

        # Start periodic updates
        await self._schedule_update_timer()

    # ------------------------------------------------------------------
    # Frame generation
    # ------------------------------------------------------------------

    async def generate_frame(self, **kwargs) -> list[list[int]]:
        """Read weather entity and build a 6x22 grid.

        Layout:
        - Row 0: colored bar (weather-condition-based color)
        - Row 1: condition label (e.g. "SUNNY") centered
        - Row 2: HI[tile]## LO[tile]## [tile]## (high, low, current w/ color tiles)
        - Row 3: RH ##%   WIND [tiles] (humidity + wind meter)
        - Row 4: blank
        - Row 5: time of reading
        """
        cfg = self.args or {}
        weather_entity = str(cfg.get("weather_entity", ""))
        if not weather_entity:
            self.log("No weather_entity configured", level="WARNING")
            return blank_grid()

        # Read current state
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

        current_temp = _safe_int(attrs.get("temperature"))
        humidity = _safe_int(attrs.get("humidity"))
        wind_speed = _safe_float(attrs.get("wind_speed"))

        # Fetch daily forecast for high/low
        high_temp, low_temp = await self._fetch_daily_high_low(weather_entity)

        # Build condition label
        condition_label = _CONDITION_LABELS.get(
            condition, condition.upper().replace("-", " ").replace("_", " ")
        )

        # Time of reading
        now = datetime.now()
        time_str = now.strftime("%I:%M %p").lstrip("0")

        # Color bar
        bar_color = _CONDITION_COLORS.get(condition, COLOR_CODES["white"])

        grid = blank_grid()
        grid[0] = [bar_color] * COLS
        grid[1] = _center_text_row(condition_label)
        grid[2] = _build_temp_row(high_temp, low_temp, current_temp)
        grid[3] = _build_detail_row(humidity, wind_speed)
        grid[5] = _center_text_row(time_str)

        self.log(
            f"Weather frame: {condition_label} hi={high_temp} lo={low_temp} "
            f"cur={current_temp} rh={humidity}% wind={wind_speed}mph at {time_str}",
            level="INFO",
        )
        return grid

    async def _fetch_daily_high_low(
        self, weather_entity: str
    ) -> tuple[Optional[int], Optional[int]]:
        """Fetch today's high and low from the daily forecast via HA WebSocket.

        AppDaemon's call_service doesn't support response data from HA
        services that use ``return_response``, so we open a short-lived
        WebSocket connection via HaRestClient to call ``get_forecasts``.
        """
        try:
            from providers.ha_provisioner.ha_rest_client import HaRestClient
            from providers.secrets import resolve_secret

            ha_url = resolve_secret(self.args.get("ha_url_env", "HA_URL"))
            ha_token = resolve_secret(self.args.get("ha_token_env", "TOKEN"))

            async with HaRestClient(ha_url, ha_token) as client:
                ws_result = await client.send_ws_command({
                    "type": "call_service",
                    "domain": "weather",
                    "service": "get_forecasts",
                    "service_data": {"type": "daily"},
                    "target": {"entity_id": weather_entity},
                    "return_response": True,
                })

            if ws_result.get("success"):
                response = ws_result.get("result", {}).get("response", {})
                forecasts = response.get(weather_entity, {}).get("forecast", [])
                if forecasts:
                    today = forecasts[0]
                    high = _safe_int(today.get("temperature"))
                    low = _safe_int(today.get("templow"))
                    self.log(
                        f"Daily forecast: high={high} low={low}",
                        level="DEBUG",
                    )
                    return high, low
            else:
                error = ws_result.get("error", {})
                self.log(
                    f"WS get_forecasts failed: {error.get('message', ws_result)}",
                    level="WARNING",
                )
        except Exception as exc:
            self.log(f"Failed to fetch daily forecast: {exc!r}", level="WARNING")

        return None, None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_int(value) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(round(float(value)))
    except (ValueError, TypeError):
        return None


def _safe_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None
