"""OpenAI image generation provider."""

from __future__ import annotations

import base64
import json
import mimetypes
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from ..image_generation_provider import (
    ExternalImageGenError,
    ImageGenerationProvider,
    ImageProviderCapabilities,
    ImageProviderName,
)
from ..provider_settings import validate_image_model


@dataclass(frozen=True)
class OpenAIImageEditConfig:
    api_key: str
    base_url: str = "https://api.openai.com"
    model: str = "gpt-image-1.5"
    size: str = "1536x1024"
    quality: str = "medium"
    output_format: str = "png"
    moderation: str = "auto"
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


class OpenAIImageGenerationProvider(ImageGenerationProvider):
    name = ImageProviderName.OPENAI
    capabilities = ImageProviderCapabilities(
        supports_text_to_image=True,
        supports_image_to_image=True,
        supports_inpaint=True,
        notes="Uses OpenAI Images edit endpoint (/v1/images/edits).",
    )

    def __init__(self, config: OpenAIImageEditConfig):
        ok, err = validate_image_model("openai", config.model)
        if not ok and err:
            raise ValueError(err)
        self._config = config

    def edit_image(
        self,
        *,
        input_image_paths: Sequence[str],
        prompt: str,
        output_image_path: str,
    ) -> Dict[str, Any]:
        in_paths = [
            Path(p)
            for p in (list(input_image_paths) if input_image_paths else [])
            if str(p).strip()
        ]
        out_path = Path(output_image_path)

        if in_paths:
            missing = [p for p in in_paths if not p.exists()]
            if missing:
                raise ExternalImageGenError(f"input image(s) do not exist: {[str(p) for p in missing]}")
        if not prompt or not str(prompt).strip():
            raise ExternalImageGenError("prompt is required")

        prompt_preview = str(prompt)[:400]

        if in_paths:
            # Image-to-image edit
            data_urls = [{"image_url": _file_to_data_url(p)} for p in in_paths]
            body: dict[str, Any] = {
                "images": data_urls,
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
        else:
            # Text-to-image generation (no input images)
            body = {
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
            url = f"{self._config.base_url.rstrip('/')}/v1/images/generations"
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
            "input_paths": [str(p) for p in in_paths],
            "output_path": str(out_path),
            "revised_prompt": first.get("revised_prompt"),
            "request": {"prompt_len": len(str(prompt)), "prompt_preview": prompt_preview},
        }
