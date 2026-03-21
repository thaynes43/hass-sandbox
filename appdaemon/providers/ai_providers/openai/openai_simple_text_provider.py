"""OpenAI simple text-to-structured JSON provider."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional

from ..multimodal_text_provider import ExternalDataGenError
from ..provider_settings import validate_simple_text_model
from ..simple_text_provider import SimpleTextProvider, SimpleTextProviderName
from ._chat_helpers import _safe_json, extract_assistant_json_text, parse_json_from_content


@dataclass(frozen=True)
class OpenAISimpleTextConfig:
    api_key: str
    base_url: str = "https://api.openai.com"
    model: str = "gpt-5.2"  # Can use gpt-5-mini for cheaper text-only
    timeout_s: float = 60.0
    max_output_tokens: int = 300
    user: Optional[str] = None


class OpenAISimpleTextProvider(SimpleTextProvider):
    name = SimpleTextProviderName.OPENAI

    def __init__(self, config: OpenAISimpleTextConfig):
        ok, err = validate_simple_text_model("openai", config.model)
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
        url = f"{self._config.base_url.rstrip('/')}/v1/chat/completions"

        body: dict[str, Any] = {
            "model": self._config.model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a careful assistant. Output ONLY valid JSON, with no extra text.",
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "text", "text": f"\n\nINPUT:\n{str(input_text or '')}"},
                    ],
                },
            ],
            "response_format": {"type": "json_object"},
            "max_completion_tokens": int(self._config.max_output_tokens),
        }
        if self._config.user:
            body["user"] = self._config.user

        started = time.time()
        try:
            req = urllib.request.Request(
                url=url,
                method="POST",
                data=_safe_json(body),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._config.api_key}",
                },
            )
            with urllib.request.urlopen(req, timeout=float(self._config.timeout_s)) as resp:
                payload_bytes = resp.read()
                payload = json.loads(payload_bytes.decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise ExternalDataGenError(f"openai http error: {e.code} {e.reason}; {detail}") from e
        except Exception as e:
            raise ExternalDataGenError(f"openai request failed: {e!r}") from e

        try:
            choices = payload.get("choices") or []
            choice0 = choices[0] if choices else {}
            content, content_debug = extract_assistant_json_text(choice0)
            if not content:
                finish_reason = choice0.get("finish_reason")
                msg = choice0.get("message") or {}
                refusal = msg.get("refusal")
                raise ExternalDataGenError(
                    "model returned empty content "
                    f"(finish_reason={finish_reason!r} refusal={refusal!r} debug={content_debug})"
                )
        except ExternalDataGenError:
            raise
        except Exception as e:
            raise ExternalDataGenError(f"openai response missing content: {payload!r}") from e

        obj = parse_json_from_content(content)
        if expected_keys:
            for k in expected_keys:
                obj.setdefault(k, None)

        usage = payload.get("usage") if isinstance(payload, dict) else None
        obj["_meta"] = {
            "backend": "external",
            "provider": "openai",
            "endpoint": url,
            "model": self._config.model,
            "created_at_epoch": time.time(),
            "elapsed_s": round(time.time() - started, 3),
            "request": {
                "max_completion_tokens": int(self._config.max_output_tokens),
                "response_format": "json_object",
                "prompt_len": len(prompt),
                "prompt": prompt,
                "input_text": str(input_text or ""),
            },
            "response": {"content": str(content), "usage": usage},
        }
        return obj
