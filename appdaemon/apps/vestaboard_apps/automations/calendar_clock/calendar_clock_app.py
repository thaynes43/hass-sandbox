"""Calendar Clock automation app — shows a 7-column calendar grid + date/time."""

from __future__ import annotations

import calendar
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

# Calendar pane is 7 columns wide (S M T W T F S).
_CAL_COLS = 7
# Separator between calendar pane and right pane.
_SEP_COLS = 2
# Right pane is whatever remains.
_RIGHT_COLS = COLS - _CAL_COLS - _SEP_COLS  # 13

# Month-specific color pairs: (tile_day, tile_today).
_MONTH_COLORS: dict[int, tuple[int, int]] = {
    1:  (COLOR_CODES["blue"],   COLOR_CODES["white"]),
    2:  (COLOR_CODES["violet"], COLOR_CODES["red"]),
    3:  (COLOR_CODES["green"],  COLOR_CODES["yellow"]),
    4:  (COLOR_CODES["yellow"], COLOR_CODES["white"]),
    5:  (COLOR_CODES["green"],  COLOR_CODES["white"]),
    6:  (COLOR_CODES["orange"], COLOR_CODES["yellow"]),
    7:  (COLOR_CODES["red"],    COLOR_CODES["white"]),
    8:  (COLOR_CODES["orange"], COLOR_CODES["white"]),
    9:  (COLOR_CODES["green"],  COLOR_CODES["orange"]),
    10: (COLOR_CODES["orange"], COLOR_CODES["yellow"]),
    11: (COLOR_CODES["red"],    COLOR_CODES["orange"]),
    12: (COLOR_CODES["red"],    COLOR_CODES["green"]),
}

_BLACK = COLOR_CODES["black"]
_DOW_HEADER = "SMTWTFS"


def _encode_char(ch: str) -> int:
    if ch == " ":
        return 0
    return CHAR_TO_CODE.get(ch.upper(), 0)


def _encode_str_row(text: str, width: int) -> list[int]:
    row: list[int] = []
    for i in range(width):
        if i < len(text):
            row.append(_encode_char(text[i]))
        else:
            row.append(0)
    return row


