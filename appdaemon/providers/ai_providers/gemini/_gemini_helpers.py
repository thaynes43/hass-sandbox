"""Shared Gemini API helpers for encoding and requests."""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
from pathlib import Path
from typing import Any, Dict

from ..multimodal_text_provider import ExternalDataGenError

logger = logging.getLogger(__name__)

TRUNCATE_PREVIEW = 400


def _guess_mime(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def file_to_inline_data(path: Path) -> dict[str, str]:
    """Build Gemini inlineData part from file."""
    mime = _guess_mime(path)
    data = path.read_bytes()
    b64 = base64.b64encode(data).decode("ascii")
    return {"mimeType": mime, "data": b64}


def _safe_json(obj: Any) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def safe_log_preview(label: str, data: Any) -> None:
    """Log truncated preview for debugging; never log full tokens/keys."""
    if data is None:
        logger.debug("%s: (none)", label)
        return
    s = str(data) if not isinstance(data, str) else data
    preview = s[:TRUNCATE_PREVIEW] + ("..." if len(s) > TRUNCATE_PREVIEW else "")
    logger.debug("%s: %s", label, preview)


def parse_json_from_content(text: str) -> Dict[str, Any]:
    """Parse a JSON object from model output. Be forgiving if the model wraps it."""
    s = (text or "").strip()
    if not s:
        raise ExternalDataGenError("model returned empty content")

    if "```" in s:
        parts = s.split("```")
        for block in parts:
            candidate = block.strip()
            if not candidate:
                continue
            if candidate.lower().startswith("json"):
                candidate = candidate[4:].strip()
            if candidate.startswith("{") and candidate.endswith("}"):
                s = candidate
                break
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


def build_response_schema(expected_keys: list[str]) -> Dict[str, Any]:
    """
    Build a lightweight Gemini response schema from the expected top-level keys.

    We keep this intentionally small and permissive so Gemini can still choose
    exact numeric values while reliably returning an object with the right shape.
    """
    properties: dict[str, Any] = {}
    integer_keys = {
        "male_count",
        "female_count",
        "animal_count",
        "people_min",
        "people_max",
        "confidence",
    }
    number_keys = {"person_score", "face_score", "frame_score", "score"}
    array_string_keys = {"key_events"}
    string_keys = {"summary", "pose", "run_summary", "tone", "label"}

    for key in expected_keys:
        if key in integer_keys:
            properties[key] = {"type": "INTEGER"}
        elif key in number_keys:
            properties[key] = {"type": "NUMBER"}
        elif key in array_string_keys:
            properties[key] = {"type": "ARRAY", "items": {"type": "STRING"}}
        elif key in string_keys:
            properties[key] = {"type": "STRING"}
        else:
            properties[key] = {"type": "STRING"}

    return {
        "type": "OBJECT",
        "properties": properties,
        "required": list(expected_keys),
    }
