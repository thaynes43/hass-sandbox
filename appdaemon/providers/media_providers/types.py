"""Media provider interfaces and shared data types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# MediaItem
# ---------------------------------------------------------------------------

@dataclass
class MediaItem:
    """A normalized media item across all sources (TMDb, Tautulli, etc.)."""

    id: str
    title: str
    year: int = 0
    poster_path: str = ""
    local_poster: str = ""
    media_type: str = "movie"
    subtitle: str = ""
    rating: str = ""
    runtime_min: int = 0
    tmdb_score: float = 0.0
    tmdb_popularity: float = 0.0
    vote_count: int = 0
    genres: str = ""
    summary: str = ""
    release_date: str = ""
    release_type: str = ""
    has_showtimes: bool = False
    source: str = ""
    added_at: str = ""
    tmdb_id: Optional[int] = None
    imdb_rating: float = 0.0
    rt_critics: int = 0
    rt_audience: int = 0
    metacritic: int = 0
    director: str = ""
    revenue: int = 0
    tagline: str = ""
    certification: str = ""

    def to_dict(self) -> dict:
        """Serialize to the lean format used by the main sensor (excludes summary)."""
        d: dict = {
            "id": self.id,
            "title": self.title,
        }
        if self.year:
            d["year"] = self.year
        if self.poster_path:
            d["poster_path"] = self.poster_path
        if self.local_poster:
            d["local_poster"] = self.local_poster
        if self.media_type != "movie":
            d["media_type"] = self.media_type
        if self.subtitle:
            d["subtitle"] = self.subtitle
        if self.rating:
            d["rating"] = self.rating
        if self.runtime_min:
            d["runtime_min"] = self.runtime_min
        if self.tmdb_score:
            d["tmdb_score"] = self.tmdb_score
        if self.tmdb_popularity:
            d["tmdb_popularity"] = self.tmdb_popularity
        if self.vote_count:
            d["vote_count"] = self.vote_count
        if self.genres:
            d["genres"] = self.genres
        if self.release_date:
            d["release_date"] = self.release_date
        if self.release_type:
            d["release_type"] = self.release_type
        if self.has_showtimes:
            d["has_showtimes"] = self.has_showtimes
        if self.source:
            d["source"] = self.source
        if self.added_at:
            d["added_at"] = self.added_at
        if self.tmdb_id is not None:
            d["tmdb_id"] = self.tmdb_id
        if self.imdb_rating:
            d["imdb_rating"] = self.imdb_rating
        if self.rt_critics:
            d["rt_critics"] = self.rt_critics
        if self.rt_audience:
            d["rt_audience"] = self.rt_audience
        if self.metacritic:
            d["metacritic"] = self.metacritic
        if self.director:
            d["director"] = self.director
        if self.revenue:
            d["revenue"] = self.revenue
        if self.tagline:
            d["tagline"] = self.tagline
        if self.certification:
            d["certification"] = self.certification
        return d

    def to_detail_dict(self) -> dict:
        """Serialize including summary and all detail fields for detail views."""
        d = self.to_dict()
        if self.summary:
            d["summary"] = self.summary
        # Always include detail-only fields when non-empty/non-zero
        if self.director:
            d["director"] = self.director
        if self.revenue:
            d["revenue"] = self.revenue
        if self.tagline:
            d["tagline"] = self.tagline
        if self.certification:
            d["certification"] = self.certification
        if self.imdb_rating:
            d["imdb_rating"] = self.imdb_rating
        if self.rt_critics:
            d["rt_critics"] = self.rt_critics
        if self.rt_audience:
            d["rt_audience"] = self.rt_audience
        if self.metacritic:
            d["metacritic"] = self.metacritic
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "MediaItem":
        return cls(
            id=data["id"],
            title=data["title"],
            year=data.get("year", 0),
            poster_path=data.get("poster_path", ""),
            local_poster=data.get("local_poster", ""),
            media_type=data.get("media_type", "movie"),
            subtitle=data.get("subtitle", ""),
            rating=data.get("rating", ""),
            runtime_min=data.get("runtime_min", 0),
            tmdb_score=data.get("tmdb_score", 0.0),
            tmdb_popularity=data.get("tmdb_popularity", 0.0),
            vote_count=data.get("vote_count", 0),
            genres=data.get("genres", ""),
            summary=data.get("summary", ""),
            release_date=data.get("release_date", ""),
            release_type=data.get("release_type", ""),
            has_showtimes=data.get("has_showtimes", False),
            source=data.get("source", ""),
            added_at=data.get("added_at", ""),
            tmdb_id=data.get("tmdb_id"),
            imdb_rating=data.get("imdb_rating", 0.0),
            rt_critics=data.get("rt_critics", 0),
            rt_audience=data.get("rt_audience", 0),
            metacritic=data.get("metacritic", 0),
            director=data.get("director", ""),
            revenue=data.get("revenue", 0),
            tagline=data.get("tagline", ""),
            certification=data.get("certification", ""),
        )


# ---------------------------------------------------------------------------
# ShowtimeEntry
# ---------------------------------------------------------------------------

@dataclass
class ShowtimeEntry:
    """Showtimes for one film at one cinema on one day."""

    cinema_name: str
    times: List[str] = field(default_factory=list)
    day: str = ""    # e.g. "Today", "Tomorrow", "Mon, Mar 31"
    date: str = ""   # e.g. "Mar 29"


# ---------------------------------------------------------------------------
# ShowtimeCache
# ---------------------------------------------------------------------------

@dataclass
class ShowtimeCache:
    """Full showtime cache keyed by item ID."""

    date: str = ""
    films: Dict[str, List[ShowtimeEntry]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "films": {
                item_id: [
                    {
                        "cinema_name": entry.cinema_name,
                        "times": entry.times,
                        "day": entry.day,
                        "date": entry.date,
                    }
                    for entry in entries
                ]
                for item_id, entries in self.films.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ShowtimeCache":
        films: Dict[str, List[ShowtimeEntry]] = {}
        for item_id, raw_entries in data.get("films", {}).items():
            films[item_id] = [
                ShowtimeEntry(
                    cinema_name=e["cinema_name"],
                    times=e.get("times", []),
                    day=e.get("day", ""),
                    date=e.get("date", ""),
                )
                for e in raw_entries
            ]
        return cls(
            date=data.get("date", ""),
            films=films,
        )


# ---------------------------------------------------------------------------
# CinemaInfo
# ---------------------------------------------------------------------------

@dataclass
class CinemaInfo:
    """A discovered cinema returned by a showtime provider."""

    cinema_id: str
    cinema_name: str
    distance: float = 0.0


# ---------------------------------------------------------------------------
# FetchResult
# ---------------------------------------------------------------------------

@dataclass
class FetchResult:
    """Result of a media fetcher run."""

    items: List[MediaItem] = field(default_factory=list)
    status: str = "ok"
    error_message: str = ""