class CalendarClockApp(hass.Hass, VestaboardAutomation):
    """Automation that renders a calendar + clock frame every minute."""

    automation_type = "calendar_clock"
    display_name = "Calendar Clock"
    display_description = "Shows the current time and date on your board."
    default_ttl_s = None
    default_max_age_s = None
    default_should_expire = False

    DEFAULT_UI_CONFIG = {
        "enabled": True,
        "ttl_minutes": None,
        "should_expire": False,
    }

    _timer_handle = None

    @classmethod
    def get_config_schema(cls) -> dict:
        return {
            "enabled": {"type": "bool", "label": "Enabled", "default": True},
            "ttl_minutes": {"type": "int", "label": "TTL (minutes)", "min": 1, "max": 1440, "default": None},
            "should_expire": {"type": "bool", "label": "Should Expire", "default": False},
        }

    def get_preview_frame(self) -> list[list[int]]:
        tile_day = COLOR_CODES["green"]
        tile_today = COLOR_CODES["yellow"]

        sun_offset = 6
        days_in_month = 31
        today = 12

        cal_rows: list[list[int]] = []
        for week in range(5):
            row_cells: list[int] = []
            for dow in range(7):
                cell = week * 7 + dow
                daynum = cell - sun_offset + 1
                if daynum < 1 or daynum > days_in_month:
                    row_cells.append(_BLACK)
                elif daynum == today:
                    row_cells.append(tile_today)
                else:
                    row_cells.append(tile_day)
            cal_rows.append(row_cells)

        sep = [0] * _SEP_COLS
        blank_right = [0] * _RIGHT_COLS

        dow_str   = "WEDNESDAY    "[:_RIGHT_COLS]
        mon_str   = "MARCH 12     "[:_RIGHT_COLS]
        time_str  = "10:30 AM     "[:_RIGHT_COLS]

        right_pane = [
            _encode_str_row(dow_str, _RIGHT_COLS),
            _encode_str_row(mon_str, _RIGHT_COLS),
            blank_right,
            _encode_str_row(time_str, _RIGHT_COLS),
            blank_right,
        ]

        header_codes = [CHAR_TO_CODE.get(ch, 0) for ch in _DOW_HEADER]
        grid_row0 = header_codes + sep + blank_right

        grid: list[list[int]] = [grid_row0]
        for w in range(5):
            row = cal_rows[w] + sep + right_pane[w]
            grid.append(row)
        return grid

    def initialize(self) -> None:
        self.register_with_controller()
        # Do NOT start timer here — wait for config event from controller
        # (event-based registration is async; enabled state arrives later)

    def terminate(self) -> None:
        self._stop_timer()
        self.deregister_from_controller()

    def _start_timer(self) -> None:
        from datetime import datetime as dt
        self._timer_handle = self.run_every(self._on_tick, dt.now(), 60)

    def _stop_timer(self) -> None:
        if self._timer_handle is not None:
            try:
                self.cancel_timer(self._timer_handle)
            except Exception:
                pass
            self._timer_handle = None

    def set_enabled(self, enabled: bool) -> None:
        if enabled:
            self._start_timer()
            self.create_task(self._generate_and_push())
        else:
            self._stop_timer()

    def on_config_updated(self, config: dict[str, Any]) -> None:
        super().on_config_updated(config)
        # Start or stop timer based on enabled state from config
        if "enabled" in config:
            if config["enabled"]:
                self._start_timer()
            else:
                self._stop_timer()

    def _on_tick(self, kwargs: dict) -> None:
        self.create_task(self._generate_and_push())

    async def generate_frame(self, **kwargs) -> list[list[int]]:
        now = datetime.now()
        month = now.month
        year = now.year
        today = now.day

        tile_day, tile_today = _MONTH_COLORS.get(month, (_BLACK, COLOR_CODES["white"]))

        first_weekday = calendar.monthrange(year, month)[0]
        sun_offset = (first_weekday + 1) % 7
        days_in_month = calendar.monthrange(year, month)[1]

        cal_rows: list[list[int]] = []
        for week in range(5):
            row_cells: list[int] = []
            for dow in range(7):
                cell = week * 7 + dow
                daynum = cell - sun_offset + 1
                if daynum < 1 or daynum > days_in_month:
                    row_cells.append(_BLACK)
                elif daynum == today:
                    row_cells.append(tile_today)
                else:
                    row_cells.append(tile_day)
            cal_rows.append(row_cells)

        dow_str = now.strftime("%A").upper()[:_RIGHT_COLS].ljust(_RIGHT_COLS)
        mon_str = now.strftime("%B %d").upper()[:_RIGHT_COLS].ljust(_RIGHT_COLS)
        time_str = now.strftime("%I:%M %p").upper()[:_RIGHT_COLS].ljust(_RIGHT_COLS)

        blank_right = [0] * _RIGHT_COLS
        sep = [0] * _SEP_COLS

        header_codes = [CHAR_TO_CODE.get(ch, 0) for ch in _DOW_HEADER]
        grid_row0 = header_codes + sep + blank_right

        right_pane = [
            _encode_str_row(dow_str, _RIGHT_COLS),
            _encode_str_row(mon_str, _RIGHT_COLS),
            blank_right,
            _encode_str_row(time_str, _RIGHT_COLS),
            blank_right,
        ]

        grid: list[list[int]] = [grid_row0]
        for w in range(5):
            row = cal_rows[w] + sep + right_pane[w]
            grid.append(row)

        self.log(
            f"Generated frame: {now.strftime('%A %B %d %I:%M %p')}",
            level="DEBUG",
        )
        return grid
