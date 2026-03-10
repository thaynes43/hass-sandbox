"""Image prompt builder: composes app instructions, population bounds, guardrails, style."""

from __future__ import annotations

from typing import Any, Optional, TYPE_CHECKING

from ..population import augment_image_instructions, augment_image_instructions_with_consensus
from .style_variants import (
    get_environment_variant,
    get_style_profile,
    random_environment_variant,
    random_style_profile,
)

if TYPE_CHECKING:
    from ..profiles import DetectionProfile


class ImagePromptBuilder:
    """Builds image-generation prompt from app instructions + reference frames + guardrails."""

    def build(
        self,
        base_instructions: str,
        population_bounds: dict[str, Any],
        narrative_text: str = "",
        frame_notes: list[str] | None = None,
        input_paths_count: int = 1,
        bundle_augmentation: Optional[str] = None,
        style_profile_id: Optional[str] = None,
        environment_variant_id: Optional[str] = None,
        consensus_bounds: Optional[dict[str, Any]] = None,
        profile: Optional[DetectionProfile] = None,
    ) -> str:
        """Build full image prompt.

        Composes:
        - App image instructions + population bounds (from population.augment_image_instructions)
        - Reference frame context
        - Critical constraints (hallucination guardrails)
        - Content safety
        - Scene composition guidance
        - Narrative context
        - Frame notes
        - Bundle augmentation (from provider config)
        - Style profile + environment variant (randomly selected for variety)
        """
        if consensus_bounds and profile:
            base_prompt = augment_image_instructions_with_consensus(
                str(base_instructions or ""), consensus_bounds, profile
            )
        else:
            base_prompt = augment_image_instructions(str(base_instructions or ""), population_bounds)
        # Build subject label for constraints based on profile categories
        if profile and profile.categories:
            subject_names = [c.display_name or c.name for c in profile.categories]
            subjects_label = ", ".join(subject_names).lower()
        else:
            subjects_label = "people and animals"

        prompt_lines: list[str] = [base_prompt, ""]
        prompt_lines.extend(
            [
                "Reference frames:",
                f"- You are provided {input_paths_count} image(s) captured close in time during ONE motion detection event.",
                f"- These frames are only a subset of the event; {subjects_label} may enter/leave between frames.",
                "",
                "Critical constraints:",
                f"- ONLY include {subjects_label} that are clearly present in at least ONE of the provided reference frames.",
                f"- Do NOT invent/add extra {subjects_label} that are not visible in any provided frame (avoid 'phantom' subjects).",
                "- If you are uncertain whether a subject exists, OMIT it rather than hallucinating it.",
                "- Do NOT depict the same individual multiple times (no duplicates). If a person appears in multiple frames, show them only once.",
                "- Do NOT exceed the max counts given above, even if the narrative suggests more.",
                "",
                "Content safety:",
                "- Do NOT reproduce any recognizable branded, trademarked, or copyrighted characters, logos, or products visible in the reference frames.",
                "- Replace any such items with generic alternatives (plain toys, abstract shapes, unlabeled objects).",
                "- Omit text overlays like timestamps or watermarks from the reference frames.",
                "",
                "Scene composition guidance:",
                "- Generate ONE coherent illustration that captures the essence of what happened across the provided frames.",
                "- Exact positioning/poses do not need to match a single frame; it can be a composite of the event.",
                "- Use the narrative context below for mood/intent, but do not add subjects that are not visible in the frames.",
            ]
        )
        if narrative_text:
            prompt_lines.extend(["", "Narrative context:", narrative_text])
        if frame_notes:
            prompt_lines.extend(["", "Frame notes (for the provided references):", *frame_notes])

        prompt = "\n".join([ln.rstrip() for ln in prompt_lines]).strip()

        # Bundle augmentation (from provider config)
        if bundle_augmentation:
            aug = str(bundle_augmentation).strip()
            if aug:
                prompt = f"{prompt}\n\n{aug}".strip()

        # Style profile: use specific ID if provided, otherwise randomly select
        if style_profile_id:
            style = get_style_profile(style_profile_id)
        else:
            style = random_style_profile()
        if style and style.prompt_suffix:
            prompt = (
                f"{prompt}\n\n"
                f"Rendering style directive (apply to visual appearance only — "
                f"do NOT replace or omit the subjects identified above):\n"
                f"{style.prompt_suffix}"
            ).strip()

        # Environment variant: use specific ID if provided, otherwise randomly select
        if environment_variant_id:
            env = get_environment_variant(environment_variant_id)
        else:
            env = random_environment_variant()
        if env and env.prompt_suffix:
            prompt = (
                f"{prompt}\n\n"
                f"Environment/setting directive (modify background and setting only — "
                f"ALL subjects from the reference frames MUST remain clearly present in the output):\n"
                f"{env.prompt_suffix}"
            ).strip()

        return prompt
