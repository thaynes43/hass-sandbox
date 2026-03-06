"""Tests for Gemini AI providers (mocked HTTP)."""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers.ai_providers.gemini.gemini_image_generation_provider import (
    GeminiImageGenerationConfig,
    GeminiImageGenerationProvider,
)
from providers.ai_providers.gemini.gemini_multimodal_text_provider import (
    GeminiMultimodalConfig,
    GeminiMultimodalTextProvider,
)
from providers.ai_providers.gemini.gemini_simple_text_provider import (
    GeminiSimpleTextConfig,
    GeminiSimpleTextProvider,
)
from providers.ai_providers.image_generation_provider import ExternalImageGenError
from providers.ai_providers.multimodal_text_provider import ExternalDataGenError


def _mock_response(body: dict | str, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = (
        json.dumps(body).encode("utf-8") if isinstance(body, dict) else body.encode("utf-8")
    )
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


# --- Multimodal ---


def test_gemini_multimodal_parse_success(tmp_path: Path) -> None:
    """Gemini multimodal parses JSON from mocked response."""
    img = tmp_path / "test.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    expected = {"score": 0.9, "label": "cat"}
    payload = {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": json.dumps(expected)}],
                }
            }
        ]
    }

    with patch("urllib.request.urlopen", return_value=_mock_response(payload)):
        provider = GeminiMultimodalTextProvider(
            GeminiMultimodalConfig(api_key="test-key", model="gemini-2.0-flash")
        )
        result = provider.generate_from_image(
            input_image_path=str(img),
            instructions="Score this image.",
            expected_keys=["score", "label"],
        )

    assert result["score"] == 0.9
    assert result["label"] == "cat"
    assert "_meta" in result
    assert result["_meta"]["provider"] == "gemini"
    assert result["_meta"]["model"] == "gemini-2.0-flash"


def test_gemini_multimodal_parse_fenced_json_success(tmp_path: Path) -> None:
    """Gemini multimodal tolerates fenced JSON wrappers."""
    img = tmp_path / "test.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "text": "Here is the JSON requested:\n```json\n"
                            + json.dumps({"score": 1, "label": "person"})
                            + "\n```"
                        }
                    ],
                }
            }
        ]
    }

    with patch("urllib.request.urlopen", return_value=_mock_response(payload)):
        provider = GeminiMultimodalTextProvider(
            GeminiMultimodalConfig(api_key="test-key", model="gemini-2.0-flash")
        )
        result = provider.generate_from_image(
            input_image_path=str(img),
            instructions="Score this image.",
            expected_keys=["score", "label"],
        )

    assert result["score"] == 1
    assert result["label"] == "person"


def test_gemini_multimodal_empty_response_raises(tmp_path: Path) -> None:
    """Gemini multimodal raises on empty content."""
    img = tmp_path / "test.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    payload = {"candidates": [{"content": {"parts": []}}]}

    with patch("urllib.request.urlopen", return_value=_mock_response(payload)):
        provider = GeminiMultimodalTextProvider(
            GeminiMultimodalConfig(api_key="test-key")
        )
        with pytest.raises(ExternalDataGenError) as exc_info:
            provider.generate_from_image(
                input_image_path=str(img),
                instructions="Score this.",
            )
    assert "empty" in str(exc_info.value).lower()


def test_gemini_multimodal_http_error_raises(tmp_path: Path) -> None:
    """Gemini multimodal raises on HTTP error (log-safe)."""
    import urllib.error

    img = tmp_path / "test.png"
    img.write_bytes(b"\x89PNG")
    err = urllib.error.HTTPError("url", 401, "Unauthorized", {}, None)
    err.read = MagicMock(return_value=b'{"error":"invalid key"}')

    with patch("urllib.request.urlopen", side_effect=err):
        provider = GeminiMultimodalTextProvider(
            GeminiMultimodalConfig(api_key="test-key")
        )
        with pytest.raises(ExternalDataGenError) as exc_info:
            provider.generate_from_image(
                input_image_path=str(img),
                instructions="Score this.",
            )
    assert "401" in str(exc_info.value) or "gemini" in str(exc_info.value).lower()


# --- Simple text ---


def test_gemini_simple_text_parse_success() -> None:
    """Gemini simple text parses JSON from mocked response."""
    expected = {"summary": "A short narrative.", "tone": "neutral"}
    payload = {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": json.dumps(expected)}],
                }
            }
        ]
    }

    with patch("urllib.request.urlopen", return_value=_mock_response(payload)):
        provider = GeminiSimpleTextProvider(
            GeminiSimpleTextConfig(api_key="test-key")
        )
        result = provider.generate_from_text(
            input_text="Some input.",
            instructions="Summarize.",
            expected_keys=["summary", "tone"],
        )

    assert result["summary"] == "A short narrative."
    assert result["tone"] == "neutral"
    assert "_meta" in result
    assert result["_meta"]["provider"] == "gemini"


def test_gemini_simple_text_empty_instructions_raises() -> None:
    """Gemini simple text raises when instructions empty."""
    provider = GeminiSimpleTextProvider(
        GeminiSimpleTextConfig(api_key="test-key")
    )
    with pytest.raises(ExternalDataGenError) as exc_info:
        provider.generate_from_text(input_text="x", instructions="")
    assert "instructions" in str(exc_info.value).lower()


# --- Image generation ---


def test_gemini_image_gen_parse_success(tmp_path: Path) -> None:
    """Gemini image gen writes output from mocked response."""
    in_img = tmp_path / "in.png"
    in_img.write_bytes(b"\x89PNG\r\n\x1a\n")
    out_img = tmp_path / "out.png"
    fake_img_b64 = base64.b64encode(b"fake-png-bytes").decode("ascii")
    payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"inlineData": {"mimeType": "image/png", "data": fake_img_b64}}
                    ],
                }
            }
        ]
    }

    with patch("urllib.request.urlopen", return_value=_mock_response(payload)):
        provider = GeminiImageGenerationProvider(
            GeminiImageGenerationConfig(api_key="test-key")
        )
        result = provider.edit_image(
            input_image_paths=[str(in_img)],
            prompt="Make it blue",
            output_image_path=str(out_img),
        )

    assert out_img.exists()
    assert out_img.read_bytes() == b"fake-png-bytes"
    assert result["provider"] == "gemini"
    assert result["output_path"] == str(out_img)


def test_gemini_image_gen_missing_input_raises(tmp_path: Path) -> None:
    """Gemini image gen raises when input_image_paths empty."""
    provider = GeminiImageGenerationProvider(
        GeminiImageGenerationConfig(api_key="test-key")
    )
    with pytest.raises(ExternalImageGenError) as exc_info:
        provider.edit_image(
            input_image_paths=[],
            prompt="x",
            output_image_path=str(tmp_path / "out.png"),
        )
    assert "required" in str(exc_info.value).lower()


def test_gemini_image_gen_nonexistent_input_raises(tmp_path: Path) -> None:
    """Gemini image gen raises when input file does not exist."""
    provider = GeminiImageGenerationProvider(
        GeminiImageGenerationConfig(api_key="test-key")
    )
    with pytest.raises(ExternalImageGenError) as exc_info:
        provider.edit_image(
            input_image_paths=[str(tmp_path / "nonexistent.png")],
            prompt="x",
            output_image_path=str(tmp_path / "out.png"),
        )
    assert "do not exist" in str(exc_info.value)
