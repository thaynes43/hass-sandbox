"""Capture-date matching for yearless ranges and optional absolute bounds."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .types import PhotoFilter


def _timestamp(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def matches_capture_dates(asset: dict, pf: PhotoFilter) -> bool:
    """Use the photographer's local day, falling back to the capture timestamp.

    Date-only absolute bounds include the entire specified local day. Bounds
    with a timezone describe instants, while naive times describe local time.
    Missing/invalid capture dates cannot satisfy an enabled date restriction.
    """
    if not (pf.recurring_date_ranges or pf.taken_after or pf.taken_before):
        return True
    local = _timestamp(asset.get("localDateTime"))
    utc = _timestamp(asset.get("fileCreatedAt"))
    captured = local or utc
    if captured is None:
        return False
    if pf.recurring_date_ranges and not any(
        r.contains(captured.date()) for r in pf.recurring_date_ranges
    ):
        return False

    for value, is_lower in ((pf.taken_after, True), (pf.taken_before, False)):
        if not value:
            continue
        boundary = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if len(value) == 10:
            candidate, limit = captured.date(), boundary.date()
        elif boundary.tzinfo is None:
            candidate, limit = captured.replace(tzinfo=None), boundary
        else:
            candidate = utc or captured
            candidate = candidate.replace(tzinfo=candidate.tzinfo or timezone.utc)
            limit = boundary
        if (is_lower and candidate < limit) or (not is_lower and candidate > limit):
            return False
    return True


def build_date_scan_body(pf: PhotoFilter, common: dict) -> dict:
    """Widen UTC API bounds so local whole-day matching cannot lose edge photos.

    Immich's takenAfter/takenBefore use UTC capture instants, while yearless
    filtering uses localDateTime. A day's padding admits timezone edges; the
    exact intersection is applied locally after paging.
    """
    body = dict(common)
    for value, key, padding in (
        (pf.taken_after, "takenAfter", -1),
        (pf.taken_before, "takenBefore", 2),
    ):
        if not value:
            continue
        boundary = datetime.fromisoformat(value.replace("Z", "+00:00"))
        boundary = boundary.replace(tzinfo=boundary.tzinfo or timezone.utc)
        try:
            body[key] = (boundary + timedelta(days=padding)).isoformat()
        except OverflowError:
            # At the supported calendar limits, omit the coarse API bound;
            # local matching still enforces the exact requested date.
            body.pop(key, None)
    body["withExif"] = False
    return body
