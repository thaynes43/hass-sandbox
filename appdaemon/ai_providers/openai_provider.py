from __future__ import annotations

import base64
import json
import mimetypes
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from ai_providers.types import ExternalImageGenError, ImageProvider, ImageProviderName, ProviderCapabilities


@dataclass(frozen=True)
class OpenAIImageEditConfig:
    api_key: str
    base_url: str = "https://api.openai.com"
    model: str = "gpt-image-1.5"
    size: str = "1024x1024"  # or "auto"
    quality: str = "medium"  # low|medium|high|auto
    output_format: str = "png"  # png|jpeg|webp
    moderation: str = "auto"  # low|auto
    timeout_s: float = 90.0
    user: Optional[str] = None


def _guess_mime(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def _file_to_data_url(path: Path) -> str:
    mime = _guess_mime(path)
    data = path.read_bytes()
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _safe_json(obj: Any) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class OpenAIImageProvider(ImageProvider):
    name = ImageProviderName.OPENAI
    capabilities = ProviderCapabilities(
        supports_text_to_image=True,
        supports_image_to_image=True,
        supports_inpaint=True,
        notes="Uses OpenAI Images edit endpoint (/v1/images/edits).",
    )

    def __init__(self, config: OpenAIImageEditConfig):
        self._config = config

    def edit_image(
        self,
        *,
        input_image_path: str,
        prompt: str,
        output_image_path: str,
    ) -> Dict[str, Any]:
        in_path = Path(input_image_path)
        out_path = Path(output_image_path)

        if not in_path.exists():
            raise ExternalImageGenError(f"input image does not exist: {in_path}")
        if not prompt or not str(prompt).strip():
            raise ExternalImageGenError("prompt is required")

        data_url = _file_to_data_url(in_path)
        prompt_preview = str(prompt)[:400]

        body: dict[str, Any] = {
            "images": [{"image_url": data_url}],
            "prompt": str(prompt),
            "model": self._config.model,
            "size": self._config.size,
            "quality": self._config.quality,
            "output_format": self._config.output_format,
            "moderation": self._config.moderation,
            "n": 1,
        }
        if self._config.user:
            body["user"] = self._config.user

        url = f"{self._config.base_url.rstrip('/')}/v1/images/edits"
        req = urllib.request.Request(
            url=url,
            method="POST",
            data=_safe_json(body),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._config.api_key}",
            },
        )

        started = time.time()
        try:
            with urllib.request.urlopen(req, timeout=float(self._config.timeout_s)) as resp:
                payload_bytes = resp.read()
                payload = json.loads(payload_bytes.decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise ExternalImageGenError(f"openai http error: {e.code} {e.reason}; {detail}") from e
        except Exception as e:
            raise ExternalImageGenError(f"openai request failed: {e!r}") from e

        # For GPT image models, response uses base64 images (b64_json).
        data_list = payload.get("data")
        if not isinstance(data_list, list) or not data_list:
            raise ExternalImageGenError(f"openai response missing data: {payload!r}")
        first = data_list[0]
        if not isinstance(first, dict) or not first.get("b64_json"):
            raise ExternalImageGenError(f"openai response missing b64_json: {payload!r}")

        try:
            img_bytes = base64.b64decode(first["b64_json"])
        except Exception as e:
            raise ExternalImageGenError(f"failed to decode openai image b64: {e!r}") from e

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(img_bytes)

        return {
            "backend": "external",
            "provider": "openai",
            "endpoint": url,
            "model": self._config.model,
            "size": self._config.size,
            "quality": self._config.quality,
            "output_format": self._config.output_format,
            "created_at_epoch": time.time(),
            "elapsed_s": round(time.time() - started, 3),
            "input_path": str(in_path),
            "output_path": str(out_path),
            "revised_prompt": first.get("revised_prompt"),
            "request": {
                "prompt_len": len(str(prompt)),
                "prompt_preview": prompt_preview,
            },
        }

