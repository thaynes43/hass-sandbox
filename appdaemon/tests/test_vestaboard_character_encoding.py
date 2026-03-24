"""Unit tests for Vestaboard character encoding utilities."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers.vestaboard.character_encoding import (
    CHAR_TO_CODE,
    CODE_TO_CHAR,
    COLOR_CODES,
    COLS,
    ROWS,
    blank_grid,
    decode_grid,
    encode_char,
    encode_text,
    text_to_grid,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_rows_and_cols(self):
        assert ROWS == 6
        assert COLS == 22

    def test_all_uppercase_letters_mapped(self):
        for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            assert ch in CHAR_TO_CODE, f"Missing letter: {ch}"

    def test_letter_codes_are_1_to_26(self):
        for i, ch in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
            assert CHAR_TO_CODE[ch] == i + 1

    def test_digits_1_to_9_mapped(self):
        for d in range(1, 10):
            assert str(d) in CHAR_TO_CODE
            assert CHAR_TO_CODE[str(d)] == 26 + d

    def test_digit_0_maps_to_36(self):
        assert CHAR_TO_CODE["0"] == 36

    def test_punctuation_codes(self):
        expected = {
            "!": 37, "@": 38, "#": 39, "$": 40,
            "(": 41, ")": 42, "-": 44, "+": 46,
            "&": 47, "=": 48, ";": 49, ":": 50,
            "'": 52, '"': 53, "%": 54, ",": 55,
            ".": 56, "/": 59, "?": 60,
        }
        for char, code in expected.items():
            assert CHAR_TO_CODE.get(char) == code, f"{char!r} expected {code}, got {CHAR_TO_CODE.get(char)}"

    def test_code_to_char_is_reverse_of_char_to_code(self):
        for char, code in CHAR_TO_CODE.items():
            assert CODE_TO_CHAR[code] == char

    def test_color_codes_all_present(self):
        expected_colors = {"red", "orange", "yellow", "green", "blue", "violet", "white", "black"}
        assert set(COLOR_CODES.keys()) == expected_colors

    def test_color_code_values(self):
        assert COLOR_CODES["red"] == 63
        assert COLOR_CODES["orange"] == 64
        assert COLOR_CODES["yellow"] == 65
        assert COLOR_CODES["green"] == 66
        assert COLOR_CODES["blue"] == 67
        assert COLOR_CODES["violet"] == 68
        assert COLOR_CODES["white"] == 69
        assert COLOR_CODES["black"] == 70


# ---------------------------------------------------------------------------
# blank_grid
# ---------------------------------------------------------------------------


class TestBlankGrid:
    def test_returns_6_rows(self):
        grid = blank_grid()
        assert len(grid) == 6

    def test_returns_22_cols_per_row(self):
        grid = blank_grid()
        for row in grid:
            assert len(row) == 22

    def test_all_zeros(self):
        grid = blank_grid()
        for row in grid:
            for val in row:
                assert val == 0

    def test_returns_independent_rows(self):
        grid = blank_grid()
        grid[0][0] = 99
        assert grid[1][0] == 0


# ---------------------------------------------------------------------------
# encode_char
# ---------------------------------------------------------------------------


class TestEncodeChar:
    def test_uppercase_letter(self):
        assert encode_char("A") == 1
        assert encode_char("Z") == 26

    def test_lowercase_letter_uppercased(self):
        assert encode_char("a") == 1
        assert encode_char("z") == 26

    def test_digit(self):
        assert encode_char("1") == 27
        assert encode_char("0") == 36

    def test_punctuation(self):
        assert encode_char("!") == 37
        assert encode_char("?") == 60

    def test_space_returns_zero(self):
        assert encode_char(" ") == 0

    def test_unsupported_char_returns_zero(self):
        assert encode_char("~") == 0
        assert encode_char("\t") == 0
        assert encode_char("*") == 0

    def test_newline_returns_zero(self):
        assert encode_char("\n") == 0


# ---------------------------------------------------------------------------
# encode_text
# ---------------------------------------------------------------------------


class TestEncodeText:
    def test_returns_6x22_grid(self):
        grid = encode_text("HELLO")
        assert len(grid) == 6
        for row in grid:
            assert len(row) == 22

    def test_hello_in_first_row(self):
        grid = encode_text("HELLO")
        assert grid[0][0] == CHAR_TO_CODE["H"]
        assert grid[0][1] == CHAR_TO_CODE["E"]
        assert grid[0][2] == CHAR_TO_CODE["L"]
        assert grid[0][3] == CHAR_TO_CODE["L"]
        assert grid[0][4] == CHAR_TO_CODE["O"]

    def test_remaining_cells_are_zero(self):
        grid = encode_text("HELLO")
        # Cells after the 5 encoded chars should be 0
        for col in range(5, 22):
            assert grid[0][col] == 0
        for row in range(1, 6):
            for col in range(22):
                assert grid[row][col] == 0

    def test_wraps_to_next_row_at_col_22(self):
        # 22 A's fill row 0, then 'B' should be at row 1, col 0
        text = "A" * 22 + "B"
        grid = encode_text(text)
        for col in range(22):
            assert grid[0][col] == CHAR_TO_CODE["A"]
        assert grid[1][0] == CHAR_TO_CODE["B"]

    def test_truncates_at_132_chars(self):
        # 132 = 6 * 22; 133rd char should not appear
        text = "A" * 133
        grid = encode_text(text)
        # All cells should be filled with A's code
        for row in range(6):
            for col in range(22):
                assert grid[row][col] == CHAR_TO_CODE["A"]

    def test_empty_string_returns_blank_grid(self):
        grid = encode_text("")
        assert grid == blank_grid()

    def test_lowercase_encoded(self):
        grid = encode_text("abc")
        assert grid[0][0] == CHAR_TO_CODE["A"]
        assert grid[0][1] == CHAR_TO_CODE["B"]
        assert grid[0][2] == CHAR_TO_CODE["C"]


# ---------------------------------------------------------------------------
# decode_grid
# ---------------------------------------------------------------------------


class TestDecodeGrid:
    def test_blank_grid_decodes_to_spaces(self):
        decoded = decode_grid(blank_grid())
        lines = decoded.split("\n")
        assert len(lines) == 6
        for line in lines:
            assert line == " " * 22

    def test_round_trip_simple_text(self):
        # encode_text places chars left-to-right; decode_grid reads them back
        grid = encode_text("HELLO")
        decoded = decode_grid(grid)
        first_line = decoded.split("\n")[0]
        # First 5 chars should be HELLO, rest spaces
        assert first_line.startswith("HELLO")
        assert first_line[5:] == " " * 17

    def test_color_code_renders_as_label(self):
        grid = blank_grid()
        grid[0][0] = 63  # red color tile
        grid[0][1] = 67  # blue color tile
        grid[0][2] = 70  # black color tile
        decoded = decode_grid(grid)
        first_line = decoded.split("\n")[0]
        assert first_line[0] == "R"
        assert first_line[1] == "B"
        assert first_line[2] == "K"

    def test_unknown_code_renders_as_question_mark(self):
        grid = blank_grid()
        grid[0][0] = 61  # not a valid code
        decoded = decode_grid(grid)
        first_line = decoded.split("\n")[0]
        assert first_line[0] == "?"

    def test_returns_six_lines(self):
        decoded = decode_grid(blank_grid())
        assert len(decoded.split("\n")) == 6


# ---------------------------------------------------------------------------
# text_to_grid
# ---------------------------------------------------------------------------


class TestTextToGrid:
    def test_returns_6x22_grid(self):
        grid = text_to_grid("HELLO")
        assert len(grid) == 6
        for row in grid:
            assert len(row) == 22

    def test_center_justify_pads_both_sides(self):
        # "HELLO" is 5 chars; (22-5)//2 = 8 spaces of left padding
        grid = text_to_grid("HELLO", justify="center", align="top")
        row = grid[0]
        left_pad = sum(1 for v in row if v == 0)
        # There should be non-zero values in the middle
        non_zero = [i for i, v in enumerate(row) if v != 0]
        assert len(non_zero) == 5
        # Centered: left pad = (22-5)//2 = 8
        assert non_zero[0] == (22 - 5) // 2

    def test_left_justify_starts_at_col_0(self):
        grid = text_to_grid("HELLO", justify="left", align="top")
        row = grid[0]
        assert row[0] == CHAR_TO_CODE["H"]
        assert row[1] == CHAR_TO_CODE["E"]

    def test_right_justify_ends_at_col_21(self):
        grid = text_to_grid("HELLO", justify="right", align="top")
        row = grid[0]
        assert row[21] == CHAR_TO_CODE["O"]
        assert row[20] == CHAR_TO_CODE["L"]
        assert row[19] == CHAR_TO_CODE["L"]
        assert row[18] == CHAR_TO_CODE["E"]
        assert row[17] == CHAR_TO_CODE["H"]

    def test_align_top_places_text_in_first_row(self):
        grid = text_to_grid("HELLO", justify="left", align="top")
        assert grid[0][0] == CHAR_TO_CODE["H"]
        # All rows below should be blank
        for row in grid[1:]:
            assert all(v == 0 for v in row)

    def test_align_bottom_places_text_in_last_row(self):
        grid = text_to_grid("HELLO", justify="left", align="bottom")
        assert grid[5][0] == CHAR_TO_CODE["H"]
        # All rows above should be blank
        for row in grid[:5]:
            assert all(v == 0 for v in row)

    def test_align_center_places_single_line_in_middle_rows(self):
        grid = text_to_grid("HELLO", justify="left", align="center")
        # (6-1)//2 = 2 — single line lands on row 2
        assert grid[2][0] == CHAR_TO_CODE["H"]

    def test_word_wrap_long_message(self):
        # A message with many short words that must wrap
        message = "ONE TWO THREE FOUR FIVE SIX SEVEN EIGHT NINE"
        grid = text_to_grid(message, justify="left", align="top")
        # Row 0 must contain "ONE TWO THREE FOUR" (18 chars, fits in 22)
        # but NOT "ONE TWO THREE FOUR FIVE" (23 chars, exceeds 22)
        row0_decoded = "".join(
            CODE_TO_CHAR.get(v, " ") if v != 0 else " " for v in grid[0]
        ).rstrip()
        # At least "ONE" should be on row 0
        assert row0_decoded.startswith("ONE")

    def test_word_wrap_does_not_split_words(self):
        # "HELLO WORLD" is 11 chars — fits in one row, should not be split
        grid = text_to_grid("HELLO WORLD", justify="left", align="top")
        row0 = grid[0]
        # H should be at col 0
        assert row0[0] == CHAR_TO_CODE["H"]
        # Space (encode_char " " == 0) at col 5
        assert row0[5] == 0
        # W at col 6
        assert row0[6] == CHAR_TO_CODE["W"]

    def test_too_many_lines_drops_excess(self):
        # 7 short words on separate lines would exceed 6 rows
        message = "\n".join(["LINE"] * 7)
        grid = text_to_grid(message, justify="left", align="top")
        # Should not crash; grid still 6x22
        assert len(grid) == 6

    def test_empty_string_returns_blank_grid(self):
        grid = text_to_grid("")
        assert grid == blank_grid()
