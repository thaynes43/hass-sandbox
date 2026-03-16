"""Async HTTP client for the School Nutrition and Fitness API.

Pure-Python — no AppDaemon dependency.  Uses ``aiohttp`` for all I/O.

The site exposes two useful APIs:

1. **downloadMenu.php** redirect — resolves a human-facing numeric menu ID
   to an internal MongoDB ObjectId via a 302 redirect.

2. **GraphQL** at ``https://api.schoolnutritionandfitness.com/graphql`` —
   returns structured menu data (items per day, categories, allergens).

Typical flow:
    1. Resolve download IDs once → get MongoDB IDs + site codes.
    2. Fetch current month via GraphQL using the MongoDB ID.
    3. Navigate to next/previous months via ``previousMonthPublished``
       and ``nextMonthPublished`` links in the response.
"""

from __future__ import annotations

import calendar
import logging
import re
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from .types import MenuDay, MenuItem, MenuMonth

logger = logging.getLogger(__name__)

GRAPHQL_URL = "https://api.schoolnutritionandfitness.com/graphql"
SITE_BASE_URL = "https://www.schoolnutritionandfitness.com"

# GraphQL query to fetch menu data for a given month
MENU_QUERY = """\
{
    menu(id: "%s") {
        id
        month
        year
        menuType {
            id
            name
        }
        items {
            day
            month
            year
            product {
                name
                category
                hide_on_calendars
                is_ancillary
            }
        }
        previousMonthPublished { id }
        nextMonthPublished { id }
    }
}"""


