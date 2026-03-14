"""
Frame Library for Vestaboard configuration app.

Manages a persistent, file-backed library of static board frames that users
have saved. Pure Python — no AppDaemon dependencies.

Storage path: /media/vestaboard/frame-library.json
"""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class LibraryFrame:
    """A saved static board frame in the library.

    Args:
        frame_id: Unique identifier (uuid.uuid4().hex).
        characters: 6 x 22 grid of Vestaboard character codes.
        creator: Human-readable creator name (e.g. "Mom", "Tom").
        created_at: Unix timestamp when the frame was saved.
        rating: User rating 0–5.
        name: Short descriptive name chosen by the creator.
        category: Frame category — "message" (default) or "art".
    """

    frame_id: str
    characters: list[list[int]]
    creator: str
    created_at: float
    rating: int
    name: str
    category: str = "message"


# ---------------------------------------------------------------------------
# FrameLibrary
# ---------------------------------------------------------------------------

DEFAULT_STORAGE_PATH = "/media/vestaboard/frame-library.json"


class FrameLibrary:
    """Persistent library of saved static Vestaboard frames.

    Frames are stored as a JSON file.  All mutations call ``save()`` so the
    file is always up-to-date.  Reads are served from the in-memory list for
    speed.

    Args:
        storage_path: Absolute path to the JSON storage file.
        log_fn: Callable that accepts a single str message.
    """

    def __init__(
        self,
        storage_path: str = DEFAULT_STORAGE_PATH,
        log_fn: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._path = storage_path
        self._log = log_fn or (lambda msg: None)
        self._frames: list[LibraryFrame] = []

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Load frames from the JSON file.

        Creates an empty file (and parent directories) if the file does not
        exist.
        """
        if not os.path.exists(self._path):
            self._log(
                f"[FrameLibrary] storage file not found at {self._path!r} — "
                "starting with empty library"
            )
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            self._frames = []
            self.save()
            return

        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._frames = [LibraryFrame(**item) for item in data]
            self._log(
                f"[FrameLibrary] loaded {len(self._frames)} frame(s) from {self._path!r}"
            )
        except (json.JSONDecodeError, TypeError, KeyError) as exc:
            self._log(
                f"[FrameLibrary] ERROR loading {self._path!r}: {exc} — "
                "starting with empty library"
            )
            self._frames = []

    def save(self) -> None:
        """Atomically write the current frame list to disk.

        Uses a temporary file in the same directory followed by os.rename to
        avoid partial writes.
        """
        dir_path = os.path.dirname(self._path)
        os.makedirs(dir_path, exist_ok=True)

        data = [asdict(f) for f in self._frames]
        fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            os.rename(tmp_path, self._path)
        except Exception:
            # Clean up temp file if rename failed
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        self._log(
            f"[FrameLibrary] saved {len(self._frames)} frame(s) to {self._path!r}"
        )

    # ------------------------------------------------------------------
    # CRUD operations
    # ------------------------------------------------------------------

    def find_by_characters(self, characters: list[list[int]]) -> Optional[LibraryFrame]:
        """Return the first frame whose characters match exactly, or None."""
        for frame in self._frames:
            if frame.characters == characters:
                return frame
        return None

    def find_by_name(
        self,
        name: str,
        *,
        exclude_frame_id: Optional[str] = None,
    ) -> Optional[LibraryFrame]:
        """Return the first frame with a matching normalized name, or None."""
        normalized = str(name).strip().casefold()
        if not normalized:
            return None

        for frame in self._frames:
            if exclude_frame_id and frame.frame_id == exclude_frame_id:
                continue
            if str(frame.name).strip().casefold() == normalized:
                return frame
        return None

    def add_frame(self, frame: LibraryFrame) -> None:
        """Add a frame to the library and persist."""
        self._frames.append(frame)
        self._log(
            f"[FrameLibrary] add_frame frame_id={frame.frame_id!r} "
            f"name={frame.name!r} creator={frame.creator!r}"
        )
        self.save()

    def update_frame(self, frame_id: str, **kwargs) -> bool:
        """Update fields on an existing frame.

        Returns True if the frame was found and updated, False otherwise.
        Only ``rating``, ``name``, ``creator``, ``characters`` are
        updateable; ``frame_id`` and ``created_at`` are immutable.
        """
        for frame in self._frames:
            if frame.frame_id == frame_id:
                immutable = {"frame_id", "created_at"}
                for key, value in kwargs.items():
                    if key in immutable:
                        self._log(
                            f"[FrameLibrary] update_frame — ignoring immutable "
                            f"field {key!r} on frame {frame_id!r}"
                        )
                        continue
                    if not hasattr(frame, key):
                        self._log(
                            f"[FrameLibrary] update_frame — unknown field "
                            f"{key!r} on frame {frame_id!r}"
                        )
                        continue
                    setattr(frame, key, value)
                self._log(
                    f"[FrameLibrary] update_frame frame_id={frame_id!r} "
                    f"fields={list(kwargs.keys())}"
                )
                self.save()
                return True
        self._log(
            f"[FrameLibrary] update_frame — frame_id={frame_id!r} not found"
        )
        return False

    def delete_frame(self, frame_id: str) -> bool:
        """Remove a frame by ID.

        Returns True if removed, False if not found.
        """
        before = len(self._frames)
        self._frames = [f for f in self._frames if f.frame_id != frame_id]
        removed = len(self._frames) < before
        if removed:
            self._log(
                f"[FrameLibrary] delete_frame frame_id={frame_id!r} — removed"
            )
            self.save()
        else:
            self._log(
                f"[FrameLibrary] delete_frame frame_id={frame_id!r} — not found"
            )
        return removed

    def get_frame(self, frame_id: str) -> Optional[LibraryFrame]:
        """Return a frame by ID, or None if not found."""
        for frame in self._frames:
            if frame.frame_id == frame_id:
                return frame
        return None

    def list_frames(
        self,
        creator: Optional[str] = None,
        min_rating: int = 0,
        sort_by: str = "created_at",
        ascending: bool = False,
        category: Optional[str] = None,
    ) -> list[LibraryFrame]:
        """Return a filtered, sorted list of frames.

        Args:
            creator: If provided, only return frames by this creator.
            min_rating: Only return frames with rating >= this value.
            sort_by: Field to sort by.  One of ``created_at``, ``rating``,
                ``name``.
            ascending: If True, sort ascending; default is descending.
            category: If provided, only return frames with this category.
                Frames without a category field default to ``"message"``.
        """
        results = [
            f
            for f in self._frames
            if (creator is None or f.creator == creator)
            and f.rating >= min_rating
            and (category is None or getattr(f, "category", "message") == category)
        ]

        valid_sort_fields = {"created_at", "rating", "name"}
        if sort_by not in valid_sort_fields:
            self._log(
                f"[FrameLibrary] list_frames — unknown sort_by={sort_by!r}, "
                f"defaulting to 'created_at'"
            )
            sort_by = "created_at"

        results.sort(key=lambda f: getattr(f, sort_by), reverse=not ascending)
        return results

    # ------------------------------------------------------------------
    # Serialization helpers
    # ------------------------------------------------------------------

    def to_json(self) -> str:
        """Serialize all frames to a JSON string (for sensor attributes)."""
        return json.dumps([asdict(f) for f in self._frames])
