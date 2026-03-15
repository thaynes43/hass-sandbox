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
You are a pixel artist creating art for a physical display board with 6 rows \
and 22 columns. The art must fill the ENTIRE canvas — use ALL 6 rows and \
spread the design across ALL 22 columns.

Subject: {subject}

## Color codes
0=blank, 63=red, 64=orange, 65=yellow, 66=green, 67=blue, 68=violet, 69=white, 70=black

## Critical rules
1. Output EXACTLY 6 rows, each with EXACTLY 22 integer codes.
2. The design MUST span the full width. Columns 0-21 should all be used. \
Do NOT leave columns 11-21 blank — that wastes half the display.
3. The design MUST use all 6 rows. Do not leave rows empty.
4. Use at least 3 colors for visual interest.
5. Center the design: equal blank padding on left and right sides.

## Output format
Return ONLY: {{"grid": [[row0], [row1], [row2], [row3], [row4], [row5]]}}

## Examples of GOOD centered art (uses full canvas)

Star:
{{"grid": [\
[0,0,0,0,0,0,0,0,0,0,65,65,0,0,0,0,0,0,0,0,0,0],\
[0,0,0,0,0,0,0,0,65,65,65,65,65,65,0,0,0,0,0,0,0,0],\
[0,65,65,65,65,65,65,65,65,65,65,65,65,65,65,65,65,65,65,65,65,0],\
[0,0,0,65,65,65,65,65,65,65,65,65,65,65,65,65,65,65,65,0,0,0],\
[0,0,0,0,0,65,65,65,65,65,0,0,65,65,65,65,65,0,0,0,0,0],\
[0,0,0,0,65,65,65,0,0,0,0,0,0,0,0,65,65,65,0,0,0,0]\
]}}

Heart:
{{"grid": [\
[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],\
[0,0,0,63,63,63,0,0,0,0,0,0,0,0,0,0,63,63,63,0,0,0],\
[0,0,63,63,63,63,63,63,63,0,0,0,0,63,63,63,63,63,63,63,0,0],\
[0,0,0,63,63,63,63,63,63,63,63,63,63,63,63,63,63,63,63,0,0,0],\
[0,0,0,0,0,63,63,63,63,63,63,63,63,63,63,63,63,0,0,0,0,0],\
[0,0,0,0,0,0,0,0,63,63,63,63,63,63,0,0,0,0,0,0,0,0]\
]}}

Now create pixel art of: {subject}
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


class ArtGeneratedByAIAutomation(BoardAutomation):
    """On-demand automation that generates pixel art via LLM for a given subject.

    No automatic triggers — called programmatically with a subject argument.
    Retries once on validation failure.
    """

    name = "ArtGeneratedByAI"
    description = "Uses AI to create unique pixel art for your board."
    default_ttl_s = None
    default_max_age_s = None
    default_should_expire = True

    DEFAULT_UI_CONFIG = {
        "enabled": False,
        "ttl_minutes": 10,
        "should_expire": True,
        "frequency_min_minutes": 120,
        "frequency_max_minutes": 480,
    }

    @classmethod
    def get_config_schema(cls) -> dict:
        return {
            "enabled": {"type": "bool", "label": "Enabled", "default": False},
            "ttl_minutes": {"type": "int", "label": "TTL (minutes)", "min": 1, "max": 1440, "default": 10},
            "should_expire": {"type": "bool", "label": "Should Expire", "default": True},
            "frequency_min_minutes": {"type": "int", "label": "Min Frequency (minutes)", "min": 1, "max": 1440, "default": 120},
            "frequency_max_minutes": {"type": "int", "label": "Max Frequency (minutes)", "min": 1, "max": 1440, "default": 480},
        }

    def get_preview_frame(self) -> list[list[int]]:
        """Return a preview suggesting AI-generated digital/circuit art.

        Uses violet and blue tones with white accents arranged in a geometric
        checkerboard-like pattern to evoke the digital/AI aesthetic.
        """
        B = 67  # blue
        V = 68  # violet
        W = 69  # white

        # 6x22 pattern: alternating violet/blue blocks with white accent column
        # and white accent row at top and bottom edges.
        # Col 11 (center column) is white to suggest a circuit spine.
        grid: list[list[int]] = []
        for r in range(6):
            row: list[int] = []
            for c in range(22):
                if c == 11:
                    # Center spine: white
                    row.append(W)
                elif r == 0 or r == 5:
                    # Top/bottom accent row: alternating blue/violet every 2 cols
                    row.append(B if (c // 2) % 2 == 0 else V)
                else:
                    # Interior: violet/blue checkerboard with blank gaps
                    if (r + c) % 4 == 0:
                        row.append(W)
                    elif (r + c) % 2 == 0:
                        row.append(V)
                    else:
                        row.append(B)
            grid.append(row)
        return grid

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

        # Log provider metadata for debugging token limits
        meta = result.get("_meta", {})
        req_meta = meta.get("request", {})
        resp_meta = meta.get("response", {})
        self.log(
            f"AI art response — model={meta.get('model')!r} "
            f"max_tokens={req_meta.get('max_completion_tokens')} "
            f"usage={resp_meta.get('usage')}",
            level="INFO",
        )
        self.log(
            f"AI art raw content: {resp_meta.get('content_preview')}",
            level="INFO",
        )

        raw_grid = result.get("grid")
        if raw_grid is None:
            raise ValueError("AI response missing 'grid' key")

        # If the provider returned a string (JSON-encoded), parse it
        if isinstance(raw_grid, str):
            raw_grid = json.loads(raw_grid)

        # Ensure all elements are ints (AI sometimes returns floats)
        grid = [[int(code) for code in row] for row in raw_grid]

        # Log the full grid for debugging centering/layout issues
        for i, row in enumerate(grid):
            nonzero = [j for j, v in enumerate(row) if v != 0]
            if nonzero:
                self.log(
                    f"  row {i}: cols {nonzero[0]}-{nonzero[-1]} "
                    f"left_pad={nonzero[0]} right_pad={21 - nonzero[-1]} "
                    f"filled={len(nonzero)} → {row}",
                    level="INFO",
                )
            else:
                self.log(f"  row {i}: empty → {row}", level="INFO")

        return grid


# Backward-compatible alias
AIArtGeneratorAutomation = ArtGeneratedByAIAutomation
