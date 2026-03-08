"""Ollama simple text-to-structured JSON provider."""

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

from ._ollama_helpers import _safe_json, parse_json_from_response

logger = logging.getLogger(__name__)

OLLAMA_DEFAULT_TIMEOUT_S = 300.0
OLLAMA_DEFAULT_MODEL = "qwen3.5:9b"
OLLAMA_COLD_START_LOG_THRESHOLD_S = 1.0


@dataclass(frozen=True)
class OllamaSimpleTextConfig:
    base_url: str
    model: str = OLLAMA_DEFAULT_MODEL
    timeout_s: float = OLLAMA_DEFAULT_TIMEOUT_S
    max_output_tokens: int = 300


class OllamaSimpleTextProvider(SimpleTextProvider):
    """
    Ollama text-only structured output via /api/generate.
    Targets qwen3.5:9b. Timeouts and logs account for cold-start/model download.
    """

    name = SimpleTextProviderName.OLLAMA

    def __init__(self, config: OllamaSimpleTextConfig):
        ok, err = validate_simple_text_model("ollama", config.model)
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
                "\n\nReturn ONLY a JSON object with these top-level keys:\n- "
                + "\n- ".join(str(k) for k in expected_keys)
            )
        prompt = f"{instructions.strip()}{keys_clause}".strip()
        prompt += f"\n\nINPUT:\n{str(input_text or '')}"
        prompt_preview = prompt[:400]

        body: dict[str, Any] = {
            "model": self._config.model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {"num_predict": int(self._config.max_output_tokens)},
        }

        url = f"{self._config.base_url.rstrip('/')}/api/generate"
        req = urllib.request.Request(
            url=url,
            method="POST",
            data=_safe_json(body),
            headers={"Content-Type": "application/json"},
        )

        logger.debug(
            "ollama simple-text request: provider=ollama model=%s endpoint=%s timeout_s=%s prompt_preview=%s...",
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
                "ollama simple-text http error: code=%s reason=%s detail_preview=%s",
                e.code,
                e.reason,
                (detail or "")[:200],
            )
            raise ExternalDataGenError(f"ollama http error: {e.code} {e.reason}; {detail}") from e
        except urllib.error.URLError as e:
            logger.warning(
                "ollama simple-text request failed (timeout/connection): timeout_s=%s err=%s",
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
        response_text = (payload.get("response") or "").strip()
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
                f"done_reason={payload.get('done_reason')!r})"
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
            "request": {
                "max_output_tokens": self._config.max_output_tokens,
                "prompt_len": len(prompt),
                "prompt_preview": prompt_preview,
                "input_text_preview": str(input_text or "")[:400],
            },
            "response": {"content_preview": response_text[:400]},
        }
        return obj
