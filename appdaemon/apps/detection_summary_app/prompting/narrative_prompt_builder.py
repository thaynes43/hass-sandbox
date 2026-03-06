"""Narrative prompt builder: builds run narrative instructions."""

from __future__ import annotations

from typing import Optional

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

    def build(self, custom_instructions: Optional[str] = None, max_chars: int = 220) -> str:
        """Build narrative instructions. Uses default template when custom is empty."""
        inst = (custom_instructions or "").strip() or DEFAULT_NARRATIVE_INSTRUCTIONS
        return inst.format(max_chars=max(60, int(max_chars)))
