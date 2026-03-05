"""Photo provider interfaces and shared data types."""

from __future__ import annotations

import re
from dataclasses import dataclass
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
    r"(T\d{2}:\d{2}(:\d{2})?)?"
    r"(Z|[+-]\d{2}:\d{2})?$"
)


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
    favorites_only: bool = False
    rating: Optional[int] = None

    def validate(self) -> None:  # noqa: C901 – intentionally thorough
        if not self.name:
            raise ValueError("Filter name must not be empty")
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
        if self.search_pool_size < 1 or self.search_pool_size > 1000:
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
            if val is not None and not _ISO_DATE_RE.match(val):
                raise ValueError(
                    f"{field_name} must be an ISO-format date string, got '{val}'"
                )

        # Rating
        if self.rating is not None and not (1 <= self.rating <= 5):
            raise ValueError("rating must be between 1 and 5")

    # -- Serialisation helpers -----------------------------------------------

    def to_dict(self) -> dict:
        d: dict = {"name": self.name, "selection": self.selection}
        if self.randomize is not True:
            d["randomize"] = self.randomize
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
        if self.favorites_only:
            d["favorites_only"] = self.favorites_only
        if self.rating is not None:
            d["rating"] = self.rating
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "PhotoFilter":
        return cls(
            name=data["name"],
            selection=data["selection"],
            randomize=data.get("randomize", True),
            search_query=data.get("search_query"),
            search_pool_size=data.get("search_pool_size", 250),
            album_name=data.get("album_name"),
            people=data.get("people"),
            location=data.get("location"),
            taken_after=data.get("taken_after"),
            taken_before=data.get("taken_before"),
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
