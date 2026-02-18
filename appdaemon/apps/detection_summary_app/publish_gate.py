from __future__ import annotations

from .selection import ScoreResult


def should_publish_bundle(
    *,
    scored: dict[int, ScoreResult],
    best_person_score: float,
    best_min_person_score: float,
    best_min_animal_count: int = 1,
) -> bool:
    """
    Decide whether to publish a bundle for this run.

    Current behavior:
    - publish when we have strong evidence of people (best_person_score >= threshold), OR
    - publish when any analyzed frame contains animals (animal_count > 0)
    """
    try:
        if float(best_person_score) >= float(best_min_person_score):
            return True
    except Exception:
        # fall through to animal check
        pass

    min_animals = 1
    try:
        min_animals = max(1, int(best_min_animal_count))
    except Exception:
        min_animals = 1

    for _i, r in (scored or {}).items():
        try:
            if int(getattr(r, "animal_count", 0) or 0) >= min_animals:
                return True
        except Exception:
            continue
    return False

