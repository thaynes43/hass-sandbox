"""Art From Library automation app — picks a pre-built pixel art frame from art_library.json."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import sys

sys.path.append(str(Path(__file__).resolve().parents[4]))  # adds appdaemon/

import hassapi as hass

from providers.vestaboard.character_encoding import COLOR_CODES, blank_grid

from vestaboard_apps._shared.base import VestaboardAutomation

_ART_LIBRARY_PATH = Path(__file__).parent / "art_library.json"


class ArtFromLibraryApp(hass.Hass, VestaboardAutomation):
    """On-demand automation that picks a random art frame from the library.

    Supports random interval scheduling when enabled with frequency config.
    """

    automation_type = "art_from_library"
    display_name = "Art From Library"
    display_description = "Randomly displays starred art from your library."
    default_ttl_s = None
    default_max_age_s = None
    default_should_expire = True

    DEFAULT_UI_CONFIG = {
        "enabled": False,
        "ttl_minutes": 10,
        "should_expire": True,
        "frequency_min_minutes": 60,
        "frequency_max_minutes": 240,
        "min_stars": 2,
    }

    @classmethod
    def get_config_schema(cls) -> dict:
        return {
            "enabled": {"type": "bool", "label": "Enabled", "default": False},
            "ttl_minutes": {"type": "int", "label": "TTL (minutes)", "min": 1, "max": 1440, "default": 10},
            "should_expire": {"type": "bool", "label": "Should Expire", "default": True},
            "frequency_min_minutes": {"type": "int", "label": "Min Frequency (minutes)", "min": 1, "max": 1440, "default": 60},
            "frequency_max_minutes": {"type": "int", "label": "Max Frequency (minutes)", "min": 1, "max": 1440, "default": 240},
            "min_stars": {"type": "int", "label": "Min Stars", "min": 0, "max": 5, "default": 2},
        }

    def get_preview_frame(self) -> list[list[int]]:
        R = 63; O = 64; Y = 65; G = 66; B = 67; V = 68
        stripe_colors = [R, O, Y, G, B, V]
        return [[c] * 22 for c in stripe_colors]

    def initialize(self) -> None:
        self._library: list[dict] = []
        self._load_library()

        self.register_with_controller()

        cfg = self.args or {}
        if cfg.get("enabled", self.DEFAULT_UI_CONFIG.get("enabled", False)):
            self._start_random_interval()

    def terminate(self) -> None:
        self._cancel_random_interval()
        self.deregister_from_controller()

    def _load_library(self) -> None:
        try:
            with open(_ART_LIBRARY_PATH, "r", encoding="utf-8") as fh:
                self._library = json.load(fh)
            self.log(
                f"Loaded {len(self._library)} art frames from library",
                level="INFO",
            )
        except Exception as exc:
            self.log(f"Failed to load art library: {exc!r}", level="ERROR")
            self._library = []

    def set_enabled(self, enabled: bool) -> None:
        if enabled:
            self._start_random_interval()
        else:
            self._cancel_random_interval()

    def on_config_updated(self, config: dict[str, Any]) -> None:
        super().on_config_updated(config)
        if "frequency_min_minutes" in config or "frequency_max_minutes" in config:
            if config.get("enabled", True):
                self._start_random_interval()

    def _start_random_interval(self) -> None:
        cfg = self.args or {}
        freq_min = cfg.get("frequency_min_minutes", 60)
        freq_max = cfg.get("frequency_max_minutes", 240)
        self._schedule_random_interval(
            self._on_random_fire,
            min_minutes=float(freq_min),
            max_minutes=float(freq_max),
        )

    def _on_random_fire(self, kwargs: dict) -> None:
        self.create_task(self._generate_and_push())
        self._start_random_interval()

    async def generate_frame(self, **kwargs) -> list[list[int]]:
        if not self._library:
            self.log("Art library is empty — returning blank grid", level="WARNING")
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
            return blank_grid()

        return characters
