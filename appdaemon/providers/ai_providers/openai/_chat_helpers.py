"""Shared OpenAI Chat Completions helpers for multimodal and simple-text providers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from ..multimodal_text_provider import ExternalDataGenError


def _guess_mime(path: Path) -> str:
    import mimetypes
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def _file_to_data_url(path: Path) -> str:
    import base64
    mime = _guess_mime(path)
    data = path.read_bytes()
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _safe_json(obj: Any) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def parse_json_from_content(text: str) -> Dict[str, Any]:
    """Parse a JSON object from model output. Be forgiving if the model wraps it."""
    s = (text or "").strip()
    if not s:
        raise ExternalDataGenError("model returned empty content")
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

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
                if isinstance(txt, dict):
                    val = txt.get("value")
                    if isinstance(val, str):
                        parts.append(val)
        return "".join(parts)
    return str(content)


def extract_assistant_json_text(choice0: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Extract the model's JSON output from a chat completion choice."""
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
            return args_parts[0], debug

    fc = msg.get("function_call")
    if isinstance(fc, dict):
        a = fc.get("arguments")
        if isinstance(a, str) and a.strip():
            debug["extracted_from"] = "function_call.arguments"
            return a.strip(), debug

    return "", debug
