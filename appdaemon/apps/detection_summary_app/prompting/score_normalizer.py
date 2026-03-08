"""Schema-driven score normalization.

Replaces hardcoded _safe_float / _safe_int logic in manager with
schema-driven extraction from raw LLM output.
"""

from __future__ import annotations

from typing import Any

from .schema_specs import ScoreFieldSpec, ScoreSchemaSpec, default_score_schema

# Import at runtime to avoid circular deps (prompting -> selection)
# selection does not import from prompting.


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _extract_value(data: dict[str, Any], spec: ScoreFieldSpec) -> Any:
    """Extract and coerce a value from raw data per field spec."""
    keys_to_try = (spec.key,) + spec.alt_keys
    raw = None
    for k in keys_to_try:
        if k in data:
            raw = data[k]
            break
    if raw is None:
        return spec.default
    if spec.type_hint == "int":
        return _safe_int(raw, default=int(spec.default) if isinstance(spec.default, (int, float)) else 0)
    if spec.type_hint == "float":
        return _safe_float(raw, default=float(spec.default) if isinstance(spec.default, (int, float)) else 0.0)
    if spec.type_hint == "str":
        return str(raw or "").strip()
    return spec.default


def normalize_score_data(
    data: dict[str, Any],
    schema: ScoreSchemaSpec | None = None,
) -> "ScoreResult":
    """Normalize raw LLM output to ScoreResult using schema.

    Uses default_score_schema when schema is None.
    """
    from ..selection import ScoreResult

    if schema is None:
        schema = default_score_schema()

    by_key: dict[str, Any] = {}
    for spec in schema.fields:
        by_key[spec.key] = _extract_value(data, spec)

    # frame_score defaults to person_score when missing
    person = by_key.get("person_score", 0.0)
    frame = by_key.get("frame_score", person)
    if frame == 0 and person != 0:
        frame = person

    # Standard ScoreResult fields
    _standard_keys = {
        "male_count", "female_count", "animal_count",
        "person_score", "face_score", "frame_score",
        "pose", "summary",
    }

    # Extra signals: any schema fields not in the standard ScoreResult attrs
    extra_signals: dict[str, Any] = {}
    for key, val in by_key.items():
        if key not in _standard_keys:
            extra_signals[key] = val

    return ScoreResult(
        male_count=int(by_key.get("male_count", 0)),
        female_count=int(by_key.get("female_count", 0)),
        animal_count=int(by_key.get("animal_count", 0)),
        person_score=float(by_key.get("person_score", 0.0)),
        face_score=float(by_key.get("face_score", 0.0)),
        frame_score=float(frame),
        pose=str(by_key.get("pose", "") or "").strip().lower(),
        summary=str(by_key.get("summary", "") or "").strip(),
        structured=dict(data) if isinstance(data, dict) else {},
        extra_signals=extra_signals,
    )
