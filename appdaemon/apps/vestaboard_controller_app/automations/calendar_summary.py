"""Calendar Summary automation — shows upcoming HA calendar events."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[4]))

from providers.vestaboard.character_encoding import (
    CHAR_TO_CODE,
    COLS,
    ROWS,
    blank_grid,
    text_to_grid,
)

from .base import BoardAutomation

# Default reminder: show event 15 minutes before it starts.
_DEFAULT_REMINDER_MINUTES = 15


def _encode_text_row(text: str, width: int = COLS) -> list[int]:
    """Encode *text* into a list of *width* Vestaboard codes (0 = blank/space)."""
    row: list[int] = []
    for i in range(width):
        if i < len(text):
            ch = text[i].upper()
            row.append(CHAR_TO_CODE.get(ch, 0))
        else:
            row.append(0)
    return row


def _center_text_row(text: str, width: int = COLS) -> list[int]:
    """Center *text* within *width* columns."""
    padded = text[:width].center(width)
    return _encode_text_row(padded, width)


def _format_countdown(total_seconds: int) -> str:
    """Return a human-readable countdown string like '14 MIN' or '2 HRS'."""
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
    """Build a 6x22 grid displaying event info.

    Row 0: blank
    Row 1: event name (centered, up to 22 chars)
    Row 2: event name continued if needed
    Row 3: blank
    Row 4: event time (centered)
    Row 5: countdown (centered)
    """
    # Truncate/wrap event name across rows 1-2
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


class CalendarSummaryAutomation(BoardAutomation):
    """Automation that watches a single calendar entity and shows upcoming events.

    Each instance handles one calendar. To watch multiple calendars, configure
    multiple instances with different IDs using the ``type: calendar_summary``
    key in the automations config.

    Triggers on:
    - State changes on the configured calendar entity.
    - Periodic interval to catch time-based transitions.

    Config keys:
    - calendar_entity: HA calendar entity ID to watch (single string).
    - reminder_minutes: minutes before event to show reminder (default 15).
    - check_interval_s: how often to re-check calendar state (default 300).
    - ttl_minutes: TTL in minutes for pushed frames (default: dynamic from event end time).
    - rotation_interval_hours: minimum hours between pushes for the same event (default: None — no throttle).
    - time_before_event_hours: hours before event start to begin showing it (default: derived from reminder_minutes).
    """

    name = "CalendarSummary"
    description = "Displays upcoming events from a calendar on your board."
    default_ttl_s = None       # TTL set dynamically per event
    default_max_age_s = None  # Expiration set dynamically per event
    default_should_expire = False

    DEFAULT_UI_CONFIG = {
        "enabled": False,
        "ttl_minutes": 30,
        "should_expire": False,
        "time_before_event_hours": 240,
        "rotation_interval_hours": 12,
    }

    @classmethod
    def get_config_schema(cls) -> dict:
        return {
            "enabled": {"type": "bool", "label": "Enabled", "default": False},
            "ttl_minutes": {"type": "int", "label": "TTL (minutes)", "min": 1, "max": 1440, "default": 30},
            "should_expire": {"type": "bool", "label": "Should Expire", "default": False},
            "time_before_event_hours": {"type": "int", "label": "Hours Before Event", "min": 1, "max": 720, "default": 240},
            "rotation_interval_hours": {"type": "int", "label": "Rotation Interval (hours)", "min": 1, "max": 168, "default": 12},
        }

    def __init__(self, app: Any, config: dict[str, Any]) -> None:
        super().__init__(app=app, config=config)
        # Track last push time per event summary to support rotation_interval_hours
        self._last_push_time: dict[str, float] = {}
        # Store the automation_id so we can push frames with the correct source
        self._automation_id: str = ""

    def set_automation_id(self, automation_id: str) -> None:
        """Set the automation ID used as the source when pushing frames."""
        self._automation_id = automation_id
        # Build a friendly display name from the automation ID
        # e.g. "calendar_summary_family" → "Calendar: Family"
        #      "calendar_summary_hot_tub" → "Calendar: Hot Tub"
        suffix = automation_id.replace("calendar_summary_", "").replace("calendar_summary", "")
        if suffix:
            friendly = suffix.replace("_", " ").title()
            self.name = f"Calendar: {friendly}"

    def get_preview_frame(self) -> list[list[int]]:
        """Return a representative calendar event preview frame.

        Shows a sample upcoming meeting with a time and countdown, illustrating
        the layout produced by this automation.
        """
        return _build_event_grid(
            event_name="TEAM MEETING",
            event_time="10:30 AM",
            countdown="14 MIN",
        )

    def get_triggers(self) -> list[dict[str, Any]]:
        triggers: list[dict[str, Any]] = []

        entity_id = str(self.config.get("calendar_entity", ""))
        if entity_id:
            triggers.append(
                {
                    "type": "state",
                    "entity_id": entity_id,
                    "callback": self._on_calendar_state_change,
                }
            )

        # Also check on interval so we catch time-based transitions
        interval_s = int(self.config.get("check_interval_s", 300))
        triggers.append(
            {
                "type": "time_interval",
                "interval_s": interval_s,
                "callback": self._on_interval,
            }
        )

        return triggers

    def _on_calendar_state_change(self, entity: str, attribute: str, old: Any, new: Any, kwargs: dict) -> None:
        """Called when the calendar entity's state changes."""
        self.log(f"Calendar state change on {entity}: {old!r} -> {new!r}", level="DEBUG")
        self.app.create_task(self._fire_frame_if_event())

    def _on_interval(self, kwargs: dict[str, Any]) -> None:
        """Called on interval to check for upcoming events."""
        self.app.create_task(self._fire_frame_if_event())

    async def generate_frame(self) -> list[list[int]]:
        """Generate a frame for the nearest upcoming event, or blank grid."""
        await self._fire_frame_if_event()
        return blank_grid()

    async def _fire_frame_if_event(self) -> None:
        """Check the calendar entity and push a frame if an event is upcoming."""
        entity_id = str(self.config.get("calendar_entity", ""))
        if not entity_id:
            self.log("No calendar_entity configured", level="WARNING")
            return

        self.log(
            f"Checking calendar: entity={entity_id!r} "
            f"time_before_hours={self.config.get('time_before_event_hours')} "
            f"reminder_min={self.config.get('reminder_minutes')}",
            level="DEBUG",
        )

        # Determine reminder window: use time_before_event_hours if set,
        # otherwise fall back to reminder_minutes.
        time_before_hours = self.config.get("time_before_event_hours")
        if time_before_hours is not None:
            reminder_minutes = int(float(time_before_hours) * 60)
        else:
            reminder_minutes = int(self.config.get("reminder_minutes", _DEFAULT_REMINDER_MINUTES))

        now = datetime.now(tz=timezone.utc)
        event = await self._get_current_or_upcoming_event(entity_id, now, reminder_minutes)

        if event is None:
            self.log(
                f"No upcoming events within reminder window "
                f"(entity={entity_id!r} reminder_minutes={reminder_minutes})",
                level="DEBUG",
            )
            return

        # Check rotation interval throttle
        rotation_hours = self.config.get("rotation_interval_hours")
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

        # Override TTL from config (fall back to DEFAULT_UI_CONFIG if not in config store)
        ttl_minutes_cfg = self.config.get("ttl_minutes", self.DEFAULT_UI_CONFIG.get("ttl_minutes"))
        if ttl_minutes_cfg is not None:
            ttl_s = int(float(ttl_minutes_cfg) * 60)

        automation_id = self._automation_id or "calendar_summary"

        self.log(
            f"Pushing calendar event: {event.get('summary', '?')!r} "
            f"ttl_s={ttl_s} max_age_s={max_age_s} automation_id={automation_id!r}",
            level="INFO",
        )

        self.app._push_automation_frame(
            automation_id=automation_id,
            source_label=self.name,
            grid=grid,
            ttl_s=ttl_s,
            max_age_s=max_age_s,
        )

        # Record push time for rotation throttle
        if rotation_hours is not None:
            import time as _time
            self._last_push_time[event.get("summary", "")] = _time.time()

    async def _get_current_or_upcoming_event(
        self, entity_id: str, now: datetime, reminder_minutes: int
    ) -> Optional[dict]:
        """Read a calendar entity and return event info if within reminder window."""
        try:
            state = self.app.get_state(entity_id, attribute="all")
            if hasattr(state, "__await__"):
                state = await state
            if state is None:
                return None

            attrs = state.get("attributes", {}) if isinstance(state, dict) else {}
            entity_state = state.get("state", "off") if isinstance(state, dict) else state

            # Calendar entity is "on" when an event is active
            if entity_state == "on":
                # Currently active event
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

            # Check if upcoming within reminder window
            next_event_summary = attrs.get("message", "")
            next_start_str = attrs.get("start_time", "")
            next_end_str = attrs.get("end_time", "")

            if not next_start_str or not next_event_summary:
                return None

            try:
                next_start = datetime.fromisoformat(next_start_str.replace("Z", "+00:00"))
                if not next_start.tzinfo:
                    # HA returns local time without timezone — use system local tz
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
        """Build grid and timing data for an event.

        Returns (grid, ttl_s, max_age_s).
        """
        summary = event.get("summary", "Event")
        seconds_until = event.get("seconds_until", 0)
        start_time_str = event.get("start_time_str", "")
        end_time_str = event.get("end_time_str", "")

        # Format event time for display
        try:
            start_dt = datetime.fromisoformat(start_time_str.replace("Z", "+00:00"))
            event_time_display = start_dt.astimezone().strftime("%I:%M %p").lstrip("0")
        except (ValueError, AttributeError):
            event_time_display = ""

        countdown_str = _format_countdown(seconds_until)
        grid = _build_event_grid(summary, event_time_display, countdown_str)

        # TTL: hold through the event duration
        # For upcoming events: reminder time + estimated event duration
        ttl_s: Optional[int] = None
        max_age_s: Optional[int] = None

        if end_time_str and start_time_str:
            try:
                start_dt = datetime.fromisoformat(
                    start_time_str.replace("Z", "+00:00")
                )
                end_dt = datetime.fromisoformat(
                    end_time_str.replace("Z", "+00:00")
                )
                if not start_dt.tzinfo:
                    start_dt = start_dt.replace(tzinfo=timezone.utc)
                if not end_dt.tzinfo:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)

                # TTL: seconds until event end
                seconds_until_end = int((end_dt - now).total_seconds())
                if seconds_until_end > 0:
                    ttl_s = seconds_until_end
                    # Expiration: 30 minutes after event ends
                    max_age_s = seconds_until_end + 1800
            except (ValueError, AttributeError):
                pass

        return grid, ttl_s, max_age_s
