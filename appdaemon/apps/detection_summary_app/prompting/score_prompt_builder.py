"""Score prompt builder: builds scoring instructions from app intent + schema."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .schema_specs import ScoreSchemaSpec, default_score_schema

if TYPE_CHECKING:
    from ..profiles import DetectionProfile


class ScorePromptBuilder:
    """Builds scoring instructions for frame analysis (multimodal LLM)."""

    def __init__(
        self,
        schema: ScoreSchemaSpec | None = None,
        profile: DetectionProfile | None = None,
    ):
        self.profile = profile
        if profile is not None:
            from .schema_specs import schema_from_profile
            self.schema = schema or schema_from_profile(profile)
        else:
            self.schema = schema or default_score_schema()

    def build(self, app_instructions: str) -> str:
        """Build full scoring prompt: app intent + required fields + scoring guidance."""
        base = str(app_instructions or "").strip()
        parts = [base]
        parts.append("")
        parts.append(self.schema.required_fields_block())

        # Profile-specific category guidance
        if self.profile is not None:
            extra_cats = [
                c for c in self.profile.categories
                if c.name not in ("people", "animals")
            ]
            if extra_cats:
                parts.append("")
                parts.append("Additional detection categories:")
                for cat in extra_cats:
                    signals = ", ".join(cat.count_signals) if cat.count_signals else cat.name
                    parts.append(f"- Also detect {cat.display_name or cat.name} (signals: {signals})")

        parts.append("")
        parts.append("Scoring guidance:")
        parts.append(self.schema.scoring_guidance_block())
        return "\n".join(parts).strip()
