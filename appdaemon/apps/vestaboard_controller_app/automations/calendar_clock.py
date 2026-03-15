"""Calendar Clock automation — shows a 7-column calendar grid + date/time."""

from __future__ import annotations

import calendar
from datetime import datetime
from typing import Any

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[4]))

from providers.vestaboard.character_encoding import (
    CHAR_TO_CODE,
    COLOR_CODES,
    COLS,
    ROWS,
    blank_grid,
)

from .base import BoardAutomation

# Calendar pane is 7 columns wide (S M T W T F S).
_CAL_COLS = 7
# Separator between calendar pane and right pane.
_SEP_COLS = 2
# Right pane is whatever remains.
_RIGHT_COLS = COLS - _CAL_COLS - _SEP_COLS  # 13

# Month-specific color pairs: (tile_day, tile_today).
# tile_day is always the darker color, tile_today is lighter.
_MONTH_COLORS: dict[int, tuple[int, int]] = {
    1:  (COLOR_CODES["blue"],   COLOR_CODES["white"]),   # Jan
    2:  (COLOR_CODES["violet"], COLOR_CODES["red"]),     # Feb
    3:  (COLOR_CODES["green"],  COLOR_CODES["yellow"]),  # Mar
    4:  (COLOR_CODES["yellow"], COLOR_CODES["white"]),   # Apr
    5:  (COLOR_CODES["green"],  COLOR_CODES["white"]),   # May
    6:  (COLOR_CODES["orange"], COLOR_CODES["yellow"]),  # Jun
    7:  (COLOR_CODES["red"],    COLOR_CODES["white"]),   # Jul
    8:  (COLOR_CODES["orange"], COLOR_CODES["white"]),   # Aug
    9:  (COLOR_CODES["green"],  COLOR_CODES["orange"]),  # Sep
    10: (COLOR_CODES["orange"], COLOR_CODES["yellow"]),  # Oct
    11: (COLOR_CODES["red"],    COLOR_CODES["orange"]),  # Nov
    12: (COLOR_CODES["red"],    COLOR_CODES["green"]),   # Dec
}

_BLACK = COLOR_CODES["black"]  # 70 — empty calendar cell

_DOW_HEADER = "SMTWTFS"  # 7 chars


def _encode_char(ch: str) -> int:
    """Encode a single character, returning 0 for space/unknown."""
    if ch == " ":
        return 0
    return CHAR_TO_CODE.get(ch.upper(), 0)


def _encode_str_row(text: str, width: int) -> list[int]:
    """Encode *text* into a list of *width* Vestaboard codes (padded with 0)."""
    row: list[int] = []
    for i in range(width):
        if i < len(text):
            row.append(_encode_char(text[i]))
        else:
            row.append(0)
    return row


