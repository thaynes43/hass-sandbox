"""Tests for OpenAI text-to-image (no input images) support."""

from __future__ import annotations

import base64
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers.ai_providers.openai.openai_image_generation_provider import (
    OpenAIImageEditConfig,
    OpenAIImageGenerationProvider,
)
from providers.ai_providers.image_generation_provider import ExternalImageGenError

import pytest


def _fake_response(b64_data: str = "iVBORw0KGgo=", revised_prompt: str = "test"):
    """Create a fake urllib response."""
    payload = json.dumps({
        "data": [{
            "b64_json": b64_data,
            "revised_prompt": revised_prompt,
        }]
    }).encode("utf-8")

    resp = MagicMock()
    resp.read.return_value = payload
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


class TestTextToImage:
    def test_empty_input_paths_uses_generations_endpoint(self):
        config = OpenAIImageEditConfig(api_key="test-key")
        provider = OpenAIImageGenerationProvider(config)

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            output_path = f.name

        with patch("urllib.request.urlopen", return_value=_fake_response()) as mock_urlopen:
            result = provider.edit_image(
                input_image_paths=[],
                prompt="A beautiful sunset",
                output_image_path=output_path,
            )

        # Verify it used the generations endpoint
        req = mock_urlopen.call_args[0][0]
        assert "/v1/images/generations" in req.full_url
        assert result["endpoint"].endswith("/v1/images/generations")

        # Verify no images in request body
        body = json.loads(req.data)
        assert "images" not in body
        assert body["prompt"] == "A beautiful sunset"

    def test_with_input_paths_uses_edits_endpoint(self):
        config = OpenAIImageEditConfig(api_key="test-key")
        provider = OpenAIImageGenerationProvider(config)

        # Create a fake input image
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG\r\n")
            input_path = f.name

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            output_path = f.name

        with patch("urllib.request.urlopen", return_value=_fake_response()) as mock_urlopen:
            result = provider.edit_image(
                input_image_paths=[input_path],
                prompt="Edit this image",
                output_image_path=output_path,
            )

        req = mock_urlopen.call_args[0][0]
        assert "/v1/images/edits" in req.full_url
        body = json.loads(req.data)
        assert "images" in body

    def test_empty_prompt_raises(self):
        config = OpenAIImageEditConfig(api_key="test-key")
        provider = OpenAIImageGenerationProvider(config)

        with pytest.raises(ExternalImageGenError, match="prompt is required"):
            provider.edit_image(
                input_image_paths=[],
                prompt="",
                output_image_path="/tmp/out.png",
            )

    def test_none_input_paths_uses_generations(self):
        config = OpenAIImageEditConfig(api_key="test-key")
        provider = OpenAIImageGenerationProvider(config)

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            output_path = f.name

        with patch("urllib.request.urlopen", return_value=_fake_response()) as mock_urlopen:
            provider.edit_image(
                input_image_paths=None,
                prompt="Generate something",
                output_image_path=output_path,
            )

        req = mock_urlopen.call_args[0][0]
        assert "/v1/images/generations" in req.full_url
