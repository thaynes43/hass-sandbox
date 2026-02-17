from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Optional

try:
    from ai_providers.types import DataProvider, ExternalDataGenError
except Exception:  # pragma: no cover
    import sys
    from pathlib import Path

    # AppDaemon often only adds `appdaemon/apps` to sys.path. Our shared libraries
    # live at `appdaemon/ai_providers`, so add the AppDaemon root directory.
    sys.path.append(str(Path(__file__).resolve().parents[2]))
    from ai_providers.types import DataProvider, ExternalDataGenError  # type: ignore


@dataclass(frozen=True)
class NarrativeConfig:
    enabled: bool = True
    max_chars: int = 220
    # Keep small for push notifications.
    expected_keys: tuple[str, ...] = ("run_summary", "people_min", "people_max", "confidence", "key_events")


DEFAULT_NARRATIVE_INSTRUCTIONS = """
You are writing a short push-notification narrative summarizing a short security-camera MOTION RUN.

You are given a JSON list of frame observations. Each item includes:
- t_s: seconds since the run started (float)
- idx: frame index (int)
- person_count: detected people count (int)
- summary: brief description of what is happening in that frame
- pose: coarse pose label
- person_score/face_score/frame_score: numeric scoring signals

Your job:
- Produce ONE coherent narrative of what happened across the entire run (someone arrived/left / what they did).
- Use time deltas between frames to estimate durations (e.g. "waited ~20s").
- Handle contradictions by being conservative:
  - If person counts differ, describe it as "someone" unless you're confident there were multiple.
  - If summaries conflict, pick the most consistent storyline, and avoid over-specific claims.
- Keep it human and action-focused (what they did), avoid environment descriptions.

Output JSON ONLY with:
- run_summary: string, MUST be <= {max_chars} characters (hard limit), past tense, 1 sentence preferred, no newlines
- people_min: int (minimum people you believe appeared)
- people_max: int (maximum people you believe appeared)
- confidence: int 0-10 (your confidence the narrative is broadly accurate)
- key_events: array of short strings (optional), ordered roughly by time
""".strip()


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def synthesize_run_narrative(
    *,
    provider: DataProvider,
    run_id: str,
    bundle_key: str,
    frame_facts: list[dict[str, Any]],
    instructions: Optional[str],
    cfg: NarrativeConfig,
) -> Optional[dict[str, Any]]:
    if not cfg.enabled:
        return None
    if not frame_facts:
        return None

    t0 = time.time()
    max_chars = max(60, int(cfg.max_chars))
    inst = (instructions or "").strip() or DEFAULT_NARRATIVE_INSTRUCTIONS
    inst = inst.format(max_chars=max_chars)

    # Keep input small + stable; this is what the LLM should reason over.
    compact_facts: list[dict[str, Any]] = []
    for f in frame_facts:
        compact_facts.append(
            {
                "t_s": float(f.get("t_s") or 0.0),
                "idx": _safe_int(f.get("idx"), 0),
                "person_count": _safe_int(f.get("person_count"), 0),
                "summary": str(f.get("summary") or "").strip(),
                "pose": str(f.get("pose") or "").strip(),
                "person_score": float(f.get("person_score") or 0.0),
                "face_score": float(f.get("face_score") or 0.0),
                "frame_score": float(f.get("frame_score") or 0.0),
            }
        )

    # Chronological
    compact_facts.sort(key=lambda x: float(x.get("t_s") or 0.0))
    input_text = json.dumps(compact_facts, ensure_ascii=False, separators=(",", ":"))

    try:
        out = provider.generate_data_from_text(
            input_text=input_text,
            instructions=inst,
            expected_keys=list(cfg.expected_keys),
        )
    except ExternalDataGenError:
        return None
    except Exception:
        return None

    if not isinstance(out, dict):
        return None

    out.setdefault("run_summary", "")
    out.setdefault("people_min", 0)
    out.setdefault("people_max", 0)
    out.setdefault("confidence", 0)

    # Defensive trimming (notifications/helpers have limits downstream).
    summary = str(out.get("run_summary") or "").strip()
    original_len = len(summary)
    was_truncated = False
    if len(summary) > max_chars:
        was_truncated = True
        summary = summary[: max(0, max_chars - 3)].rstrip() + "..."
    out["run_summary"] = summary

    # Add our own metadata wrapper (provider already adds _meta; keep both).
    out["_narrative_meta"] = {
        "bundle_key": str(bundle_key),
        "run_id": str(run_id),
        "frame_facts_count": int(len(compact_facts)),
        "elapsed_s": round(time.time() - t0, 3),
        "max_chars": int(max_chars),
        "original_len": int(original_len),
        "was_truncated": bool(was_truncated),
    }
    return out

