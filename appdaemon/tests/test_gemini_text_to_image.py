"""Tests for Gemini text-to-image (no input images) support."""

from __future__ import annotations

import base64
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers.ai_providers.gemini.gemini_image_generation_provider import (
    GeminiImageGenerationConfig,
    GeminiImageGenerationProvider,
)
from providers.ai_providers.image_generation_provider import ExternalImageGenError

import pytest


def _fake_response(img_b64: str = "iVBORw0KGgo="):
    """Create a fake urllib response with Gemini format."""
    payload = json.dumps({
        "candidates": [{
            "content": {
                "parts": [{
                    "inlineData": {
                        "mimeType": "image/png",
                        "data": img_b64,
                    }
                }]
            }
        }]
    }).encode("utf-8")

    resp = MagicMock()
    resp.read.return_value = payload
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


class TestGeminiTextToImage:
    def test_empty_input_paths_works(self):
        config = GeminiImageGenerationConfig(api_key="test-key")
        provider = GeminiImageGenerationProvider(config)

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            output_path = f.name

        with patch("urllib.request.urlopen", return_value=_fake_response()) as mock_urlopen:
            result = provider.edit_image(
                input_image_paths=[],
                prompt="A beautiful sunset",
                output_image_path=output_path,
            )

        # Verify request was sent
        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data)

        # Should only have text part, no inlineData
        parts = body["contents"][0]["parts"]
        assert len(parts) == 1
        assert "text" in parts[0]
        assert parts[0]["text"] == "A beautiful sunset"

        assert result["provider"] == "gemini"

    def test_with_input_image_includes_inline_data(self):
        config = GeminiImageGenerationConfig(api_key="test-key")
        provider = GeminiImageGenerationProvider(config)

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG\r\n")
            input_path = f.name

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            output_path = f.name

        with patch("urllib.request.urlopen", return_value=_fake_response()) as mock_urlopen:
            provider.edit_image(
                input_image_paths=[input_path],
                prompt="Edit this",
                output_image_path=output_path,
            )

        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data)
        parts = body["contents"][0]["parts"]
        # Should have inlineData + text
        assert len(parts) == 2
        assert "inlineData" in parts[0]

    def test_empty_prompt_raises(self):
        config = GeminiImageGenerationConfig(api_key="test-key")
        provider = GeminiImageGenerationProvider(config)

        with pytest.raises(ExternalImageGenError, match="prompt is required"):
            provider.edit_image(
                input_image_paths=[],
                prompt="",
                output_image_path="/tmp/out.png",
            )
