"""Data types for the school menu provider."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class MenuItem:
    """A single menu item for a given day."""

    name: str
    category: str = ""
    is_ancillary: bool = False


@dataclass
class MenuDay:
    """All menu items for a single calendar day."""

    day: int
    month: int
    year: int
    items: List[MenuItem] = field(default_factory=list)
    notice: str = ""  # e.g. "EARLY RELEASE", "NO SCHOOL"


@dataclass
class MenuMonth:
    """A full month of menu data for a specific menu type."""

    menu_id: str
    menu_type_name: str
    month: int  # 0-indexed (0=Jan, 11=Dec) as returned by the API
    year: int
    days: List[MenuDay] = field(default_factory=list)
    previous_month_id: Optional[str] = None
    next_month_id: Optional[str] = None

    @property
    def display_month(self) -> int:
        """1-indexed month (1=Jan, 12=Dec) for human display."""
        return self.month + 1
