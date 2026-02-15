from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass
class ScoreResult:
    person_score: float
    face_score: float
    frame_score: float
    pose: str
    summary: str
    structured: dict[str, Any]


@dataclass
class SelectionMeta:
    budget: int
    scored_indices: list[int]
    probes: list[int]
    cutoff_idx_inclusive: int
    best_idx: int


def _pose_rank(pose: str) -> int:
    p = (pose or "").strip().lower()
    return {"standing": 3, "stationary": 3, "sitting": 2, "walking": 1, "moving": 1}.get(p, 0)


def _pick_key(res: ScoreResult) -> tuple:
    has_person = 1 if res.person_score > 0 else 0
    has_summary = 1 if (res.summary or "").strip() else 0
    return (
        has_person,
        res.face_score,
        res.frame_score,
        _pose_rank(res.pose),
        res.person_score,
        has_summary,
    )


def _weighted_random_indices(
    rng: random.Random, n: int, k: int, *, max_idx_inclusive: Optional[int] = None
) -> list[int]:
    """
    Pick k indices in [0, n-1] with a bias toward earlier indices.
    Uses a square transform to skew toward 0.
    """
    if n <= 0 or k <= 0:
        return []
    out: set[int] = set()
    max_idx = n - 1 if max_idx_inclusive is None else max(0, min(n - 1, int(max_idx_inclusive)))
    while len(out) < min(k, n):
        u = rng.random()
        # skew toward 0
        idx = int((u * u * u) * max_idx)
        out.add(max(0, min(max_idx, idx)))
    return sorted(out)


