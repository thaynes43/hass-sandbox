"""Prompt builder for dashboard notification image generation."""

from __future__ import annotations

from typing import Optional

from providers.ai_providers.style_variants import (
    EnvironmentVariant,
    StyleProfile,
    random_environment_variant,
    random_style_profile,
)


# Class-specific modifiers for image generation
CLASS_MODIFIERS: dict[str, str] = {
    "UrgentImage": "Use bold, attention-grabbing colors and high contrast. The mood should convey urgency.",
    "BasicTextImage": "Use clear, friendly imagery that reinforces the message.",
    "FunPictureImage": "Use whimsical, playful, and humorous imagery. Make it fun and lighthearted.",
}

PLACEHOLDER_HINTS: list[str] = [
    "a peaceful suburban home at golden hour with soft lighting",
    "a calm garden scene with flowers and butterflies",
    "a cozy living room with warm lighting and a comfortable atmosphere",
    "a serene nature landscape with rolling hills and blue sky",
    "a friendly neighborhood street scene on a pleasant day",
]


def build_notification_prompt(
    notification_text: str,
    prompt_hint: str,
    image_class: str,
    style_profile: Optional[StyleProfile] = None,
    environment_variant: Optional[EnvironmentVariant] = None,
) -> str:
    """Build a complete image generation prompt for a notification.

    Args:
        notification_text: The display text for the notification.
        prompt_hint: Scene description hint from config.
        image_class: Notification class (BasicTextImage, FunPictureImage, etc.).
        style_profile: Optional style override; random if None.
        environment_variant: Optional environment override; random if None.

    Returns:
        Complete prompt string for image generation.
    """
    lines: list[str] = [
        "Generate a landscape illustration (16:9 aspect ratio) for a wall display notification.",
        "",
    ]

    if prompt_hint:
        lines.append(f"Scene: {prompt_hint}")
        lines.append("")

    lines.append(f'The notification message is: "{notification_text}"')
    lines.append("Incorporate the theme of this message into the illustration visually.")
    lines.append("")

    modifier = CLASS_MODIFIERS.get(image_class, CLASS_MODIFIERS["BasicTextImage"])
    lines.append(modifier)
    lines.append("")

    lines.append("Content safety:")
    lines.append("- Do NOT include any text, words, or letters in the image.")
    lines.append("- Do NOT reproduce any branded, trademarked, or copyrighted content.")
    lines.append("- Keep the illustration family-friendly and appropriate for all ages.")

    prompt = "\n".join(lines).strip()

    # Style profile
    if style_profile is None:
        style_profile = random_style_profile()
    if style_profile and style_profile.prompt_suffix:
        prompt = f"{prompt}\n\n{style_profile.prompt_suffix}"

    # Environment variant
    if environment_variant is None:
        environment_variant = random_environment_variant()
    if environment_variant and environment_variant.prompt_suffix:
        prompt = f"{prompt}\n\n{environment_variant.prompt_suffix}"

    return prompt.strip()


def build_placeholder_prompt(
    style_profile: Optional[StyleProfile] = None,
    environment_variant: Optional[EnvironmentVariant] = None,
) -> str:
    """Build a prompt for the 'no notifications' placeholder image."""
    import random

    hint = random.choice(PLACEHOLDER_HINTS)

    lines = [
        "Generate a calming, beautiful landscape illustration (16:9 aspect ratio) for a wall display.",
        "",
        f"Scene: {hint}",
        "",
        "This is a calm idle screen — no notification is active. The image should be soothing and pleasant.",
        "",
        "Content safety:",
        "- Do NOT include any text, words, or letters in the image.",
        "- Do NOT reproduce any branded, trademarked, or copyrighted content.",
        "- Keep the illustration family-friendly.",
    ]

    prompt = "\n".join(lines).strip()

    if style_profile is None:
        style_profile = random_style_profile()
    if style_profile and style_profile.prompt_suffix:
        prompt = f"{prompt}\n\n{style_profile.prompt_suffix}"

    if environment_variant is None:
        environment_variant = random_environment_variant()
    if environment_variant and environment_variant.prompt_suffix:
        prompt = f"{prompt}\n\n{environment_variant.prompt_suffix}"

    return prompt.strip()