class SchoolMenuClient:
    """Async client for fetching school lunch menus.

    Callers are responsible for opening / closing the underlying
    ``aiohttp.ClientSession`` (or using this class as an async context
    manager).
    """

    def __init__(
        self,
        sid: str,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> None:
        self.sid = sid
        self._owns_session = session is None
        self._session = session

    # -- Context manager -----------------------------------------------------

    async def __aenter__(self) -> "SchoolMenuClient":
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError(
                "SchoolMenuClient has no active session — "
                "use 'async with SchoolMenuClient(...)' or pass a session"
            )
        return self._session

    # -- ID resolution -------------------------------------------------------

    async def resolve_menu_id(self, download_id: str) -> Dict[str, str]:
        """Resolve a numeric download ID to a MongoDB ObjectId.

        Follows the 302 redirect from ``downloadMenu.php`` and parses the
        ``id`` and ``siteCode`` from the Location header.

        Returns a dict with keys ``id`` and ``site_code``.
        """
        url = f"{SITE_BASE_URL}/downloadMenu.php/{self.sid}/{download_id}"
        logger.info("Resolving download ID %s via %s", download_id, url)

        async with self.session.get(url, allow_redirects=False) as resp:
            location = resp.headers.get("Location", "")

        if not location:
            raise ValueError(
                f"No redirect for download ID {download_id} — "
                f"got status {resp.status}"
            )

        # Parse id= and siteCode= from the redirect URL fragment
        # Example: .../webmenus2/#/view-no-design?id=69652cd2...&siteCode=244
        params: Dict[str, str] = {}
        fragment = location.split("#")[-1] if "#" in location else location
        query_part = fragment.split("?")[-1] if "?" in fragment else ""
        for pair in query_part.split("&"):
            if "=" in pair:
                key, value = pair.split("=", 1)
                params[key] = value

        mongo_id = params.get("id", "")
        site_code = params.get("siteCode", "")

        if not mongo_id:
            raise ValueError(
                f"Could not parse MongoDB ID from redirect: {location}"
            )

        logger.info(
            "Resolved download ID %s → mongo_id=%s, site_code=%s",
            download_id, mongo_id, site_code,
        )
        return {"id": mongo_id, "site_code": site_code}

    # -- GraphQL menu fetch --------------------------------------------------

    async def fetch_menu(self, menu_id: str) -> MenuMonth:
        """Fetch a month of menu data via GraphQL + content notices.

        Parameters
        ----------
        menu_id : str
            The MongoDB ObjectId for the menu month.

        Returns
        -------
        MenuMonth
            Parsed menu data with items grouped by day, including
            notice days (holidays, early releases) from content overlays.
        """
        query = MENU_QUERY % menu_id
        logger.info("Fetching menu %s via GraphQL", menu_id)

        async with self.session.post(
            GRAPHQL_URL,
            json={"query": query},
            headers={"Content-Type": "application/json"},
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()

        menu_data = data.get("data", {}).get("menu")
        if not menu_data:
            errors = data.get("errors", [])
            raise ValueError(
                f"GraphQL returned no menu for id={menu_id}: {errors}"
            )

        menu_month = self._parse_menu(menu_data)

        # Fetch content overlays (notices) from REST API and merge
        try:
            content = await self._fetch_content(menu_id)
            if content:
                self._merge_notices(menu_month, content)
        except Exception as exc:
            logger.warning(
                "Failed to fetch content notices for %s: %s", menu_id, exc
            )

        return menu_month

    # -- Content overlay (notices) -------------------------------------------

    async def _fetch_content(self, menu_id: str) -> List[Dict[str, Any]]:
        """Fetch the content overlays from the REST API."""
        url = (
            f"{SITE_BASE_URL}/webmenus2/api/"
            f"menuController.php/open-raw?id={menu_id}"
        )
        async with self.session.get(url) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
        return data.get("content", [])

    @staticmethod
    def _merge_notices(
        menu_month: MenuMonth,
        content: List[Dict[str, Any]],
    ) -> None:
        """Map content overlay notices to missing calendar days.

        The school's system stores holiday/early-release announcements as
        positioned HTML text boxes on the calendar design.  This method:

        1. Parses content for notice keywords (NO SCHOOL, EARLY RELEASE, etc.)
        2. Builds the Mon-Fri calendar grid for the month.
        3. Finds weekdays that have no menu items (missing days).
        4. Matches notices to missing days by calendar row (week),
           sorted left-to-right within each row.
        """
        # 1. Extract notices with their positions
        notice_kw = (
            "NO SCHOOL", "EARLY RELEASE", "GRAB AND GO",
            "HOLIDAY", "VACATION", "CLOSED",
        )
        notices: List[Dict[str, Any]] = []
        for c in content:
            html = c.get("html", "")
            text = re.sub(r"<[^>]+>", " ", html).strip()
            text = re.sub(r"&nbsp;", " ", text)
            text = re.sub(r"\s+", " ", text)
            if not any(kw in text.upper() for kw in notice_kw):
                continue
            try:
                left = float(str(c.get("left", "0")).rstrip("%"))
                top = float(str(c.get("top", "0")).rstrip("%"))
            except (ValueError, TypeError):
                continue
            notices.append({"text": text, "left": left, "top": top})

        if not notices:
            return

        # 2. Build Mon-Fri grid for this month
        display_month = menu_month.display_month  # 1-indexed
        year = menu_month.year
        cal_weeks = calendar.monthcalendar(year, display_month)
        grid: List[List[Optional[int]]] = []
        for week in cal_weeks:
            row = [d if d != 0 else None for d in week[0:5]]
            if any(d is not None for d in row):
                grid.append(row)

        n_rows = len(grid)
        if n_rows == 0:
            return

        # 3. Find days that already have menu items
        existing_days = {d.day for d in menu_month.days}

        # 4. Grid geometry (empirical from observed calendars)
        grid_top = 10.0
        grid_bottom = 95.0
        row_height = (grid_bottom - grid_top) / n_rows

        # 5. Match notices to missing days per week row
        month_val = menu_month.month  # 0-indexed for MenuDay
        for row_idx, week in enumerate(grid):
            row_top = grid_top + row_height * row_idx
            row_bottom = row_top + row_height

            # Missing weekdays in this row
            missing: List[Tuple[int, int]] = []  # (col, day)
            for col, day in enumerate(week):
                if day is not None and day not in existing_days:
                    missing.append((col, day))

            if not missing:
                continue

            # Notices whose top falls in this row, sorted left-to-right
            row_notices = sorted(
                [n for n in notices if row_top <= n["top"] < row_bottom],
                key=lambda n: n["left"],
            )

            if not row_notices:
                continue

            # Assign notices to missing days (both sorted left-to-right)
            missing.sort(key=lambda x: x[0])
            for i, (col, day) in enumerate(missing):
                notice_text = (
                    row_notices[i]["text"] if i < len(row_notices) else ""
                )
                if not notice_text:
                    continue

                # Build the notice MenuDay
                notice_items: List[MenuItem] = []
                upper = notice_text.upper()
                if "GRAB AND GO" in upper:
                    notice_items.append(
                        MenuItem(
                            name="Grab and Go Breakfast & Lunch",
                            category="Entrees",
                        )
                    )

                menu_month.days.append(
                    MenuDay(
                        day=day,
                        month=month_val,
                        year=year,
                        items=notice_items,
                        notice=notice_text,
                    )
                )
                logger.debug(
                    "Added notice for day %d: %s", day, notice_text
                )

        # Re-sort days by day number
        menu_month.days.sort(key=lambda d: d.day)

    # -- Parsing -------------------------------------------------------------

    @staticmethod
    def _parse_menu(data: Dict[str, Any]) -> MenuMonth:
        """Parse a GraphQL menu response into a MenuMonth."""
        items_raw = data.get("items", [])

        # Group items by day
        by_day: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
        for item in items_raw:
            product = item.get("product")
            if not product:
                continue
            # Skip items hidden on calendars
            if product.get("hide_on_calendars") == "1":
                continue
            by_day[item["day"]].append(item)

        days: List[MenuDay] = []
        for day_num in sorted(by_day.keys()):
            day_items: List[MenuItem] = []
            for item in by_day[day_num]:
                product = item["product"]
                day_items.append(
                    MenuItem(
                        name=product.get("name", ""),
                        category=product.get("category", ""),
                        is_ancillary=product.get("is_ancillary", False),
                    )
                )
            days.append(
                MenuDay(
                    day=day_num,
                    month=item.get("month", data.get("month", 0)),
                    year=item.get("year", data.get("year", 0)),
                    items=day_items,
                )
            )

        prev_id = None
        if data.get("previousMonthPublished"):
            prev_id = data["previousMonthPublished"].get("id")

        next_id = None
        if data.get("nextMonthPublished"):
            next_id = data["nextMonthPublished"].get("id")

        menu_type = data.get("menuType", {})

        return MenuMonth(
            menu_id=data.get("id", ""),
            menu_type_name=menu_type.get("name", ""),
            month=data.get("month", 0),
            year=data.get("year", 0),
            days=days,
            previous_month_id=prev_id,
            next_month_id=next_id,
        )
