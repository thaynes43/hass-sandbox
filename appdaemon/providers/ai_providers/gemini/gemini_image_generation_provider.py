"""Gemini image generation provider using generateContent with image output."""

from __future__ import annotations

import base64
import json
import logging
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
from ._gemini_helpers import _safe_json, file_to_inline_data, safe_log_preview

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_IMAGE_MODEL = "gemini-2.5-flash-image"


@dataclass(frozen=True)
class GeminiImageGenerationConfig:
    api_key: str
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_IMAGE_MODEL
    timeout_s: float = 90.0


class GeminiImageGenerationProvider(ImageGenerationProvider):
    name = ImageProviderName.GEMINI
    capabilities = ImageProviderCapabilities(
        supports_text_to_image=True,
        supports_image_to_image=True,
        supports_inpaint=False,
        notes="Uses Gemini generateContent with responseModalities IMAGE.",
    )

    def __init__(self, config: GeminiImageGenerationConfig):
        ok, err = validate_image_model("gemini", config.model)
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

        if not in_paths:
            raise ExternalImageGenError("input_image_paths is required")
        missing = [p for p in in_paths if not p.exists()]
        if missing:
            raise ExternalImageGenError(
                f"input image(s) do not exist: {[str(p) for p in missing]}"
            )
        if not prompt or not str(prompt).strip():
            raise ExternalImageGenError("prompt is required")

        prompt_preview = str(prompt)[:400]
        logger.debug(
            "gemini image gen: provider=gemini model=%s timeout_s=%s prompt_preview=%s",
            self._config.model,
            self._config.timeout_s,
            prompt_preview,
        )

        parts: list[dict[str, Any]] = []
        for p in in_paths:
            parts.append({"inlineData": file_to_inline_data(p)})
        parts.append({"text": str(prompt or "").strip()})

        body: dict[str, Any] = {
            "contents": [{"parts": parts}],
            "generationConfig": {
                "responseModalities": ["IMAGE"],
            },
        }

        url = f"{self._config.base_url.rstrip('/')}/models/{self._config.model}:generateContent"
        req = urllib.request.Request(
            url=url,
            method="POST",
            data=_safe_json(body),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": self._config.api_key,
            },
        )

        started = time.time()
        try:
            with urllib.request.urlopen(
                req, timeout=float(self._config.timeout_s)
            ) as resp:
                payload_bytes = resp.read()
                payload = json.loads(payload_bytes.decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            safe_log_preview("gemini image gen response_error", detail)
            raise ExternalImageGenError(
                f"gemini http error: {e.code} {e.reason}; {detail}"
            ) from e
        except Exception as e:
            raise ExternalImageGenError(f"gemini request failed: {e!r}") from e

        safe_log_preview("gemini image gen response", str(payload)[:400])

        candidates = payload.get("candidates") or []
        if not candidates:
            raise ExternalImageGenError(
                f"gemini response missing candidates: {str(payload)[:400]!r}"
            )
        c0 = candidates[0]
        content = c0.get("content") or {}
        resp_parts = content.get("parts") or []

        img_b64: Optional[str] = None
        for part in resp_parts:
            inline = part.get("inlineData") or part.get("inline_data")
            if isinstance(inline, dict) and inline.get("data"):
                img_b64 = inline.get("data")
                break

        if not img_b64:
            raise ExternalImageGenError(
                f"gemini response missing image in parts: {resp_parts!r}"
            )

        try:
            img_bytes = base64.b64decode(img_b64)
        except Exception as e:
            raise ExternalImageGenError(
                f"failed to decode gemini image b64: {e!r}"
            ) from e

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(img_bytes)

        return {
            "backend": "external",
            "provider": "gemini",
            "endpoint": url,
            "model": self._config.model,
            "created_at_epoch": time.time(),
            "elapsed_s": round(time.time() - started, 3),
            "input_paths": [str(p) for p in in_paths],
            "output_path": str(out_path),
            "request": {"prompt_len": len(str(prompt)), "prompt_preview": prompt_preview},
        }
