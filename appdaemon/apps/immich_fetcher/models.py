"""Data models for the Immich fetcher.

FetcherConfig and related app-level config. PhotoFilter and LocationAlias
live in photo_providers.types.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from providers.photo_providers.types import LocationAlias, PhotoFilter


DOWNLOAD_QUALITIES = ("preview", "fullsize", "original")


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
        if type(self.num_photos) is not int or self.num_photos < 1:
            raise ValueError("num_photos must be >= 1")
        if type(self.update_interval_minutes) is not int or self.update_interval_minutes < 1:
            raise ValueError("update_interval_minutes must be >= 1")
        if self.download_quality not in DOWNLOAD_QUALITIES:
            raise ValueError(
                f"Invalid download_quality '{self.download_quality}'; "
                f"must be one of {DOWNLOAD_QUALITIES}"
            )
        if type(self.people_cutoff) is not int or not 1 <= self.people_cutoff <= 99:
            raise ValueError("people_cutoff must be between 1 and 99")
        if not isinstance(self.show_favorite_people_only, bool):
            raise ValueError("show_favorite_people_only must be a boolean")
        if not isinstance(self.favorite_people, list) or any(
            not isinstance(n, str) or not n.strip() for n in self.favorite_people
        ):
            raise ValueError("favorite_people must be a list of non-empty names")

        names: set[str] = set()
        for f in self.filters:
            try:
                f.validate()
            except ValueError as exc:
                raise ValueError(f"Filter '{f.name}': {exc}") from exc
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
        if not isinstance(data, dict):
            raise ValueError("Configuration must be an object")
        filters_raw = data.get("filters", [])
        if not isinstance(filters_raw, list):
            raise ValueError("filters must be a list")
        aliases_raw = data.get("location_aliases", {})
        if not isinstance(aliases_raw, dict):
            raise ValueError("location_aliases must be an object")
        for name, value in aliases_raw.items():
            if not isinstance(name, str) or not name.strip() or not isinstance(value, dict):
                raise ValueError("Location aliases must have names and location objects")
        aliases = {
            k: LocationAlias(**v) for k, v in aliases_raw.items()
        }
        filters = []
        for raw in filters_raw:
            try:
                filters.append(PhotoFilter.from_dict(raw))
            except ValueError as exc:
                name = raw.get("name", "unnamed") if isinstance(raw, dict) else "unnamed"
                raise ValueError(f"Filter '{name}': {exc}") from exc
        return cls(
            filters=filters,
            num_photos=data.get("num_photos", 10),
            update_interval_minutes=data.get("update_interval_minutes", 60),
            download_quality=data.get("download_quality", "preview"),
            location_aliases=aliases,
            favorite_people=data.get("favorite_people", []),
            people_cutoff=data.get("people_cutoff", 5),
            show_favorite_people_only=data.get("show_favorite_people_only", False),
        )
