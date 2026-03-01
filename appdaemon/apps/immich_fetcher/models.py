"""Data models for the Immich fetcher.

Pure-Python dataclasses with validation – no AppDaemon dependency.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional


# ---------------------------------------------------------------------------
# Location alias
# ---------------------------------------------------------------------------

@dataclass
class LocationAlias:
    """Maps a friendly display name to Immich reverse-geocode fields."""

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
DOWNLOAD_QUALITIES = ("preview", "fullsize", "original")


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
        if self.selection == "search" and self.randomize and self.search_pool_size != 250:
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
# Fetcher config (top-level)
# ---------------------------------------------------------------------------

@dataclass
class FetcherConfig:
    """Full configuration persisted in /media/immich-fetcher/config.json."""

    filters: List[PhotoFilter]
    num_photos: int = 10
    update_interval_minutes: int = 60
    download_quality: str = "preview"
    location_aliases: Dict[str, LocationAlias] = field(default_factory=dict)
    favorite_people: List[str] = field(default_factory=list)
    people_cutoff: int = 5
    show_favorite_people_only: bool = False

    def validate(self) -> None:
        if not self.filters:
            raise ValueError("At least one filter must be configured")
        if self.num_photos < 1:
            raise ValueError("num_photos must be >= 1")
        if self.update_interval_minutes < 1:
            raise ValueError("update_interval_minutes must be >= 1")
        if self.download_quality not in DOWNLOAD_QUALITIES:
            raise ValueError(
                f"Invalid download_quality '{self.download_quality}'; "
                f"must be one of {DOWNLOAD_QUALITIES}"
            )
        if self.people_cutoff < 1 or self.people_cutoff > 99:
            raise ValueError("people_cutoff must be between 1 and 99")

        names: set[str] = set()
        for f in self.filters:
            f.validate()
            if f.name in names:
                raise ValueError(f"Duplicate filter name: '{f.name}'")
            names.add(f.name)

        for alias_name, alias in self.location_aliases.items():
            alias.validate()

    # -- Serialisation -------------------------------------------------------

    def to_dict(self) -> dict:
        d: dict = {
            "filters": [f.to_dict() for f in self.filters],
            "num_photos": self.num_photos,
            "update_interval_minutes": self.update_interval_minutes,
            "download_quality": self.download_quality,
            "location_aliases": {
                k: v.to_dict() for k, v in self.location_aliases.items()
            },
        }
        if self.favorite_people:
            d["favorite_people"] = self.favorite_people
        if self.people_cutoff != 5:
            d["people_cutoff"] = self.people_cutoff
        if self.show_favorite_people_only:
            d["show_favorite_people_only"] = self.show_favorite_people_only
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "FetcherConfig":
        aliases_raw = data.get("location_aliases", {})
        aliases = {
            k: LocationAlias(**v) for k, v in aliases_raw.items()
        }
        return cls(
            filters=[PhotoFilter.from_dict(f) for f in data.get("filters", [])],
            num_photos=data.get("num_photos", 10),
            update_interval_minutes=data.get("update_interval_minutes", 60),
            download_quality=data.get("download_quality", "preview"),
            location_aliases=aliases,
            favorite_people=data.get("favorite_people", []),
            people_cutoff=data.get("people_cutoff", 5),
            show_favorite_people_only=data.get("show_favorite_people_only", False),
        )
