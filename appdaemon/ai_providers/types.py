from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional, Protocol, Sequence


class ExternalImageGenError(RuntimeError):
    pass


class ImageProviderName(str, Enum):
    OPENAI = "openai"
    OLLAMA = "ollama"

    @classmethod
    def parse(cls, value: Any) -> "ImageProviderName":
        s = str(value or "").strip().lower()
        if s in {"openai"}:
            return cls.OPENAI
        if s in {"ollama"}:
            return cls.OLLAMA
        raise ValueError(f"Unsupported image provider: {value!r}")


@dataclass(frozen=True)
class ProviderCapabilities:
    # Image generation modes
    supports_text_to_image: bool
    supports_image_to_image: bool
    supports_inpaint: bool = False

    # Notes for humans/debugging
    notes: str = ""


class ImageProvider(Protocol):
    name: ImageProviderName
    capabilities: ProviderCapabilities

    def edit_image(
        self,
        *,
        input_image_paths: Sequence[str],
        prompt: str,
        output_image_path: str,
    ) -> Dict[str, Any]:
        """
        Image-to-image generation (“edit”) using `input_image_paths` as context.
        Writes the resulting image to `output_image_path`.
        """
        raise NotImplementedError


class ExternalDataGenError(RuntimeError):
    pass


class DataProviderName(str, Enum):
    OPENAI = "openai"
    OLLAMA = "ollama"

    @classmethod
    def parse(cls, value: Any) -> "DataProviderName":
        s = str(value or "").strip().lower()
        if s in {"openai"}:
            return cls.OPENAI
        if s in {"ollama"}:
            return cls.OLLAMA
        raise ValueError(f"Unsupported data provider: {value!r}")


@dataclass(frozen=True)
class DataProviderCapabilities:
    # Structured data generation modes
    supports_image_to_json: bool
    supports_text_to_json: bool = True

    notes: str = ""


class DataProvider(Protocol):
    name: DataProviderName
    capabilities: DataProviderCapabilities

    def generate_data_from_image(
        self,
        *,
        input_image_path: str,
        instructions: str,
        expected_keys: Optional[list[str]] = None,
    ) -> Dict[str, Any]:
        """
        Generate structured data from an image (vision).
        Returns a dict (typically JSON-decoded).
        """
        raise NotImplementedError

    def generate_data_from_text(
        self,
        *,
        input_text: str,
        instructions: str,
        expected_keys: Optional[list[str]] = None,
    ) -> Dict[str, Any]:
        """
        Generate structured data from text input (text-only LLM).
        Returns a dict (typically JSON-decoded).
        """
        raise NotImplementedError

