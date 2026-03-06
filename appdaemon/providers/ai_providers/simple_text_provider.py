"""Simple text-to-structured JSON capability interface."""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Optional, Protocol


class SimpleTextProviderName(str, Enum):
    OPENAI = "openai"
    GEMINI = "gemini"
    OLLAMA = "ollama"

    @classmethod
    def parse(cls, value: Any) -> "SimpleTextProviderName":
        s = str(value or "").strip().lower()
        if s in {"openai"}:
            return cls.OPENAI
        if s in {"gemini"}:
            return cls.GEMINI
        if s in {"ollama"}:
            return cls.OLLAMA
        raise ValueError(f"Unsupported simple-text provider: {value!r}")


class SimpleTextProvider(Protocol):
    """Protocol for text-only to structured JSON providers (narrative, etc.)."""

    name: SimpleTextProviderName

    def generate_from_text(
        self,
        *,
        input_text: str,
        instructions: str,
        expected_keys: Optional[list[str]] = None,
    ) -> Dict[str, Any]:
        """
        Generate structured data from text input.
        Returns a dict with requested keys plus _meta.
        """
        raise NotImplementedError
