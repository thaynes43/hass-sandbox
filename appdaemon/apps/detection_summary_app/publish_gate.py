from __future__ import annotations

from typing import TYPE_CHECKING

from .selection import ScoreResult, _get_signal_value

if TYPE_CHECKING:
    from .profiles import DetectionProfile


def should_publish_bundle(
    *,
    scored: dict[int, ScoreResult],
    best_person_score: float,
    best_min_person_score: float,
    best_min_animal_count: int = 1,
    profile: DetectionProfile | None = None,
) -> bool:
    """
    Decide whether to publish a bundle for this run.

    When profile is provided: publish if ANY required_for_publish category meets
    its threshold in any scored frame.

    Fallback (no profile): legacy logic using person_score + animal_count.
    """
    if profile is not None:
        return _profile_gate(scored=scored, profile=profile, best_person_score=best_person_score, best_min_person_score=best_min_person_score)

    # Legacy behavior (backward compat)
    return _legacy_gate(
        scored=scored,
        best_person_score=best_person_score,
        best_min_person_score=best_min_person_score,
        best_min_animal_count=best_min_animal_count,
    )


def _legacy_gate(
    *,
    scored: dict[int, ScoreResult],
    best_person_score: float,
    best_min_person_score: float,
    best_min_animal_count: int,
) -> bool:
    """Original publish gate: people score OR animal count."""
    try:
        if float(best_person_score) >= float(best_min_person_score):
            return True
    except Exception:
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


def _profile_gate(
    *,
    scored: dict[int, ScoreResult],
    profile: DetectionProfile,
    best_person_score: float,
    best_min_person_score: float,
) -> bool:
    """Profile-driven publish gate: any required category meets threshold."""
    # Keep the person_score check as a fallback for the people category
    try:
        if float(best_person_score) >= float(best_min_person_score):
            return True
    except Exception:
        pass

    for cat in profile.categories:
        if not cat.required_for_publish:
            continue

        # Check if any scored frame meets the category's threshold
        for _idx, r in (scored or {}).items():
            if not r:
                continue
            total = 0
            for sig_key in cat.count_signals:
                try:
                    total += max(0, int(_get_signal_value(r, sig_key) or 0))
                except (TypeError, ValueError):
                    pass
            if total >= cat.min_count_for_publish:
                return True

    return False
