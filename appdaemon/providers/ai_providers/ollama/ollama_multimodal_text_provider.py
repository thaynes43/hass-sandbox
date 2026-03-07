"""Ollama multimodal (image+text) to structured JSON provider."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from ..multimodal_text_provider import ExternalDataGenError, MultimodalProviderName, MultimodalTextProvider
from ..provider_settings import validate_multimodal_model

from ._ollama_helpers import (
    _safe_json,
    image_file_to_base64,
    parse_json_from_response,
)

logger = logging.getLogger(__name__)

# Default timeout allows for cold start / model download (first request can take minutes)
OLLAMA_DEFAULT_TIMEOUT_S = 300.0
OLLAMA_DEFAULT_MODEL = "qwen3.5:9b"
OLLAMA_COLD_START_LOG_THRESHOLD_S = 1.0


@dataclass(frozen=True)
class OllamaMultimodalConfig:
    base_url: str
    model: str = OLLAMA_DEFAULT_MODEL
    timeout_s: float = OLLAMA_DEFAULT_TIMEOUT_S
    max_output_tokens: int = 300


class OllamaMultimodalTextProvider(MultimodalTextProvider):
    """
    Ollama multimodal (vision) support via /api/chat.
    Targets qwen3.5:9b for image+text input.
    Timeouts and logs account for cold-start/model download delays.
    """

    name = MultimodalProviderName.OLLAMA

    def __init__(self, config: OllamaMultimodalConfig):
        ok, err = validate_multimodal_model("ollama", config.model)
        if not ok and err:
            raise ValueError(err)
        self._config = config

    def generate_from_image(
        self,
        *,
        input_image_path: str,
        instructions: str,
        expected_keys: Optional[list[str]] = None,
    ) -> Dict[str, Any]:
        in_path = Path(input_image_path)
        if not in_path.exists():
            raise ExternalDataGenError(f"input image does not exist: {in_path}")
        if not str(instructions or "").strip():
            raise ExternalDataGenError("instructions is required")

        keys_clause = ""
        if expected_keys:
            keys_clause = (
                "\n\nReturn ONLY a JSON object with these top-level keys:\n- "
                + "\n- ".join(str(k) for k in expected_keys)
            )
        prompt = f"{instructions.strip()}{keys_clause}".strip()
        prompt_preview = prompt[:400]

        try:
            image_b64 = image_file_to_base64(in_path)
        except Exception as e:
            raise ExternalDataGenError(f"failed to read image: {in_path}") from e

        body: dict[str, Any] = {
            "model": self._config.model,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                    "images": [image_b64],
                }
            ],
            "stream": False,
            "format": "json",
            "think": False,
            "options": {
                "num_predict": int(self._config.max_output_tokens),
                "temperature": 0,
            },
        }

        url = f"{self._config.base_url.rstrip('/')}/api/chat"
        req = urllib.request.Request(
            url=url,
            method="POST",
            data=_safe_json(body),
            headers={"Content-Type": "application/json"},
        )

        logger.debug(
            "ollama multimodal request: provider=ollama model=%s endpoint=%s timeout_s=%s prompt_preview=%s...",
            self._config.model,
            url,
            self._config.timeout_s,
            prompt_preview[:80],
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
            logger.warning(
                "ollama multimodal http error: code=%s reason=%s detail_preview=%s",
                e.code,
                e.reason,
                (detail or "")[:200],
            )
            raise ExternalDataGenError(f"ollama http error: {e.code} {e.reason}; {detail}") from e
        except urllib.error.URLError as e:
            logger.warning(
                "ollama multimodal request failed (timeout/connection): timeout_s=%s err=%s",
                self._config.timeout_s,
                str(e)[:200],
            )
            raise ExternalDataGenError(
                f"ollama request failed (timeout or connection). "
                f"First request may load/download model; timeout_s={self._config.timeout_s}. {e!r}"
            ) from e
        except Exception as e:
            raise ExternalDataGenError(f"ollama request failed: {e!r}") from e

        elapsed_s = time.time() - started
        response_text = (
            ((payload.get("message") or {}).get("content"))
            or payload.get("response")
            or ""
        ).strip()
        thinking_text = (
            ((payload.get("message") or {}).get("thinking"))
            or payload.get("thinking")
            or ""
        ).strip()
        load_duration_ns = payload.get("load_duration")
        if load_duration_ns and load_duration_ns > 0:
            load_s = load_duration_ns / 1e9
            if load_s >= OLLAMA_COLD_START_LOG_THRESHOLD_S:
                logger.info(
                    "ollama cold start: load_duration_s=%.1f (model load/download). "
                    "Subsequent requests will be faster.",
                    load_s,
                )

        if not response_text:
            raise ExternalDataGenError(
                f"ollama returned empty response (done={payload.get('done')} "
                f"done_reason={payload.get('done_reason')!r} thinking_len={len(thinking_text)}). "
                "This can indicate an unsupported image request shape, model limitation, prompt/format mismatch, "
                "or that the model spent its budget in a thinking trace instead of final content."
            )

        obj = parse_json_from_response(response_text)
        if expected_keys:
            for k in expected_keys:
                obj.setdefault(k, None)

        obj["_meta"] = {
            "backend": "external",
            "provider": "ollama",
            "endpoint": url,
            "model": self._config.model,
            "created_at_epoch": time.time(),
            "elapsed_s": round(elapsed_s, 3),
            "load_duration_ns": load_duration_ns,
            "input_path": str(in_path),
            "request": {
                "max_output_tokens": self._config.max_output_tokens,
                "prompt_len": len(prompt),
                "prompt_preview": prompt_preview,
            },
            "response": {"content_preview": response_text[:400]},
        }
        if thinking_text:
            obj["_meta"]["response"]["thinking_preview"] = thinking_text[:400]
        return obj
