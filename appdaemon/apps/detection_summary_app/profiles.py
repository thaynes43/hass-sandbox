"""Detection profiles: configurable subject categories + signal extraction.

A detection profile defines what signals to extract from images, which signals
gate bundle publishing, and how multi-frame signals are aggregated.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .prompting.schema_specs import DEFAULT_SCORE_FIELDS, ScoreFieldSpec


@dataclass(frozen=True)
class SubjectCategory:
    """A category of detectable subject (people, packages, vehicles, animals)."""

    name: str
    """Category identifier, e.g. 'people', 'packages', 'vehicles', 'animals'."""

    display_name: str = ""
    """Human-friendly name, e.g. 'People', 'Packages'."""

    required_for_publish: bool = False
    """If True, at least one must be present to publish."""

    count_signals: tuple[str, ...] = ()
    """Signal keys that count this category, e.g. ('male_count', 'female_count')."""

    min_count_for_publish: int = 1
    """Threshold for publishing (sum of count_signals must be >= this)."""

    image_constraint_signals: tuple[str, ...] = ()
    """Which count signals constrain max subjects in image generation."""


@dataclass(frozen=True)
class DetectionProfile:
    """Complete detection configuration for an app instance."""

    name: str
    """Profile identifier, e.g. 'default', 'packages', 'vehicles'."""

    description: str = ""
    """Human-friendly description."""

    categories: tuple[SubjectCategory, ...] = ()
    """Subject categories to detect."""

    score_fields: tuple[ScoreFieldSpec, ...] = DEFAULT_SCORE_FIELDS
    """Schema fields for LLM scoring output."""

    consensus_strategy: str = "mode"
    """Multi-frame signal aggregation: 'mode', 'max', or 'median'."""


# ---------------------------------------------------------------------------
# Built-in profiles
# ---------------------------------------------------------------------------

PROFILE_DEFAULT = DetectionProfile(
    name="default",
    description="People and animals (current behavior)",
    categories=(
        SubjectCategory(
            name="people",
            display_name="People",
            required_for_publish=True,
            count_signals=("male_count", "female_count"),
            min_count_for_publish=1,
            image_constraint_signals=("male_count", "female_count"),
        ),
        SubjectCategory(
            name="animals",
            display_name="Animals",
            required_for_publish=True,
            count_signals=("animal_count",),
            min_count_for_publish=1,
            image_constraint_signals=("animal_count",),
        ),
    ),
    score_fields=DEFAULT_SCORE_FIELDS,
    consensus_strategy="mode",
)

PROFILE_PACKAGES = DetectionProfile(
    name="packages",
    description="People, animals, and package detection",
    categories=(
        SubjectCategory(
            name="people",
            display_name="People",
            required_for_publish=True,
            count_signals=("male_count", "female_count"),
            min_count_for_publish=1,
            image_constraint_signals=("male_count", "female_count"),
        ),
        SubjectCategory(
            name="animals",
            display_name="Animals",
            required_for_publish=True,
            count_signals=("animal_count",),
            min_count_for_publish=1,
            image_constraint_signals=("animal_count",),
        ),
        SubjectCategory(
            name="packages",
            display_name="Packages",
            required_for_publish=True,
            count_signals=("package_count",),
            min_count_for_publish=1,
            image_constraint_signals=("package_count",),
        ),
    ),
    score_fields=DEFAULT_SCORE_FIELDS + (
        ScoreFieldSpec(
            key="package_count",
            type_hint="int",
            default=0,
            participates_in_ranking=True,
            participates_in_gating=True,
            prompt_guidance="integer count of packages/parcels/boxes visible (0 if none)",
        ),
    ),
    consensus_strategy="mode",
)

PROFILE_VEHICLES = DetectionProfile(
    name="vehicles",
    description="People, animals, and vehicle detection",
    categories=(
        SubjectCategory(
            name="people",
            display_name="People",
            required_for_publish=True,
            count_signals=("male_count", "female_count"),
            min_count_for_publish=1,
            image_constraint_signals=("male_count", "female_count"),
        ),
        SubjectCategory(
            name="animals",
            display_name="Animals",
            required_for_publish=True,
            count_signals=("animal_count",),
            min_count_for_publish=1,
            image_constraint_signals=("animal_count",),
        ),
        SubjectCategory(
            name="vehicles",
            display_name="Vehicles",
            required_for_publish=True,
            count_signals=("vehicle_count",),
            min_count_for_publish=1,
            image_constraint_signals=("vehicle_count",),
        ),
    ),
    score_fields=DEFAULT_SCORE_FIELDS + (
        ScoreFieldSpec(
            key="vehicle_count",
            type_hint="int",
            default=0,
            participates_in_ranking=True,
            participates_in_gating=True,
            prompt_guidance="integer count of vehicles visible (0 if none)",
        ),
        ScoreFieldSpec(
            key="vehicle_type",
            type_hint="str",
            default="",
            participates_in_ranking=False,
            participates_in_gating=False,
            prompt_guidance="vehicle type if visible: car, truck, van, delivery, motorcycle, bicycle, none",
        ),
    ),
    consensus_strategy="mode",
)

PROFILE_ANIMALS = DetectionProfile(
    name="animals",
    description="Animals-only detection; people are context only",
    categories=(
        SubjectCategory(
            name="animals",
            display_name="Animals",
            required_for_publish=True,
            count_signals=("animal_count",),
            min_count_for_publish=1,
            image_constraint_signals=("animal_count",),
        ),
        SubjectCategory(
            name="people",
            display_name="People",
            required_for_publish=False,
            count_signals=("male_count", "female_count"),
            min_count_for_publish=1,
            image_constraint_signals=("male_count", "female_count"),
        ),
    ),
    score_fields=DEFAULT_SCORE_FIELDS,
    consensus_strategy="mode",
)

BUILTIN_PROFILES: dict[str, DetectionProfile] = {
    "default": PROFILE_DEFAULT,
    "packages": PROFILE_PACKAGES,
    "vehicles": PROFILE_VEHICLES,
    "animals": PROFILE_ANIMALS,
}


def load_profile_by_name(name: str) -> DetectionProfile:
    """Load a built-in profile by name. Raises ValueError for unknown names."""
    profile = BUILTIN_PROFILES.get(name)
    if profile is None:
        raise ValueError(
            f"Unknown detection profile '{name}'. "
            f"Available: {', '.join(sorted(BUILTIN_PROFILES.keys()))}"
        )
    return profile


def load_profile_from_dict(data: dict[str, Any]) -> DetectionProfile:
    """Construct a DetectionProfile from an inline YAML dict."""
    categories: list[SubjectCategory] = []
    for cat_data in data.get("categories", []):
        categories.append(SubjectCategory(
            name=str(cat_data.get("name", "")),
            display_name=str(cat_data.get("display_name", cat_data.get("name", ""))),
            required_for_publish=bool(cat_data.get("required_for_publish", False)),
            count_signals=tuple(cat_data.get("count_signals", ())),
            min_count_for_publish=int(cat_data.get("min_count_for_publish", 1)),
            image_constraint_signals=tuple(cat_data.get("image_constraint_signals", cat_data.get("count_signals", ()))),
        ))

    extra_fields: list[ScoreFieldSpec] = []
    for field_data in data.get("extra_score_fields", []):
        extra_fields.append(ScoreFieldSpec(
            key=str(field_data["key"]),
            type_hint=str(field_data.get("type_hint", "float")),
            default=field_data.get("default", 0),
            participates_in_ranking=bool(field_data.get("participates_in_ranking", True)),
            participates_in_gating=bool(field_data.get("participates_in_gating", False)),
            prompt_guidance=field_data.get("prompt_guidance"),
        ))

    score_fields = DEFAULT_SCORE_FIELDS + tuple(extra_fields)

    return DetectionProfile(
        name=str(data.get("name", "custom")),
        description=str(data.get("description", "")),
        categories=tuple(categories),
        score_fields=score_fields,
        consensus_strategy=str(data.get("consensus_strategy", "mode")),
    )


def load_default_profile() -> DetectionProfile:
    """Return the default profile."""
    return BUILTIN_PROFILES["default"]
