from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

try:
    from providers.ai_providers.simple_text_provider import SimpleTextProvider
    from providers.ai_providers.multimodal_text_provider import ExternalDataGenError
except Exception:  # pragma: no cover
    import sys
    from pathlib import Path

    # AppDaemon often only adds `appdaemon/apps` to sys.path. Our shared libraries
    # live at `appdaemon/providers`, so add the AppDaemon root directory.
    sys.path.append(str(Path(__file__).resolve().parents[2]))
    from providers.ai_providers.simple_text_provider import SimpleTextProvider  # type: ignore
    from providers.ai_providers.multimodal_text_provider import ExternalDataGenError  # type: ignore

from .prompting.narrative_prompt_builder import NarrativePromptBuilder

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NarrativeConfig:
    enabled: bool = True
    max_chars: int = 220
    # Keep small for push notifications.
    expected_keys: tuple[str, ...] = ("run_summary", "people_min", "people_max", "confidence", "key_events")


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def synthesize_run_narrative(
    *,
    provider: SimpleTextProvider,
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
    inst = NarrativePromptBuilder().build(instructions, max_chars)

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
        out = provider.generate_from_text(
            input_text=input_text,
            instructions=inst,
            expected_keys=list(cfg.expected_keys),
        )
    except ExternalDataGenError as e:
        logger.warning(
            "narrative LLM call failed (ExternalDataGenError): bundle_key=%s run_id=%s err=%s",
            bundle_key, run_id, e,
        )
        raise
    except Exception as e:
        logger.warning(
            "narrative LLM call failed (unexpected): bundle_key=%s run_id=%s err=%r",
            bundle_key, run_id, e,
        )
        raise

    if not isinstance(out, dict):
        logger.warning(
            "narrative LLM returned non-dict: bundle_key=%s run_id=%s type=%s preview=%s",
            bundle_key, run_id, type(out).__name__, str(out)[:200],
        )
        raise ExternalDataGenError(f"narrative LLM returned {type(out).__name__}, expected dict")

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

