"""Calendar Summary automation app — shows upcoming HA calendar events.

Supports multi-event rotation with dynamic countdowns and an urgent
reminder threshold that can force-push to the board.
"""

from __future__ import annotations

import math
import time as _time
from datetime import datetime, timedelta, timezone
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
)

from vestaboard_apps._shared.base import VestaboardAutomation


# ---------------------------------------------------------------------------
# Grid helpers
# ---------------------------------------------------------------------------

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


def _word_wrap(text: str, width: int) -> list[str]:
    """Wrap text at word boundaries to fit within *width* characters.

    Returns at most 2 lines.  If a single word exceeds *width* it is
    truncated rather than split across lines.
    """
    words = text.split()
    if not words:
        return [""]

    lines: list[str] = []
    current = words[0][:width]

    for word in words[1:]:
        if len(lines) >= 1:
            # Already on second line — just keep appending
            if len(current) + 1 + len(word) <= width:
                current += " " + word
            else:
                current = current[:width]
                break
        elif len(current) + 1 + len(word) <= width:
            current += " " + word
        else:
            # Start second line
            lines.append(current)
            current = word[:width]

    lines.append(current)
    return lines[:2]


def _build_event_grid(
    event_name: str, event_time: str, countdown: str
) -> list[list[int]]:
    name_upper = event_name.upper()
    lines = _word_wrap(name_upper, COLS)
    name_line1 = lines[0] if lines else ""
    name_line2 = lines[1] if len(lines) > 1 else ""

    grid = blank_grid()
    grid[1] = _center_text_row(name_line1)
    if name_line2:
        grid[2] = _center_text_row(name_line2)
    grid[4] = _center_text_row(event_time)
    grid[5] = _center_text_row(countdown)
    return grid


def _build_overflow_grid(overflow_count: int) -> list[list[int]]:
    """Build a summary frame for overflow events."""
    grid = blank_grid()
    grid[2] = _center_text_row(f"+{overflow_count} MORE")
    label = "EVENT" if overflow_count == 1 else "EVENTS"
    grid[3] = _center_text_row(f"{label} UPCOMING")
    return grid


# ---------------------------------------------------------------------------
# Countdown formatting
# ---------------------------------------------------------------------------

