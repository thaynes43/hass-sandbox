"""Image prompt builder: composes app instructions, population bounds, guardrails, style."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence, TYPE_CHECKING

from ..population import augment_image_instructions, augment_image_instructions_with_consensus
from .style_variants import (
    get_environment_variant,
    get_style_profile,
    random_environment_variant,
    random_style_profile,
)

if TYPE_CHECKING:
    from ..profiles import DetectionProfile


@dataclass(frozen=True)
class ImagePromptResult:
    """Prompt text plus metadata about which style/env were applied."""
    prompt: str
    style_profile_id: Optional[str] = None
    style_profile_description: Optional[str] = None
    environment_variant_id: Optional[str] = None
    environment_variant_description: Optional[str] = None


@dataclass(frozen=True)
class FrameNote:
    """What one reference frame contained, for the prompt's frame-notes block.

    The caller passes these in the order the frames are sent to the provider
    and passes only the frames it actually sends; the builder owns how they are
    labelled, because how a reference is named to the model is prompt policy.
    """

    summary: str = ""
    # Seconds after the capture started, when that is known.
    time_offset_s: Optional[float] = None
    male_count: int = 0
    female_count: int = 0
    animal_count: int = 0


def _render_frame_note(note: FrameNote, position: int) -> str:
    """Render one note, labelled by the position the image is sent in.

    Positions, not filenames: the provider renames every upload before it
    leaves (ComfyUI uploads as ``<zone>-slot<N>``), so a filename in the prompt
    names nothing the model can see. "Image 1" is what the model is looking at.
    """
    label = "Image 1 (primary frame)" if position == 1 else f"Image {position}"
    offset = note.time_offset_s
    time_part = f" t={float(offset):.1f}s" if isinstance(offset, (int, float)) else ""
    summary = str(note.summary or "").strip() or "(no summary)"
    counts = (
        f"(m={int(note.male_count)}, "
        f"f={int(note.female_count)}, "
        f"animals={int(note.animal_count)})"
    )
    return f"- {label}{time_part}: {summary} {counts}"


class ImagePromptBuilder:
    """Builds image-generation prompt from app instructions + reference frames + guardrails."""

    def build(
        self,
        base_instructions: str,
        population_bounds: dict[str, Any],
        narrative_text: str = "",
        frame_notes: Sequence[FrameNote] | None = None,
        input_paths_count: int = 1,
        bundle_augmentation: Optional[str] = None,
        style_profile_id: Optional[str] = None,
        environment_variant_id: Optional[str] = None,
        consensus_bounds: Optional[dict[str, Any]] = None,
        profile: Optional[DetectionProfile] = None,
    ) -> ImagePromptResult:
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

        ``frame_notes`` describes the images the provider will actually
        receive, in the order it receives them, and ``input_paths_count`` is how
        many that is — the caller trims both to the provider's
        ``max_input_images`` before calling. Notes are labelled here by that
        position ("Image 1 (primary frame)", "Image 2", ...), which is the only
        handle the model has on them.
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
            rendered_notes = [
                _render_frame_note(note, position)
                for position, note in enumerate(frame_notes, start=1)
            ]
            prompt_lines.extend(
                ["", "Frame notes (for the provided references):", *rendered_notes]
            )

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

        return ImagePromptResult(
            prompt=prompt,
            style_profile_id=getattr(style, "id", None) if style else None,
            style_profile_description=getattr(style, "description", None) if style else None,
            environment_variant_id=getattr(env, "id", None) if env else None,
            environment_variant_description=getattr(env, "description", None) if env else None,
        )
