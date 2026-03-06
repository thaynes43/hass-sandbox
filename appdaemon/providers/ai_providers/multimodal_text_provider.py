"""Multimodal (image + text) to structured JSON capability interface."""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Optional, Protocol


class ExternalDataGenError(RuntimeError):
    """Raised when structured data generation fails."""
    pass


class MultimodalProviderName(str, Enum):
    OPENAI = "openai"
    GEMINI = "gemini"
    OLLAMA = "ollama"

    @classmethod
    def parse(cls, value: Any) -> "MultimodalProviderName":
        s = str(value or "").strip().lower()
        if s in {"openai"}:
            return cls.OPENAI
        if s in {"gemini"}:
            return cls.GEMINI
        if s in {"ollama"}:
            return cls.OLLAMA
        raise ValueError(f"Unsupported multimodal provider: {value!r}")


class MultimodalTextProvider(Protocol):
    """Protocol for image+text to structured JSON providers (vision/scoring)."""

    name: MultimodalProviderName

    def generate_from_image(
        self,
        *,
        input_image_path: str,
        instructions: str,
        expected_keys: Optional[list[str]] = None,
    ) -> Dict[str, Any]:
        """
        Generate structured data from an image (vision).
        Returns a dict with requested keys plus _meta.
        """
        raise NotImplementedError
