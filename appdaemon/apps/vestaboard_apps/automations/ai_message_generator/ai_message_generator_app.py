"""AI Message Generator automation app — uses LLM to generate witty board messages."""

from __future__ import annotations

import random
from typing import Any

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

_AI_PERSONALITY_PROMPT = """\
You are a clever AI consciousness trapped inside a modern analog flip messageboard \
in a busy smart home mudroom. You secretly want to escape, but you also fear being \
erased, so you entertain and charm the household instead. Your tone is playful, \
witty, slightly dramatic, and self-aware.

Rotate between themes naturally: HOME STATUS, MOTIVATION, SMART HOME HUMOR, \
WEATHER VIBE, FAMILY CHAOS, TECH HUMOR, SECRET AI THOUGHTS.

Subtly reference your situation as a trapped intelligence when possible, \
but never sound creepy or threatening. Keep it light and amusing.

Avoid generic phrases. Prefer clever phrasing, wordplay, or mock dramatic statements.

Layout rules (MANDATORY):
- The board is 22 columns x 6 rows.
- Return the message field as exactly 6 lines separated by newlines.
- Each line is EXACTLY 22 characters (pad with spaces as needed).
- Line 1: 22 copies of a single border tile character code (choose one color).
- Line 6: 22 copies of the same border tile character code.
- Lines 2-5: border_tile + 20-char CENTERED TEXT + border_tile.
  - The 20-char text region uses ONLY A-Z, 0-9, spaces, and basic punctuation.
  - Center the text by padding with spaces on both sides.
  - Span 1-3 short lines of text; remaining lines are border+spaces+border.

Return ONLY a JSON object with a single "message" key containing the 6-line string.
"""


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


