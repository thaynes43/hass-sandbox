"""Prompt and schema abstractions for DetectionSummary.

Prompt builders own task policy, scoring guidance, and style composition.
Providers consume the final prompt only.
"""

from .schema_specs import ScoreFieldSpec, ScoreSchemaSpec, default_score_schema, schema_from_profile
from .score_prompt_builder import ScorePromptBuilder
from .score_normalizer import normalize_score_data
from .image_prompt_builder import ImagePromptBuilder, ImagePromptResult
from .narrative_prompt_builder import NarrativePromptBuilder
from .style_variants import ENVIRONMENT_VARIANTS, STYLE_PROFILES

__all__ = [
    "ScoreFieldSpec",
    "ScoreSchemaSpec",
    "default_score_schema",
    "schema_from_profile",
    "ScorePromptBuilder",
    "normalize_score_data",
    "ImagePromptBuilder",
    "ImagePromptResult",
    "NarrativePromptBuilder",
    "STYLE_PROFILES",
    "ENVIRONMENT_VARIANTS",
]
