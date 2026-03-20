"""Template resolution for Vestaboard frames.

Frames can contain ``{entity_id}`` placeholders (e.g. ``"UPS LOAD:
{sensor.apc_2700w_load}W"``) that are substituted with live Home Assistant
entity state values at display time.

This module is pure Python — no AppDaemon dependency.  The caller wraps
``self.get_state(entity_id)`` in a ``state_getter`` callable and passes it in.
"""

from __future__ import annotations

import re
from typing import Callable, Optional

# Matches {domain.entity_name} patterns — e.g. {sensor.apc_2700w_load}
_ENTITY_PATTERN = re.compile(r"\{([a-z_]+\.[a-z0-9_]+)\}")

# Vestaboard dimensions: 6 rows × 22 columns
_BOARD_ROWS = 6
_BOARD_COLS = 22
_DEFAULT_MAX_TOTAL_CHARS = _BOARD_ROWS * _BOARD_COLS  # 132

# Sentinel values from HA that mean "no real value available"
_UNAVAILABLE_STATES = frozenset({"unavailable", "unknown"})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def find_template_entities(template: str) -> list[str]:
    """Extract all ``{entity_id}`` placeholders from *template*.

    Only patterns matching ``{domain.entity}`` (lower-case letters, digits, and
    underscores) are returned.  Duplicate entity IDs are deduplicated while
    preserving first-seen order.

    Args:
        template: A free-form string that may contain ``{entity_id}`` tokens.

    Returns:
        Ordered list of unique entity IDs found in the template.

    Examples::

        find_template_entities("Load: {sensor.ups_load}W Temp: {sensor.temp}C")
        # → ["sensor.ups_load", "sensor.temp"]
    """
    seen: set[str] = set()
    result: list[str] = []
    for match in _ENTITY_PATTERN.finditer(template):
        entity_id = match.group(1)
        if entity_id not in seen:
            seen.add(entity_id)
            result.append(entity_id)
    return result


def resolve_template(
    template: str,
    state_getter: Callable[[str], Optional[str]],
    max_total_chars: int = _DEFAULT_MAX_TOTAL_CHARS,
) -> tuple[str, dict[str, str]]:
    """Substitute ``{entity_id}`` placeholders with live HA state values.

    Unavailable or None values are replaced with ``"N/A"``.

    Overflow protection: if the fully-resolved string would exceed
    *max_total_chars*, entity values are truncated proportionally (each value
    gets ``floor(budget * weight)`` characters, where weight is proportional to
    its share of the total excess).  The static parts of the template are never
    truncated.

    Args:
        template: Template string with zero or more ``{entity_id}`` tokens.
        state_getter: Callable that accepts an entity_id and returns its current
            state string, or ``None`` if not found.
        max_total_chars: Maximum length of the resolved string.  Defaults to 132
            (6 rows × 22 columns).

    Returns:
        A 2-tuple of:
        - The fully-resolved string (possibly truncated to *max_total_chars*).
        - A dict mapping each entity_id to the value it was resolved to
          (useful for logging / debugging).

    Examples::

        text, resolutions = resolve_template(
            "UPS LOAD: {sensor.ups_load}W",
            lambda eid: "85",
        )
        # text == "UPS LOAD: 85W"
        # resolutions == {"sensor.ups_load": "85"}
    """
    entities = find_template_entities(template)
    if not entities:
        return template, {}

    # Resolve raw values from state_getter
    raw_values: dict[str, str] = {}
    for entity_id in entities:
        raw = state_getter(entity_id)
        if raw is None or raw in _UNAVAILABLE_STATES:
            raw_values[entity_id] = "N/A"
        else:
            raw_values[entity_id] = str(raw)

    # Build static frame length (template with all placeholders replaced by "")
    static_text = _ENTITY_PATTERN.sub("", template)
    static_len = len(static_text)

    # Total characters consumed by entity values (before any truncation)
    total_value_chars = sum(len(v) for v in raw_values.values())
    resolved_len = static_len + total_value_chars

    # Apply overflow truncation if necessary
    if resolved_len > max_total_chars:
        budget = max(0, max_total_chars - static_len)
        # Proportional truncation: each entity gets a share of the budget
        # based on its fraction of total value chars.  We avoid zero-length
        # values unless budget itself is 0.
        truncated: dict[str, str] = {}
        if total_value_chars > 0 and budget > 0:
            for entity_id, value in raw_values.items():
                share = len(value) / total_value_chars
                allotted = max(1, int(budget * share))
                truncated[entity_id] = value[:allotted]
        else:
            # No budget at all — collapse every value to empty string
            truncated = {eid: "" for eid in raw_values}
        resolved_values = truncated
    else:
        resolved_values = raw_values

    # Substitute placeholders in order
    def _replace(match: re.Match) -> str:
        return resolved_values[match.group(1)]

    resolved_text = _ENTITY_PATTERN.sub(_replace, template)
    return resolved_text, resolved_values


def resolve_entities(
    entities: list[dict],
    state_getter: Callable[[str], Optional[str]],
) -> list[dict]:
    """Enrich an AI prompt entity bundle with current HA state values.

    Each entry in *entities* is expected to have at minimum:

    .. code-block:: python

        {"entity_id": "sensor.ups_load", "description": "UPS load percentage"}

    The function adds a ``"current_value"`` key to each dict.  Unavailable or
    None states resolve to ``"N/A"``.  The original dicts are not mutated —
    new dicts are returned.

    Args:
        entities: List of ``{"entity_id": str, "description": str, ...}`` dicts.
        state_getter: Callable that accepts an entity_id and returns its current
            state string, or ``None`` if not found.

    Returns:
        New list of dicts with ``"current_value"`` added to each entry.

    Examples::

        enriched = resolve_entities(
            [{"entity_id": "sensor.temp", "description": "Indoor temperature"}],
            lambda eid: "72",
        )
        # [{"entity_id": "sensor.temp", "description": "Indoor temperature",
        #   "current_value": "72"}]
    """
    result: list[dict] = []
    for entry in entities:
        entity_id = entry.get("entity_id", "")
        raw = state_getter(entity_id) if entity_id else None
        if raw is None or raw in _UNAVAILABLE_STATES:
            current_value = "N/A"
        else:
            current_value = str(raw)
        result.append({**entry, "current_value": current_value})
    return result


def has_template(text: Optional[str]) -> bool:
    """Return True if *text* contains at least one ``{entity_id}`` placeholder.

    Args:
        text: Any string, or ``None``.

    Returns:
        ``True`` if the text contains one or more ``{entity_id}`` tokens.

    Examples::

        has_template("Hello world")          # False
        has_template("{sensor.temp} C")      # True
        has_template(None)                   # False
        has_template("{not_an_entity}")       # False (no dot)
    """
    if not text:
        return False
    return bool(_ENTITY_PATTERN.search(text))
