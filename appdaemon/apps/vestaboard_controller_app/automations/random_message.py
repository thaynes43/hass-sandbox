"""Random Message automation — AI-generated or curated board messages."""

from __future__ import annotations

import random
from typing import Any

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[4]))

from providers.vestaboard.character_encoding import (
    COLOR_CODES,
    COLS,
    blank_grid,
    text_to_grid,
)

from .base import BoardAutomation

# Curated fallback messages when no AI provider is configured.
# Each message should be concise enough to fit in 6x22 with word wrap.
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
    # Top and bottom rows: all border_code
    grid[0] = [border_code] * COLS
    grid[5] = [border_code] * COLS
    # Middle rows: first and last cell as border
    for row_idx in range(1, 5):
        grid[row_idx][0] = border_code
        grid[row_idx][21] = border_code

    return grid


class RandomMessageAutomation(BoardAutomation):
    """On-demand automation that generates a random or AI message.

    No automatic triggers — the frame is generated on user request via command.
    """

    name = "RandomMessage"
    default_ttl_s = None
    default_expiration_s = None

    def get_triggers(self) -> list[dict[str, Any]]:
        """No automatic triggers — user-driven via push_frame command."""
        return []

    async def generate_frame(self) -> list[list[int]]:
        """Generate a random message grid.

        Tries the configured simple_text AI provider first; falls back to
        the curated message list if unavailable.
        """
        cfg = self.config

        # Try AI generation
        ai_provider_conf = cfg.get("ai_provider_conf")
        if ai_provider_conf:
            try:
                return await self._generate_ai_frame(ai_provider_conf)
            except Exception as exc:
                self.log(
                    f"AI generation failed, using fallback: {exc!r}",
                    level="WARNING",
                )

        # Fallback: curated message list
        return self._generate_fallback_frame()

    async def _generate_ai_frame(self, ai_provider_conf: dict) -> list[list[int]]:
        """Use simple_text AI provider to generate a message."""
        from providers.ai_providers.registry import (
            build_simple_text_provider,
            simple_text_config_from_appdaemon_args,
        )

        provider_cfg = simple_text_config_from_appdaemon_args(
            {"ai_provider_conf": ai_provider_conf}
        )
        provider = build_simple_text_provider(provider_cfg)

        self.log("Generating AI random message...", level="INFO")

        result = provider.generate_from_text(
            input_text="Generate a witty board message.",
            instructions=_AI_PERSONALITY_PROMPT,
            expected_keys=["message"],
        )

        message_text = str(result.get("message", "")).strip()
        if not message_text:
            raise ValueError("AI returned empty message")

        self.log(
            f"AI message generated ({len(message_text)} chars)",
            level="INFO",
        )

        # Try to parse as pre-formatted 6-line grid
        lines = message_text.split("\n")
        if len(lines) == 6 and all(len(line) == 22 for line in lines):
            return self._parse_preformatted_grid(lines)

        # Otherwise word-wrap into bordered grid
        return _build_bordered_grid(message_text)

    def _parse_preformatted_grid(self, lines: list[str]) -> list[list[int]]:
        """Parse a pre-formatted 6-line x 22-char grid from AI output.

        AI returns text characters; this converts them to Vestaboard codes.
        Border lines of repeated characters become color tiles.
        """
        from providers.vestaboard.character_encoding import CHAR_TO_CODE, COLOR_CODES

        emoji_to_code = {
            "\U0001f7e5": COLOR_CODES["red"],      # 🟥
            "\U0001f7e7": COLOR_CODES["orange"],   # 🟧
            "\U0001f7e8": COLOR_CODES["yellow"],   # 🟨
            "\U0001f7e9": COLOR_CODES["green"],    # 🟩
            "\U0001f7e6": COLOR_CODES["blue"],     # 🟦
            "\U0001f7ea": COLOR_CODES["violet"],   # 🟪
            "\u2b1b": COLOR_CODES["black"],        # ⬛
            "\u2b1c": 0,                           # ⬜ (white/blank)
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
            # Pad to 22 if needed
            while len(row) < 22:
                row.append(0)
            grid.append(row)
        return grid

    def _generate_fallback_frame(self) -> list[list[int]]:
        """Pick a random message from the curated list and build a bordered grid."""
        message = random.choice(_FALLBACK_MESSAGES)
        self.log(f"Using fallback message: {message!r}", level="INFO")
        return _build_bordered_grid(message)
