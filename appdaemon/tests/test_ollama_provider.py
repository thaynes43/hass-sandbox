"""Tests for Ollama AI provider: multimodal, simple-text, image (unsupported), validation."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers.ai_providers.image_generation_provider import ExternalImageGenError
from providers.ai_providers.multimodal_text_provider import ExternalDataGenError
from providers.ai_providers.ollama.ollama_image_generation_provider import (
    OllamaImageGenerationProvider,
)
from providers.ai_providers.ollama.ollama_multimodal_text_provider import (
    OllamaMultimodalConfig,
    OllamaMultimodalTextProvider,
)
from providers.ai_providers.ollama.ollama_simple_text_provider import (
    OllamaSimpleTextConfig,
    OllamaSimpleTextProvider,
)
from providers.ai_providers.provider_settings import validate_multimodal_model
from providers.ai_providers.registry import (
    build_image_provider,
    build_multimodal_text_provider,
    build_simple_text_provider,
    ImageProviderConfig,
    ImageProviderName,
    MultimodalTextProviderConfig,
    MultimodalProviderName,
    SimpleTextProviderConfig,
    SimpleTextProviderName,
)


# --- Multimodal ---


def test_ollama_multimodal_accepts_qwen35_9b() -> None:
    ok, err = validate_multimodal_model("ollama", "qwen3.5:9b")
    assert ok is True
    assert err is None


def test_ollama_multimodal_rejects_unknown_model() -> None:
    ok, err = validate_multimodal_model("ollama", "unknown-model")
    assert ok is False
    assert err is not None
    assert "qwen3.5:9b" in (err or "")


def test_ollama_multimodal_generate_from_image_success(tmp_path: Path) -> None:
    """Mocked HTTP: successful multimodal response parsed to JSON."""
    img_path = tmp_path / "test.png"
    img_path.write_bytes(b"\x89PNG\r\n\x1a\n")  # minimal PNG header

    response_body = json.dumps(
        {
            "model": "qwen3.5:9b",
            "message": {"role": "assistant", "content": '{"score": 0.8, "label": "test"}'},
            "done": True,
            "done_reason": "stop",
            "load_duration": 0,
        }
    ).encode("utf-8")

    mock_resp = MagicMock()
    mock_resp.read.return_value = response_body
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)

    provider = OllamaMultimodalTextProvider(
        OllamaMultimodalConfig(
            base_url="http://localhost:11434",
            model="qwen3.5:9b",
            timeout_s=300.0,
        )
    )

    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = provider.generate_from_image(
            input_image_path=str(img_path),
            instructions="Analyze this image.",
            expected_keys=["score", "label"],
        )

    assert result["score"] == 0.8
    assert result["label"] == "test"
    assert "_meta" in result
    assert result["_meta"]["provider"] == "ollama"
    assert result["_meta"]["model"] == "qwen3.5:9b"


def test_ollama_multimodal_empty_instructions_raises(tmp_path: Path) -> None:
    img_path = tmp_path / "test.png"
    img_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    provider = OllamaMultimodalTextProvider(
        OllamaMultimodalConfig(base_url="http://localhost:11434")
    )
    with pytest.raises(ExternalDataGenError) as exc_info:
        provider.generate_from_image(
            input_image_path=str(img_path),
            instructions="",
        )
    assert "instructions" in str(exc_info.value).lower()


def test_ollama_multimodal_missing_image_raises() -> None:
    provider = OllamaMultimodalTextProvider(
        OllamaMultimodalConfig(base_url="http://localhost:11434")
    )
    with pytest.raises(ExternalDataGenError) as exc_info:
        provider.generate_from_image(
            input_image_path="/nonexistent/image.png",
            instructions="Analyze.",
        )
    assert "exist" in str(exc_info.value).lower()


def test_ollama_multimodal_empty_response_raises(tmp_path: Path) -> None:
    img_path = tmp_path / "test.png"
    img_path.write_bytes(b"\x89PNG\r\n\x1a\n")

    response_body = json.dumps(
        {"model": "qwen3.5:9b", "message": {"role": "assistant", "content": ""}, "done": True}
    ).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = response_body
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)

    provider = OllamaMultimodalTextProvider(
        OllamaMultimodalConfig(base_url="http://localhost:11434")
    )
    with patch("urllib.request.urlopen", return_value=mock_resp):
        with pytest.raises(ExternalDataGenError) as exc_info:
            provider.generate_from_image(
                input_image_path=str(img_path),
                instructions="Analyze.",
            )
    assert "empty" in str(exc_info.value).lower()


# --- Simple text ---


def test_ollama_simple_text_generate_success() -> None:
    response_body = json.dumps(
        {
            "model": "qwen3.5:9b",
            "message": {"role": "assistant", "content": '{"summary": "A short summary.", "tone": "neutral"}'},
            "done": True,
            "load_duration": 0,
        }
    ).encode("utf-8")

    mock_resp = MagicMock()
    mock_resp.read.return_value = response_body
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)

    provider = OllamaSimpleTextProvider(
        OllamaSimpleTextConfig(
            base_url="http://localhost:11434",
            model="qwen3.5:9b",
        )
    )

    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = provider.generate_from_text(
            input_text="Some input text.",
            instructions="Summarize the input.",
            expected_keys=["summary", "tone"],
        )

    assert result["summary"] == "A short summary."
    assert result["tone"] == "neutral"
    assert "_meta" in result
    assert result["_meta"]["provider"] == "ollama"


def test_ollama_simple_text_empty_instructions_raises() -> None:
    provider = OllamaSimpleTextProvider(
        OllamaSimpleTextConfig(base_url="http://localhost:11434")
    )
    with pytest.raises(ExternalDataGenError) as exc_info:
        provider.generate_from_text(
            input_text="x",
            instructions="",
        )
    assert "instructions" in str(exc_info.value).lower()


# --- Image generation (explicitly unsupported) ---


def test_ollama_image_generation_raises_clear_error() -> None:
    provider = OllamaImageGenerationProvider(base_url="http://localhost:11434")
    with pytest.raises(ExternalImageGenError) as exc_info:
        provider.edit_image(
            input_image_paths=["/a.png"],
            prompt="Draw something",
            output_image_path="/out.png",
        )
    assert "ollama" in str(exc_info.value).lower()
    assert "image" in str(exc_info.value).lower()
    assert "openai" in str(exc_info.value).lower()


def test_ollama_image_generation_capabilities() -> None:
    provider = OllamaImageGenerationProvider(base_url="http://localhost:11434")
    assert not provider.capabilities.supports_text_to_image
    assert not provider.capabilities.supports_image_to_image


# --- Registry integration ---


def test_build_multimodal_provider_ollama_returns_real_impl() -> None:
    cfg = MultimodalTextProviderConfig(
        provider=MultimodalProviderName.OLLAMA,
        base_url="http://localhost:11434",
        model="qwen3.5:9b",
    )
    provider = build_multimodal_text_provider(cfg)
    assert provider.name == MultimodalProviderName.OLLAMA


def test_build_simple_text_provider_ollama_returns_real_impl() -> None:
    cfg = SimpleTextProviderConfig(
        provider=SimpleTextProviderName.OLLAMA,
        base_url="http://localhost:11434",
        model="qwen3.5:9b",
    )
    provider = build_simple_text_provider(cfg)
    assert provider.name == SimpleTextProviderName.OLLAMA


def test_build_multimodal_ollama_invalid_model_raises() -> None:
    cfg = MultimodalTextProviderConfig(
        provider=MultimodalProviderName.OLLAMA,
        base_url="http://localhost:11434",
        model="unknown-model",
    )
    with pytest.raises(ValueError) as exc_info:
        build_multimodal_text_provider(cfg)
    assert "qwen3.5:9b" in str(exc_info.value) or "ollama" in str(exc_info.value).lower()
