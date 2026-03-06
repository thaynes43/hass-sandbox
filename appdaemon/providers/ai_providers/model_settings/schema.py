"""Schema for model settings and capability bundles."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class ProviderDefaults:
    """Shared defaults for a provider (credentials, URLs)."""

    api_key_env: Optional[str] = None
    base_url: Optional[str] = None
    base_url_env: Optional[str] = None
    timeout_s: Optional[float] = None


@dataclass
class BundleConfig:
    """
    Capability bundle: provider-neutral model/settings for a named preset.

    Normalized fields:
    - multimodal_model, simple_text_model, image_model
    - multimodal_max_output_tokens, simple_text_max_output_tokens
    - multimodal_media_resolution (maps to OpenAI image_detail, Gemini mediaResolution)
    - multimodal_image_detail (OpenAI-specific; kept for backward compat)
    - timeout_s overrides
    - image_prompt_augmentation, style_profile, style_variant_profile (hooks for prompt builders)
    - provider_options: backend-only extras (OpenAI size, quality, output_format; etc.)
    """

    provider: str
    bundle_id: str

    # Model selection per capability
    multimodal_model: Optional[str] = None
    simple_text_model: Optional[str] = None
    image_model: Optional[str] = None

    # Token/output limits
    multimodal_max_output_tokens: Optional[int] = None
    simple_text_max_output_tokens: Optional[int] = None

    # Media resolution (provider-neutral: low/medium/high)
    # Adapters map: OpenAI -> image_detail; Gemini -> mediaResolution
    multimodal_media_resolution: Optional[str] = None
    # OpenAI-specific; kept for bundles that need it
    multimodal_image_detail: Optional[str] = None

    # Timeouts
    multimodal_timeout_s: Optional[float] = None
    simple_text_timeout_s: Optional[float] = None
    image_timeout_s: Optional[float] = None

    # Image generation
    image_size: Optional[str] = None
    image_quality: Optional[str] = None
    image_output_format: Optional[str] = None
    image_prompt_augmentation: Optional[str] = None

    # Style hooks (for prompt builders)
    style_profile: Optional[str] = None
    style_variant_profile: Optional[str] = None

    # Provider-specific extras
    provider_options: Dict[str, Any] = field(default_factory=dict)

    # Credentials/URLs from provider defaults (merged at resolution time)
    api_key_env: Optional[str] = None
    base_url: Optional[str] = None
    base_url_env: Optional[str] = None


@dataclass
class ProviderModelSettings:
    """Full model settings for a provider: defaults + named bundles."""

    provider: str
    defaults: ProviderDefaults
    bundles: Dict[str, BundleConfig] = field(default_factory=dict)
