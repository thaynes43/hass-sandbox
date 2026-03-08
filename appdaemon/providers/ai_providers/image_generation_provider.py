"""Image generation capability interface and related types."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Protocol, Sequence


class ExternalImageGenError(RuntimeError):
    """Raised when image generation fails (HTTP, parsing, validation)."""
    pass


class ImageProviderName(str, Enum):
    OPENAI = "openai"
    GEMINI = "gemini"
    OLLAMA = "ollama"
    COMFYUI = "comfyui"

    @classmethod
    def parse(cls, value: Any) -> "ImageProviderName":
        s = str(value or "").strip().lower()
        if s in {"openai"}:
            return cls.OPENAI
        if s in {"gemini"}:
            return cls.GEMINI
        if s in {"ollama"}:
            return cls.OLLAMA
        if s in {"comfyui"}:
            return cls.COMFYUI
        raise ValueError(f"Unsupported image provider: {value!r}")


@dataclass(frozen=True)
class ImageProviderCapabilities:
    supports_text_to_image: bool
    supports_image_to_image: bool
    supports_inpaint: bool = False
    notes: str = ""


class ImageGenerationProvider(Protocol):
    """Protocol for image generation (edit/image-to-image) providers."""

    name: ImageProviderName
    capabilities: ImageProviderCapabilities

    def edit_image(
        self,
        *,
        input_image_paths: Sequence[str],
        prompt: str,
        output_image_path: str,
    ) -> Dict[str, Any]:
        """
        Image-to-image generation using input_image_paths as context.
        Writes the result to output_image_path.
        """
        raise NotImplementedError
