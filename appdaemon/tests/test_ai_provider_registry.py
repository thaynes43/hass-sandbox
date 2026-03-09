"""Tests for AI provider registry and capability-specific config parsing."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers.ai_providers.registry import (
    build_image_provider,
    build_multimodal_text_provider,
    build_simple_text_provider,
    ImageProviderConfig,
    ImageProviderName,
    MultimodalProviderName,
    MultimodalTextProviderConfig,
    SimpleTextProviderConfig,
    SimpleTextProviderName,
    multimodal_text_config_from_appdaemon_args,
    provider_config_from_appdaemon_args,
    simple_text_config_from_appdaemon_args,
)


def test_image_config_parses_new_keys() -> None:
    args = {
        "ai_provider_conf": {
            "provider": "openai",
            "api_key": "test-key",
            "image_model": "gpt-image-1.5",
            "image_timeout_s": 120,
            "image_size": "1024x1024",
            "image_quality": "high",
            "image_output_format": "png",
        }
    }
    cfg = provider_config_from_appdaemon_args(args)
    assert cfg.provider == ImageProviderName.OPENAI
    assert cfg.model == "gpt-image-1.5"
    assert cfg.timeout_s == 120.0
    assert cfg.size == "1024x1024"
    assert cfg.quality == "high"
    assert cfg.output_format == "png"


def test_multimodal_config_parses_new_and_legacy_keys() -> None:
    # New keys
    args = {
        "ai_provider_conf": {
            "provider": "openai",
            "api_key": "test-key",
            "multimodal_model": "gpt-5.2",
            "multimodal_timeout_s": 90,
            "multimodal_max_output_tokens": 500,
            "multimodal_image_detail": "high",
        }
    }
    cfg = multimodal_text_config_from_appdaemon_args(args)
    assert cfg.provider == MultimodalProviderName.OPENAI
    assert cfg.model == "gpt-5.2"
    assert cfg.timeout_s == 90.0
    assert cfg.max_output_tokens == 500
    assert cfg.image_detail == "high"

    # Legacy fallback
    args_legacy = {
        "ai_provider_conf": {
            "provider": "openai",
            "api_key": "k",
            "data_model": "gpt-4o",
            "data_timeout_s": 60,
            "data_max_output_tokens": 300,
            "data_image_detail": "low",
        }
    }
    cfg2 = multimodal_text_config_from_appdaemon_args(args_legacy)
    assert cfg2.model == "gpt-4o"
    assert cfg2.timeout_s == 60.0
    assert cfg2.max_output_tokens == 300
    assert cfg2.image_detail == "low"


def test_simple_text_config_parses_new_and_legacy_keys() -> None:
    args = {
        "ai_provider_conf": {
            "provider": "openai",
            "api_key": "test-key",
            "simple_text_model": "gpt-5-mini",
            "simple_text_timeout_s": 30,
            "simple_text_max_output_tokens": 200,
        }
    }
    cfg = simple_text_config_from_appdaemon_args(args)
    assert cfg.provider == SimpleTextProviderName.OPENAI
    assert cfg.model == "gpt-5-mini"
    assert cfg.timeout_s == 30.0
    assert cfg.max_output_tokens == 200

    args_legacy = {
        "ai_provider_conf": {
            "provider": "openai",
            "api_key": "k",
            "data_model": "gpt-5.2",
            "data_timeout_s": 45,
        }
    }
    cfg2 = simple_text_config_from_appdaemon_args(args_legacy)
    assert cfg2.model == "gpt-5.2"
    assert cfg2.timeout_s == 45.0


def test_build_image_provider_openai() -> None:
    cfg = ImageProviderConfig(
        provider=ImageProviderName.OPENAI,
        api_key="test-key",
        model="gpt-image-1.5",
    )
    provider = build_image_provider(cfg)
    assert provider.name == ImageProviderName.OPENAI
    assert provider.capabilities.supports_image_to_image


def test_build_image_provider_ollama_raises() -> None:
    cfg = ImageProviderConfig(
        provider=ImageProviderName.OLLAMA,
        base_url="http://localhost:11434",
    )
    with pytest.raises(ValueError) as exc_info:
        build_image_provider(cfg)
    assert "ollama" in str(exc_info.value).lower()
    assert "image generation" in str(exc_info.value).lower()


def test_build_image_provider_comfyui() -> None:
    cfg = ImageProviderConfig(
        provider=ImageProviderName.COMFYUI,
        base_url="https://comfyui.haynesops.com",
        model="qwen-image-edit-2509",
        provider_options={"workflow_path": "workflows/02_qwen_Image_edit_subgraphed_API.json"},
    )
    provider = build_image_provider(cfg)
    assert provider.name == ImageProviderName.COMFYUI
    assert provider.capabilities.supports_image_to_image
    assert not provider.capabilities.supports_text_to_image


def test_build_multimodal_provider_openai() -> None:
    cfg = MultimodalTextProviderConfig(
        provider=MultimodalProviderName.OPENAI,
        api_key="test-key",
        model="gpt-5.2",
    )
    provider = build_multimodal_text_provider(cfg)
    assert provider.name == MultimodalProviderName.OPENAI


def test_build_multimodal_provider_ollama() -> None:
    cfg = MultimodalTextProviderConfig(
        provider=MultimodalProviderName.OLLAMA,
        base_url="http://localhost:11434",
        model="qwen3.5:9b",
    )
    provider = build_multimodal_text_provider(cfg)
    assert provider.name == MultimodalProviderName.OLLAMA


def test_build_simple_text_provider_openai() -> None:
    cfg = SimpleTextProviderConfig(
        provider=SimpleTextProviderName.OPENAI,
        api_key="test-key",
        model="gpt-5.2",
    )
    provider = build_simple_text_provider(cfg)
    assert provider.name == SimpleTextProviderName.OPENAI


def test_build_simple_text_provider_ollama() -> None:
    cfg = SimpleTextProviderConfig(
        provider=SimpleTextProviderName.OLLAMA,
        base_url="http://localhost:11434",
    )
    provider = build_simple_text_provider(cfg)
    assert provider.name == SimpleTextProviderName.OLLAMA


def test_build_image_provider_gemini() -> None:
    cfg = ImageProviderConfig(
        provider=ImageProviderName.GEMINI,
        api_key="test-gemini-key",
        model="gemini-2.0-flash-exp",
    )
    provider = build_image_provider(cfg)
    assert provider.name == ImageProviderName.GEMINI
    assert provider.capabilities.supports_image_to_image


def test_build_multimodal_provider_gemini() -> None:
    cfg = MultimodalTextProviderConfig(
        provider=MultimodalProviderName.GEMINI,
        api_key="test-gemini-key",
        model="gemini-2.0-flash",
    )
    provider = build_multimodal_text_provider(cfg)
    assert provider.name == MultimodalProviderName.GEMINI


def test_build_simple_text_provider_gemini() -> None:
    cfg = SimpleTextProviderConfig(
        provider=SimpleTextProviderName.GEMINI,
        api_key="test-gemini-key",
        model="gemini-2.0-flash",
    )
    provider = build_simple_text_provider(cfg)
    assert provider.name == SimpleTextProviderName.GEMINI


def test_gemini_config_parses_provider() -> None:
    """Gemini provider parses from ai_provider_conf."""
    args = {
        "ai_provider_conf": {
            "provider": "gemini",
            "api_key": "test-key",
            "multimodal_model": "gemini-2.0-flash",
        }
    }
    cfg = multimodal_text_config_from_appdaemon_args(args)
    assert cfg.provider == MultimodalProviderName.GEMINI
    assert cfg.model == "gemini-2.0-flash"


def test_build_multimodal_provider_openai_requires_api_key() -> None:
    cfg = MultimodalTextProviderConfig(
        provider=MultimodalProviderName.OPENAI,
        model="gpt-5.2",
    )
    with pytest.raises(ValueError) as exc_info:
        build_multimodal_text_provider(cfg)
    assert "api key" in str(exc_info.value).lower()


def test_build_simple_text_provider_gemini_requires_api_key() -> None:
    cfg = SimpleTextProviderConfig(
        provider=SimpleTextProviderName.GEMINI,
        model="gemini-2.0-flash",
    )
    with pytest.raises(ValueError) as exc_info:
        build_simple_text_provider(cfg)
    assert "api key" in str(exc_info.value).lower()


def test_provider_config_from_pointer_image() -> None:
    """Capability-pointer ref resolves to full image config."""
    import os

    os.environ["GEMINI_API_KEY"] = "test-gemini-key"
    try:
        args = {"ai_provider_conf": {"image": "gemini-sota"}}
        cfg = provider_config_from_appdaemon_args(args)
        assert cfg.provider == ImageProviderName.GEMINI
        assert cfg.model == "gemini-3.1-flash-image-preview"
        assert cfg.api_key == "test-gemini-key"
    finally:
        os.environ.pop("GEMINI_API_KEY", None)


def test_provider_config_from_pointer_image_comfyui() -> None:
    import os

    os.environ["COMFYUI_URL"] = "https://comfyui.haynesops.com"
    try:
        args = {"ai_provider_conf": {"image": "comfyui-qwen-edit"}}
        cfg = provider_config_from_appdaemon_args(args)
        assert cfg.provider == ImageProviderName.COMFYUI
        assert cfg.model == "qwen-image-edit-2509"
        assert cfg.base_url == "https://comfyui.haynesops.com"
        assert cfg.provider_options["workflow_path"] == "workflows/02_qwen_Image_edit_subgraphed_API.json"
    finally:
        os.environ.pop("COMFYUI_URL", None)


def test_provider_config_from_scoped_bundle_override_comfyui() -> None:
    args = {
        "ai_provider_conf": {
            "image": {
                "bundle": "comfyui-qwen-edit",
                "base_url": "https://override.example.com",
            }
        }
    }
    cfg = provider_config_from_appdaemon_args(args)
    assert cfg.provider == ImageProviderName.COMFYUI
    assert cfg.base_url == "https://override.example.com"
    assert cfg.model == "qwen-image-edit-2509"


def test_multimodal_config_from_pointer() -> None:
    """Capability-pointer ref resolves to full multimodal config."""
    import os

    os.environ["OPENAPI_TOKEN"] = "test-openai-key"
    try:
        args = {"ai_provider_conf": {"multimodal": "openai-default"}}
        cfg = multimodal_text_config_from_appdaemon_args(args)
        assert cfg.provider == MultimodalProviderName.OPENAI
        assert cfg.model == "gpt-5.2"
        assert cfg.api_key == "test-openai-key"
    finally:
        os.environ.pop("OPENAPI_TOKEN", None)


def test_simple_text_config_from_pointer() -> None:
    """Capability-pointer ref resolves to full simple_text config."""
    import os

    os.environ["OPENAPI_TOKEN"] = "test-openai-key"
    try:
        args = {"ai_provider_conf": {"simple_text": "openai-budget"}}
        cfg = simple_text_config_from_appdaemon_args(args)
        assert cfg.provider == SimpleTextProviderName.OPENAI
        assert cfg.model == "gpt-5-nano"
        assert cfg.api_key == "test-openai-key"
    finally:
        os.environ.pop("OPENAPI_TOKEN", None)


def test_inline_config_still_works_alongside_pointer() -> None:
    """Inline config remains supported (backward compatibility)."""
    args = {
        "ai_provider_conf": {
            "provider": "openai",
            "api_key": "test-key",
            "multimodal_model": "gpt-5.2",
            "multimodal_image_detail": "auto",
        }
    }
    cfg = multimodal_text_config_from_appdaemon_args(args)
    assert cfg.provider == MultimodalProviderName.OPENAI
    assert cfg.model == "gpt-5.2"
    assert cfg.image_detail == "auto"
    assert cfg.api_key == "test-key"


def test_image_pointer_ollama_raises() -> None:
    """Image capability with Ollama bundle raises (Ollama has no image gen)."""
    args = {"ai_provider_conf": {"image": "ollama-qwen9b"}}
    with pytest.raises(ValueError) as exc_info:
        provider_config_from_appdaemon_args(args)
    assert "ollama" in str(exc_info.value).lower()
    assert "image" in str(exc_info.value).lower()


def test_image_config_includes_prompt_augmentation_from_bundle() -> None:
    """gemini-default bundle exposes style hook fields (None when not set in YAML)."""
    import os

    os.environ["GEMINI_API_KEY"] = "test-key"
    try:
        args = {"ai_provider_conf": {"image": "gemini-default"}}
        cfg = provider_config_from_appdaemon_args(args)
        # Style hooks are not set in gemini-default — random selection happens at prompt build time
        assert cfg.image_prompt_augmentation is None
        assert cfg.style_profile is None
    finally:
        os.environ.pop("GEMINI_API_KEY", None)
