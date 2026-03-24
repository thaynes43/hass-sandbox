"""Tests for ComfyUI image generation provider."""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers.ai_providers.comfyui.comfyui_image_generation_provider import (
    ComfyUIImageGenerationConfig,
    ComfyUIImageGenerationProvider,
    _get_image_dimensions,
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


# ---------- Workflow helpers ----------

_WORKFLOW_PATH = (
    Path(__file__).resolve().parents[1]
    / "providers"
    / "ai_providers"
    / "comfyui"
    / "workflows"
    / "02_qwen_Image_edit_subgraphed_API.json"
)


def _make_provider(**kwargs: object) -> ComfyUIImageGenerationProvider:
    defaults = {
        "base_url": "https://comfyui.haynesops.com",
        "workflow_path": str(_WORKFLOW_PATH),
    }
    defaults.update(kwargs)
    return ComfyUIImageGenerationProvider(ComfyUIImageGenerationConfig(**defaults))


# ---------- Sampler / scale override tests ----------


def test_build_workflow_sampler_overrides() -> None:
    provider = _make_provider(sampler_cfg=4.0, sampler_steps=8, sampler_denoise=0.85)
    wf = provider._build_workflow(prompt="test", uploaded_name="img.jpg", output_path=Path("out.png"))
    sampler = wf["115:3"]["inputs"]
    assert sampler["cfg"] == 4.0
    assert sampler["steps"] == 8
    assert sampler["denoise"] == 0.85


def test_build_workflow_scale_override() -> None:
    provider = _make_provider(scale_node_id="115:93", scale_megapixels=1.5)
    wf = provider._build_workflow(prompt="test", uploaded_name="img.jpg", output_path=Path("out.png"))
    assert wf["115:93"]["inputs"]["megapixels"] == 1.5


def test_build_workflow_no_overrides_preserves_defaults() -> None:
    provider = _make_provider()
    wf = provider._build_workflow(prompt="test", uploaded_name="img.jpg", output_path=Path("out.png"))
    sampler = wf["115:3"]["inputs"]
    assert sampler["cfg"] == 1
    assert sampler["steps"] == 4
    assert sampler["denoise"] == 1
    assert wf["115:93"]["inputs"]["megapixels"] == 1


# ---------- Image dimension parsing ----------


def _make_minimal_png(w: int, h: int) -> bytes:
    """Create minimal valid PNG header with given dimensions."""
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return sig + b"\x00\x00\x00\x0d" + b"IHDR" + ihdr_data


def _make_minimal_jpeg(w: int, h: int) -> bytes:
    """Create minimal JPEG with SOF0 marker for given dimensions."""
    sof0 = b"\xff\xc0"
    # SOF0 segment: length(2) + precision(1) + height(2) + width(2) + components(1)
    seg = struct.pack(">HBHH", 8, 8, h, w) + b"\x01"
    return b"\xff\xd8" + sof0 + seg


def test_get_image_dimensions_png(tmp_path: Path) -> None:
    p = tmp_path / "test.png"
    p.write_bytes(_make_minimal_png(1600, 1200))
    assert _get_image_dimensions(p) == (1600, 1200)


def test_get_image_dimensions_jpeg(tmp_path: Path) -> None:
    p = tmp_path / "test.jpg"
    p.write_bytes(_make_minimal_jpeg(640, 360))
    assert _get_image_dimensions(p) == (640, 360)


def test_get_image_dimensions_unknown_format(tmp_path: Path) -> None:
    p = tmp_path / "test.bmp"
    p.write_bytes(b"BM" + b"\x00" * 50)
    assert _get_image_dimensions(p) is None


# ---------- Low-res input warning ----------


def test_low_res_input_logs_warning(tmp_path: Path) -> None:
    input_path = tmp_path / "small.jpg"
    input_path.write_bytes(_make_minimal_jpeg(640, 360))
    output_path = tmp_path / "out.png"

    provider = _make_provider(min_input_pixels=500000)

    upload_resp = _mock_response(json.dumps({"name": "small.jpg"}).encode("utf-8"))
    prompt_resp = _mock_response(json.dumps({"prompt_id": "pid-1"}).encode("utf-8"))
    history_resp = _mock_response(
        json.dumps({
            "pid-1": {
                "outputs": {"60": {"images": [{"filename": "g.png", "subfolder": "", "type": "output"}]}},
                "status": {"messages": []},
            }
        }).encode("utf-8")
    )
    view_resp = _mock_response(b"\x89PNG\r\n\x1a\n")

    with patch("urllib.request.urlopen", side_effect=[upload_resp, prompt_resp, history_resp, view_resp]), \
         patch("providers.ai_providers.comfyui.comfyui_image_generation_provider.logger") as mock_logger:
        result = provider.edit_image(
            input_image_paths=[str(input_path)],
            prompt="test prompt",
            output_image_path=str(output_path),
        )
    mock_logger.warning.assert_called_once()
    assert "640" in str(mock_logger.warning.call_args)
    assert result["input_dimensions"] == {"width": 640, "height": 360}


def test_high_res_input_no_warning(tmp_path: Path) -> None:
    input_path = tmp_path / "big.png"
    input_path.write_bytes(_make_minimal_png(1600, 1200))
    output_path = tmp_path / "out.png"

    provider = _make_provider(min_input_pixels=500000)

    upload_resp = _mock_response(json.dumps({"name": "big.png"}).encode("utf-8"))
    prompt_resp = _mock_response(json.dumps({"prompt_id": "pid-2"}).encode("utf-8"))
    history_resp = _mock_response(
        json.dumps({
            "pid-2": {
                "outputs": {"60": {"images": [{"filename": "g.png", "subfolder": "", "type": "output"}]}},
                "status": {"messages": []},
            }
        }).encode("utf-8")
    )
    view_resp = _mock_response(b"\x89PNG\r\n\x1a\n")

    with patch("urllib.request.urlopen", side_effect=[upload_resp, prompt_resp, history_resp, view_resp]), \
         patch("providers.ai_providers.comfyui.comfyui_image_generation_provider.logger") as mock_logger:
        provider.edit_image(
            input_image_paths=[str(input_path)],
            prompt="test prompt",
            output_image_path=str(output_path),
        )
    mock_logger.warning.assert_not_called()
