"""Diff desired events against HA calendar and apply creates/deletes."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from calendar_from_schedule_app.event_generator import EventInstance
from calendar_from_schedule_app.sync_state import SyncState

logger = logging.getLogger(__name__)


async def _fetch_existing_events(
    ha_client: Any,
    calendar_entity_id: str,
    start: date,
    end: date,
) -> List[Dict[str, Any]]:
    """Fetch events from HA calendar via REST API."""
    start_str = start.isoformat()
    end_str = end.isoformat()
    path = f"/api/calendars/{calendar_entity_id}?start={start_str}&end={end_str}"
    try:
        return await ha_client.get(path)
    except Exception as e:
        logger.error("Failed to fetch calendar events: %s", e)
        return []


async def _create_event(
    app: Any,
    calendar_entity_id: str,
    event: EventInstance,
) -> Optional[str]:
    """Create a calendar event via HA service call."""
    try:
        await app.call_service(
            "calendar/create_event",
            entity_id=calendar_entity_id,
            summary=event.title,
            description=event.description,
            start_date=event.start_date.isoformat(),
            end_date=event.end_date.isoformat(),
        )
        return event.sync_key
    except Exception as e:
        logger.error("Failed to create event '%s': %s", event.title, e)
        return None


async def _delete_event(
    app: Any,
    calendar_entity_id: str,
    uid: str,
) -> bool:
    """Delete a calendar event by UID via HA service call."""
    try:
        await app.call_service(
            "calendar/delete_event",
            entity_id=calendar_entity_id,
            uid=uid,
        )
        return True
    except Exception as e:
        logger.error("Failed to delete event UID '%s': %s", uid, e)
        return False


def _match_events_to_state(
    ha_events: List[Dict[str, Any]],
    sync_state: SyncState,
) -> Dict[str, str]:
    """Build a mapping of sync_key -> HA UID from fetched events and state."""
    # The sync state is the source of truth for sync_key -> UID mapping.
    # We verify UIDs still exist in the fetched events.
    existing_uids = set()
    for ev in ha_events:
        uid = ev.get("uid")
        if uid:
            existing_uids.add(uid)

    verified: Dict[str, str] = {}
    for sync_key, uid in sync_state.events.items():
        if uid in existing_uids:
            verified[sync_key] = uid

    return verified


async def sync_calendar(
    app: Any,
    calendar_entity_id: str,
    desired_events: List[EventInstance],
    sync_state: SyncState,
    ha_client: Any,
    *,
    full_resync: bool = False,
) -> SyncState:
    """Sync desired events to the HA calendar. Returns updated sync state.

    When full_resync is True (e.g. schedule file changed), all existing
    events are deleted and all desired events are created fresh.
    """
    # Determine the date range to fetch
    if not desired_events:
        return sync_state

    min_date = min(e.start_date for e in desired_events)
    max_date = max(e.end_date for e in desired_events)

    # Fetch existing events from HA
    ha_events = await _fetch_existing_events(ha_client, calendar_entity_id, min_date, max_date)

    # Build verified sync_key -> UID map
    verified = _match_events_to_state(ha_events, sync_state)

    # Build desired sync_key set
    desired_keys = {e.sync_key for e in desired_events}
    desired_map = {e.sync_key: e for e in desired_events}

    if full_resync:
        # Delete everything, recreate everything
        to_delete = dict(verified)
        to_create = list(desired_events)
    else:
        # Determine what to delete (in state but not desired)
        to_delete = {k: uid for k, uid in verified.items() if k not in desired_keys}

        # Determine what to create (desired but not in verified state)
        to_create = [desired_map[k] for k in desired_keys if k not in verified]

    logger.info(
        "Sync: %d desired, %d existing, %d to create, %d to delete",
        len(desired_events),
        len(verified),
        len(to_create),
        len(to_delete),
    )

    new_state = SyncState(
        file_hash=sync_state.file_hash,
        horizon_end=sync_state.horizon_end,
        events=dict(verified),
        last_sync_iso=datetime.now(timezone.utc).isoformat(),
    )

    # Remove stale events from state
    for sync_key, uid in to_delete.items():
        logger.info("Deleting event: %s (uid=%s)", sync_key, uid)
        if await _delete_event(app, calendar_entity_id, uid):
            new_state.events.pop(sync_key, None)
        await asyncio.sleep(0.15)

    # Create new events
    for event in to_create:
        logger.info(
            "Creating event: %s on %s (%s)",
            event.title, event.start_date.isoformat(), event.sync_key,
        )
        result = await _create_event(app, calendar_entity_id, event)
        if result:
            # We don't get the UID back from create_event, so store sync_key as placeholder.
            # On next sync, we'll match by fetching events again.
            new_state.events[event.sync_key] = f"pending_{event.sync_key}"
        await asyncio.sleep(0.15)

    return new_state


async def refresh_uids(
    app: Any,
    calendar_entity_id: str,
    sync_state: SyncState,
    ha_client: Any,
    desired_events: List[EventInstance],
) -> SyncState:
    """Re-fetch events from HA to resolve pending UIDs after creation."""
    if not desired_events:
        return sync_state

    min_date = min(e.start_date for e in desired_events)
    max_date = max(e.end_date for e in desired_events)

    ha_events = await _fetch_existing_events(ha_client, calendar_entity_id, min_date, max_date)

    # Build a lookup by summary + start_date for matching
    desired_map = {e.sync_key: e for e in desired_events}

    # Try to match HA events to desired events by summary and date
    for ha_ev in ha_events:
        uid = ha_ev.get("uid")
        summary = ha_ev.get("summary", "")
        start_raw = ha_ev.get("start", {})
        start_str = start_raw.get("date") if isinstance(start_raw, dict) else str(start_raw)

        if not uid or not start_str:
            continue

        for sync_key, desired in desired_map.items():
            if desired.title == summary and desired.start_date.isoformat() == start_str:
                if sync_key in sync_state.events:
                    sync_state.events[sync_key] = uid
                break

    return sync_state
