"""School Lunch Menu AppDaemon app.

Fetches daily lunch menus from the School Nutrition and Fitness API for
multiple configured schools, publishes structured menu data to a virtual
sensor, and handles card commands for school selection and month navigation.

All external HTTP calls are delegated to ``providers/school_menu/client.py``
(security rule S2 — no HTTP in app code).
"""

from __future__ import annotations

import datetime
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# AppDaemon only adds `appdaemon/apps` to sys.path. Our shared libraries
# live at `appdaemon/providers`, so add the AppDaemon root directory.
sys.path.append(str(Path(__file__).resolve().parents[2]))

import hassapi as hass

from providers.school_menu.client import SchoolMenuClient
from providers.school_menu.types import MenuMonth

logger = logging.getLogger(__name__)

SENSOR_ENTITY_ID = "sensor.school_lunch_menu"
SELECTION_ENTITY_ID = "input_text.school_lunch_selected_schools"


class SchoolLunchApp(hass.Hass):
    """AppDaemon app that fetches and publishes school lunch menus."""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        args = self.args or {}

        # Config from apps.yaml
        self._sid: str = args.get("sid", "")
        self._menus: List[Dict[str, str]] = args.get("menus", [])
        self._default_selected: List[str] = args.get("default_selected", [])
        self._show_tomorrow_after: str = args.get("show_tomorrow_after", "15:00:00")

        # State: resolved menus {name: {download_id, mongo_id, site_code}}
        self._resolved_menus: Dict[str, Dict[str, str]] = {}

        # State: last good school data for sensor (list of school dicts)
        self._school_data: List[Dict[str, Any]] = []

        self.log(
            f"SchoolLunchApp initialising: sid={self._sid}, "
            f"schools={[m['name'] for m in self._menus]}, "
            f"default_selected={self._default_selected}",
            level="INFO",
        )

        # Defer async provisioning + data fetching to startup wrapper
        self.run_in(self._on_startup, 0)

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def _on_startup(self, kwargs: Any) -> None:
        """run_in callback — launches the async startup coroutine."""
        self.create_task(self._async_startup())

    async def _async_startup(self) -> None:
        """Provision entities, resolve IDs, fetch menus, register listeners."""
        await self._provision_entities()

        # Create the client (no session yet — used as async context manager per call)
        self._client = SchoolMenuClient(sid=self._sid)

        # Resolve download IDs to MongoDB IDs for all configured schools
        await self._resolve_menu_ids()

        # Fetch current month menus for all resolved schools
        self._school_data = await self._fetch_all_menus()

        # Publish initial sensor state
        self._publish_sensor(self._school_data)

        # Register event listener for relay commands
        self.listen_event(self._on_command, "school_lunch_command")

        # Schedule daily refresh at 5 AM
        self.run_daily(self._daily_fetch, datetime.time(5, 0))

        self.log(
            f"SchoolLunchApp started: {len(self._resolved_menus)} schools resolved, "
            f"{len(self._school_data)} schools with menu data",
            level="INFO",
        )

    async def _provision_entities(self) -> None:
        """Create the input_text helper and relay script if they don't exist."""
        ha_url = self.args.get("ha_url")
        ha_token_env = self.args.get("ha_token_env")
        if not ha_url or not ha_token_env:
            self.log(
                "ha_url / ha_token_env not configured — skipping provisioning",
                level="WARNING",
            )
            return

        from providers.ha_provisioner import HAProvisioner

        prov = HAProvisioner(ha_url=ha_url, ha_token_env=ha_token_env)

        # Provision the selected-schools input_text helper
        try:
            initial_value = json.dumps(self._default_selected)
            created = await prov.ensure_helper(
                "input_text",
                "School Lunch Selected Schools",
                max=255,
                initial=initial_value,
            )
            msg = "created" if created else "already exists"
            self.log(
                f"Helper {SELECTION_ENTITY_ID} {msg}",
                level="INFO" if created else "DEBUG",
            )
            # Set the default value if the helper was just created
            if created:
                self.call_service(
                    "input_text/set_value",
                    entity_id=SELECTION_ENTITY_ID,
                    value=initial_value,
                )
        except Exception as exc:
            self.log(
                f"Failed to provision input_text helper: {exc!r}",
                level="ERROR",
            )

        # Provision the relay script
        try:
            created = await prov.ensure_script("school_lunch_relay", {
                "alias": "School Lunch Relay",
                "description": "Relays dashboard commands to AppDaemon",
                "mode": "queued",
                "max": 10,
                "fields": {
                    "command": {
                        "name": "Command",
                        "description": "Command name",
                        "required": True,
                        "selector": {"text": {}},
                    },
                    "payload": {
                        "name": "Payload",
                        "description": "JSON-encoded command data",
                        "required": False,
                        "selector": {"text": {}},
                    },
                },
                "sequence": [{
                    "event": "school_lunch_command",
                    "event_data": {
                        "command": "{{ command }}",
                        "payload": "{{ payload | default('{}') }}",
                    },
                }],
            })
            msg = "created" if created else "already exists"
            self.log(
                f"Relay script.school_lunch_relay {msg}",
                level="INFO" if created else "DEBUG",
            )
        except Exception as exc:
            self.log(
                f"Failed to provision relay script: {exc!r}",
                level="ERROR",
            )

    async def _resolve_menu_ids(self) -> None:
        """Resolve all configured download IDs to MongoDB IDs."""
        async with self._client:
            for menu_cfg in self._menus:
                name = menu_cfg.get("name", "")
                download_id = menu_cfg.get("download_id", "")
                if not name or not download_id:
                    self.log(
                        f"Skipping menu entry with missing name/download_id: {menu_cfg}",
                        level="WARNING",
                    )
                    continue
                try:
                    result = await self._client.resolve_menu_id(download_id)
                    self._resolved_menus[name] = {
                        "download_id": download_id,
                        "mongo_id": result["id"],
                        "site_code": result.get("site_code", ""),
                    }
                    self.log(
                        f"Resolved '{name}' download_id={download_id} "
                        f"-> mongo_id={result['id']}",
                        level="DEBUG",
                    )
                except Exception as exc:
                    self.log(
                        f"Failed to resolve menu ID for '{name}' "
                        f"(download_id={download_id}): {exc!r}",
                        level="ERROR",
                    )

        self.log(
            f"ID resolution complete: {len(self._resolved_menus)}/{len(self._menus)} "
            f"schools resolved",
            level="INFO",
        )

    # ------------------------------------------------------------------
    # Menu fetching
    # ------------------------------------------------------------------

    async def _fetch_all_menus(self) -> List[Dict[str, Any]]:
        """Fetch current month menu for all resolved schools.

        Returns a list of school dicts suitable for sensor attributes.
        Per-school errors are logged but don't abort the whole batch.
        """
        schools = []
        async with self._client:
            for name, info in self._resolved_menus.items():
                mongo_id = info["mongo_id"]
                try:
                    menu_month = await self._client.fetch_menu(mongo_id)
                    school_dict = self._build_school_dict(name, menu_month)
                    schools.append(school_dict)
                    self.log(
                        f"Fetched menu for '{name}': "
                        f"month={menu_month.display_month}/{menu_month.year}, "
                        f"days={len(menu_month.days)}",
                        level="DEBUG",
                    )
                except Exception as exc:
                    self.log(
                        f"Failed to fetch menu for '{name}' "
                        f"(mongo_id={mongo_id}): {exc!r}",
                        level="WARNING",
                    )
        return schools

    @staticmethod
    def _build_school_dict(name: str, menu_month: MenuMonth) -> Dict[str, Any]:
        """Build a school dict for the sensor attributes from a MenuMonth.

        Each item gets a ``role`` of ``"option"`` or ``"includes"``:

        - Items whose lowercased name appears on >= 75% of menu days
          are tagged ``"includes"`` (e.g. "Chilled Fruit", "Milk Choice").
        - Items starting with "OR " / "or " get the prefix stripped and
          are tagged ``"option"``.
        - Everything else is ``"option"``.
        """
        # 1. Count how many days each item name appears on
        days_with_items = [d for d in menu_month.days if d.items]
        total_days = len(days_with_items)

        name_day_count: Dict[str, int] = {}
        for menu_day in days_with_items:
            seen: set = set()
            for item in menu_day.items:
                low = item.name.strip().lower()
                if low and low not in seen:
                    name_day_count[low] = name_day_count.get(low, 0) + 1
                    seen.add(low)

        # 2. Items on >= 75% of days → includes
        threshold = max(1, int(total_days * 0.75))
        includes_names = {
            n for n, count in name_day_count.items() if count >= threshold
        }

        # 3. Build days with classified items
        import re
        or_prefix_re = re.compile(r"^or\s+", re.IGNORECASE)

        days = []
        for menu_day in menu_month.days:
            items = []
            for item in menu_day.items:
                raw_name = item.name.strip()
                low = raw_name.lower()

                if low in includes_names:
                    items.append({"name": raw_name, "role": "includes"})
                else:
                    # Strip "OR " prefix for cleaner display
                    cleaned = or_prefix_re.sub("", raw_name)
                    items.append({"name": cleaned, "role": "option"})

            day_dict: Dict[str, Any] = {"day": menu_day.day, "items": items}
            if menu_day.notice:
                day_dict["notice"] = menu_day.notice
            days.append(day_dict)

        return {
            "name": name,
            "month": menu_month.display_month,  # 1-indexed for display
            "year": menu_month.year,
            "days": days,
            "prev_month_id": menu_month.previous_month_id,
            "next_month_id": menu_month.next_month_id,
        }

    # ------------------------------------------------------------------
    # Sensor publication
    # ------------------------------------------------------------------

    def _publish_sensor(
        self,
        schools: List[Dict[str, Any]],
        state: str = "ok",
    ) -> None:
        """Publish (or update) sensor.school_lunch_menu."""
        attrs = {
            "schools": schools,
            "show_tomorrow_after": self._show_tomorrow_after,
            "last_updated": datetime.datetime.now().isoformat(timespec="seconds"),
            "friendly_name": "School Lunch Menu",
            "icon": "mdi:silverware-fork-knife",
        }
        self.log(
            f"Publishing sensor: state={state}, schools={len(schools)}",
            level="DEBUG",
        )
        self.set_state(SENSOR_ENTITY_ID, state=state, attributes=attrs)

    # ------------------------------------------------------------------
    # Daily fetch schedule
    # ------------------------------------------------------------------

    def _daily_fetch(self, kwargs: Any) -> None:
        """Scheduled callback at 5 AM — triggers async menu refresh."""
        self.log("Daily menu fetch starting", level="INFO")
        self.create_task(self._do_daily_fetch())

    async def _do_daily_fetch(self) -> None:
        """Fetch all menus, update sensor; keep stale data on partial failure."""
        try:
            new_data = await self._fetch_all_menus()
        except Exception as exc:
            self.log(
                f"Daily fetch failed entirely: {exc!r}",
                level="ERROR",
            )
            self._publish_sensor(self._school_data, state="error")
            return

        if not new_data:
            self.log(
                "Daily fetch returned no data for any school",
                level="ERROR",
            )
            self._publish_sensor(self._school_data, state="error")
            return

        # Merge: use new data for schools that fetched OK, keep stale for failures
        merged = self._merge_school_data(self._school_data, new_data)
        self._school_data = merged

        self.log(
            f"Daily fetch complete: {len(new_data)} schools updated",
            level="INFO",
        )
        self._publish_sensor(self._school_data)

    def _merge_school_data(
        self,
        old: List[Dict[str, Any]],
        new: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Merge new school data into old, preserving stale entries for failures."""
        new_by_name = {s["name"]: s for s in new}
        old_by_name = {s["name"]: s for s in old}

        # Build merged list: new data takes precedence, old kept for missing
        merged = []
        for name in self._resolved_menus:
            if name in new_by_name:
                merged.append(new_by_name[name])
            elif name in old_by_name:
                self.log(
                    f"Using stale data for '{name}' (fetch failed)",
                    level="WARNING",
                )
                merged.append(old_by_name[name])
        return merged

    # ------------------------------------------------------------------
    # Command handler
    # ------------------------------------------------------------------

    def _on_command(self, event_name: str, data: dict, kwargs: Any) -> None:
        """Route commands dispatched by the relay script."""
        cmd = data.get("command")
        raw = data.get("payload", "{}")
        try:
            payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except (json.JSONDecodeError, TypeError):
            self.log(f"Invalid command payload: {raw!r}", level="WARNING")
            return

        self.log(f"Received command: {cmd}", level="DEBUG")

        if cmd == "select_schools":
            self._handle_select_schools(payload)
        elif cmd == "fetch_month":
            self._handle_fetch_month(payload)
        else:
            self.log(f"Unknown command: {cmd!r}", level="WARNING")

    def _handle_select_schools(self, payload: dict) -> None:
        """Update the selected-schools input_text entity."""
        schools = payload.get("schools", [])
        if not schools:
            self.log(
                "select_schools command rejected: must select at least 1 school",
                level="WARNING",
            )
            return

        value = json.dumps(schools)
        self.call_service(
            "input_text/set_value",
            entity_id=SELECTION_ENTITY_ID,
            value=value,
        )
        self.log(
            f"Selected schools updated: {schools}",
            level="INFO",
        )

    def _handle_fetch_month(self, payload: dict) -> None:
        """Fetch a specific month for a school and update the sensor."""
        school_name = payload.get("school", "")
        menu_id = payload.get("menu_id", "")

        if not school_name or not menu_id:
            self.log(
                f"fetch_month command missing school/menu_id: {payload!r}",
                level="WARNING",
            )
            return

        self.log(
            f"Fetching month for '{school_name}', menu_id={menu_id}",
            level="DEBUG",
        )
        self.create_task(self._do_fetch_month(school_name, menu_id))

    async def _do_fetch_month(self, school_name: str, menu_id: str) -> None:
        """Async: fetch a specific month and update the sensor."""
        try:
            async with self._client:
                menu_month = await self._client.fetch_menu(menu_id)
            school_dict = self._build_school_dict(school_name, menu_month)
        except Exception as exc:
            self.log(
                f"Failed to fetch month for '{school_name}' "
                f"(menu_id={menu_id}): {exc!r}",
                level="WARNING",
            )
            return

        # Replace or add this school's data in the current state
        updated = [
            school_dict if s["name"] == school_name else s
            for s in self._school_data
        ]
        # If school wasn't already in the list, append it
        if school_name not in {s["name"] for s in self._school_data}:
            updated.append(school_dict)

        self._school_data = updated
        self._publish_sensor(self._school_data)
        self.log(
            f"Updated menu for '{school_name}': "
            f"month={menu_month.display_month}/{menu_month.year}",
            level="INFO",
        )
