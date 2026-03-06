"""Tests for AI provider model/capability validation."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers.ai_providers.provider_settings import (
    validate_image_model,
    validate_multimodal_model,
    validate_simple_text_model,
)


def test_validate_multimodal_rejects_gpt5_mini() -> None:
    ok, err = validate_multimodal_model("openai", "gpt-5-mini")
    assert ok is False
    assert err is not None
    assert "gpt-5-mini" in (err or "")
    assert "multimodal" in (err or "").lower() or "vision" in (err or "").lower()


def test_validate_multimodal_accepts_gpt52() -> None:
    ok, err = validate_multimodal_model("openai", "gpt-5.2")
    assert ok is True
    assert err is None


def test_validate_multimodal_accepts_gpt4o() -> None:
    ok, err = validate_multimodal_model("openai", "gpt-4o")
    assert ok is True
    assert err is None


def test_validate_multimodal_rejects_unknown_openai_model() -> None:
    ok, err = validate_multimodal_model("openai", "gpt-made-up")
    assert ok is False
    assert err is not None
    assert "allowed multimodal set" in (err or "").lower()


def test_validate_image_rejects_ollama() -> None:
    ok, err = validate_image_model("ollama", "any-model")
    assert ok is False
    assert err is not None
    assert "ollama" in (err or "").lower()
    assert "image" in (err or "").lower()


def test_validate_image_accepts_openai() -> None:
    ok, err = validate_image_model("openai", "gpt-image-1.5")
    assert ok is True
    assert err is None


def test_validate_multimodal_ollama_accepts_qwen35_9b() -> None:
    ok, err = validate_multimodal_model("ollama", "qwen3.5:9b")
    assert ok is True
    assert err is None


def test_validate_multimodal_ollama_rejects_unknown_model() -> None:
    ok, err = validate_multimodal_model("ollama", "random-model")
    assert ok is False
    assert err is not None
    assert "qwen3.5:9b" in (err or "")


def test_validate_image_accepts_gemini() -> None:
    ok, err = validate_image_model("gemini", "gemini-2.0-flash-exp")
    assert ok is True
    assert err is None


def test_validate_multimodal_rejects_unknown_gemini_model() -> None:
    ok, err = validate_multimodal_model("gemini", "gemini-made-up")
    assert ok is False
    assert err is not None
    assert "allowed multimodal set" in (err or "").lower()


def test_validate_simple_text_accepts_openai_gpt5_nano() -> None:
    ok, err = validate_simple_text_model("openai", "gpt-5-nano")
    assert ok is True
    assert err is None


def test_validate_simple_text_rejects_unknown_gemini_model() -> None:
    ok, err = validate_simple_text_model("gemini", "gemini-made-up")
    assert ok is False
    assert err is not None
    assert "gemini" in (err or "").lower()


def test_validate_image_rejects_unknown_openai_image_model() -> None:
    ok, err = validate_image_model("openai", "dall-e-2")
    assert ok is False
    assert err is not None
    assert "allowed image set" in (err or "").lower()


def test_multimodal_openai_invalid_model_raises_at_build() -> None:
    """OpenAIMultimodalTextProvider validates model at construction."""
    from providers.ai_providers.openai.openai_multimodal_text_provider import (
        OpenAIMultimodalConfig,
        OpenAIMultimodalTextProvider,
    )

    with pytest.raises(ValueError) as exc_info:
        OpenAIMultimodalTextProvider(
            OpenAIMultimodalConfig(api_key="k", model="gpt-5-mini")
        )
    assert "gpt-5-mini" in str(exc_info.value)
    assert "multimodal" in str(exc_info.value).lower() or "vision" in str(exc_info.value).lower()


def test_simple_text_gemini_invalid_model_raises_at_build() -> None:
    from providers.ai_providers.gemini.gemini_simple_text_provider import (
        GeminiSimpleTextConfig,
        GeminiSimpleTextProvider,
    )

    with pytest.raises(ValueError) as exc_info:
        GeminiSimpleTextProvider(GeminiSimpleTextConfig(api_key="k", model="gemini-made-up"))
    assert "allowed simple-text set" in str(exc_info.value).lower()


def test_image_openai_invalid_model_raises_at_build() -> None:
    from providers.ai_providers.openai.openai_image_generation_provider import (
        OpenAIImageEditConfig,
        OpenAIImageGenerationProvider,
    )

    with pytest.raises(ValueError) as exc_info:
        OpenAIImageGenerationProvider(OpenAIImageEditConfig(api_key="k", model="dall-e-2"))
    assert "allowed image set" in str(exc_info.value).lower()
