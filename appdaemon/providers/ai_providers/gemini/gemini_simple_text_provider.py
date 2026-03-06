"""Gemini simple text-to-structured JSON provider."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional

from ..multimodal_text_provider import ExternalDataGenError
from ..provider_settings import validate_simple_text_model
from ..simple_text_provider import SimpleTextProvider, SimpleTextProviderName
from ._gemini_helpers import (
    _safe_json,
    build_response_schema,
    parse_json_from_content,
    safe_log_preview,
)
from .gemini_multimodal_text_provider import _extract_text_from_gemini_response

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_SIMPLE_TEXT_MODEL = "gemini-2.5-flash-lite"


@dataclass(frozen=True)
class GeminiSimpleTextConfig:
    api_key: str
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_SIMPLE_TEXT_MODEL
    timeout_s: float = 60.0
    max_output_tokens: int = 300


class GeminiSimpleTextProvider(SimpleTextProvider):
    name = SimpleTextProviderName.GEMINI

    def __init__(self, config: GeminiSimpleTextConfig):
        ok, err = validate_simple_text_model("gemini", config.model)
        if not ok and err:
            raise ValueError(err)
        self._config = config

    def generate_from_text(
        self,
        *,
        input_text: str,
        instructions: str,
        expected_keys: Optional[list[str]] = None,
    ) -> Dict[str, Any]:
        if not str(instructions or "").strip():
            raise ExternalDataGenError("instructions is required")

        keys_clause = ""
        if expected_keys:
            keys_clause = (
                "\n\nReturn ONLY a valid JSON object with these top-level keys:\n- "
                + "\n- ".join(str(k) for k in expected_keys)
            )

        prompt = f"{instructions.strip()}{keys_clause}".strip()
        input_block = f"\n\nINPUT:\n{str(input_text or '')}"

        logger.debug(
            "gemini simple_text: provider=gemini model=%s timeout_s=%s",
            self._config.model,
            self._config.timeout_s,
        )

        body: dict[str, Any] = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt},
                        {"text": input_block},
                    ]
                }
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "maxOutputTokens": int(self._config.max_output_tokens),
            },
        }
        if expected_keys:
            body["generationConfig"]["responseSchema"] = build_response_schema(expected_keys)

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
            safe_log_preview("gemini simple_text response_error", detail)
            raise ExternalDataGenError(
                f"gemini http error: {e.code} {e.reason}; {detail}"
            ) from e
        except Exception as e:
            raise ExternalDataGenError(f"gemini request failed: {e!r}") from e

        content = _extract_text_from_gemini_response(payload)
        safe_log_preview("gemini simple_text response content", content)

        if not content:
            candidate0 = (payload.get("candidates") or [{}])[0] or {}
            raise ExternalDataGenError(
                "gemini returned empty content "
                f"(candidates={len(payload.get('candidates') or [])} "
                f"finishReason={candidate0.get('finishReason')!r} "
                f"candidate_keys={sorted(candidate0.keys()) if isinstance(candidate0, dict) else []})"
            )

        obj = parse_json_from_content(content)
        if expected_keys:
            for k in expected_keys:
                obj.setdefault(k, None)

        obj["_meta"] = {
            "backend": "external",
            "provider": "gemini",
            "endpoint": url,
            "model": self._config.model,
            "created_at_epoch": time.time(),
            "elapsed_s": round(time.time() - started, 3),
            "request": {
                "max_output_tokens": int(self._config.max_output_tokens),
                "responseMimeType": "application/json",
                "prompt_len": len(prompt),
                "prompt_preview": prompt[:400],
                "input_text_preview": str(input_text or "")[:400],
            },
            "response": {"content_preview": content[:400]},
        }
        return obj
