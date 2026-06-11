"""Async client for ComfyUI's queue-status endpoint (``GET /prompt``).

Separate from the image-generation provider: this is a lightweight
read-only poll used by the imagegen health checker.  ComfyUI responds
with ``{"exec_info": {"queue_remaining": N}}`` where N counts queued +
in-flight jobs (the queue is in-memory, so a ComfyUI restart resets it
to 0).

All failures raise :class:`ComfyUIStatusError` so callers can map
unreachability to a health status instead of crashing.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


class ComfyUIStatusError(Exception):
    """Raised when the ComfyUI status endpoint is unreachable or malformed."""


class ComfyUIStatusClient:
    """Minimal client for polling ComfyUI queue depth."""

    def __init__(self, base_url: str, timeout_s: int = 10) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s

    @property
    def base_url(self) -> str:
        return self._base_url

    async def fetch_queue_remaining(self) -> int:
        """Return ``exec_info.queue_remaining`` from ``GET /prompt``."""
        url = f"{self._base_url}/prompt"
        timeout = aiohttp.ClientTimeout(total=self._timeout_s)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        raise ComfyUIStatusError(f"HTTP {resp.status}")
                    payload: Any = await resp.json(content_type=None)
        except ComfyUIStatusError:
            raise
        except Exception as exc:
            raise ComfyUIStatusError(f"request failed: {exc}") from exc

        try:
            remaining = payload["exec_info"]["queue_remaining"]
            return int(remaining)
        except (KeyError, TypeError, ValueError) as exc:
            raise ComfyUIStatusError(
                f"unexpected payload shape: {str(payload)[:200]}"
            ) from exc
