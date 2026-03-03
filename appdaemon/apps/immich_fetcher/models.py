"""Data models for the Immich fetcher.

FetcherConfig and related app-level config. PhotoFilter and LocationAlias
live in photo_providers.types.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from photo_providers.types import LocationAlias, PhotoFilter


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
