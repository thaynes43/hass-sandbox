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


def _content_to_text(content: Any) -> str:
    """
    OpenAI Chat Completions may return `message.content` as:
    - a string
    - a list of content parts (dicts), depending on model/version
    - None (e.g., refusal / content filter)
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
                continue
            if isinstance(p, dict):
                txt = p.get("text")
                if isinstance(txt, str):
                    parts.append(txt)
                    continue
                # Some variants nest text as {"value": "..."}
                if isinstance(txt, dict):
                    val = txt.get("value")
                    if isinstance(val, str):
                        parts.append(val)
        return "".join(parts)
    return str(content)


def _extract_assistant_json_text(choice0: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """
    Extract the model's JSON output from a chat completion choice.

    Some models may place structured output in tool_calls/function_call rather than content.
    Returns (text, debug) where debug is safe metadata for logging/errors.
    """
    msg = (choice0 or {}).get("message") or {}
    content_raw = msg.get("content", None)
    content = _content_to_text(content_raw).strip()

    debug: dict[str, Any] = {
        "message_keys": sorted([str(k) for k in msg.keys()])[:50],
        "content_raw_type": type(content_raw).__name__,
        "content_raw_preview": repr(content_raw)[:400],
    }

    if content:
        return content, debug

    # Try tool_calls (function.arguments usually contains a JSON string)
    tool_calls = msg.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        args_parts: list[str] = []
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function")
            if isinstance(fn, dict):
                a = fn.get("arguments")
                if isinstance(a, str) and a.strip():
                    args_parts.append(a.strip())
        if args_parts:
            debug["extracted_from"] = "tool_calls.function.arguments"
            debug["tool_calls_count"] = len(tool_calls)
            return args_parts[0], debug

    # Try legacy function_call
    fc = msg.get("function_call")
    if isinstance(fc, dict):
        a = fc.get("arguments")
        if isinstance(a, str) and a.strip():
            debug["extracted_from"] = "function_call.arguments"
            return a.strip(), debug

    return "", debug


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
            choice0 = choices[0] if choices else {}
            content, content_debug = _extract_assistant_json_text(choice0)
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

        obj = _parse_json_maybe(content)

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

    def generate_data_from_text(
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
            content, content_debug = _extract_assistant_json_text(choice0)
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

        obj = _parse_json_maybe(content)

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
                "prompt_preview": prompt[:400],
                "input_text_preview": str(input_text or "")[:400],
            },
            "response": {"content_preview": str(content)[:400], "usage": usage},
        }
        return obj

