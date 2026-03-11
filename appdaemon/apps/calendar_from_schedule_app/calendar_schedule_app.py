"""Calendar from Schedule — AppDaemon app.

Reads a generic YAML maintenance schedule, expands recurring tasks into
concrete calendar events, and syncs them to a Home Assistant local calendar
using a rolling horizon. Periodically re-syncs to extend the horizon and
detect schedule file changes.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# AppDaemon only adds `appdaemon/apps` to sys.path. Shared libraries
# live at `appdaemon/providers`, so add the AppDaemon root directory.
sys.path.append(str(Path(__file__).resolve().parents[2]))

import hassapi as hass

from calendar_from_schedule_app.calendar_sync import refresh_uids, sync_calendar
from calendar_from_schedule_app.event_generator import generate_events
from calendar_from_schedule_app.schedule_parser import compute_file_hash, load_schedule
from calendar_from_schedule_app.sync_state import load_sync_state, save_sync_state
from providers.secrets import resolve_arg_secret, resolve_secret

logger = logging.getLogger(__name__)


class CalendarScheduleApp(hass.Hass):
    """Sync a YAML schedule file to a HA local calendar."""

    def initialize(self) -> None:
        args = self.args or {}

        self._schedule_dir: str = args.get("schedule_dir", "/media/calendar-schedules")
        self._schedule_file: str = args["schedule_file"]
        self._calendar_entity_id: str = args["calendar_entity_id"]
        self._state_dir: str = args.get("state_dir", "/media/calendar-schedules/sync-state")
        self._ha_url: str = str(resolve_arg_secret(args, "ha_url", required=True))
        self._ha_token_env: str = args["ha_token_env"]
        self._horizon_days: int = int(args.get("horizon_days", 90))
        self._sync_interval_hours: float = float(args.get("sync_interval_hours", 6))

        schedule_path = os.path.join(self._schedule_dir, self._schedule_file)
        state_path = os.path.join(self._state_dir, f"{self._schedule_file}.state.json")

        self._schedule_path = schedule_path
        self._state_path = state_path

        self.run_in(self._deferred_start, 2)

    def _deferred_start(self, kwargs: dict) -> None:
        self.create_task(self._async_startup())

    async def _async_startup(self) -> None:
        # Verify schedule file exists
        if not os.path.exists(self._schedule_path):
            self.log(
                f"Schedule file not found: {self._schedule_path}. "
                "Ensure the file is copied to the schedule directory.",
                level="ERROR",
            )
            return

        # Verify calendar entity exists
        state = await self.get_state(self._calendar_entity_id)
        if state is None:
            self.log(
                f"Calendar entity '{self._calendar_entity_id}' not found. "
                "Create a Local Calendar integration in HA first.",
                level="ERROR",
            )
            return

        self.log(f"Starting sync for schedule: {self._schedule_file}")
        await self._do_sync()

        # Schedule periodic sync
        interval_seconds = int(self._sync_interval_hours * 3600)
        self.run_every(
            self._periodic_sync,
            f"now+{interval_seconds}",
            interval_seconds,
        )
        self.log(f"Periodic sync scheduled every {self._sync_interval_hours} hours")

    def _periodic_sync(self, kwargs: dict) -> None:
        self.create_task(self._do_sync())

    async def _do_sync(self) -> None:
        try:
            # Load current state
            sync_state = load_sync_state(self._state_path)

            # Check file hash
            current_hash = compute_file_hash(self._schedule_path)
            horizon_end = date.today() + timedelta(days=self._horizon_days)
            horizon_str = horizon_end.isoformat()

            if (
                current_hash == sync_state.file_hash
                and sync_state.horizon_end == horizon_str
            ):
                self.log("No changes detected, skipping sync")
                return

            # File changed — full resync (delete all old, create all new)
            file_changed = current_hash != sync_state.file_hash
            if file_changed:
                self.log(
                    "Schedule file changed, performing full resync "
                    "(all existing events will be deleted and recreated)"
                )

            # Load and generate
            schedule = load_schedule(self._schedule_path)
            desired_events = generate_events(schedule, horizon_end)
            self.log(f"Generated {len(desired_events)} events through {horizon_str}")

            # Create HA REST client
            from providers.ha_provisioner.ha_rest_client import HaRestClient

            token = resolve_secret(self._ha_token_env)
            async with HaRestClient(self._ha_url, token) as ha_client:
                # Update state metadata before sync
                sync_state.file_hash = current_hash
                sync_state.horizon_end = horizon_str

                # Sync to calendar
                new_state = await sync_calendar(
                    app=self,
                    calendar_entity_id=self._calendar_entity_id,
                    desired_events=desired_events,
                    sync_state=sync_state,
                    ha_client=ha_client,
                    full_resync=file_changed,
                )

                # Refresh UIDs for any pending entries
                new_state = await refresh_uids(
                    app=self,
                    calendar_entity_id=self._calendar_entity_id,
                    sync_state=new_state,
                    ha_client=ha_client,
                    desired_events=desired_events,
                )

            # Persist state
            save_sync_state(new_state, self._state_path)
            self.log(
                f"Sync complete: {len(new_state.events)} events tracked, "
                f"horizon={horizon_str}"
            )

        except Exception:
            self.log("Sync failed", level="ERROR")
            self.log(self._format_traceback(), level="ERROR")

    def _format_traceback(self) -> str:
        import traceback

        return traceback.format_exc()
