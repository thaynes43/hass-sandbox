"""Narrative prompt builder: builds run narrative instructions."""

from __future__ import annotations

from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..profiles import DetectionProfile

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


class NarrativePromptBuilder:
    """Builds narrative instructions for run-level summary (text-only LLM)."""

    def build(
        self,
        custom_instructions: Optional[str] = None,
        max_chars: int = 220,
        profile: Optional[DetectionProfile] = None,
    ) -> str:
        """Build narrative instructions. Uses default template when custom is empty.

        When profile includes non-default categories, adds them to the JSON schema description.
        """
        inst = (custom_instructions or "").strip() or DEFAULT_NARRATIVE_INSTRUCTIONS
        result = inst.format(max_chars=max(60, int(max_chars)))

        if profile is not None:
            extra_cats = [
                c for c in profile.categories
                if c.name not in ("people", "animals")
            ]
            if extra_cats:
                extra_signals: list[str] = []
                for cat in extra_cats:
                    extra_signals.extend(cat.count_signals)
                if extra_signals:
                    result += f"\n\nEach observation item also includes: {', '.join(extra_signals)}"

        return result
