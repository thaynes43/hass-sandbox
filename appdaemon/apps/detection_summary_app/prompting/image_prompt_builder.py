"""Image prompt builder: composes app instructions, subject counts, guardrails, style.

The reference frames are one camera, seconds apart, so they show the SAME
people and animals at different moments. The image models this prompt drives
are editors that are trained, when handed several images, to combine what
each one shows into one picture (Qwen's own multi-image examples put the bear
from image 1 next to the bear from image 2). Asked to combine our frames, they
draw the person once per frame. So every part of this prompt says one thing:
draw the moment in Image 1, and draw each subject once. It deliberately never
describes a subject at more than one moment: not the other frames' own
summaries (each one places the same person somewhere else) and not the run
narrative (a sequence of actions is drawn as several people).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence, TYPE_CHECKING

from .style_variants import (
    get_environment_variant,
    get_style_profile,
    random_environment_variant,
    random_style_profile,
)

if TYPE_CHECKING:
    from ..profiles import DetectionProfile, SubjectCategory


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
    """What one reference frame contained, for the prompt's per-image block.

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
    # Whether this is the run's best-scoring frame. Carried rather than
    # inferred from position: the best frame is normally first, but it is
    # dropped when its file never appeared, and calling a secondary reference
    # the primary one would be the same kind of untrue claim this labelling
    # exists to prevent. No note carries it when the best frame was not sent.
    is_primary: bool = False


def _as_count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _join_words(words: Sequence[str]) -> str:
    """'people', 'people and animals', 'people, animals and packages'."""
    words = [w for w in words if w]
    if len(words) <= 1:
        return "".join(words)
    return f"{', '.join(words[:-1])} and {words[-1]}"


def _category_count_line(cat: SubjectCategory, consensus: Mapping[str, Any]) -> Optional[str]:
    """How many of one category to draw: an exact number when the frames agree.

    Drawn from the category total per frame (see
    ``population.compute_population_consensus``), so a person the scorer read
    as a man in one frame and a woman in another is still one person. A caller
    whose consensus predates the totals falls back to summing the signals.

    No breakdown by signal ("1 man"): the scorer's gender reading varies from
    frame to frame, and a majority reading can contradict Image 1, which is
    the one being drawn. Image 1's own note and the image carry that.
    """
    if not cat.count_signals:
        return None
    signals = list(dict.fromkeys(cat.count_signals))
    name = cat.display_name or cat.name
    likely_raw = consensus.get(f"consensus_{cat.name}_total")
    most_raw = consensus.get(f"max_{cat.name}_total")
    likely = (
        _as_count(likely_raw)
        if likely_raw is not None
        else sum(_as_count(consensus.get(f"consensus_{s}")) for s in signals)
    )
    most = (
        _as_count(most_raw)
        if most_raw is not None
        else sum(_as_count(consensus.get(f"max_{s}")) for s in signals)
    )
    if most <= 0:
        return f"- {name}: none"
    if likely < most:
        # The frames disagree; the images decide, within the ceiling.
        return f"- {name}: at most {most}"
    return f"- {name}: exactly {most}"


def _profile_count_lines(consensus: Mapping[str, Any], profile: DetectionProfile) -> list[str]:
    lines: list[str] = []
    for cat in profile.categories:
        line = _category_count_line(cat, consensus)
        if line:
            lines.append(line)
    return lines


def _bounds_count_lines(bounds: Mapping[str, Any]) -> list[str]:
    """Ceilings from the per-signal maxima alone, for a caller without a profile."""
    people = _as_count(bounds.get("max_male_count")) + _as_count(bounds.get("max_female_count"))
    animals = _as_count(bounds.get("max_animal_count"))
    return [
        f"- People: at most {people}" if people else "- People: none",
        f"- Animals: at most {animals}" if animals else "- Animals: none",
    ]


def _render_frame_note(note: FrameNote, position: int, first: FrameNote) -> str:
    """Render one note, labelled by the position the image is sent in.

    Positions, not filenames: the provider renames every upload before it
    leaves (ComfyUI uploads as ``<zone>-slot<N>``), so a filename in the prompt
    names nothing the model can see. "Image 1" is what the model is looking at.

    Only Image 1 — the moment being drawn — is described in its own words.
    Every later image shows the same subjects somewhere else, and its own
    summary would place them there too ("a man near left", then "a man at the
    door"), which the model draws as a second man. A later image is described
    by what it adds to Image 1, from the counts.
    """
    label = f"Image {position}" + (" (primary frame)" if note.is_primary else "")
    offset = note.time_offset_s
    time_part = f" t={float(offset):.1f}s" if isinstance(offset, (int, float)) else ""
    if position == 1:
        summary = str(note.summary or "").strip() or "(no summary)"
        counts = (
            f"(m={int(note.male_count)}, "
            f"f={int(note.female_count)}, "
            f"animals={int(note.animal_count)})"
        )
        return f"- {label}{time_part}: {summary} {counts}"

    # People as one total, for the same reason the counts use totals: a
    # gender the scorer flipped between frames is not a new person.
    new_people = (_as_count(note.male_count) + _as_count(note.female_count)) - (
        _as_count(first.male_count) + _as_count(first.female_count)
    )
    new_animals = _as_count(note.animal_count) - _as_count(first.animal_count)
    added = []
    if new_people > 0:
        added.append(f"{new_people} {'person' if new_people == 1 else 'people'}")
    if new_animals > 0:
        added.append(f"{new_animals} {'animal' if new_animals == 1 else 'animals'}")
    if not added:
        return f"- {label}{time_part}: the same subjects as Image 1 at another moment; nobody new."
    take = "add only that one" if max(new_people, 0) + max(new_animals, 0) == 1 else "add only those, each once"
    return (
        f"- {label}{time_part}: the same scene at another moment, with {_join_words(added)} "
        f"more than Image 1: {take}; everyone else in it is already in Image 1."
    )


