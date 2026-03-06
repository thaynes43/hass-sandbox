"""
Shared types and compatibility aliases for AI providers.

After refactor, capability interfaces live in:
- image_generation_provider
- multimodal_text_provider
- simple_text_provider

This module re-exports errors and aliases for backward compatibility.
"""

from __future__ import annotations

from .image_generation_provider import (
    ExternalImageGenError,
    ImageProviderCapabilities,
    ImageProviderName,
)
from .multimodal_text_provider import ExternalDataGenError

# Aliases for tests/consumers that still expect these
__all__ = [
    "ExternalDataGenError",
    "ExternalImageGenError",
    "ImageProviderCapabilities",
    "ImageProviderName",
]
