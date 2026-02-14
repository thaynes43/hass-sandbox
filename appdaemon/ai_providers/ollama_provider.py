from __future__ import annotations

from typing import Any, Dict

from ai_providers.types import ExternalImageGenError, ImageProvider, ImageProviderName, ProviderCapabilities


class OllamaImageProvider(ImageProvider):
    """
    Placeholder provider for Ollama.

    As of today, Ollama is primarily a text LLM server. Some users run separate
    image tools (e.g., ComfyUI) alongside it. We model this provider explicitly
    so configs can select "ollama" now, and we can expand later.
    """

    name = ImageProviderName.OLLAMA
    capabilities = ProviderCapabilities(
        supports_text_to_image=False,
        supports_image_to_image=False,
        supports_inpaint=False,
        notes="Ollama does not natively provide image generation in this project yet.",
    )

    def __init__(self, *, base_url: str):
        self._base_url = base_url

    def edit_image(
        self,
        *,
        input_image_path: str,
        prompt: str,
        output_image_path: str,
    ) -> Dict[str, Any]:
        raise ExternalImageGenError(
            "Ollama provider is configured, but image generation is not implemented/supported yet. "
            "Use provider=openai, or wire a ComfyUI provider in the future."
        )