class ImagePromptBuilder:
    """Builds image-generation prompt from app instructions + reference frames + guardrails."""

    def build(
        self,
        base_instructions: str,
        population_bounds: dict[str, Any],
        frame_notes: Sequence[FrameNote] | None = None,
        input_paths_count: int = 1,
        bundle_augmentation: Optional[str] = None,
        style_profile_id: Optional[str] = None,
        environment_variant_id: Optional[str] = None,
        consensus_bounds: Optional[dict[str, Any]] = None,
        profile: Optional[DetectionProfile] = None,
    ) -> ImagePromptResult:
        """Build full image prompt.

        Composes, in order:
        - App image instructions
        - What to draw: the scene of Image 1, one moment
        - Subjects: how many of each profile category, each drawn once
        - What the reference images are (the same subjects at other moments)
        - Rules (one scene, no invented subjects, gender presentation)
        - Content safety
        - What each image shows
        - Bundle augmentation (from provider config)
        - Style profile + environment variant (randomly selected for variety)

        ``frame_notes`` describes the images the provider will actually
        receive, in the order it receives them, and ``input_paths_count`` is how
        many that is — the caller trims both to the provider's
        ``max_input_images`` before calling. Notes are labelled here by that
        position ("Image 1 (primary frame)", "Image 2", ...), which is the only
        handle the model has on them; the "primary frame" qualifier comes from
        the note's own ``is_primary``, not from being first.

        The counts come from ``consensus_bounds`` when a ``profile`` is given,
        else from the per-signal maxima in ``population_bounds``.
        """
        notes = list(frame_notes or [])
        image_count = max(1, int(input_paths_count or 1))

        # Name the subjects after the profile's categories
        if profile and profile.categories:
            subjects = _join_words([(c.display_name or c.name).lower() for c in profile.categories])
        else:
            subjects = "people and animals"
        if consensus_bounds and profile:
            count_lines = _profile_count_lines(consensus_bounds, profile)
        else:
            count_lines = _bounds_count_lines(population_bounds or {})

        refs = "the reference image" if image_count == 1 else "the reference images"

        prompt_lines: list[str] = []
        base = str(base_instructions or "").strip()
        if base:
            prompt_lines.extend([base, ""])

        if image_count == 1:
            prompt_lines.extend(
                [
                    "Draw the scene of the reference image: keep its camera view and composition, "
                    f"and keep the {subjects} where it shows them, in the same poses.",
                    "",
                    "Subjects to draw, each exactly once:",
                    *count_lines,
                    "",
                    "Reference image:",
                    "- You are provided 1 image: a frame from one security camera during a motion event.",
                ]
            )
        else:
            first = "Image 1 (the primary frame)" if notes and notes[0].is_primary else "Image 1"
            others = "Image 2" if image_count == 2 else f"Images 2-{image_count}"
            prompt_lines.extend(
                [
                    f"Draw the scene of {first}: keep its camera view and composition, "
                    f"and keep the {subjects} where Image 1 shows them, in the same poses. "
                    "The illustration shows that one moment.",
                    "",
                    "Subjects to draw, each exactly once:",
                    *count_lines,
                    "",
                    "Reference images:",
                    f"- You are provided {image_count} images: frames from one security camera, "
                    "seconds apart, during one motion event.",
                    f"- They show the same {subjects} at different moments. Anyone or anything that "
                    "appears in several images is one individual: draw it once, where Image 1 shows it.",
                    f"- Use {others} only to see a subject more clearly, or to find one that "
                    "Image 1 does not show; draw such a subject once.",
                ]
            )

        prompt_lines.extend(
            [
                "",
                "Rules:",
                "- Show one single moment in one continuous scene, with every subject appearing once "
                "(not a sequence of actions, comic panels, or a repeated pattern of the same subject).",
                f"- Draw only subjects that are clearly visible in {refs}; "
                "if you are unsure a subject exists, leave it out.",
                "- Never draw more than the counts above.",
                "- Keep each person's apparent gender presentation; do not turn women into men.",
                "",
                "Content safety:",
                "- Do NOT reproduce any recognizable branded, trademarked, or copyrighted characters, "
                f"logos, or products visible in {refs}.",
                "- Replace any such items with generic alternatives (plain toys, abstract shapes, unlabeled objects).",
                f"- Omit text overlays like timestamps or watermarks from {refs}.",
            ]
        )
        if notes:
            rendered_notes = [
                _render_frame_note(note, position, notes[0])
                for position, note in enumerate(notes, start=1)
            ]
            prompt_lines.extend(["", "What each image shows:", *rendered_notes])

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
                f"keep exactly the subjects above, each drawn once, in one scene):\n"
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
                f"the subjects above must stay clearly present, each drawn once):\n"
                f"{env.prompt_suffix}"
            ).strip()

        return ImagePromptResult(
            prompt=prompt,
            style_profile_id=getattr(style, "id", None) if style else None,
            style_profile_description=getattr(style, "description", None) if style else None,
            environment_variant_id=getattr(env, "id", None) if env else None,
            environment_variant_description=getattr(env, "description", None) if env else None,
        )
