"""Messages From Library automation app — selects saved messages from the frame library."""

from __future__ import annotations

import random
from typing import Any, Optional

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[4]))  # adds appdaemon/

import hassapi as hass

from providers.vestaboard.character_encoding import (
    CHAR_TO_CODE,
    COLOR_CODES,
    COLS,
    text_to_grid,
)

from vestaboard_apps._shared.base import VestaboardAutomation

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

    grid[0] = [border_code] * COLS
    grid[5] = [border_code] * COLS
    for row_idx in range(1, 5):
        grid[row_idx][0] = border_code
        grid[row_idx][21] = border_code

    return grid


class MessagesFromLibraryApp(hass.Hass, VestaboardAutomation):
    """On-demand automation that selects a random message from the frame library.

    Supports random interval scheduling when enabled with frequency config.
    """

    automation_type = "messages_from_library"
    display_name = "Messages From Library"
    display_description = "Randomly displays starred messages from your library."
    default_ttl_s = None
    default_max_age_s = None
    default_should_expire = True

    DEFAULT_UI_CONFIG = {
        "enabled": False,
        "ttl_minutes": 5,
        "should_expire": True,
        "frequency_min_minutes": 30,
        "frequency_max_minutes": 120,
        "min_stars": 3,
    }

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

        return [
            border_row,
            _bordered(blank_interior),
            _bordered(interior),
            _bordered(blank_interior),
            _bordered(blank_interior),
            border_row,
        ]

    def initialize(self) -> None:
        self._frame_library: Optional[Any] = None
        self._frame_library_path: Optional[str] = (self.args or {}).get("frame_library_path")

        self.register_with_controller()
        # Do NOT start interval here — wait for config event from controller

    def terminate(self) -> None:
        self._cancel_random_interval()
        self.deregister_from_controller()

    def set_enabled(self, enabled: bool) -> None:
        if enabled:
            self._start_random_interval()
        else:
            self._cancel_random_interval()

    def on_config_updated(self, config: dict[str, Any]) -> None:
        super().on_config_updated(config)
        if "enabled" in config:
            if config["enabled"]:
                self._start_random_interval()
            else:
                self._cancel_random_interval()
        elif "frequency_min_minutes" in config or "frequency_max_minutes" in config:
            if self.args.get("enabled", False):
                self._start_random_interval()

    def _start_random_interval(self) -> None:
        cfg = self.args or {}
        freq_min = cfg.get("frequency_min_minutes", 30)
        freq_max = cfg.get("frequency_max_minutes", 120)
        self._schedule_random_interval(
            self._on_random_fire,
            min_minutes=float(freq_min),
            max_minutes=float(freq_max),
        )

    def _on_random_fire(self, kwargs: dict) -> None:
        self._clear_random_interval_handle()  # handle already fired
        self.create_task(self._generate_and_push())
        self._start_random_interval()

    def _get_frame_library(self) -> Optional[Any]:
        if self._frame_library is not None:
            return self._frame_library

        path = self._frame_library_path
        if not path:
            return None

        try:
            from vestaboard_apps.vestaboard_configuration.frame_library import FrameLibrary
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

    async def generate_frame(self, **kwargs) -> list[list[int]]:
        cfg = self.args or {}
        min_stars = int(cfg.get("min_stars", 3))

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

        return self._generate_fallback_frame()

    def _generate_fallback_frame(self) -> list[list[int]]:
        message = random.choice(_FALLBACK_MESSAGES)
        self.log(f"Using fallback message: {message!r}", level="INFO")
        return _build_bordered_grid(message)