def adaptive_select_and_score(
    *,
    total_frames: int,
    budget: int,
    score_index: Callable[[int], ScoreResult],
    score_indices: Optional[Callable[[list[int]], dict[int, ScoreResult]]] = None,
    seed: str,
    no_people_threshold: float = 1.0,
    lookahead_after_no_people: int = 2,
) -> tuple[dict[int, ScoreResult], SelectionMeta]:
    """
    Adaptive selection that attempts to find a peak-quality frame while avoiding spending
    budget far past the point where people disappear.

    This is intentionally heuristic and designed to be improved over time.
    """
    n = int(total_frames)
    budget = max(1, int(budget))
    rng = random.Random(seed)

    scored: dict[int, ScoreResult] = {}
    probes: list[int] = []

    def ensure_batch(indices: list[int]) -> None:
        # Fill cache for any missing indices, respecting budget.
        if not indices or len(scored) >= budget:
            return
        uniq: list[int] = []
        for ii in indices:
            ii = max(0, min(n - 1, int(ii)))
            if ii in scored:
                continue
            if ii in uniq:
                continue
            uniq.append(ii)
        if not uniq:
            return
        remaining = budget - len(scored)
        uniq = uniq[:remaining]
        probes.extend(uniq)
        if score_indices is not None:
            got = score_indices(uniq)
            for k, v in (got or {}).items():
                if k not in scored and v is not None:
                    scored[int(k)] = v
            return
        for ii in uniq:
            scored[ii] = score_index(ii)

    def ensure(i: int) -> ScoreResult:
        i = max(0, min(n - 1, int(i)))
        if i in scored:
            return scored[i]
        if len(scored) >= budget:
            # Budget exhausted: return a very low score placeholder.
            return ScoreResult(0, 0, 0, "none", "", {})
        ensure_batch([i])
        return scored.get(i, ScoreResult(0, 0, 0, "none", "", {}))

    if n <= budget:
        for i in range(n):
            ensure(i)
        best_idx = max(scored.keys(), key=lambda i: _pick_key(scored[i]))
        meta = SelectionMeta(budget=budget, scored_indices=sorted(scored.keys()), probes=probes, cutoff_idx_inclusive=n - 1, best_idx=best_idx)
        return scored, meta

    # --- 1) seed ---
    seeds = {0, n // 2, n - 1}
    # Keep seeding light so we preserve budget for peak search + cutoff + neighbors.
    early_max = int((n - 1) * 0.75)
    seeds.update(_weighted_random_indices(rng, n, k=min(1, max(0, budget - 3)), max_idx_inclusive=early_max))
    # Add a centered sample around the middle to catch peaks near mid-run.
    if budget >= 4:
        span = max(1, n // 6)
        seeds.add(max(0, min(n - 1, (n // 2) + rng.randint(-span, span))))
    ensure_batch(sorted(seeds))

    # If we already exhausted budget, pick best among what we have.
    if len(scored) >= budget:
        best_idx = max(scored.keys(), key=lambda i: _pick_key(scored[i]))
        meta = SelectionMeta(budget=budget, scored_indices=sorted(scored.keys()), probes=probes, cutoff_idx_inclusive=n - 1, best_idx=best_idx)
        return scored, meta

    # --- 2) ternary-ish search for peak ---
    lo = 0
    hi = n - 1
    iterations = min(6, max(2, budget // 2))
    for _ in range(iterations):
        if len(scored) >= budget or (hi - lo) < 3:
            break
        m1 = lo + (hi - lo) // 3
        m2 = hi - (hi - lo) // 3
        ensure_batch([m1, m2])
        r1 = ensure(m1)
        r2 = ensure(m2)
        if _pick_key(r1) >= _pick_key(r2):
            hi = m2
        else:
            lo = m1

    best_idx = max(scored.keys(), key=lambda i: _pick_key(scored[i]))

    # --- 3) find trailing cutoff after no-people ---
    cutoff = n - 1
    # If we already have evidence of a no-people frame after best_idx from prior probes,
    # infer a cutoff without spending more budget.
    existing_no = [i for i, r in scored.items() if i > best_idx and r.person_score <= no_people_threshold]
    if existing_no:
        first_no = min(existing_no)
        existing_people = [i for i, r in scored.items() if i < first_no and r.person_score > no_people_threshold]
        if existing_people:
            cutoff = max(existing_people)
    # Walk forward with exponential steps from best_idx to find first no-people.
    step = 1
    last_people = best_idx
    first_no_people: Optional[int] = None
    while len(scored) < budget:
        j = best_idx + step
        if j >= n:
            break
        ensure_batch([j])
        r = ensure(j)
        if r.person_score <= no_people_threshold:
            first_no_people = j
            break
        last_people = j
        step *= 2
        if step > n:
            break

    if first_no_people is not None:
        # Binary search boundary between last_people and first_no_people (best effort).
        a = last_people
        b = first_no_people
        while (b - a) > 1 and len(scored) < budget:
            mid = (a + b) // 2
            r = ensure(mid)
            if r.person_score <= no_people_threshold:
                b = mid
            else:
                a = mid
        boundary = a  # last index with people (best effort)

        # Lookahead slightly past the first no-people to confirm (only if we still have budget).
        look_ok = True
        if len(scored) < budget:
            for k in range(1, lookahead_after_no_people + 1):
                jj = min(n - 1, b + k)
                if jj <= boundary or len(scored) >= budget:
                    continue
                ensure_batch([jj])
                rr = ensure(jj)
                if rr.person_score > no_people_threshold:
                    look_ok = False
                    break
        if look_ok:
            cutoff = boundary

    # --- 4) spend remaining budget around peak within cutoff ---
    if len(scored) < budget:
        # neighbors around current best
        radius = 1
        while len(scored) < budget and radius <= max(3, budget):
            batch: list[int] = []
            for j in (best_idx - radius, best_idx + radius):
                if 0 <= j <= cutoff and len(scored) < budget:
                    batch.append(j)
            ensure_batch(batch)
            radius += 1

    if len(scored) < budget:
        remaining = budget - len(scored)
        candidates = list(range(0, cutoff + 1))
        rng.shuffle(candidates)
        for j in candidates:
            if len(scored) >= budget:
                break
            ensure(j)

    best_idx = max(scored.keys(), key=lambda i: _pick_key(scored[i]))
    meta = SelectionMeta(
        budget=budget,
        scored_indices=sorted(scored.keys()),
        probes=probes,
        cutoff_idx_inclusive=int(cutoff),
        best_idx=int(best_idx),
    )
    return scored, meta

