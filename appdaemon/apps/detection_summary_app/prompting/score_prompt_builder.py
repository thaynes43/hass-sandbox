"""Score prompt builder: builds scoring instructions from app intent + schema."""

from __future__ import annotations

from .schema_specs import ScoreSchemaSpec, default_score_schema


class ScorePromptBuilder:
    """Builds scoring instructions for frame analysis (multimodal LLM)."""

    def __init__(self, schema: ScoreSchemaSpec | None = None):
        self.schema = schema or default_score_schema()

    def build(self, app_instructions: str) -> str:
        """Build full scoring prompt: app intent + required fields + scoring guidance."""
        base = str(app_instructions or "").strip()
        parts = [base]
        parts.append("")
        parts.append(self.schema.required_fields_block())
        parts.append("")
        parts.append("Scoring guidance:")
        parts.append(self.schema.scoring_guidance_block())
        return "\n".join(parts).strip()