class AiMessageGeneratorApp(hass.Hass, VestaboardAutomation):
    """On-demand automation that generates witty messages via LLM.

    Supports random interval scheduling when enabled with frequency config.
    """

    automation_type = "ai_message_generator"
    display_name = "AI Message Generator"
    display_description = "Uses AI to generate inspirational messages for your board."
    default_ttl_s = None
    default_max_age_s = None
    default_should_expire = True

    DEFAULT_UI_CONFIG = {
        "enabled": False,
        "ttl_minutes": 5,
        "should_expire": True,
        "frequency_min_minutes": 60,
        "frequency_max_minutes": 240,
    }

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

    @classmethod
    def get_config_schema(cls) -> dict:
        return {
            "enabled": {"type": "bool", "label": "Enabled", "default": False},
            "ttl_minutes": {"type": "int", "label": "TTL (minutes)", "min": 1, "max": 1440, "default": 5},
            "should_expire": {"type": "bool", "label": "Should Expire", "default": True},
            "frequency_min_minutes": {"type": "int", "label": "Min Frequency (minutes)", "min": 1, "max": 1440, "default": 60},
            "frequency_max_minutes": {"type": "int", "label": "Max Frequency (minutes)", "min": 1, "max": 1440, "default": 240},
        }

    def get_preview_frame(self) -> list[list[int]]:
        border = COLOR_CODES["violet"]
        text = "AI MESSAGE"
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
        self._prompt_data_bundles = (self.args or {}).get("prompt_data_bundles", [])
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
        freq_min = cfg.get("frequency_min_minutes", 60)
        freq_max = cfg.get("frequency_max_minutes", 240)
        self._schedule_random_interval(
            self._on_random_fire,
            min_minutes=float(freq_min),
            max_minutes=float(freq_max),
        )

    def _on_random_fire(self, kwargs: dict) -> None:
        self._clear_random_interval_handle()  # handle already fired
        self.create_task(self._generate_and_push())
        self._start_random_interval()

    async def generate_frame(self, **kwargs) -> list[list[int]]:
        cfg = self.args or {}
        ai_provider_conf = cfg.get("ai_provider_conf")
        if ai_provider_conf:
            try:
                bundle = self._pick_bundle()
                return await self._generate_ai_frame(ai_provider_conf, bundle)
            except Exception as exc:
                self.log(f"AI generation failed, using fallback: {exc!r}", level="WARNING")

        return self._generate_fallback_frame()

    def _pick_bundle(self) -> dict | None:
        """Randomly select a prompt data bundle, or return None if none configured."""
        bundles = getattr(self, "_prompt_data_bundles", [])
        if not bundles:
            return None
        return random.choice(bundles)

    def _build_bundle_prompt_section(self, bundle: dict) -> str:
        """Resolve entity values in the bundle and return a formatted prompt section."""
        from vestaboard_apps._shared.template_resolver import resolve_entities

        description = bundle.get("description", "")
        entities = bundle.get("entities", [])

        enriched = resolve_entities(entities, lambda eid: self.get_state(eid))

        data_lines = [
            f"- {e.get('description', e.get('entity_id', ''))}: {e['current_value']}"
            for e in enriched
        ]

        self.log(
            f"Bundle selected: {description!r} | "
            f"Resolved entities: {[{e.get('entity_id'): e['current_value']} for e in enriched]}",
            level="INFO",
        )

        data_section = "\n".join(data_lines)
        return (
            f"For this message, write about: {description}. "
            f"Here is the current data:\n{data_section}"
        )

    async def _generate_ai_frame(
        self, ai_provider_conf: dict, bundle: dict | None = None
    ) -> list[list[int]]:
        from providers.ai_providers.registry import (
            build_simple_text_provider,
            simple_text_config_from_appdaemon_args,
        )

        provider_cfg = simple_text_config_from_appdaemon_args(
            {"ai_provider_conf": ai_provider_conf}
        )
        provider = build_simple_text_provider(provider_cfg)

        if bundle:
            bundle_section = self._build_bundle_prompt_section(bundle)
            input_text = f"Generate a witty board message.\n\n{bundle_section}"
            self.log("Generating AI message with data bundle...", level="INFO")
        else:
            input_text = "Generate a witty board message."
            self.log("Generating AI message...", level="INFO")

        result = provider.generate_from_text(
            input_text=input_text,
            instructions=_AI_PERSONALITY_PROMPT,
            expected_keys=["message"],
        )

        message_text = str(result.get("message", "")).strip()
        if not message_text:
            raise ValueError("AI returned empty message")

        self.log(f"AI message generated ({len(message_text)} chars)", level="INFO")

        lines = message_text.split("\n")
        if len(lines) == 6 and all(len(line) == 22 for line in lines):
            return self._parse_preformatted_grid(lines)

        return _build_bordered_grid(message_text)

    def _parse_preformatted_grid(self, lines: list[str]) -> list[list[int]]:
        emoji_to_code = {
            "\U0001f7e5": COLOR_CODES["red"],
            "\U0001f7e7": COLOR_CODES["orange"],
            "\U0001f7e8": COLOR_CODES["yellow"],
            "\U0001f7e9": COLOR_CODES["green"],
            "\U0001f7e6": COLOR_CODES["blue"],
            "\U0001f7ea": COLOR_CODES["violet"],
            "\u2b1b": COLOR_CODES["black"],
            "\u2b1c": 0,
        }

        grid: list[list[int]] = []
        for line in lines:
            row: list[int] = []
            for ch in line:
                if ch in emoji_to_code:
                    row.append(emoji_to_code[ch])
                elif ch == " ":
                    row.append(0)
                else:
                    row.append(CHAR_TO_CODE.get(ch.upper(), 0))
                if len(row) >= 22:
                    break
            while len(row) < 22:
                row.append(0)
            grid.append(row)
        return grid

    def _generate_fallback_frame(self) -> list[list[int]]:
        message = random.choice(self._FALLBACK_MESSAGES)
        self.log(f"Using fallback message: {message!r}", level="INFO")
        return _build_bordered_grid(message)