class CalendarClockAutomation(BoardAutomation):
    """Automation that renders a calendar + clock frame every minute.

    Left pane (7 cols): S M T W T F S header + 5 weeks of the current month.
    Right pane (13 cols): day-of-week, month+date, blank, time.
    """

    name = "CalendarClock"
    description = "Shows the current time and date on your board."
    default_ttl_s = None       # freely replaceable — no TTL protection
    default_max_age_s = None
    default_should_expire = False

    DEFAULT_UI_CONFIG = {
        "enabled": True,
        "ttl_minutes": None,
        "should_expire": False,
    }

    @classmethod
    def get_config_schema(cls) -> dict:
        return {
            "enabled": {"type": "bool", "label": "Enabled", "default": True},
            "ttl_minutes": {"type": "int", "label": "TTL (minutes)", "min": 1, "max": 1440, "default": None},
            "should_expire": {"type": "bool", "label": "Should Expire", "default": False},
        }

    def get_preview_frame(self) -> list[list[int]]:
        """Return a representative calendar+clock preview frame.

        Shows a March calendar (month 3: green/yellow colors) with Wednesday,
        March 12 highlighted and a sample time on the right pane.
        """
        # March colors: tile_day=green(66), tile_today=yellow(65)
        tile_day = COLOR_CODES["green"]    # 66
        tile_today = COLOR_CODES["yellow"] # 65

        # March 2025: first day is Saturday (weekday=5), sun_offset=(5+1)%7=6
        # So week 0: days 1 positions 6 only; week 1: days 2-8; etc.
        # For a clean preview use a representative 5-week layout.
        # Build a simplified 5x7 calendar where day 12 is highlighted.
        # March 1 = Saturday -> sun_offset = 6
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

        # Right pane (13 cols): DOW, MONTH+DATE, blank, TIME, blank
        sep = [0] * _SEP_COLS
        blank_right = [0] * _RIGHT_COLS

        dow_str   = "WEDNESDAY    "[:_RIGHT_COLS]  # 13 chars
        mon_str   = "MARCH 12     "[:_RIGHT_COLS]
        time_str  = "10:30 AM     "[:_RIGHT_COLS]

        right_pane = [
            _encode_str_row(dow_str, _RIGHT_COLS),
            _encode_str_row(mon_str, _RIGHT_COLS),
            blank_right,
            _encode_str_row(time_str, _RIGHT_COLS),
            blank_right,
        ]

        # Row 0: SMTWTFS header + sep + blank right
        header_codes = [CHAR_TO_CODE.get(ch, 0) for ch in _DOW_HEADER]
        grid_row0 = header_codes + sep + blank_right

        grid: list[list[int]] = [grid_row0]
        for w in range(5):
            row = cal_rows[w] + sep + right_pane[w]
            grid.append(row)

        return grid

    def get_triggers(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "time_interval",
                "interval_s": 60,
                "callback": self._on_tick,
            }
        ]

    def _on_tick(self, kwargs: dict[str, Any]) -> None:
        """Callback fired by AppDaemon timer — schedules async frame generation."""
        self.app.create_task(self._fire_frame())

    async def _fire_frame(self) -> None:
        grid = await self.generate_frame()
        cfg = self.config
        ttl_s = self.default_ttl_s
        ttl_minutes = cfg.get("ttl_minutes")
        if ttl_minutes is not None:
            ttl_s = int(float(ttl_minutes) * 60)
        should_expire = bool(cfg.get("should_expire", self.default_should_expire))
        self.app._push_automation_frame(
            automation_id="calendar_clock",
            source_label=self.name,
            grid=grid,
            ttl_s=ttl_s,
            max_age_s=self.default_max_age_s,
            should_expire=should_expire,
        )

    async def generate_frame(self) -> list[list[int]]:
        """Build and return the 6x22 calendar+clock grid."""
        now = datetime.now()
        month = now.month
        year = now.year
        today = now.day

        tile_day, tile_today = _MONTH_COLORS.get(month, (_BLACK, COLOR_CODES["white"]))

        # Calendar grid: 5 weeks x 7 days
        first_weekday = calendar.monthrange(year, month)[0]  # Monday=0
        # Convert Monday=0 to Sunday=0 offset
        sun_offset = (first_weekday + 1) % 7
        days_in_month = calendar.monthrange(year, month)[1]

        # Build 5-row calendar (each row is a list of 7 color codes)
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

        # Right pane text rows (each 13 chars)
        dow_str = now.strftime("%A").upper()[:_RIGHT_COLS].ljust(_RIGHT_COLS)
        mon_str = now.strftime("%B %d").upper()[:_RIGHT_COLS].ljust(_RIGHT_COLS)
        time_str = now.strftime("%I:%M %p").upper()[:_RIGHT_COLS].ljust(_RIGHT_COLS)

        # Assemble full 6x22 grid
        # Row 0: DOW header | sep | blank
        # Row 1: cal_rows[0] | sep | dow_str
        # Row 2: cal_rows[1] | sep | mon_str
        # Row 3: cal_rows[2] | sep | blank
        # Row 4: cal_rows[3] | sep | time_str
        # Row 5: cal_rows[4] | sep | blank

        blank_right = [0] * _RIGHT_COLS
        sep = [0] * _SEP_COLS

        # Row 0: header
        header_codes = [CHAR_TO_CODE.get(ch, 0) for ch in _DOW_HEADER]
        grid_row0 = header_codes + sep + blank_right

        # Rows 1-5: calendar weeks + right pane
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
