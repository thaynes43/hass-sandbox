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
from typing import Any, Dict, List, Optional

import aiohttp

from .types import MenuDay, MenuItem, MenuMonth

logger = logging.getLogger(__name__)

GRAPHQL_URL = "https://api.schoolnutritionandfitness.com/graphql"
SITE_BASE_URL = "https://www.schoolnutritionandfitness.com"

# Where the Mon-Fri calendar grid sits on a menu design, in percent of the
# page (content boxes are positioned in the same units).  Empirical, from the
# district's 2026 designs: the rows fill 10%-95% top to bottom, the five
# weekday columns fill 5%-82% left to right, and a sidebar of standing text
# sits to the right of them.  A notice box starts about 2% inside its day's
# column: Monday at 6.9%, Tuesday at 21.7%-22.1%, Thursday at 53.6%.
GRID_TOP = 10.0
GRID_BOTTOM = 95.0
GRID_LEFT = 5.0
GRID_RIGHT = 82.0
GRID_COLUMNS = 5
GRID_COLUMN_WIDTH = (GRID_RIGHT - GRID_LEFT) / GRID_COLUMNS


def _percent(value: Any) -> float:
    """Parse a content box coordinate such as ``"22.13%"`` (or a number)."""
    return float(str(value).strip().rstrip("%"))

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
        4. Places each notice in the grid cell where the box starts: the row
           from its top edge, the column from the centre of its first
           column's worth of width (so a banner spanning several days lands
           on its first day).  A notice is kept only when that cell is a
           missing day of this month; any other notice is logged and dropped.

        A notice never moves to another day.  Up to 1.25.1 the notices of a
        week were handed out left to right to that week's missing days, so in
        a week with a holiday that has no notice box and an early release that
        has one, the early release landed on the holiday.  October 2026:
        Monday 12 (no menu and no notice box) got Tuesday 13's early-release
        notice, and Tuesday read as no school.  June 2025 had the same shift
        (Thursday 19 got Friday 20's notice).
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
                left = _percent(c.get("left", "0"))
                top = _percent(c.get("top", "0"))
                width = _percent(c.get("width") or "0")
            except (ValueError, TypeError):
                logger.warning(
                    "Notice dropped: unreadable box position "
                    "(left=%r, top=%r, width=%r): %s",
                    c.get("left"), c.get("top"), c.get("width"), text,
                )
                continue
            # Anchor on the box's start: a box wider than a column is a
            # banner over several days, and its own centre would sit in a
            # later one.  (The row already comes from the top edge.)
            x = left + min(width, GRID_COLUMN_WIDTH) / 2
            notices.append({"text": text, "x": x, "top": top})

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

        # 4. Place each notice in the cell under it (see GRID_* above)
        row_height = (GRID_BOTTOM - GRID_TOP) / n_rows
        month_val = menu_month.month  # 0-indexed for MenuDay
        placed: Dict[int, str] = {}
        for notice in sorted(notices, key=lambda n: (n["top"], n["x"])):
            notice_text = notice["text"]
            row_idx = int((notice["top"] - GRID_TOP) // row_height)
            col_idx = int((notice["x"] - GRID_LEFT) // GRID_COLUMN_WIDTH)
            if not (0 <= row_idx < n_rows and 0 <= col_idx < GRID_COLUMNS):
                # Header or sidebar text that uses a keyword, or a layout
                # the GRID_* constants do not fit
                logger.warning(
                    "%04d-%02d notice not placed: it sits outside the calendar "
                    "grid (x=%.1f%%, top=%.1f%%): %s",
                    year, display_month, notice["x"], notice["top"], notice_text,
                )
                continue
            day = grid[row_idx][col_idx]
            if day is None or day in existing_days or day in placed:
                # A day with a menu can carry an early-release box, so that
                # one is expected; the other two point at a layout the GRID_*
                # constants do not fit.
                reason = (
                    "a day that has menu items" if day in existing_days
                    else "a cell outside this month" if day is None
                    else "a day that already has a notice"
                )
                logger.log(
                    logging.INFO if day in existing_days else logging.WARNING,
                    "%04d-%02d notice not placed: it sits over %s "
                    "(row %d, column %d, x=%.1f%%, top=%.1f%%): %s",
                    year, display_month, reason, row_idx, col_idx,
                    notice["x"], notice["top"], notice_text,
                )
                continue
            placed[day] = notice_text

            # Build the notice MenuDay
            notice_items: List[MenuItem] = []
            if "GRAB AND GO" in notice_text.upper():
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
            logger.debug("Added notice for day %d: %s", day, notice_text)

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
