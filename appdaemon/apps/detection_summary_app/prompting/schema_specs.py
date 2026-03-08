"""Schema specs for DetectionSummary structured output.

Replaces hardcoded expected_keys and inline normalization with spec objects
that own field names, type/default behavior, and ranking/gating participation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..profiles import DetectionProfile


@dataclass(frozen=True)
class ScoreFieldSpec:
    """Spec for a single score schema field."""

    key: str
    """JSON key expected from the LLM."""

    type_hint: str = "float"
    """Type hint for the LLM: 'int', 'float', 'str'."""

    default: Any = 0
    """Default value when missing or invalid."""

    alt_keys: tuple[str, ...] = ()
    """Alternative keys to try (e.g. 'score' for person_score)."""

    participates_in_ranking: bool = True
    """Whether this field is used for frame selection/ranking."""

    participates_in_gating: bool = False
    """Whether this field affects publish decisions (e.g. best_min_person_score)."""

    prompt_guidance: Optional[str] = None
    """Optional human guidance text for the provider prompt."""


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


# Default schema: people + animals, full scoring fields.
# A backyard/animal-only app could use a reduced schema.
DEFAULT_SCORE_FIELDS: tuple[ScoreFieldSpec, ...] = (
    ScoreFieldSpec(
        key="male_count",
        type_hint="int",
        default=0,
        participates_in_ranking=False,
        participates_in_gating=False,
        prompt_guidance="integer count of men/boys visible in the frame (0 if none)",
    ),
    ScoreFieldSpec(
        key="female_count",
        type_hint="int",
        default=0,
        participates_in_ranking=False,
        participates_in_gating=False,
        prompt_guidance="integer count of women/girls visible in the frame (0 if none)",
    ),
    ScoreFieldSpec(
        key="animal_count",
        type_hint="int",
        default=0,
        participates_in_ranking=True,
        participates_in_gating=True,
        prompt_guidance="integer count of animals/pets visible in the frame (0 if none)",
    ),
    ScoreFieldSpec(
        key="person_score",
        type_hint="float",
        default=0.0,
        alt_keys=("score",),
        participates_in_ranking=True,
        participates_in_gating=True,
        prompt_guidance="numeric score for person presence/visibility",
    ),
    ScoreFieldSpec(
        key="face_score",
        type_hint="float",
        default=0.0,
        participates_in_ranking=True,
        participates_in_gating=False,
        prompt_guidance="numeric score for face visibility (people OR animals)",
    ),
    ScoreFieldSpec(
        key="frame_score",
        type_hint="float",
        default=0.0,
        participates_in_ranking=True,
        participates_in_gating=False,
        prompt_guidance="overall frame quality score; increase when animals are clearly visible",
    ),
    ScoreFieldSpec(
        key="pose",
        type_hint="str",
        default="",
        participates_in_ranking=True,
        participates_in_gating=False,
        prompt_guidance="main subject pose (person or animal when possible)",
    ),
    ScoreFieldSpec(
        key="summary",
        type_hint="str",
        default="",
        participates_in_ranking=True,
        participates_in_gating=False,
        prompt_guidance="1 sentence <= 140 chars; mention animals when relevant",
    ),
)


@dataclass
class ScoreSchemaSpec:
    """Schema spec for frame scoring structured output."""

    fields: tuple[ScoreFieldSpec, ...] = field(default_factory=lambda: DEFAULT_SCORE_FIELDS)

    def expected_keys(self) -> list[str]:
        """Keys the LLM must return (for provider expected_keys)."""
        return [f.key for f in self.fields]

    def scoring_guidance_block(self) -> str:
        """Human guidance for scoring (animals, face_score, pose, summary)."""
        return "\n".join(
            [
                "- If animals are present and clearly visible, increase frame_score appropriately.",
                "- face_score can be influenced by clearly visible faces of people OR animals.",
                "- pose should describe the main subject (person or animal) when possible.",
                "- summary should mention animals when they are relevant, but remain 1 sentence <= 140 characters.",
            ]
        )

    def required_fields_block(self) -> str:
        """Block describing required count/score fields for the prompt."""
        lines = ["Additional required fields:"]
        for f in self.fields:
            if f.prompt_guidance:
                lines.append(f"- {f.key}: {f.prompt_guidance}")
        return "\n".join(lines)


def default_score_schema() -> ScoreSchemaSpec:
    """Default schema with people + animals + full scoring fields."""
    return ScoreSchemaSpec(fields=DEFAULT_SCORE_FIELDS)


def schema_from_profile(profile: "DetectionProfile") -> ScoreSchemaSpec:
    """Generate a ScoreSchemaSpec from a DetectionProfile's score_fields."""
    return ScoreSchemaSpec(fields=profile.score_fields)
