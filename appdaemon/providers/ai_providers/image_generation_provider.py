"""Image generation capability interface and related types."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional, Protocol, Sequence


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
    # How many input images this provider will actually send. ``None`` means
    # "no limit the caller has to care about" — every image handed to
    # ``edit_image`` is used. A number means anything past it is dropped, so a
    # caller that describes its references in the prompt must trim to this
    # first or the prompt will describe images the model never receives.
    #
    # Only ComfyUI has a real limit today (the selected workflow's image slot
    # count), and only its provider resolves it per instance. Gemini and OpenAI
    # send every frame they are given, so they leave this ``None``.
    max_input_images: Optional[int] = None


class ImageGenerationProvider(Protocol):
    """Protocol for image generation (edit/image-to-image) providers."""

    name: ImageProviderName
    # Providers whose capabilities depend on their configuration (ComfyUI, whose
    # ``max_input_images`` is the resolved workflow's slot count) set this per
    # instance; the rest declare it once on the class. Read it off the instance.
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
