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
    """Automation that watches calendar entities and shows upcoming events.

    Triggers on:
    - State changes on configured calendar entities.
    - Periodic interval to catch time-based transitions.

    Config keys:
    - calendar_entities: list of HA calendar entity IDs to watch.
    - reminder_minutes: minutes before event to show reminder (default 15).
    - check_interval_s: how often to re-check calendar state (default 300).
    """

    name = "CalendarSummary"
    default_ttl_s = None       # TTL set dynamically per event
    default_expiration_s = None  # Expiration set dynamically per event

    def get_triggers(self) -> list[dict[str, Any]]:
        triggers: list[dict[str, Any]] = []

        entities: list[str] = list(self.config.get("calendar_entities") or [])
        for entity_id in entities:
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
        """Called when a calendar entity's state changes."""
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
        """Check calendar entities and push a frame if an event is upcoming."""
        entities: list[str] = list(self.config.get("calendar_entities") or [])
        reminder_minutes = int(self.config.get("reminder_minutes", _DEFAULT_REMINDER_MINUTES))

        now = datetime.now(tz=timezone.utc)
        best_event: Optional[dict] = None
        best_seconds_until: Optional[int] = None

        for entity_id in entities:
            event = self._get_current_or_upcoming_event(entity_id, now, reminder_minutes)
            if event is None:
                continue
            seconds_until = event.get("seconds_until", 9999999)
            if best_event is None or seconds_until < best_seconds_until:
                best_event = event
                best_seconds_until = seconds_until

        if best_event is None:
            self.log("No upcoming events within reminder window", level="DEBUG")
            return

        grid, ttl_s, expiration_s = self._build_event_data(best_event, now)

        self.log(
            f"Pushing calendar event: {best_event.get('summary', '?')!r} "
            f"ttl_s={ttl_s} expiration_s={expiration_s}",
            level="INFO",
        )

        self.app._push_automation_frame(
            automation_id="calendar_summary",
            source_label=self.name,
            grid=grid,
            ttl_s=ttl_s,
            expiration_s=expiration_s,
        )

    def _get_current_or_upcoming_event(
        self, entity_id: str, now: datetime, reminder_minutes: int
    ) -> Optional[dict]:
        """Read a calendar entity and return event info if within reminder window."""
        try:
            state = self.app.get_state(entity_id, attribute="all")
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
                    next_start = next_start.replace(tzinfo=timezone.utc)
            except ValueError:
                return None

            seconds_until = int((next_start - now).total_seconds())
            reminder_seconds = reminder_minutes * 60

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

        Returns (grid, ttl_s, expiration_s).
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
        expiration_s: Optional[int] = None

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
                    expiration_s = seconds_until_end + 1800
            except (ValueError, AttributeError):
                pass

        return grid, ttl_s, expiration_s
