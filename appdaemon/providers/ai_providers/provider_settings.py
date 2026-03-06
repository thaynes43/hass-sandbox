"""Shared contract for provider/model capability validation.

Capability metadata:
- OpenAI multimodal: supports multimodal_image_detail (low|high|auto), max_completion_tokens.
- OpenAI image: supports image_size, image_quality, image_output_format.
- Gemini multimodal: uses mediaResolution (LOW/MEDIUM/HIGH), maxOutputTokens.
  Gemini has no direct equivalent to OpenAI image_quality; media_resolution is the closer analog.
- Gemini image: uses responseModalities for image output; no OpenAI-style image_quality.
- Ollama: multimodal/simple_text via /api/generate; no image generation in this project.
"""

from __future__ import annotations

from typing import Any, Optional, Tuple

# Recommended models for bundle defaults (documented for loader/registry consistency)
GEMINI_DEFAULT_MULTIMODAL = "gemini-2.5-flash"
GEMINI_DEFAULT_IMAGE = "gemini-2.5-flash-image"
OPENAI_IMAGE_CANDIDATES = frozenset({"gpt-image-1.5", "chatgpt-image-latest"})


def validate_multimodal_model(provider: str, model: str) -> Tuple[bool, Optional[str]]:
    """
    Validate that the configured model supports multimodal (image+text) input.
    Returns (ok, error_message). If ok is False, error_message explains the failure.
    """
    provider_lower = (provider or "").strip().lower()
    model_str = (model or "").strip()

    if provider_lower == "openai":
        # gpt-5-mini does not support vision; gpt-5.2, gpt-5.4, gpt-5-nano do
        vision_models = frozenset({
            "gpt-5.2", "gpt-5.4", "gpt-5-nano",
            "gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-4-vision",
        })
        if model_str.lower() in {"gpt-5-mini", "gpt-4"}:
            return False, (
                f"Provider {provider!r} model {model!r} does not support multimodal (image) input. "
                f"Use one of: {sorted(vision_models)}."
            )
        if model_str and model_str.lower() not in vision_models:
            return False, (
                f"Provider {provider!r} model {model!r} is not in the allowed multimodal set. "
                f"Supported: {sorted(vision_models)}."
            )
        return True, None

    if provider_lower == "gemini":
        # gemini-2.5-flash, gemini-2.5-flash-lite, gemini-3.x preview support vision
        vision_models = frozenset({
            "gemini-1.5-flash", "gemini-1.5-flash-8b", "gemini-1.5-pro",
            "gemini-2.0-flash", "gemini-2.0-flash-exp", "gemini-1.0-pro-vision",
            "gemini-2.5-flash", "gemini-2.5-flash-lite",
            "gemini-3.1-pro-preview", "gemini-3-flash-preview",
        })
        if model_str and model_str.lower() not in vision_models:
            return False, (
                f"Provider {provider!r} model {model!r} is not in the allowed multimodal set. "
                f"Supported: {sorted(vision_models)}."
            )
        return True, None

    if provider_lower == "ollama":
        # qwen3.5:9b and variants support vision; document supported models
        vision_models = frozenset(
            {"qwen3.5:9b", "qwen3.5:4b", "qwen3.5:2b", "qwen2.5-vl:9b", "qwen2.5-vl:4b"}
        )
        model_lower = model_str.lower().strip()
        if model_str and model_lower not in vision_models:
            return False, (
                f"Provider ollama model {model!r} may not support multimodal (image) input. "
                f"Supported: {sorted(vision_models)}. Use qwen3.5:9b for best results."
            )
        return True, None

    return True, None


def validate_simple_text_model(provider: str, model: str) -> Tuple[bool, Optional[str]]:
    """
    Validate that the configured model supports text-only structured output.
    Returns (ok, error_message).
    """
    provider_lower = (provider or "").strip().lower()
    model_lower = (model or "").strip().lower()

    if provider_lower == "openai":
        supported = frozenset(
            {"gpt-5.4", "gpt-5.2", "gpt-5-nano", "gpt-5-mini", "gpt-4o", "gpt-4o-mini"}
        )
        if model_lower and model_lower not in supported:
            return False, (
                f"Provider openai model {model!r} is not in the allowed simple-text set. "
                f"Supported: {sorted(supported)}."
            )
        return True, None

    if provider_lower == "gemini":
        supported = frozenset(
            {
                "gemini-3.1-pro-preview",
                "gemini-3-flash-preview",
                "gemini-2.5-flash",
                "gemini-2.5-flash-lite",
                "gemini-2.0-flash",
            }
        )
        if model_lower and model_lower not in supported:
            return False, (
                f"Provider gemini model {model!r} is not in the allowed simple-text set. "
                f"Supported: {sorted(supported)}."
            )
        return True, None

    # Most text-capable Ollama bundles support simple text; default to qwen3.5:9b.
    if provider_lower == "ollama":
        return True, None
    return True, None


def validate_image_model(provider: str, model: str) -> Tuple[bool, Optional[str]]:
    """
    Validate that the configured model supports image generation.
    Returns (ok, error_message).
    """
    provider_lower = (provider or "").strip().lower()
    if provider_lower == "ollama":
        return False, (
            "Provider ollama does not support image generation in this project. "
            "Use provider=openai or provider=gemini for image generation."
        )
    if provider_lower == "openai":
        model_lower = (model or "").strip().lower()
        if model_lower and model_lower not in OPENAI_IMAGE_CANDIDATES:
            return False, (
                f"Provider openai model {model!r} is not in the allowed image set. "
                f"Supported: {sorted(OPENAI_IMAGE_CANDIDATES)}."
            )
        return True, None
    if provider_lower == "gemini":
        # Gemini image models: gemini-2.5-flash-image, gemini-3.x preview (SOTA)
        supported = frozenset(
            {
                "gemini-2.5-flash-image",
                "gemini-3.1-flash-image-preview",
                "gemini-3-pro-image-preview",
                "gemini-2.0-flash-exp",
                "imagen-3.0-generate-002",
            }
        )
        model_lower = (model or "").strip().lower()
        if model_lower and model_lower not in supported:
            return False, (
                f"Provider gemini model {model!r} is not in the allowed image set. "
                f"Supported: {sorted(supported)}."
            )
        return True, None
    return True, None
