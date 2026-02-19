from __future__ import annotations

from typing import Any

from .selection import ScoreResult


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

