from __future__ import annotations

from typing import Any, Dict, Optional

from .types import DataProvider, DataProviderCapabilities, DataProviderName, ExternalDataGenError


class OllamaDataProvider(DataProvider):
    """
    Placeholder provider for Ollama data/vision.

    Ollama supports text LLMs; some setups add vision models, but we aren't wiring that here yet.
    This explicit stub keeps configuration future-proof.
    """

    name = DataProviderName.OLLAMA
    capabilities = DataProviderCapabilities(
        supports_image_to_json=False,
        supports_text_to_json=False,
        notes="Text/vision-to-JSON not implemented for Ollama in this project yet.",
    )

    def __init__(self, *, base_url: str):
        self._base_url = base_url

    def generate_data_from_image(
        self,
        *,
        input_image_path: str,
        instructions: str,
        expected_keys: Optional[list[str]] = None,
    ) -> Dict[str, Any]:
        raise ExternalDataGenError(
            "Ollama provider is configured, but image-to-JSON generation is not implemented/supported yet. "
            "Use provider=openai for now."
        )

    def generate_data_from_text(
        self,
        *,
        input_text: str,
        instructions: str,
        expected_keys: Optional[list[str]] = None,
    ) -> Dict[str, Any]:
        raise ExternalDataGenError(
            "Ollama provider is configured, but text-to-JSON generation is not implemented/supported yet. "
            "Use provider=openai for now."
        )

