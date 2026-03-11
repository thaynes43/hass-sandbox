"""AI Art Generator automation — uses LLM to generate pixel art on request."""

from __future__ import annotations

import json
from typing import Any

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[4]))

from .base import BoardAutomation

# Valid Vestaboard character codes: 0-36 (blank/chars), 37-60 (punctuation),
# 63-70 (color tiles). Codes 0 and 63-70 are most useful for pixel art.
_VALID_CODES = set(range(0, 61)) | {63, 64, 65, 66, 67, 68, 69, 70}

_ART_PROMPT_TEMPLATE = """\
You are a pixel artist for a 22-column x 6-row display board.

Create pixel art of: {subject}

Return ONLY a JSON object with a single key "grid" containing a 6x22 nested list \
of integer Vestaboard character codes.

Valid codes:
- 0: blank/black background
- 63: red tile
- 64: orange tile
- 65: yellow tile
- 66: green tile
- 67: blue tile
- 68: violet/purple tile
- 69: white tile
- 70: black filled tile

Rules:
- The grid must be EXACTLY 6 rows.
- Each row must have EXACTLY 22 elements.
- Use only the valid codes listed above.
- Create a recognizable, colorful pixel art image.
- Use at least 3 different colors for visual interest.
- Center the subject within the 22x6 canvas.

Return ONLY the JSON object, no other text.
Example format:
{{"grid": [[0,0,...], [0,63,...], ...]}}
"""


def _validate_grid(grid: Any) -> tuple[bool, str]:
    """Validate that grid is 6x22 with valid codes. Returns (ok, error_msg)."""
    if not isinstance(grid, list):
        return False, f"grid is not a list: {type(grid)}"
    if len(grid) != 6:
        return False, f"grid has {len(grid)} rows, expected 6"
    for i, row in enumerate(grid):
        if not isinstance(row, list):
            return False, f"row {i} is not a list: {type(row)}"
        if len(row) != 22:
            return False, f"row {i} has {len(row)} columns, expected 22"
        for j, code in enumerate(row):
            if not isinstance(code, int) or code not in _VALID_CODES:
                return False, (
                    f"row {i} col {j} has invalid code {code!r}; "
                    f"valid codes are 0-60, 63-70"
                )
    return True, ""


class AIArtGeneratorAutomation(BoardAutomation):
    """On-demand automation that generates pixel art via LLM for a given subject.

    No automatic triggers — called programmatically with a subject argument.
    Retries once on validation failure.
    """

    name = "AIArtGenerator"
    default_ttl_s = None
    default_expiration_s = None

    def get_triggers(self) -> list[dict[str, Any]]:
        """No automatic triggers — user-driven via push_frame command."""
        return []

    async def generate_frame(self, subject: str = "abstract art") -> list[list[int]]:
        """Generate pixel art for *subject* using the configured AI provider.

        Args:
            subject: The subject/theme for the pixel art (e.g. "cat", "rocket").

        Returns:
            A valid 6x22 Vestaboard character grid.
        """
        cfg = self.config
        ai_provider_conf = cfg.get("ai_provider_conf")

        if not ai_provider_conf:
            self.log(
                "No ai_provider_conf configured — returning blank grid",
                level="WARNING",
            )
            from providers.vestaboard.character_encoding import blank_grid
            return blank_grid()

        self.log(f"Generating AI pixel art for subject: {subject!r}", level="INFO")

        for attempt in range(1, 3):
            try:
                grid = await self._call_ai(ai_provider_conf, subject)
                ok, err = _validate_grid(grid)
                if ok:
                    self.log(
                        f"AI pixel art generated successfully (attempt {attempt})",
                        level="INFO",
                    )
                    return grid
                self.log(
                    f"Attempt {attempt} grid validation failed: {err}",
                    level="WARNING",
                )
            except Exception as exc:
                self.log(
                    f"Attempt {attempt} AI call failed: {exc!r}",
                    level="WARNING",
                )

        self.log(
            "All AI art generation attempts failed — returning blank grid",
            level="ERROR",
        )
        from providers.vestaboard.character_encoding import blank_grid
        return blank_grid()

    async def _call_ai(
        self, ai_provider_conf: dict[str, Any], subject: str
    ) -> list[list[int]]:
        """Call the simple_text provider and parse the grid from the response."""
        from providers.ai_providers.registry import (
            build_simple_text_provider,
            simple_text_config_from_appdaemon_args,
        )

        provider_cfg = simple_text_config_from_appdaemon_args(
            {"ai_provider_conf": ai_provider_conf}
        )
        provider = build_simple_text_provider(provider_cfg)

        prompt = _ART_PROMPT_TEMPLATE.format(subject=subject)

        result = provider.generate_from_text(
            input_text=f"Create pixel art of: {subject}",
            instructions=prompt,
            expected_keys=["grid"],
        )

        raw_grid = result.get("grid")
        if raw_grid is None:
            raise ValueError("AI response missing 'grid' key")

        # If the provider returned a string (JSON-encoded), parse it
        if isinstance(raw_grid, str):
            raw_grid = json.loads(raw_grid)

        # Ensure all elements are ints (AI sometimes returns floats)
        grid = [[int(code) for code in row] for row in raw_grid]
        return grid
