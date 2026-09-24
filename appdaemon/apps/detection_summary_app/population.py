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
    for each count signal in the profile's categories, plus
    "consensus_<category>_total" / "max_<category>_total" for each category
    that has count signals.

    A category total is the sum of its signals *within one frame*, aggregated
    across frames — not the sum of the per-signal results. The scorer can read
    the same person as a man in one frame and a woman in the next; the
    per-signal maxima then say one man AND one woman, while no frame ever held
    more than one person. The image prompt draws people from the total.
    """
    strategy_fn = _STRATEGY_MAP.get(profile.consensus_strategy, _mode_value)
    result: dict[str, Any] = {}

    # Collect all count signal keys from profile categories
    all_count_signals: list[str] = []
    for cat in profile.categories:
        for sig in cat.count_signals:
            if sig not in all_count_signals:
                all_count_signals.append(sig)

    # One {signal: count} row per scored frame.
    frame_values: list[dict[str, int]] = []
    for _idx, r in (scored or {}).items():
        if not r:
            continue
        row: dict[str, int] = {}
        for sig_key in all_count_signals:
            try:
                row[sig_key] = max(0, int(_get_signal_value(r, sig_key) or 0))
            except (TypeError, ValueError):
                row[sig_key] = 0
        frame_values.append(row)

    for sig_key in all_count_signals:
        values = [row[sig_key] for row in frame_values]
        result[f"consensus_{sig_key}"] = strategy_fn(values) if values else 0
        result[f"max_{sig_key}"] = max(values) if values else 0

    for cat in profile.categories:
        if not cat.count_signals:
            continue
        signals = dict.fromkeys(cat.count_signals)
        totals = [sum(row[sig] for sig in signals) for row in frame_values]
        result[f"consensus_{cat.name}_total"] = strategy_fn(totals) if totals else 0
        result[f"max_{cat.name}_total"] = max(totals) if totals else 0

    return result
