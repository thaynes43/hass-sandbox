"""Random Art automation — picks a pre-built pixel art frame from art_library.json."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

from .base import BoardAutomation

# Path to the art library relative to this file.
_ART_LIBRARY_PATH = Path(__file__).parent / "art_library.json"


class RandomArtAutomation(BoardAutomation):
    """On-demand automation that picks a random art frame from the library.

    No automatic triggers — the frame is generated on user request via command.
    The art library is defined in art_library.json alongside this module.
    """

    name = "RandomArt"
    default_ttl_s = None
    default_expiration_s = None

    def __init__(self, app: Any, config: dict[str, Any]) -> None:
        super().__init__(app, config)
        self._library: list[dict] = []
        self._load_library()

    def _load_library(self) -> None:
        """Load the art library from disk."""
        try:
            with open(_ART_LIBRARY_PATH, "r", encoding="utf-8") as fh:
                self._library = json.load(fh)
            self.log(
                f"Loaded {len(self._library)} art frames from library",
                level="INFO",
            )
        except Exception as exc:
            self.log(
                f"Failed to load art library: {exc!r}",
                level="ERROR",
            )
            self._library = []

    def get_triggers(self) -> list[dict[str, Any]]:
        """No automatic triggers — user-driven via push_frame command."""
        return []

    async def generate_frame(self) -> list[list[int]]:
        """Pick a random art frame from the library and return it.

        Falls back to a blank grid if the library is empty.
        """
        if not self._library:
            self.log("Art library is empty — returning blank grid", level="WARNING")
            from providers.vestaboard.character_encoding import blank_grid
            return blank_grid()

        art = random.choice(self._library)
        self.log(
            f"Selected art frame: {art.get('name', 'unknown')} — {art.get('description', '')}",
            level="INFO",
        )

        characters = art.get("characters", [])
        if not characters or len(characters) != 6 or any(len(r) != 22 for r in characters):
            self.log(
                f"Art frame {art.get('name', '?')!r} has invalid dimensions — "
                "returning blank grid",
                level="WARNING",
            )
            from providers.vestaboard.character_encoding import blank_grid
            return blank_grid()

        return characters
