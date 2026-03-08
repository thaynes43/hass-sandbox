"""Tests for ComfyUI image generation provider."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers.ai_providers.comfyui.comfyui_image_generation_provider import (
    ComfyUIImageGenerationConfig,
    ComfyUIImageGenerationProvider,
)
from providers.ai_providers.image_generation_provider import ExternalImageGenError


def _mock_response(payload: bytes) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = payload
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def test_comfyui_image_generation_success(tmp_path: Path) -> None:
    workflow_path = (
        Path(__file__).resolve().parents[1]
        / "providers"
        / "ai_providers"
        / "comfyui"
        / "workflows"
        / "02_qwen_Image_edit_subgraphed_API.json"
    )
    input_path = tmp_path / "best.jpg"
    input_path.write_bytes(b"\xff\xd8\xff\xdb")
    output_path = tmp_path / "generated.png"

    upload_resp = _mock_response(json.dumps({"name": "uploaded-best.jpg", "subfolder": "", "type": "input"}).encode("utf-8"))
    prompt_resp = _mock_response(json.dumps({"prompt_id": "pid-123"}).encode("utf-8"))
    history_resp = _mock_response(
        json.dumps(
            {
                "pid-123": {
                    "outputs": {
                        "60": {
                            "images": [
                                {"filename": "generated.png", "subfolder": "", "type": "output"}
                            ]
                        }
                    },
                    "status": {"messages": []},
                }
            }
        ).encode("utf-8")
    )
    view_resp = _mock_response(b"\x89PNG\r\n\x1a\n")

    provider = ComfyUIImageGenerationProvider(
        ComfyUIImageGenerationConfig(
            base_url="https://comfyui.haynesops.com",
            workflow_path=str(workflow_path),
        )
    )

    with patch("urllib.request.urlopen", side_effect=[upload_resp, prompt_resp, history_resp, view_resp]):
        result = provider.edit_image(
            input_image_paths=[str(input_path)],
            prompt="Turn this into a clean illustration.",
            output_image_path=str(output_path),
        )

    assert output_path.exists()
    assert output_path.read_bytes().startswith(b"\x89PNG")
    assert result["provider"] == "comfyui"
    assert result["prompt_id"] == "pid-123"
    assert result["uploaded_input_name"] == "uploaded-best.jpg"


def test_comfyui_image_generation_requires_input(tmp_path: Path) -> None:
    workflow_path = (
        Path(__file__).resolve().parents[1]
        / "providers"
        / "ai_providers"
        / "comfyui"
        / "workflows"
        / "02_qwen_Image_edit_subgraphed_API.json"
    )
    provider = ComfyUIImageGenerationProvider(
        ComfyUIImageGenerationConfig(
            base_url="https://comfyui.haynesops.com",
            workflow_path=str(workflow_path),
        )
    )
    with pytest.raises(ExternalImageGenError) as exc_info:
        provider.edit_image(input_image_paths=[], prompt="x", output_image_path=str(tmp_path / "out.png"))
    assert "input_image_paths" in str(exc_info.value)


def test_comfyui_image_generation_missing_workflow_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError) as exc_info:
        ComfyUIImageGenerationProvider(
            ComfyUIImageGenerationConfig(
                base_url="https://comfyui.haynesops.com",
                workflow_path=str(tmp_path / "missing.json"),
            )
        )
    assert "workflow file does not exist" in str(exc_info.value).lower()
