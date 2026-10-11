"""Photo provider interfaces and shared data types."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Protocol


# ---------------------------------------------------------------------------
# Errors and enums
# ---------------------------------------------------------------------------

class PhotoProviderError(RuntimeError):
    """Raised when a photo provider operation fails."""


class PhotoProviderName(str, Enum):
    IMMICH = "immich"
    # future: GOOGLE = "google", APPLE = "apple"

    @classmethod
    def parse(cls, value: Any) -> "PhotoProviderName":
        s = str(value or "").strip().lower()
        if s in {"immich"}:
            return cls.IMMICH
        raise ValueError(f"Unsupported photo provider: {value!r}")


# ---------------------------------------------------------------------------
# Location alias
# ---------------------------------------------------------------------------

@dataclass
class LocationAlias:
    """Maps a friendly display name to reverse-geocode fields (city/state/country)."""

    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None

    def validate(self) -> None:
        for name in ("city", "state", "country"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"LocationAlias {name} must be non-empty text")
        if not any((self.city, self.state, self.country)):
            raise ValueError(
                "LocationAlias must specify at least one of city, state, or country"
            )

    def to_dict(self) -> dict:
        d: dict = {}
        if self.city:
            d["city"] = self.city
        if self.state:
            d["state"] = self.state
        if self.country:
            d["country"] = self.country
        return d


# ---------------------------------------------------------------------------
# ISO-date regex (YYYY-MM-DD with optional time)
# ---------------------------------------------------------------------------

_ISO_DATE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}"
    r"(T\d{2}:\d{2}(:\d{2})?(Z|[+-]\d{2}:\d{2})?)?$"
)


@dataclass(frozen=True)
class RecurringDateRange:
    """Inclusive month/day range, repeated each year; descending ranges wrap."""

    start: str
    end: Optional[str] = None

    def validate(self) -> None:
        for name, value in (("start", self.start), ("end", self.end)):
            if name == "end" and value is None:
                continue
            if not isinstance(value, str) or not re.fullmatch(r"\d{2}-\d{2}", value):
                raise ValueError(f"Recurring date {name} must use MM-DD")
            try:
                # A leap reference year accepts February 29 without accepting
                # impossible dates such as February 30 or April 31.
                date.fromisoformat(f"2000-{value}")
            except ValueError as exc:
                raise ValueError(f"Recurring date {name} is not a valid month/day") from exc

    def contains(self, captured: date) -> bool:
        value = captured.strftime("%m-%d")
        end = self.end or self.start
        if self.start <= end:
            return self.start <= value <= end
        return value >= self.start or value <= end

    def to_dict(self) -> dict:
        return {"start": self.start, "end": self.end or self.start}

    @classmethod
    def from_dict(cls, data: dict) -> "RecurringDateRange":
        if not isinstance(data, dict) or set(data) - {"start", "end"}:
            raise ValueError("Each recurring date range must contain start and optional end")
        result = cls(start=data.get("start"), end=data.get("end"))
        result.validate()
        return result


# ---------------------------------------------------------------------------
# Photo filter
# ---------------------------------------------------------------------------

SELECTION_TYPES = ("all_photos", "search", "album")


@dataclass
class PhotoFilter:
    """A single filter profile describing which photos to fetch."""

    name: str
    selection: Literal["all_photos", "search", "album"]
    randomize: bool = True

    # Paused filters stay configured but are skipped by the fetch rotation
    paused: bool = False

    # Search params
    search_query: Optional[str] = None
    search_pool_size: int = 250

    # Album params
    album_name: Optional[str] = None

    # Common filter criteria
    people: Optional[List[str]] = None
    location: Optional[str] = None
    taken_after: Optional[str] = None
    taken_before: Optional[str] = None
    recurring_date_ranges: List[RecurringDateRange] = field(default_factory=list)
    favorites_only: bool = False
    rating: Optional[int] = None

    def validate(self) -> None:  # noqa: C901 – intentionally thorough
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Filter name must not be empty")
        for field_name in ("randomize", "paused", "favorites_only"):
            if not isinstance(getattr(self, field_name), bool):
                raise ValueError(f"{field_name} must be a boolean")
        for field_name in ("search_query", "album_name", "location"):
            val = getattr(self, field_name)
            if val is not None and (not isinstance(val, str) or not val.strip()):
                raise ValueError(f"{field_name} must be non-empty text")
        if self.people is not None and (
            not isinstance(self.people, list)
            or any(not isinstance(n, str) or not n.strip() for n in self.people)
        ):
            raise ValueError("people must be a list of non-empty names")
        if self.selection not in SELECTION_TYPES:
            raise ValueError(
                f"Invalid selection type '{self.selection}'; "
                f"must be one of {SELECTION_TYPES}"
            )

        # all_photos always implies randomize (API only returns random)
        if self.selection == "all_photos" and not self.randomize:
            raise ValueError(
                "all_photos selection always uses randomize=True"
            )

        # search requires query
        if self.selection == "search":
            if not self.search_query:
                raise ValueError(
                    "search_query is required when selection is 'search'"
                )
        else:
            if self.search_query:
                raise ValueError(
                    "search_query should only be set for 'search' selection"
                )

        # search_pool_size only meaningful for search + randomize
        if type(self.search_pool_size) is not int or not 1 <= self.search_pool_size <= 1000:
            raise ValueError("search_pool_size must be between 1 and 1000")

        # album requires album_name
        if self.selection == "album":
            if not self.album_name:
                raise ValueError(
                    "album_name is required when selection is 'album'"
                )
        else:
            if self.album_name:
                raise ValueError(
                    "album_name should only be set for 'album' selection"
                )

        # Date strings
        for field_name in ("taken_after", "taken_before"):
            val = getattr(self, field_name)
            if val is None:
                continue
            if not isinstance(val, str) or not _ISO_DATE_RE.fullmatch(val):
                raise ValueError(
                    f"{field_name} must be an ISO-format date string, got '{val}'"
                )
            try:
                datetime.fromisoformat(val.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(f"{field_name} must be a valid ISO-format date") from exc

        if self.taken_after and self.taken_before:
            after = datetime.fromisoformat(self.taken_after.replace("Z", "+00:00"))
            before = datetime.fromisoformat(self.taken_before.replace("Z", "+00:00"))
            if len(self.taken_after) == 10 or len(self.taken_before) == 10:
                inverted = after.date() > before.date()
            else:
                inverted = after.replace(tzinfo=after.tzinfo or timezone.utc) > before.replace(
                    tzinfo=before.tzinfo or timezone.utc
                )
            if inverted:
                raise ValueError("taken_after must not be later than taken_before")

        if not isinstance(self.recurring_date_ranges, list):
            raise ValueError("recurring_date_ranges must be a list")
        for date_range in self.recurring_date_ranges:
            if not isinstance(date_range, RecurringDateRange):
                raise ValueError("Each recurring date range must contain start and optional end")
            date_range.validate()

        # Rating
        if self.rating is not None and (type(self.rating) is not int or not 1 <= self.rating <= 5):
            raise ValueError("rating must be between 1 and 5")

    # -- Serialisation helpers -----------------------------------------------

    def to_dict(self) -> dict:
        d: dict = {"name": self.name, "selection": self.selection}
        if self.randomize is not True:
            d["randomize"] = self.randomize
        if self.paused:
            d["paused"] = self.paused
        if self.search_query:
            d["search_query"] = self.search_query
        if self.selection == "search" and self.search_pool_size != 250:
            d["search_pool_size"] = self.search_pool_size
        if self.album_name:
            d["album_name"] = self.album_name
        if self.people:
            d["people"] = self.people
        if self.location:
            d["location"] = self.location
        if self.taken_after:
            d["taken_after"] = self.taken_after
        if self.taken_before:
            d["taken_before"] = self.taken_before
        if self.recurring_date_ranges:
            d["recurring_date_ranges"] = [r.to_dict() for r in self.recurring_date_ranges]
        if self.favorites_only:
            d["favorites_only"] = self.favorites_only
        if self.rating is not None:
            d["rating"] = self.rating
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "PhotoFilter":
        if not isinstance(data, dict):
            raise ValueError("Each filter must be an object")
        unknown = set(data) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"Unknown filter settings: {', '.join(sorted(unknown))}")
        ranges = data.get("recurring_date_ranges", [])
        if not isinstance(ranges, list):
            raise ValueError("recurring_date_ranges must be a list")
        return cls(
            name=data.get("name", ""),
            selection=data.get("selection", ""),
            randomize=data.get("randomize", True),
            paused=data.get("paused", False),
            search_query=data.get("search_query"),
            search_pool_size=data.get("search_pool_size", 250),
            album_name=data.get("album_name"),
            people=data.get("people"),
            location=data.get("location"),
            taken_after=data.get("taken_after"),
            taken_before=data.get("taken_before"),
            recurring_date_ranges=[RecurringDateRange.from_dict(r) for r in ranges],
            favorites_only=data.get("favorites_only", False),
            rating=data.get("rating"),
        )


# ---------------------------------------------------------------------------
# Metadata result types
# ---------------------------------------------------------------------------

@dataclass
class PhotoPerson:
    """A person (face) in the photo library, ranked by photo count."""

    name: str
    asset_count: int = 0


@dataclass
class PhotoAlbum:
    """An album in the photo library."""

    name: str
    id: str
    asset_count: int = 0


@dataclass
class PhotoMetadata:
    """People and albums from the photo source, for filter resolution and UI."""

    people: List[PhotoPerson]
    albums: List[PhotoAlbum]


# ---------------------------------------------------------------------------
# PhotoProvider Protocol
# ---------------------------------------------------------------------------

class PhotoProvider(Protocol):
    """Protocol for photo source providers (Immich, Google Photos, etc.)."""

    async def refresh_metadata(self) -> PhotoMetadata:
        """Fetch and return current people and albums from the source."""
        ...

    async def fetch_photo_ids(
        self,
        photo_filter: PhotoFilter,
        count: int,
        *,
        location_aliases: Optional[Dict[str, LocationAlias]] = None,
    ) -> List[str]:
        """Return up to count photo IDs matching the filter."""
        ...

    async def download_photo(self, photo_id: str) -> bytes:
        """Download raw bytes for a single photo."""
        ...
