"""Ollama provider helpers: JSON parsing, base64 image encoding."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, Dict

from ..multimodal_text_provider import ExternalDataGenError


def image_file_to_base64(path: Path) -> str:
    """Read image file and return base64-encoded string (no data URL prefix)."""
    data = path.read_bytes()
    return base64.b64encode(data).decode("ascii")


def parse_json_from_response(text: str) -> Dict[str, Any]:
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


def _safe_json(obj: Any) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
