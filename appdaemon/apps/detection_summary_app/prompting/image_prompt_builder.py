"""Image prompt builder: composes app instructions, population bounds, guardrails, style."""

from __future__ import annotations

from typing import Any, Optional

from ..population import augment_image_instructions, compute_population_bounds
from .style_variants import get_environment_variant, get_style_profile


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
        - Style profile + environment variant (placeholders)
        """
        base_prompt = augment_image_instructions(str(base_instructions or ""), population_bounds)
        prompt_lines: list[str] = [base_prompt, ""]
        prompt_lines.extend(
            [
                "Reference frames:",
                f"- You are provided {input_paths_count} image(s) captured close in time during ONE motion detection event.",
                "- These frames are only a subset of the event; people/animals may enter/leave between frames.",
                "",
                "Critical constraints:",
                "- ONLY include people and animals that are clearly present in at least ONE of the provided reference frames.",
                "- Do NOT invent/add animals or extra people that are not visible in any provided frame (avoid 'phantom' animals).",
                "- If you are uncertain whether a subject exists, OMIT it rather than hallucinating it.",
                "- Do NOT depict the same individual multiple times (no duplicates). If a person appears in multiple frames, show them only once.",
                "- Do NOT exceed the max male/female/animal counts given above, even if the narrative suggests more.",
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

        # Style profile (placeholder)
        style = get_style_profile(style_profile_id)
        if style and style.prompt_suffix:
            prompt = f"{prompt}\n\n{style.prompt_suffix}".strip()

        # Environment variant (placeholder)
        env = get_environment_variant(environment_variant_id)
        if env and env.prompt_suffix:
            prompt = f"{prompt}\n\n{env.prompt_suffix}".strip()

        return prompt
