import base64
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _png_bytes() -> bytes:
    # PNG signature + minimal IHDR chunk bytes (not a full valid image, but good enough for header checks)
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def test_openai_edit_image_writes_output(tmp_path: Path) -> None:
    import sys

    # Make `appdaemon/` importable for `ai_providers.*`
    sys.path.append(str(Path(__file__).resolve().parents[1]))

    from ai_providers.openai_provider import OpenAIImageEditConfig, OpenAIImageProvider

    in_path = tmp_path / "best.jpg"
    in_path.write_bytes(b"\xff\xd8\xff" + b"\x00" * 128)  # minimal fake jpeg header-ish
    out_path = tmp_path / "generated.png"

    payload = {
        "data": [
            {
                "b64_json": base64.b64encode(_png_bytes()).decode("ascii"),
                "revised_prompt": "revised",
            }
        ]
    }

    mock_resp = MagicMock()
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.read.return_value = json.dumps(payload).encode("utf-8")

    with patch("urllib.request.urlopen", return_value=mock_resp) as urlopen:
        provider = OpenAIImageProvider(OpenAIImageEditConfig(api_key="test-key", timeout_s=1))
        meta = provider.edit_image(input_image_path=str(in_path), prompt="make it cartoony", output_image_path=str(out_path))

    assert out_path.exists()
    assert out_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert meta["provider"] == "openai"
    assert meta["output_path"] == str(out_path)
    assert urlopen.called


def test_openai_edit_image_requires_input(tmp_path: Path) -> None:
    import sys

    sys.path.append(str(Path(__file__).resolve().parents[1]))
    from ai_providers.openai_provider import OpenAIImageEditConfig, OpenAIImageProvider
    from ai_providers.types import ExternalImageGenError

    with pytest.raises(ExternalImageGenError):
        provider = OpenAIImageProvider(OpenAIImageEditConfig(api_key="k"))
        provider.edit_image(input_image_path=str(tmp_path / "missing.jpg"), prompt="x", output_image_path=str(tmp_path / "out.png"))

