"""Calendar Summary automation app — shows upcoming HA calendar events."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[4]))  # adds appdaemon/

import hassapi as hass

from providers.vestaboard.character_encoding import (
    CHAR_TO_CODE,
    COLS,
    ROWS,
    blank_grid,
    text_to_grid,
)

from vestaboard_apps._shared.base import VestaboardAutomation

_DEFAULT_REMINDER_MINUTES = 15


def _encode_text_row(text: str, width: int = COLS) -> list[int]:
    row: list[int] = []
    for i in range(width):
        if i < len(text):
            ch = text[i].upper()
            row.append(CHAR_TO_CODE.get(ch, 0))
        else:
            row.append(0)
    return row


def _center_text_row(text: str, width: int = COLS) -> list[int]:
    padded = text[:width].center(width)
    return _encode_text_row(padded, width)


def _format_countdown(total_seconds: int) -> str:
    if total_seconds < 0:
        return "NOW"
    if total_seconds < 3600:
        mins = max(1, total_seconds // 60)
        return f"{mins} MIN"
    hours = total_seconds // 3600
    return f"{hours} HRS"


def _build_event_grid(
    event_name: str, event_time: str, countdown: str
) -> list[list[int]]:
    name_upper = event_name.upper()
    name_line1 = name_upper[:COLS]
    name_line2 = name_upper[COLS : COLS * 2]

    grid = blank_grid()
    grid[1] = _center_text_row(name_line1)
    if name_line2:
        grid[2] = _center_text_row(name_line2)
    grid[4] = _center_text_row(event_time)
    grid[5] = _center_text_row(countdown)
    return grid


class CalendarSummaryApp(hass.Hass, VestaboardAutomation):
    """Automation that watches a single calendar entity and shows upcoming events.

    Each instance handles one calendar. Configure multiple YAML entries with
    different calendar_entity values for multiple calendars.
    """

    automation_type = "calendar_summary"
    display_name = "Calendar Summary"
    display_description = "Displays upcoming events from a calendar on your board."
    default_ttl_s = None
    default_max_age_s = None
    default_should_expire = False

    DEFAULT_UI_CONFIG = {
        "enabled": False,
        "ttl_minutes": 30,
        "should_expire": False,
        "time_before_event_hours": 240,
        "rotation_interval_hours": 12,
    }

    _interval_handle = None
    _state_handle = None

    @classmethod
    def get_config_schema(cls) -> dict:
        return {
            "enabled": {"type": "bool", "label": "Enabled", "default": False},
            "ttl_minutes": {"type": "int", "label": "TTL (minutes)", "min": 1, "max": 1440, "default": 30},
            "should_expire": {"type": "bool", "label": "Should Expire", "default": False},
            "time_before_event_hours": {"type": "int", "label": "Hours Before Event", "min": 1, "max": 720, "default": 240},
            "rotation_interval_hours": {"type": "int", "label": "Rotation Interval (hours)", "min": 1, "max": 168, "default": 12},
        }

    def get_preview_frame(self) -> list[list[int]]:
        return _build_event_grid(
            event_name="TEAM MEETING",
            event_time="10:30 AM",
            countdown="14 MIN",
        )

    def initialize(self) -> None:
        self._last_push_time: dict[str, float] = {}

        # Build a friendly display_name from the app key
        suffix = self.name.replace("calendar_summary_", "").replace("calendar_summary", "")
        if suffix:
            friendly = suffix.replace("_", " ").title()
            self.display_name = f"Calendar: {friendly}"

        self.register_with_controller()
        self._start_listeners()

    def terminate(self) -> None:
        self._stop_listeners()
        self.deregister_from_controller()

    def _start_listeners(self) -> None:
        cfg = self.args or {}
        entity_id = str(cfg.get("calendar_entity", ""))
        if entity_id:
            self._state_handle = self.listen_state(
                self._on_calendar_state_change, entity_id
            )

        interval_s = int(cfg.get("check_interval_s", 300))
        from datetime import datetime as dt
        self._interval_handle = self.run_every(self._on_interval, dt.now(), interval_s)

    def _stop_listeners(self) -> None:
        if self._state_handle is not None:
            try:
                self.cancel_listen_state(self._state_handle)
            except Exception:
                pass
            self._state_handle = None
        if self._interval_handle is not None:
            try:
                self.cancel_timer(self._interval_handle)
            except Exception:
                pass
            self._interval_handle = None

    def set_enabled(self, enabled: bool) -> None:
        if enabled:
            self._start_listeners()
            self.create_task(self._fire_frame_if_event())
        else:
            self._stop_listeners()

    def on_config_updated(self, config: dict[str, Any]) -> None:
        super().on_config_updated(config)

    def _on_calendar_state_change(self, entity: str, attribute: str, old: Any, new: Any, kwargs: dict) -> None:
        self.log(f"Calendar state change on {entity}: {old!r} -> {new!r}", level="DEBUG")
        self.create_task(self._fire_frame_if_event())

    def _on_interval(self, kwargs: dict) -> None:
        self.create_task(self._fire_frame_if_event())

    async def generate_frame(self, **kwargs) -> list[list[int]]:
        await self._fire_frame_if_event()
        return blank_grid()

    async def _fire_frame_if_event(self) -> None:
        cfg = self.args or {}
        entity_id = str(cfg.get("calendar_entity", ""))
        if not entity_id:
            self.log("No calendar_entity configured", level="WARNING")
            return

        self.log(
            f"Checking calendar: entity={entity_id!r} "
            f"time_before_hours={cfg.get('time_before_event_hours')} "
            f"reminder_min={cfg.get('reminder_minutes')}",
            level="DEBUG",
        )

        time_before_hours = cfg.get("time_before_event_hours")
        if time_before_hours is not None:
            reminder_minutes = int(float(time_before_hours) * 60)
        else:
            reminder_minutes = int(cfg.get("reminder_minutes", _DEFAULT_REMINDER_MINUTES))

        now = datetime.now(tz=timezone.utc)
        event = await self._get_current_or_upcoming_event(entity_id, now, reminder_minutes)

        if event is None:
            self.log(
                f"No upcoming events within reminder window "
                f"(entity={entity_id!r} reminder_minutes={reminder_minutes})",
                level="DEBUG",
            )
            return

        rotation_hours = cfg.get("rotation_interval_hours")
        if rotation_hours is not None:
            import time as _time
            event_key = event.get("summary", "")
            last_push = self._last_push_time.get(event_key, 0.0)
            min_interval_s = float(rotation_hours) * 3600
            if (_time.time() - last_push) < min_interval_s:
                self.log(
                    f"Rotation throttle: skipping {event_key!r} "
                    f"(last push {_time.time() - last_push:.0f}s ago, min={min_interval_s:.0f}s)",
                    level="DEBUG",
                )
                return

        grid, ttl_s, max_age_s = self._build_event_data(event, now)

        ttl_minutes_cfg = cfg.get("ttl_minutes", self.DEFAULT_UI_CONFIG.get("ttl_minutes"))
        if ttl_minutes_cfg is not None:
            ttl_s = int(float(ttl_minutes_cfg) * 60)

        self.log(
            f"Pushing calendar event: {event.get('summary', '?')!r} "
            f"ttl_s={ttl_s} max_age_s={max_age_s}",
            level="INFO",
        )

        self.push_frame(
            grid,
            ttl_s=ttl_s,
            max_age_s=max_age_s,
        )

        if rotation_hours is not None:
            import time as _time
            self._last_push_time[event.get("summary", "")] = _time.time()

    async def _get_current_or_upcoming_event(
        self, entity_id: str, now: datetime, reminder_minutes: int
    ) -> Optional[dict]:
        try:
            state = self.get_state(entity_id, attribute="all")
            if hasattr(state, "__await__"):
                state = await state
            if state is None:
                return None

            attrs = state.get("attributes", {}) if isinstance(state, dict) else {}
            entity_state = state.get("state", "off") if isinstance(state, dict) else state

            if entity_state == "on":
                summary = attrs.get("message", attrs.get("summary", "Event"))
                start_time_str = attrs.get("start_time", "")
                end_time_str = attrs.get("end_time", "")
                return {
                    "summary": summary,
                    "start_time_str": start_time_str,
                    "end_time_str": end_time_str,
                    "seconds_until": 0,
                    "is_active": True,
                }

            next_event_summary = attrs.get("message", "")
            next_start_str = attrs.get("start_time", "")
            next_end_str = attrs.get("end_time", "")

            if not next_start_str or not next_event_summary:
                return None

            try:
                next_start = datetime.fromisoformat(next_start_str.replace("Z", "+00:00"))
                if not next_start.tzinfo:
                    local_tz = datetime.now().astimezone().tzinfo
                    next_start = next_start.replace(tzinfo=local_tz)
            except ValueError:
                return None

            seconds_until = int((next_start - now).total_seconds())
            reminder_seconds = reminder_minutes * 60

            self.log(
                f"Calendar check: {next_event_summary!r} starts={next_start.isoformat()} "
                f"seconds_until={seconds_until} reminder_window={reminder_seconds}s",
                level="DEBUG",
            )

            if 0 <= seconds_until <= reminder_seconds:
                return {
                    "summary": next_event_summary,
                    "start_time_str": next_start_str,
                    "end_time_str": next_end_str,
                    "seconds_until": seconds_until,
                    "is_active": False,
                }

        except Exception as exc:
            self.log(
                f"Error reading calendar entity {entity_id}: {exc!r}",
                level="WARNING",
            )

        return None

    def _build_event_data(
        self, event: dict, now: datetime
    ) -> tuple[list[list[int]], Optional[int], Optional[int]]:
        summary = event.get("summary", "Event")
        seconds_until = event.get("seconds_until", 0)
        start_time_str = event.get("start_time_str", "")
        end_time_str = event.get("end_time_str", "")

        try:
            start_dt = datetime.fromisoformat(start_time_str.replace("Z", "+00:00"))
            event_time_display = start_dt.astimezone().strftime("%I:%M %p").lstrip("0")
        except (ValueError, AttributeError):
            event_time_display = ""

        countdown_str = _format_countdown(seconds_until)
        grid = _build_event_grid(summary, event_time_display, countdown_str)

        ttl_s: Optional[int] = None
        max_age_s: Optional[int] = None

        if end_time_str and start_time_str:
            try:
                start_dt = datetime.fromisoformat(start_time_str.replace("Z", "+00:00"))
                end_dt = datetime.fromisoformat(end_time_str.replace("Z", "+00:00"))
                if not start_dt.tzinfo:
                    start_dt = start_dt.replace(tzinfo=timezone.utc)
                if not end_dt.tzinfo:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)

                seconds_until_end = int((end_dt - now).total_seconds())
                if seconds_until_end > 0:
                    ttl_s = seconds_until_end
                    max_age_s = seconds_until_end + 1800
            except (ValueError, AttributeError):
                pass

        return grid, ttl_s, max_age_s
