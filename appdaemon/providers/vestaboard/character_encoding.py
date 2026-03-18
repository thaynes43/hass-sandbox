"""Vestaboard character encoding utilities.

Maps characters to Vestaboard character codes and provides helpers to convert
text to the 6x22 integer grid that the Vestaboard local API expects.

Reference: https://docs.vestaboard.com/characters
"""

from __future__ import annotations

# Board dimensions
ROWS = 6
COLS = 22

# ---------------------------------------------------------------------------
# Character -> code mapping
# ---------------------------------------------------------------------------

# Letters A-Z: 1-26
_LETTERS: dict[str, int] = {chr(ord("A") + i): i + 1 for i in range(26)}

# Digits 1-9: 27-35, 0: 36
_DIGITS: dict[str, int] = {str(d): 26 + d for d in range(1, 10)}
_DIGITS["0"] = 36

# Punctuation
_PUNCT: dict[str, int] = {
    "!": 37,
    "@": 38,
    "#": 39,
    "$": 40,
    "(": 41,
    ")": 42,
    "-": 44,
    "+": 46,
    "&": 47,
    "=": 48,
    ";": 49,
    ":": 50,
    "'": 52,
    '"': 53,
    "%": 54,
    ",": 55,
    ".": 56,
    "/": 59,
    "?": 60,
}

CHAR_TO_CODE: dict[str, int] = {**_LETTERS, **_DIGITS, **_PUNCT}

# Reverse mapping
CODE_TO_CHAR: dict[int, str] = {v: k for k, v in CHAR_TO_CODE.items()}

# ---------------------------------------------------------------------------
# Color codes
# ---------------------------------------------------------------------------

COLOR_CODES: dict[str, int] = {
    "red": 63,
    "orange": 64,
    "yellow": 65,
    "green": 66,
    "blue": 67,
    "violet": 68,
    "white": 69,
    "black": 70,
}

# ---------------------------------------------------------------------------
# Grid helpers
# ---------------------------------------------------------------------------


def blank_grid() -> list[list[int]]:
    """Return a 6x22 grid filled with zeros (blank/black cells)."""
    return [[0] * COLS for _ in range(ROWS)]


def encode_char(char: str) -> int:
    """Convert a single character to its Vestaboard code.

    Lowercase letters are uppercased before lookup.  Unsupported characters
    (including space) return 0 (blank cell).
    """
    return CHAR_TO_CODE.get(char.upper(), 0)


def encode_text(text: str) -> list[list[int]]:
    """Encode a string into a 6x22 grid character by character.

    Characters are placed left-to-right, top-to-bottom.  The grid is
    truncated to 6*22 = 132 characters.  Remaining cells are 0.
    """
    grid = blank_grid()
    for idx, ch in enumerate(text):
        if idx >= ROWS * COLS:
            break
        row, col = divmod(idx, COLS)
        grid[row][col] = encode_char(ch)
    return grid


# Reverse lookup for color codes → short labels for logging
_COLOR_CODE_LABELS: dict[int, str] = {
    63: "R",  # red
    64: "O",  # orange
    65: "Y",  # yellow
    66: "G",  # green
    67: "B",  # blue
    68: "V",  # violet
    69: "W",  # white
    70: "K",  # black
}


def decode_grid(grid: list[list[int]]) -> str:
    """Convert a 6x22 grid back to a human-readable 6-line string.

    Code 0 is rendered as a space.  Color tile codes (63-70) are rendered
    as single-letter labels: R=red, O=orange, Y=yellow, G=green, B=blue,
    V=violet, W=white, K=black.  Unknown codes are rendered as ``?``.
    """
    lines: list[str] = []
    for row in grid:
        chars: list[str] = []
        for code in row:
            if code == 0:
                chars.append(" ")
            elif code in _COLOR_CODE_LABELS:
                chars.append(_COLOR_CODE_LABELS[code])
            else:
                chars.append(CODE_TO_CHAR.get(code, "?"))
        lines.append("".join(chars))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Word-wrap layout helper
# ---------------------------------------------------------------------------


def _wrap_words(words: list[str], width: int) -> list[str]:
    """Wrap a list of words to lines of at most *width* characters."""
    lines: list[str] = []
    current = ""
    for word in words:
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= width:
            current += " " + word
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def text_to_grid(
    message: str,
    justify: str = "center",
    align: str = "center",
) -> list[list[int]]:
    """Word-wrap *message* into a 6x22 grid with alignment control.

    Parameters
    ----------
    message:
        The text to render.
    justify:
        Horizontal alignment of each line: ``"left"``, ``"center"``, or
        ``"right"``.
    align:
        Vertical placement of the text block: ``"top"``, ``"center"``, or
        ``"bottom"``.

    Lines longer than 22 characters are hard-wrapped at column 22.
    The block is vertically placed according to *align*; excess lines are
    dropped silently.
    """
    # Split on newlines first, then word-wrap each paragraph
    raw_lines: list[str] = []
    for paragraph in message.split("\n"):
        words = paragraph.split()
        if not words:
            raw_lines.append("")
            continue
        wrapped = _wrap_words(words, COLS)
        raw_lines.extend(wrapped)

    # Hard-clip lines that exceed column width (safety net)
    clipped: list[str] = [line[:COLS] for line in raw_lines]

    # Limit to ROWS lines (drop overflow)
    text_lines = clipped[:ROWS]
    n_lines = len(text_lines)

    # Vertical offset
    if align == "top":
        start_row = 0
    elif align == "bottom":
        start_row = max(0, ROWS - n_lines)
    else:  # center (default)
        start_row = max(0, (ROWS - n_lines) // 2)

    grid = blank_grid()

    for i, line in enumerate(text_lines):
        row_idx = start_row + i
        if row_idx >= ROWS:
            break

        line_len = len(line)
        if justify == "left":
            col_offset = 0
        elif justify == "right":
            col_offset = COLS - line_len
        else:  # center (default)
            col_offset = (COLS - line_len) // 2

        for j, ch in enumerate(line):
            col_idx = col_offset + j
            if 0 <= col_idx < COLS:
                grid[row_idx][col_idx] = encode_char(ch)

    return grid
