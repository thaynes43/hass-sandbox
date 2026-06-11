"""Async client for the Prometheus Alertmanager v2 API.

Pure-Python — no AppDaemon dependency.  Posts alert batches to
``POST /api/v2/alerts``.  Alertmanager treats repeated posts of the same
label set as a refresh of one alert; an alert resolves when posts stop
(after ``resolve_timeout``, 5m in this cluster) or immediately when a
post carries ``endsAt`` in the past.

All failures raise :class:`AlertmanagerError` so callers can treat
Alertmanager downtime as non-fatal (log and retry on the next cycle).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

import aiohttp

logger = logging.getLogger(__name__)


class AlertmanagerError(Exception):
    """Raised when Alertmanager is unreachable or rejects a request."""


class AlertmanagerClient:
    """Minimal client for posting alerts to Alertmanager's v2 API."""

    def __init__(self, base_url: str, timeout_s: int = 10) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s

    @property
    def base_url(self) -> str:
        return self._base_url

    async def post_alerts(self, alerts: List[Dict[str, Any]]) -> None:
        """POST a batch of alerts to ``/api/v2/alerts``.

        Each alert dict follows the Alertmanager v2 schema::

            {
                "labels": {"alertname": "...", "severity": "...", ...},
                "annotations": {"summary": "...", "description": "..."},
                "startsAt": "<RFC3339>",          # keep stable across re-posts
                "endsAt": "<RFC3339>",            # optional — past time resolves
            }

        Raises :class:`AlertmanagerError` on connection failure, timeout,
        or a non-2xx response.  A successful post is logged at DEBUG.
        """
        if not alerts:
            return

        url = f"{self._base_url}/api/v2/alerts"
        timeout = aiohttp.ClientTimeout(total=self._timeout_s)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=alerts) as resp:
                    if not 200 <= resp.status < 300:
                        body = (await resp.text())[:300]
                        raise AlertmanagerError(
                            f"Alertmanager returned HTTP {resp.status}: {body}"
                        )
                    logger.debug(
                        "Posted %d alert(s) to Alertmanager", len(alerts)
                    )
        except AlertmanagerError:
            raise
        except Exception as exc:
            raise AlertmanagerError(f"Alertmanager POST failed: {exc}") from exc
