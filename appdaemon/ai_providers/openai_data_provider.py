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

from ai_providers.types import DataProvider, DataProviderCapabilities, DataProviderName, ExternalDataGenError


@dataclass(frozen=True)
class OpenAIChatVisionDataConfig:
    api_key: str
    base_url: str = "https://api.openai.com"
    # GPT-5.2 supports image input and structured outputs.
    model: str = "gpt-5.2"
    timeout_s: float = 60.0
    max_output_tokens: int = 300
    # Vision detail: "low" tends to be faster/cheaper for security snapshots
    image_detail: str = "low"  # low|high|auto
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


def _parse_json_maybe(text: str) -> Dict[str, Any]:
    """
    Parse a JSON object from model output. Be forgiving if the model wraps it.
    """
    s = (text or "").strip()
    if not s:
        raise ExternalDataGenError("model returned empty content")
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    # Try to extract a JSON object substring.
    first = s.find("{")
    last = s.rfind("}")
    if first != -1 and last != -1 and last > first:
        try:
            obj = json.loads(s[first : last + 1])
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass

    raise ExternalDataGenError(f"failed to parse JSON object from content: {s[:400]!r}")


class OpenAIDataProvider(DataProvider):
    name = DataProviderName.OPENAI
    capabilities = DataProviderCapabilities(
        supports_image_to_json=True,
        supports_text_to_json=True,
        notes="Uses OpenAI Chat Completions with vision + JSON response_format.",
    )

    def __init__(self, config: OpenAIChatVisionDataConfig):
        self._config = config

    def generate_data_from_image(
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
        data_url = _file_to_data_url(in_path)

        # Chat Completions request with multimodal user message.
        prompt_preview = prompt[:400]
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
                        {
                            "type": "image_url",
                            "image_url": {"url": data_url, "detail": self._config.image_detail},
                        },
                    ],
                },
            ],
            "response_format": {"type": "json_object"},
            # GPT-5.2 uses `max_completion_tokens` (not `max_tokens`).
            "max_completion_tokens": int(self._config.max_output_tokens),
        }
        if self._config.user:
            body["user"] = self._config.user

        url = f"{self._config.base_url.rstrip('/')}/v1/chat/completions"
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
            raise ExternalDataGenError(f"openai http error: {e.code} {e.reason}; {detail}") from e
        except Exception as e:
            raise ExternalDataGenError(f"openai request failed: {e!r}") from e

        try:
            choices = payload.get("choices") or []
            msg = choices[0]["message"]
            content = msg.get("content", "")
        except Exception as e:
            raise ExternalDataGenError(f"openai response missing content: {payload!r}") from e

        obj = _parse_json_maybe(str(content))

        # Light validation: ensure keys exist (don't over-enforce, caller can decide).
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
            "input_path": str(in_path),
            "request": {
                "image_detail": self._config.image_detail,
                "max_completion_tokens": int(self._config.max_output_tokens),
                "response_format": "json_object",
                "prompt_len": len(prompt),
                "prompt_preview": prompt_preview,
            },
            "response": {
                "content_preview": str(content)[:400],
                "usage": usage,
            },
        }
        return obj

