"""Ollama image generation provider — explicitly unsupported."""

from __future__ import annotations

import logging
from typing import Any, Dict, Sequence

from ..image_generation_provider import (
    ExternalImageGenError,
    ImageGenerationProvider,
    ImageProviderCapabilities,
    ImageProviderName,
)

logger = logging.getLogger(__name__)


class OllamaImageGenerationProvider(ImageGenerationProvider):
    """
    Ollama does not provide native image generation in this project.
    Image generation is explicitly unsupported.
    """

    name = ImageProviderName.OLLAMA
    capabilities = ImageProviderCapabilities(
        supports_text_to_image=False,
        supports_image_to_image=False,
        supports_inpaint=False,
        notes="Ollama does not natively provide image generation in this project.",
    )

    def __init__(self, *, base_url: str):
        self._base_url = base_url

    def edit_image(
        self,
        *,
        input_image_paths: Sequence[str],
        prompt: str,
        output_image_path: str,
    ) -> Dict[str, Any]:
        logger.warning(
            "Ollama provider does not support image generation. "
            "Image generation is explicitly unsupported for Ollama in this project. "
            "Use provider=openai for image generation. "
            "(input_image_paths=%s output=%s)",
            len(input_image_paths) if input_image_paths else 0,
            output_image_path,
        )
        raise ExternalImageGenError(
            "Ollama provider does not support image generation. "
            "Use provider=openai for image generation."
        )