def _format_countdown(total_seconds: int) -> str:
    """Format a countdown string based on seconds until/since event start.

    Positive = future, negative = past, zero = now.
    """
    if -300 <= total_seconds <= 0:
        # Within 5 minutes after start
        return "NOW"
    if total_seconds < -300:
        # Past event — show elapsed time
        elapsed_min = abs(total_seconds) // 60
        if elapsed_min < 60:
            return f"{elapsed_min} MIN AGO"
        hours = elapsed_min // 60
        return f"{hours} HR AGO" if hours == 1 else f"{hours} HRS AGO"
    # Future event
    if total_seconds < 3600:
        mins = max(1, total_seconds // 60)
        return f"{mins} MIN"
    hours = total_seconds // 3600
    return f"{hours} HR" if hours == 1 else f"{hours} HRS"


def _next_countdown_update_seconds(seconds_until: int) -> int:
    """Calculate seconds until the next countdown display update.

    Schedule:
        > 30 min : every 15 min, first update aligned so remaining % 15 == 0
        30 → 15  : every 5 min
        15 → 0   : every 1 min
        0 → -5   : show NOW, update at -5 min mark
        -5 → -30 : every 5 min
        < -30    : every 15 min
    """
    if seconds_until > 30 * 60:
        # Align to next 15-minute boundary
        remaining_min = seconds_until / 60
        next_boundary = math.floor(remaining_min / 15) * 15
        if next_boundary == remaining_min:
            next_boundary = remaining_min - 15
        wait = seconds_until - (next_boundary * 60)
        return max(30, int(wait))
    if seconds_until > 15 * 60:
        # Every 5 minutes
        remaining_min = seconds_until / 60
        next_boundary = math.floor(remaining_min / 5) * 5
        if next_boundary == remaining_min:
            next_boundary = remaining_min - 5
        wait = seconds_until - (next_boundary * 60)
        return max(30, int(wait))
    if seconds_until > 0:
        # Every 1 minute
        return min(60, seconds_until)
    if seconds_until > -5 * 60:
        # Show NOW until -5 min
        return abs(seconds_until) + 5 * 60 if seconds_until < 0 else 60
    if seconds_until > -30 * 60:
        # Every 5 minutes in the past
        elapsed_min = abs(seconds_until) / 60
        next_boundary = math.ceil(elapsed_min / 5) * 5
        if next_boundary == elapsed_min:
            next_boundary = elapsed_min + 5
        wait = (next_boundary * 60) - abs(seconds_until)
        return max(30, int(wait))
    # > 30 min ago — every 15 minutes
    elapsed_min = abs(seconds_until) / 60
    next_boundary = math.ceil(elapsed_min / 15) * 15
    if next_boundary == elapsed_min:
        next_boundary = elapsed_min + 15
    wait = (next_boundary * 60) - abs(seconds_until)
    return max(30, int(wait))


# ---------------------------------------------------------------------------
# Event data helpers
# ---------------------------------------------------------------------------

def _parse_dt(dt_str: str) -> Optional[datetime]:
    """Parse an ISO datetime string, returning timezone-aware datetime or None."""
    if not dt_str:
        return None
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        if not dt.tzinfo:
            local_tz = datetime.now().astimezone().tzinfo
            dt = dt.replace(tzinfo=local_tz)
        return dt
    except (ValueError, AttributeError):
        return None


def _format_event_time(start_dt: Optional[datetime]) -> str:
    """Format a start datetime for display (e.g. '10:30 AM')."""
    if start_dt is None:
        return ""
    return start_dt.astimezone().strftime("%I:%M %p").lstrip("0")


class CalendarSummaryApp(hass.Hass, VestaboardAutomation):
    """Automation that watches a calendar entity and rotates upcoming events.

    Each instance handles one calendar. Configure multiple YAML entries with
    different calendar_entity values for multiple calendars.

    Features:
    - Multi-event rotation with calculated display times
    - Urgent reminder threshold with force-push capability
    - Dynamic countdown updates at varying intervals
    - Overflow handling with "+N more" summary frame
    """

    automation_type = "calendar_summary"
    display_name = "Calendar Summary"
    display_description = "Displays upcoming events from a calendar on your board."
    default_ttl_s = None
    default_max_age_s = None
    default_should_expire = False

    DEFAULT_UI_CONFIG = {
        "enabled": False,
        "ttl_minutes": 60,
        "should_expire": True,
        "time_before_event_hours": 240,
        "reminder_threshold_minutes": 30,
        "force_push_at_reminder": True,
        "rotation_floor_minutes": 10,
        "cooldown_min_minutes": 30,
        "cooldown_max_minutes": 120,
    }

    _interval_handle = None
    _state_handle = None
    _rotation_handle = None
    _countdown_handle = None
    _cooldown_handle = None
    _in_cooldown: bool = False

    @classmethod
    def get_config_schema(cls) -> dict:
        return {
            "enabled": {"type": "bool", "label": "Enabled", "default": False},
            "ttl_minutes": {"type": "int", "label": "TTL (minutes)", "min": 1, "max": 1440, "default": 60},
            "should_expire": {"type": "bool", "label": "Should Expire", "default": True},
            "time_before_event_hours": {"type": "int", "label": "Hours Before Event", "min": 1, "max": 720, "default": 240},
            "reminder_threshold_minutes": {"type": "int", "label": "Reminder Threshold (minutes)", "min": 1, "max": 1440, "default": 30},
            "force_push_at_reminder": {"type": "bool", "label": "Force Push at Reminder", "default": True},
            "rotation_floor_minutes": {"type": "int", "label": "Min Display Per Event (minutes)", "min": 1, "max": 60, "default": 10},
            "cooldown_min_minutes": {"type": "int", "label": "Min Cooldown (minutes)", "min": 1, "max": 1440, "default": 30},
            "cooldown_max_minutes": {"type": "int", "label": "Max Cooldown (minutes)", "min": 1, "max": 1440, "default": 120},
        }

    def get_preview_frame(self) -> list[list[int]]:
        return _build_event_grid(
            event_name="TEAM MEETING",
            event_time="10:30 AM",
            countdown="14 MIN",
        )

    def initialize(self) -> None:
        # Current rotation state
        self._current_events: list[dict] = []
        self._current_event_index: int = 0
        self._is_urgent: bool = False

        # AI provider for summarizing long event names (optional)
        self._ai_provider = None
        self._summary_cache: dict[str, str] = {}
        ai_conf = (self.args or {}).get("ai_provider_conf")
        if ai_conf:
            try:
                from providers.ai_providers.registry import (
                    build_simple_text_provider,
                    simple_text_config_from_appdaemon_args,
                )
                provider_cfg = simple_text_config_from_appdaemon_args(
                    {"ai_provider_conf": ai_conf}
                )
                self._ai_provider = build_simple_text_provider(provider_cfg)
                self.log("AI provider initialized for event name summarization", level="INFO")
            except Exception as exc:
                self.log(f"Failed to initialize AI provider: {exc!r}", level="WARNING")

        # Build a friendly display_name from the app key
        suffix = self.name.replace("calendar_summary_", "").replace("calendar_summary", "")
        if suffix:
            friendly = suffix.replace("_", " ").title()
            self.display_name = f"Calendar: {friendly}"

        self.register_with_controller()

    def terminate(self) -> None:
        self._stop_listeners()
        self._cancel_rotation_timer()
        self._cancel_countdown_timer()
        self._cancel_cooldown_timer()
        self.deregister_from_controller()

    # ------------------------------------------------------------------
    # Listener management
    # ------------------------------------------------------------------

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

    def _cancel_rotation_timer(self) -> None:
        if self._rotation_handle is not None:
            try:
                self.cancel_timer(self._rotation_handle)
            except Exception:
                pass
            self._rotation_handle = None

    def _cancel_countdown_timer(self) -> None:
        if self._countdown_handle is not None:
            try:
                self.cancel_timer(self._countdown_handle)
            except Exception:
                pass
            self._countdown_handle = None

    def _cancel_cooldown_timer(self) -> None:
        if self._cooldown_handle is not None:
            try:
                self.cancel_timer(self._cooldown_handle)
            except Exception:
                pass
            self._cooldown_handle = None
            self._in_cooldown = False

    def _on_cooldown_timer(self, kwargs: dict) -> None:
        self._cooldown_handle = None
        self._in_cooldown = False
        self.log("Cooldown expired — ready to push again", level="INFO")
        self.create_task(self._run_cycle())

    async def _start_cooldown(self) -> None:
        """Start a random cooldown delay before the automation can push again."""
        import random
        self._cancel_cooldown_timer()
        cfg = self.args or {}
        min_min = float(cfg.get("cooldown_min_minutes",
                                self.DEFAULT_UI_CONFIG["cooldown_min_minutes"]))
        max_min = float(cfg.get("cooldown_max_minutes",
                                self.DEFAULT_UI_CONFIG["cooldown_max_minutes"]))
        min_min = min(min_min, max_min)
        delay_s = random.uniform(min_min, max_min) * 60
        self._in_cooldown = True
        handle = self.run_in(self._on_cooldown_timer, int(delay_s))
        if hasattr(handle, "__await__"):
            handle = await handle
        self._cooldown_handle = handle
        self.log(
            f"Cooldown started: {delay_s / 60:.1f} min "
            f"(range {min_min}-{max_min} min)",
            level="INFO",
        )

    # ------------------------------------------------------------------
    # Enable / disable / config
    # ------------------------------------------------------------------

    def set_enabled(self, enabled: bool) -> None:
        if enabled:
            self._start_listeners()
            self.create_task(self._run_cycle())
        else:
            self._stop_listeners()
            self._cancel_rotation_timer()
            self._cancel_countdown_timer()
            self._cancel_cooldown_timer()

    def on_config_updated(self, config: dict[str, Any]) -> None:
        super().on_config_updated(config)
        if "enabled" in config:
            enabled_val = config["enabled"]
            self.log(f"on_config_updated: enabled={enabled_val!r} type={type(enabled_val).__name__}", level="INFO")
            if enabled_val:
                self._start_listeners()
            else:
                self._stop_listeners()
                self._cancel_rotation_timer()
                self._cancel_countdown_timer()

    # ------------------------------------------------------------------
    # Trigger callbacks
    # ------------------------------------------------------------------

    def _on_calendar_state_change(self, entity: str, attribute: str, old: Any, new: Any, kwargs: dict) -> None:
        self.log(f"Calendar state change on {entity}: {old!r} -> {new!r}", level="DEBUG")
        self.create_task(self._run_cycle())

    def _on_interval(self, kwargs: dict) -> None:
        self.create_task(self._run_cycle())

    def _on_rotation_timer(self, kwargs: dict) -> None:
        self._rotation_handle = None
        self.create_task(self._rotate_to_next_event())

    def _on_countdown_timer(self, kwargs: dict) -> None:
        self._countdown_handle = None
        self.create_task(self._update_countdown())

    # ------------------------------------------------------------------
    # generate_frame (for Preview button compatibility)
    # ------------------------------------------------------------------

    async def generate_frame(self, **kwargs) -> list[list[int]]:
        self.log("generate_frame called", level="DEBUG")
        try:
            await self._run_cycle()
        except Exception as exc:
            self.log(f"generate_frame: _run_cycle failed: {exc!r}", level="ERROR")
        return blank_grid()

    # ------------------------------------------------------------------
    # Core cycle: fetch events → partition → start rotation
    # ------------------------------------------------------------------

    async def _run_cycle(self) -> None:
        """Main entry point: fetch events, decide what to display, start rotation."""
        self.log("_run_cycle starting", level="DEBUG")
        cfg = self.args or {}
        entity_id = str(cfg.get("calendar_entity", ""))
        if not entity_id:
            self.log("No calendar_entity configured", level="WARNING")
            return

        # Get config values
        time_before_hours = float(cfg.get("time_before_event_hours",
                                          self.DEFAULT_UI_CONFIG["time_before_event_hours"]))
        reminder_threshold_min = float(cfg.get("reminder_threshold_minutes",
                                               self.DEFAULT_UI_CONFIG["reminder_threshold_minutes"]))
        ttl_minutes = float(cfg.get("ttl_minutes", self.DEFAULT_UI_CONFIG["ttl_minutes"]))
        floor_minutes = float(cfg.get("rotation_floor_minutes",
                                      self.DEFAULT_UI_CONFIG["rotation_floor_minutes"]))
        force_push = bool(cfg.get("force_push_at_reminder",
                                  self.DEFAULT_UI_CONFIG["force_push_at_reminder"]))
        should_expire = bool(cfg.get("should_expire",
                                     self.DEFAULT_UI_CONFIG["should_expire"]))

        now = datetime.now(tz=timezone.utc)

        # Fetch all events within time_before_event_hours
        all_events = await self._fetch_upcoming_events(
            entity_id, now, time_before_hours
        )

        if not all_events:
            self.log(
                f"No upcoming events within {time_before_hours}h window "
                f"(entity={entity_id!r})",
                level="INFO",
            )
            # Cancel any running rotation/countdown
            self._cancel_rotation_timer()
            self._cancel_countdown_timer()
            self._current_events = []
            return

        # Filter out elapsed events — don't display events that started
        # more than 60 minutes ago (they're done, not upcoming).
        max_elapsed_s = 60 * 60  # 1 hour grace for in-progress events
        active_events = [
            ev for ev in all_events
            if ev["seconds_until"] > -max_elapsed_s
        ]
        if len(active_events) < len(all_events):
            dropped = len(all_events) - len(active_events)
            self.log(
                f"Filtered out {dropped} elapsed event(s) (started > {max_elapsed_s // 60} min ago)",
                level="INFO",
            )
        all_events = active_events

        if not all_events:
            self.log(
                f"No active events remaining after elapsed filter "
                f"(entity={entity_id!r})",
                level="INFO",
            )
            self._cancel_rotation_timer()
            self._cancel_countdown_timer()
            self._current_events = []
            return

        # Partition into urgent vs upcoming
        # Urgent = timed (non-all-day) events within the reminder threshold
        # All-day events are excluded from urgent — they show at midnight
        # and provide no value as force-push notifications.
        reminder_seconds = int(reminder_threshold_min * 60)
        urgent_events = []
        upcoming_events = []

        for ev in all_events:
            seconds_until = ev["seconds_until"]
            is_all_day = ev.get("is_all_day", False)
            if is_all_day:
                # All-day events are never urgent
                upcoming_events.append(ev)
            elif 0 <= seconds_until <= reminder_seconds:
                # Timed event within threshold — urgent
                urgent_events.append(ev)
            elif seconds_until < 0:
                # Already started — treat as urgent (in-progress)
                urgent_events.append(ev)
            else:
                upcoming_events.append(ev)

        if urgent_events:
            display_events = urgent_events
            is_urgent = True
            self.log(
                f"URGENT: {len(urgent_events)} event(s) within reminder threshold "
                f"({reminder_threshold_min} min): "
                f"{[e['summary'] for e in urgent_events]}",
                level="INFO",
            )
        else:
            display_events = all_events
            is_urgent = False

            # Non-urgent: apply cooldown if we're in one
            if self._in_cooldown:
                self.log(
                    "In cooldown — skipping non-urgent push",
                    level="DEBUG",
                )
                return
            self.log(
                f"Upcoming: {len(all_events)} event(s) within {time_before_hours}h window: "
                f"{[e['summary'] for e in all_events]}",
                level="INFO",
            )

        # Calculate rotation parameters
        ttl_s = int(ttl_minutes * 60)
        n_events = len(display_events)
        max_slots = max(1, int(ttl_s / (floor_minutes * 60)))
        shown_count = min(n_events, max_slots)
        overflow = max(0, n_events - max_slots)

        # Pick the events to show (prioritized by soonest start)
        shown_events = display_events[:shown_count]

        # If there's overflow, reserve last slot for summary
        if overflow > 0:
            shown_events = display_events[: shown_count - 1]
            overflow = n_events - len(shown_events)

        display_time_s = max(int(floor_minutes * 60), int(ttl_s / n_events)) if n_events > 0 else ttl_s

        self.log(
            f"Rotation plan: {len(shown_events)} events shown, "
            f"display_time={display_time_s}s ({display_time_s // 60} min), "
            f"overflow={overflow}, urgent={is_urgent}, "
            f"ttl={ttl_s}s, floor={floor_minutes} min",
            level="INFO",
        )

        # Check if event set changed — skip re-push if unchanged and
        # timers are already running (avoids duplicate board writes when
        # both the interval timer and state listener fire close together).
        new_event_key = tuple(e["summary"] for e in shown_events)
        old_event_key = tuple(e["summary"] for e in self._current_events)
        timers_active = (
            self._rotation_handle is not None
            or self._countdown_handle is not None
        )
        if (
            new_event_key == old_event_key
            and is_urgent == self._is_urgent
            and timers_active
        ):
            self.log("Event set unchanged and timers active — skipping re-push", level="DEBUG")
            return

        # Update state
        self._current_events = shown_events
        self._current_overflow = overflow
        self._current_event_index = 0
        self._is_urgent = is_urgent
        self._display_time_s = display_time_s
        self._force_push = force_push and is_urgent
        self._should_expire = should_expire
        self._ttl_s = ttl_s

        # Cancel existing timers and start fresh
        self._cancel_rotation_timer()
        self._cancel_countdown_timer()

        # Push the first event immediately
        await self._push_current_event()

        # Start cooldown for non-urgent pushes so the automation doesn't
        # immediately reclaim the board after its TTL expires.
        # Urgent events skip cooldown — they need to hold the board.
        if not is_urgent:
            await self._start_cooldown()

    # ------------------------------------------------------------------
    # Rotation
    # ------------------------------------------------------------------

    async def _rotate_to_next_event(self) -> None:
        """Advance to the next event in the rotation."""
        if not self._current_events:
            return

        total_slots = len(self._current_events) + (1 if self._current_overflow > 0 else 0)
        self._current_event_index = (self._current_event_index + 1) % total_slots

        self.log(
            f"Rotating to slot {self._current_event_index}/{total_slots - 1}",
            level="DEBUG",
        )

        await self._push_current_event()

    async def _push_current_event(self) -> None:
        """Push the current event (or overflow summary) to the board."""
        total_slots = len(self._current_events) + (1 if self._current_overflow > 0 else 0)

        if self._current_overflow > 0 and self._current_event_index >= len(self._current_events):
            # Overflow summary slot
            grid = _build_overflow_grid(self._current_overflow)
            event_label = f"+{self._current_overflow} more"
        else:
            event = self._current_events[self._current_event_index]
            now = datetime.now(tz=timezone.utc)
            start_dt = _parse_dt(event.get("start_time_str", ""))
            seconds_until = int((start_dt - now).total_seconds()) if start_dt else event.get("seconds_until", 0)
            event_time = _format_event_time(start_dt)
            countdown = _format_countdown(seconds_until)
            display_name = self._get_display_name(event)
            grid = _build_event_grid(display_name, event_time, countdown)
            event_label = event["summary"]

        self.log(
            f"Pushing event [{self._current_event_index}/{total_slots - 1}]: "
            f"{event_label!r} | force_push={self._force_push} "
            f"display_time={self._display_time_s}s",
            level="INFO",
        )

        self.push_frame(
            grid,
            ttl_s=self._display_time_s,
            override_ttl=self._force_push,
            should_expire=self._should_expire,
        )

        # Schedule rotation to next event
        if total_slots > 1:
            handle = self.run_in(
                self._on_rotation_timer, self._display_time_s
            )
            if hasattr(handle, "__await__"):
                handle = await handle
            self._rotation_handle = handle
            self.log(
                f"Rotation timer set: next event in {self._display_time_s}s "
                f"({self._display_time_s // 60} min)",
                level="DEBUG",
            )

        # Schedule countdown update for the current event
        await self._schedule_countdown_update()

    async def _schedule_countdown_update(self) -> None:
        """Schedule the next countdown update for the currently displayed event."""
        self._cancel_countdown_timer()

        if self._current_overflow > 0 and self._current_event_index >= len(self._current_events):
            return  # No countdown for overflow summary

        if not self._current_events:
            return

        event = self._current_events[self._current_event_index]
        start_dt = _parse_dt(event.get("start_time_str", ""))
        if start_dt is None:
            return

        now = datetime.now(tz=timezone.utc)
        seconds_until = int((start_dt - now).total_seconds())
        update_in = _next_countdown_update_seconds(seconds_until)

        # Don't schedule an update past the rotation time
        if hasattr(self, "_display_time_s") and update_in >= self._display_time_s:
            self.log(
                f"Countdown update ({update_in}s) would exceed display_time "
                f"({self._display_time_s}s) — skipping",
                level="DEBUG",
            )
            return

        handle = self.run_in(self._on_countdown_timer, update_in)
        if hasattr(handle, "__await__"):
            handle = await handle
        self._countdown_handle = handle
        self.log(
            f"Countdown update scheduled in {update_in}s for "
            f"{event['summary']!r} (seconds_until={seconds_until})",
            level="DEBUG",
        )

    async def _update_countdown(self) -> None:
        """Re-push the current event with an updated countdown."""
        if not self._current_events:
            return
        if self._current_overflow > 0 and self._current_event_index >= len(self._current_events):
            return

        event = self._current_events[self._current_event_index]
        start_dt = _parse_dt(event.get("start_time_str", ""))
        if start_dt is None:
            return

        now = datetime.now(tz=timezone.utc)
        seconds_until = int((start_dt - now).total_seconds())
        event_time = _format_event_time(start_dt)
        countdown = _format_countdown(seconds_until)
        display_name = self._get_display_name(event)
        grid = _build_event_grid(display_name, event_time, countdown)

        self.log(
            f"Countdown update: {event['summary']!r} → {countdown!r} "
            f"(seconds_until={seconds_until})",
            level="INFO",
        )

        # Same-source push replaces the displayed frame without TTL override
        self.push_frame(
            grid,
            ttl_s=self._display_time_s if hasattr(self, "_display_time_s") else None,
            override_ttl=False,
            should_expire=self._should_expire if hasattr(self, "_should_expire") else False,
        )

        # Schedule the next countdown update
        await self._schedule_countdown_update()

    # ------------------------------------------------------------------
    # AI event name summarization
    # ------------------------------------------------------------------

    _SUMMARIZE_PROMPT = (
        "You are writing a short, human-readable title for a Vestaboard "
        "split-flap display. The board is 22 characters wide. You have at "
        "most 2 lines (44 chars total). Prefer 1 line (22 chars) when possible.\n\n"
        "Rules:\n"
        "- Only uppercase letters A-Z, digits 0-9, spaces, and basic punctuation "
        "(period, comma, exclamation, question mark, hyphen, apostrophe, colon).\n"
        "- Write a friendly, plain-English summary of WHAT the event is. "
        "Think of it as what you'd write on a wall calendar.\n"
        "- Ignore metadata like severity, priority, checklists, or instructions "
        "from the description. Focus only on the core activity.\n"
        "- Do NOT use abbreviations, acronyms, or shorthand unless the original "
        "title uses them. Write it so anyone in the household can understand it.\n"
        "- No newlines in your answer — just one continuous string.\n\n"
        "Return ONLY the summary text, nothing else."
    )

    def _get_display_name(self, event: dict) -> str:
        """Get a board-friendly display name for an event.

        If an AI provider is configured and the event name is longer than
        22 characters, uses the LLM to generate a concise summary.
        Results are cached by event summary to avoid repeated LLM calls.
        Falls back to the raw event name (which gets truncated by the grid).
        """
        raw_name = event.get("summary", "Event")

        # Short names don't need summarization
        if len(raw_name) <= COLS:
            return raw_name

        # Check cache
        if raw_name in self._summary_cache:
            return self._summary_cache[raw_name]

        # No AI provider — use raw name
        if self._ai_provider is None:
            return raw_name

        # Build input with title + description if available
        description = event.get("description", "")
        input_text = f"Event title: {raw_name}"
        if description:
            # Truncate very long descriptions to save tokens
            input_text += f"\nDescription: {description[:300]}"

        try:
            result = self._ai_provider.generate_from_text(
                input_text=input_text,
                instructions=self._SUMMARIZE_PROMPT,
                expected_keys=["summary"],
            )
            summary = result.get("summary", "")
            if not summary:
                # Provider might return the text directly without JSON wrapping
                summary = str(result.get("_raw", raw_name))

            # Clean and validate
            summary = summary.strip().upper()[:COLS * 2]
            if summary:
                self._summary_cache[raw_name] = summary
                self.log(
                    f"AI summary: {raw_name!r} → {summary!r}",
                    level="INFO",
                )
                return summary
        except Exception as exc:
            self.log(
                f"AI summarization failed for {raw_name!r}: {exc!r}",
                level="WARNING",
            )

        return raw_name

    # ------------------------------------------------------------------
    # Event fetching
    # ------------------------------------------------------------------

    async def _fetch_upcoming_events(
        self, entity_id: str, now: datetime, time_before_hours: float
    ) -> list[dict]:
        """Fetch upcoming events from the calendar entity.

        Uses calendar.get_events service to get multiple events, falling
        back to entity state attributes for the current/next event.
        """
        events: list[dict] = []
        end_time = now + timedelta(hours=time_before_hours)

        # Try HA REST API for multi-event support.
        # AppDaemon's call_service doesn't support response data from HA
        # services, so we use the calendar REST endpoint via HaRestClient.
        try:
            from providers.ha_provisioner.ha_rest_client import HaRestClient
            from providers.secrets import resolve_secret

            ha_url = resolve_secret(self.args.get("ha_url_env", "HA_URL"))
            ha_token = resolve_secret(self.args.get("ha_token_env", "TOKEN"))

            async with HaRestClient(ha_url, ha_token) as client:
                cal_events = await client.get(
                    f"/api/calendars/{entity_id}",
                    params={
                        "start": now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                        "end": end_time.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    },
                )

            if isinstance(cal_events, list):
                self.log(
                    f"Calendar REST API returned {len(cal_events)} event(s) "
                    f"for {entity_id!r}",
                    level="INFO",
                )
                for ev in cal_events:
                    parsed = self._parse_service_event(ev, now)
                    if parsed is not None:
                        events.append(parsed)

                events.sort(key=lambda e: e.get("seconds_until", 0))
                return events
        except Exception as exc:
            self.log(
                f"Calendar REST API failed for {entity_id!r}: {exc!r} — "
                f"falling back to entity state",
                level="WARNING",
            )

        # Fallback: read entity state for current/next event
        event = await self._get_event_from_state(entity_id, now, time_before_hours)
        if event is not None:
            events.append(event)

        return events

    def _parse_service_event(self, ev: dict, now: datetime) -> Optional[dict]:
        """Parse a single event from the HA calendar REST API response.

        The API returns ``start``/``end`` as dicts:
        - Timed events:   ``{"dateTime": "2026-03-19T13:00:00-04:00"}``
        - All-day events:  ``{"date": "2026-03-19"}``
        """
        summary = ev.get("summary", "")
        raw_start = ev.get("start")
        raw_end = ev.get("end")

        if not summary or not raw_start:
            return None

        start_str = self._extract_dt_str(raw_start)
        end_str = self._extract_dt_str(raw_end) if raw_end else ""

        if not start_str:
            return None

        start_dt = _parse_dt(start_str)
        if start_dt is None:
            return None

        seconds_until = int((start_dt - now).total_seconds())

        # Detect all-day events: start has "date" key but no "dateTime"
        is_all_day = (
            isinstance(raw_start, dict)
            and "date" in raw_start
            and "dateTime" not in raw_start
        )

        return {
            "summary": summary,
            "description": ev.get("description", ""),
            "start_time_str": start_str,
            "end_time_str": end_str,
            "seconds_until": seconds_until,
            "is_active": seconds_until <= 0,
            "is_all_day": is_all_day,
        }

    @staticmethod
    def _extract_dt_str(value) -> str:
        """Extract an ISO datetime string from a calendar API start/end value.

        Handles both dict form (``{"dateTime": ...}`` or ``{"date": ...}``)
        and plain string form.
        """
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            # Prefer dateTime (timed events) over date (all-day events)
            dt_str = value.get("dateTime") or value.get("date", "")
            # All-day "date" values like "2026-03-19" need a time component
            if dt_str and "T" not in dt_str:
                dt_str = f"{dt_str}T00:00:00"
            return dt_str
        return ""

    async def _get_event_from_state(
        self, entity_id: str, now: datetime, time_before_hours: float
    ) -> Optional[dict]:
        """Read current/next event from HA entity state (single-event fallback)."""
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

            next_summary = attrs.get("message", "")
            next_start_str = attrs.get("start_time", "")
            next_end_str = attrs.get("end_time", "")

            if not next_start_str or not next_summary:
                return None

            start_dt = _parse_dt(next_start_str)
            if start_dt is None:
                return None

            seconds_until = int((start_dt - now).total_seconds())
            max_seconds = int(time_before_hours * 3600)

            if 0 <= seconds_until <= max_seconds:
                return {
                    "summary": next_summary,
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
