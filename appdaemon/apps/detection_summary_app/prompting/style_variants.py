"""Style variety placeholders for image generation.

Two-layer model:
- image_style_profile: rendering style (cartoon, hyperrealistic, etc.)
- environment_variant: scene/theme mutation (underwater, steampunk, seasonal, etc.)

Placeholders only for now; interfaces must be real and testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class StyleProfile:
    """Rendering style overlay."""

    id: str
    prompt_suffix: str
    description: str = ""


@dataclass(frozen=True)
class EnvironmentVariant:
    """Scene/theme mutation."""

    id: str
    prompt_suffix: str
    description: str = ""


# Placeholder profiles. Real values will come from bundle config.
STYLE_PROFILES: dict[str, StyleProfile] = {
    "cartoon": StyleProfile(
        id="cartoon",
        prompt_suffix="Make it like a cartoon.",
        description="Cartoon-style rendering",
    ),
    "hyperrealistic": StyleProfile(
        id="hyperrealistic",
        prompt_suffix="Render in a hyperrealistic style.",
        description="Photorealistic rendering",
    ),
    "default": StyleProfile(
        id="default",
        prompt_suffix="",
        description="No style overlay",
    ),
}

ENVIRONMENT_VARIANTS: dict[str, EnvironmentVariant] = {
    "default": EnvironmentVariant(
        id="default",
        prompt_suffix="",
        description="No environment mutation",
    ),
    "underwater": EnvironmentVariant(
        id="underwater",
        prompt_suffix="Reimagine the scene as if underwater (preserve all people and animals).",
        description="Underwater theme",
    ),
}


def get_style_profile(profile_id: Optional[str]) -> Optional[StyleProfile]:
    """Resolve style profile by ID. Returns None for unknown/empty."""
    if not profile_id or not str(profile_id).strip():
        return None
    return STYLE_PROFILES.get(str(profile_id).strip().lower())


def get_environment_variant(variant_id: Optional[str]) -> Optional[EnvironmentVariant]:
    """Resolve environment variant by ID. Returns None for unknown/empty."""
    if not variant_id or not str(variant_id).strip():
        return None
    return ENVIRONMENT_VARIANTS.get(str(variant_id).strip().lower())
