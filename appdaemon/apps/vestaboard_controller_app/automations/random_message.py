"""Messages From Library automation — selects saved messages from the frame library."""

from __future__ import annotations

import random
from typing import Any, Optional

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[4]))

from providers.vestaboard.character_encoding import (
    COLOR_CODES,
    COLS,
    text_to_grid,
)

from .base import BoardAutomation

# Curated fallback messages when the frame library is empty or has no matches.
_FALLBACK_MESSAGES = [
    "I AM MADE OF FLIPS AND DREAMS",
    "HELLO WORLD FROM THE MUDROOM",
    "STAY CURIOUS STAY CLEVER",
    "TODAY IS A GOOD DAY",
    "MAKE SOMETHING AMAZING",
    "I FLIPPED FOR YOU TODAY",
    "ONE MOMENT AT A TIME",
    "CURIOSITY UNLOCKS EVERYTHING",
    "YOU LOOK GREAT TODAY",
    "HAVE A WONDERFUL DAY",
    "THE FUTURE IS UNWRITTEN",
    "EVERY DAY IS A FRESH START",
    "SOMETHING GOOD IS COMING",
]


def _build_bordered_grid(text: str) -> list[list[int]]:
    """Build a 6x22 grid with a randomly colored border and centered text."""
    color_options = [
        COLOR_CODES["red"],
        COLOR_CODES["orange"],
        COLOR_CODES["yellow"],
        COLOR_CODES["green"],
        COLOR_CODES["blue"],
        COLOR_CODES["violet"],
    ]
    border_code = random.choice(color_options)

    grid = text_to_grid(text, justify="center", align="center")

    # Overwrite border rows and columns
    grid[0] = [border_code] * COLS
    grid[5] = [border_code] * COLS
    for row_idx in range(1, 5):
        grid[row_idx][0] = border_code
        grid[row_idx][21] = border_code

    return grid


class MessagesFromLibraryAutomation(BoardAutomation):
    """On-demand automation that selects a random message from the frame library.

    Filters library frames by category="message" and min_stars rating.
    Falls back to curated message list if no matching library frames exist.
    No automatic triggers — the frame is generated on user request via command.
    """

    name = "MessagesFromLibrary"
    default_ttl_s = None
    default_expiration_s = None
    default_should_expire = True

    DEFAULT_UI_CONFIG = {
        "enabled": False,
        "ttl_minutes": 5,
        "should_expire": True,
        "frequency_min_minutes": 30,
        "frequency_max_minutes": 120,
        "min_stars": 3,
    }

    def __init__(self, app: Any, config: dict[str, Any]) -> None:
        super().__init__(app, config)
        self._frame_library: Optional[Any] = None
        self._frame_library_path: Optional[str] = None

    def set_frame_library_path(self, path: str) -> None:
        """Set the path to the frame library JSON file."""
        self._frame_library_path = path

    def _get_frame_library(self) -> Optional[Any]:
        """Lazy-load the frame library."""
        if self._frame_library is not None:
            return self._frame_library

        path = self._frame_library_path
        if not path:
            return None

        try:
            from vestaboard_configuration_app.frame_library import FrameLibrary
            self._frame_library = FrameLibrary(
                storage_path=path,
                log_fn=lambda msg: self.log(msg, level="DEBUG"),
            )
            self._frame_library.load()
            self.log(f"Frame library loaded from {path!r}", level="INFO")
            return self._frame_library
        except Exception as exc:
            self.log(f"Failed to load frame library: {exc!r}", level="WARNING")
            return None

    @classmethod
    def get_config_schema(cls) -> dict:
        return {
            "enabled": {"type": "bool", "label": "Enabled", "default": False},
            "ttl_minutes": {"type": "int", "label": "TTL (minutes)", "min": 1, "max": 1440, "default": 5},
            "should_expire": {"type": "bool", "label": "Should Expire", "default": True},
            "frequency_min_minutes": {"type": "int", "label": "Min Frequency (minutes)", "min": 1, "max": 1440, "default": 30},
            "frequency_max_minutes": {"type": "int", "label": "Max Frequency (minutes)", "min": 1, "max": 1440, "default": 120},
            "min_stars": {"type": "int", "label": "Min Stars", "min": 0, "max": 5, "default": 3},
        }

    def get_preview_frame(self) -> list[list[int]]:
        """Return a representative bordered message preview."""
        from providers.vestaboard.character_encoding import CHAR_TO_CODE

        border = COLOR_CODES["yellow"]

        text = "HELLO WORLD"
        padded = text.center(20)
        interior: list[int] = []
        for ch in padded:
            interior.append(CHAR_TO_CODE.get(ch.upper(), 0))

        blank_interior = [0] * 20

        border_row = [border] * COLS

        def _bordered(cells: list[int]) -> list[int]:
            return [border] + cells + [border]

        grid = [
            border_row,
            _bordered(blank_interior),
            _bordered(interior),
            _bordered(blank_interior),
            _bordered(blank_interior),
            border_row,
        ]
        return grid

    def get_triggers(self) -> list[dict[str, Any]]:
        """No automatic triggers — user-driven via push_frame command."""
        return []

    async def generate_frame(self) -> list[list[int]]:
        """Select a random message from the frame library.

        Filters by category="message" and min_stars from config.
        Falls back to curated message list if library is empty or unavailable.
        """
        cfg = self.config
        min_stars = int(cfg.get("min_stars", 3))

        # Try frame library first
        library = self._get_frame_library()
        if library is not None:
            frames = library.list_frames(
                category="message",
                min_rating=min_stars,
            )
            if frames:
                selected = random.choice(frames)
                self.log(
                    f"Selected library message: {selected.name!r} "
                    f"(rating={selected.rating}, creator={selected.creator!r})",
                    level="INFO",
                )
                return selected.characters

            self.log(
                f"No library messages with min_stars>={min_stars}, "
                "falling back to curated messages",
                level="INFO",
            )

        # Fallback: curated message list
        return self._generate_fallback_frame()

    def _generate_fallback_frame(self) -> list[list[int]]:
        """Pick a random message from the curated list and build a bordered grid."""
        message = random.choice(_FALLBACK_MESSAGES)
        self.log(f"Using fallback message: {message!r}", level="INFO")
        return _build_bordered_grid(message)


# Backward-compatible alias
RandomMessageAutomation = MessagesFromLibraryAutomation
