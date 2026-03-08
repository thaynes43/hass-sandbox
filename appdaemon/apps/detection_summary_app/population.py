from __future__ import annotations

import math
from collections import Counter
from typing import Any, TYPE_CHECKING

from .selection import ScoreResult, _get_signal_value

if TYPE_CHECKING:
    from .profiles import DetectionProfile


def compute_population_bounds(scored: dict[int, ScoreResult]) -> dict[str, int]:
    """
    Compute "upper bound" counts across the analyzed snapshots.

    These are used as *possibility* bounds for image generation. The best frame used for
    image-to-image may contain fewer (or different) subjects than the max across analyzed frames.
    """
    max_male = 0
    max_female = 0
    max_animals = 0
    for _idx, r in (scored or {}).items():
        if not r:
            continue
        try:
            max_male = max(max_male, int(getattr(r, "male_count", 0) or 0))
            max_female = max(max_female, int(getattr(r, "female_count", 0) or 0))
            max_animals = max(max_animals, int(getattr(r, "animal_count", 0) or 0))
        except Exception:
            continue
    return {
        "max_male_count": int(max_male),
        "max_female_count": int(max_female),
        "max_animal_count": int(max_animals),
    }


def _mode_value(values: list[int]) -> int:
    """Most common value. Ties broken by preferring lower count (conservative)."""
    if not values:
        return 0
    counts = Counter(values)
    max_freq = max(counts.values())
    candidates = [v for v, c in counts.items() if c == max_freq]
    return min(candidates)


def _max_value(values: list[int]) -> int:
    """Maximum value."""
    return max(values) if values else 0


def _median_value(values: list[int]) -> int:
    """Median value, rounded down."""
    if not values:
        return 0
    s = sorted(values)
    n = len(s)
    if n % 2 == 1:
        return s[n // 2]
    return math.floor((s[n // 2 - 1] + s[n // 2]) / 2)


_STRATEGY_MAP = {
    "mode": _mode_value,
    "max": _max_value,
    "median": _median_value,
}


def compute_population_consensus(
    scored: dict[int, ScoreResult],
    profile: DetectionProfile,
) -> dict[str, Any]:
    """
    Compute consensus counts across frames using the profile's strategy.

    Returns dict with keys like "consensus_male_count", "max_male_count", etc.
    for each count signal in the profile's categories.
    """
    strategy_fn = _STRATEGY_MAP.get(profile.consensus_strategy, _mode_value)
    result: dict[str, Any] = {}

    # Collect all count signal keys from profile categories
    all_count_signals: list[str] = []
    for cat in profile.categories:
        for sig in cat.count_signals:
            if sig not in all_count_signals:
                all_count_signals.append(sig)

    for sig_key in all_count_signals:
        values: list[int] = []
        for _idx, r in (scored or {}).items():
            if not r:
                continue
            try:
                val = int(_get_signal_value(r, sig_key) or 0)
                values.append(max(0, val))
            except (TypeError, ValueError):
                values.append(0)

        result[f"consensus_{sig_key}"] = strategy_fn(values) if values else 0
        result[f"max_{sig_key}"] = max(values) if values else 0

    return result


def augment_image_instructions(base_instructions: str, bounds: dict[str, Any]) -> str:
    """
    Augment the image-edit prompt with facts derived from analyzed frames.
    """
    base = str(base_instructions or "").strip()
    b = bounds or {}
    try:
        mm = int(b.get("max_male_count", 0) or 0)
        ff = int(b.get("max_female_count", 0) or 0)
        aa = int(b.get("max_animal_count", 0) or 0)
    except Exception:
        mm, ff, aa = 0, 0, 0

    lines: list[str] = []
    if base:
        lines.append(base)

    lines.extend(
        [
            "",
            "Additional context (derived from multiple analyzed snapshots in this run):",
            f"- The scene may include up to {mm} male person(s) and up to {ff} female person(s).",
            f"- The scene may include up to {aa} animal(s)/pet(s).",
            "- Important: the best snapshot used for the illustration may show fewer/different people/animals than these maxima.",
            "- Preserve the apparent gender presentation of people in the input image; avoid defaulting women to men.",
            "- If animals are visible in any provided input image(s), include them in the illustration.",
            "",
            "Hard constraints (do not violate):",
            f"- Do NOT include more than {mm} male person(s).",
            f"- Do NOT include more than {ff} female person(s).",
            f"- Do NOT include more than {aa} animal(s)/pet(s).",
        ]
    )
    return "\n".join(lines).strip()


def augment_image_instructions_with_consensus(
    base_instructions: str,
    consensus: dict[str, Any],
    profile: DetectionProfile,
) -> str:
    """
    Augment the image-edit prompt using consensus data from profile-driven analysis.

    Uses "most likely" + "hard limit" language when consensus is available.
    """
    base = str(base_instructions or "").strip()
    lines: list[str] = []
    if base:
        lines.append(base)

    lines.append("")
    lines.append("Additional context (derived from multiple analyzed snapshots in this run):")

    # Build per-category context lines
    for cat in profile.categories:
        for sig in cat.count_signals:
            consensus_val = int(consensus.get(f"consensus_{sig}", 0))
            max_val = int(consensus.get(f"max_{sig}", 0))
            label = sig.replace("_", " ")
            lines.append(
                f"- The scene most likely contains {consensus_val} {label} (max: {max_val})"
            )

    lines.extend([
        "- Important: the best snapshot used for the illustration may show fewer/different subjects than these estimates.",
        "- Preserve the apparent gender presentation of people in the input image; avoid defaulting women to men.",
        "- If animals are visible in any provided input image(s), include them in the illustration.",
    ])

    # Hard constraints from max values
    lines.append("")
    lines.append("Hard constraints (do not violate):")
    for cat in profile.categories:
        for sig in cat.count_signals:
            max_val = int(consensus.get(f"max_{sig}", 0))
            label = sig.replace("_", " ")
            lines.append(f"- Do NOT include more than {max_val} {label}.")

    # Profile-specific hallucination guardrails for non-default categories
    extra_cats = [c for c in profile.categories if c.name not in ("people", "animals")]
    if extra_cats:
        lines.append("")
        lines.append("Category-specific guardrails:")
        for cat in extra_cats:
            lines.append(
                f"- Do NOT include {cat.display_name or cat.name} unless clearly visible in the reference frames."
            )

    return "\n".join(lines).strip()
