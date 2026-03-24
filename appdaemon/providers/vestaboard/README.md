# Vestaboard Provider

Shared library for communicating with the Vestaboard local API and encoding/decoding the 6x22 character grid.

## Modules

### `vestaboard_client.py`

Async HTTP client for the Vestaboard local API. Supports writing frames and reading the current board state.

- `VestaboardClient(ip, api_key)` — async context manager
- `write_frame(characters)` — write a 6x22 grid to the board
- `read_current()` — read the currently displayed grid

### `character_encoding.py`

Utilities for converting between text and Vestaboard character codes (0-70).

- `text_to_grid(text, justify, align)` — render a text string to a 6x22 character code grid
- `decode_grid(grid)` — convert a 6x22 grid back to readable text (for logging)
- `blank_grid()` — return an all-zeros 6x22 grid
- `apply_border(grid, color)` — apply a colored border to rows 0 and 5
- `detect_border_color(grid)` — detect the border color from an existing grid
- `CHAR_TO_CODE` / `CODE_TO_CHAR` — character lookup dictionaries
- `COLOR_CODES` — named color code mapping (red, orange, yellow, green, blue, violet, white)

## Usage

This provider is used by:
- `vestaboard_controller` — writes frames to the board, reads current state
- `vestaboard_configuration` — encodes user-created frames
- All automation apps — via `text_to_grid` for frame generation

## Security

No credentials are stored in this module. The Vestaboard IP and API key are passed in at construction time by the controller app, which resolves them from environment variables via `providers.secrets.resolve_secret()`.
