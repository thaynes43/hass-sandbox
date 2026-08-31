"""Async client for the Prometheus Alertmanager v2 API.

Pure-Python — no AppDaemon dependency.  Posts alert batches to
``POST /api/v2/alerts``.  Alertmanager treats repeated posts of the same
label set as a refresh of one alert; an alert resolves when posts stop
(after ``resolve_timeout``, 5m in this cluster) or immediately when a
post carries ``endsAt`` in the past.

**Replica fan-out.**  Alertmanager HA pairs replicate silences and the
notification log via gossip — *not* the alert set.  Prometheus therefore
sends every alert to every replica; a direct poster must do the same or
each replica sees an intermittent alert (whichever POSTs happen to land
on it), the pair's notification-log dedup never lines up, and both
replicas page — observed 2026-08-31, when one flapping incident paged
twice per transition.  When the configured host resolves to multiple
addresses (a headless Service such as ``alertmanager-operated``), the
batch is POSTed to every address concurrently; the post succeeds if at
least one replica accepts it.  A single-address host (a ClusterIP
Service, an IP) behaves exactly as before.

All failures raise :class:`AlertmanagerError` so callers can treat
Alertmanager downtime as non-fatal (log and retry on the next cycle).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import urllib.parse
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

    async def _resolve_replica_urls(self) -> List[str]:
        """Resolve the base URL's host to per-replica URLs.

        Returns one URL per distinct address when the host resolves to
        several (headless Service), else just ``[base_url]``.  Any
        resolution problem falls back to the base URL — DNS trouble must
        never block an alert post.
        """
        parsed = urllib.parse.urlsplit(self._base_url)
        host = parsed.hostname
        if not host:
            return [self._base_url]
        try:
            ipaddress.ip_address(host)
            return [self._base_url]  # already an address
        except ValueError:
            pass
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            loop = asyncio.get_running_loop()
            infos = await loop.getaddrinfo(host, port)
        except Exception as exc:
            logger.debug("Could not resolve %s (%s) — posting to base URL", host, exc)
            return [self._base_url]
        addrs = sorted({info[4][0] for info in infos})
        if len(addrs) <= 1:
            return [self._base_url]
        urls = []
        for addr in addrs:
            hostpart = f"[{addr}]" if ":" in addr else addr
            urls.append(
                urllib.parse.urlunsplit(
                    (parsed.scheme, f"{hostpart}:{port}", parsed.path, "", "")
                ).rstrip("/")
            )
        return urls

    async def post_alerts(self, alerts: List[Dict[str, Any]]) -> None:
        """POST a batch of alerts to ``/api/v2/alerts`` on every replica.

        Each alert dict follows the Alertmanager v2 schema::

            {
                "labels": {"alertname": "...", "severity": "...", ...},
                "annotations": {"summary": "...", "description": "..."},
                "startsAt": "<RFC3339>",          # keep stable across re-posts
                "endsAt": "<RFC3339>",            # optional — past time resolves
            }

        Raises :class:`AlertmanagerError` when *no* replica accepts the
        batch (connection failure, timeout, or a non-2xx response).
        Partial replica failures are logged and tolerated — gossip-level
        dedup needs every reachable replica to have the alert, but one
        replica down must not lose the page.  A successful post is
        logged at DEBUG.
        """
        if not alerts:
            return

        urls = await self._resolve_replica_urls()
        timeout = aiohttp.ClientTimeout(total=self._timeout_s)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                results = await asyncio.gather(
                    *(self._post_one(session, url, alerts) for url in urls),
                    return_exceptions=True,
                )
        except Exception as exc:
            raise AlertmanagerError(f"Alertmanager POST failed: {exc}") from exc

        failures = [r for r in results if isinstance(r, BaseException)]
        if len(failures) == len(results):
            failure = failures[0]
            if len(results) == 1:
                # Single-address behaviour matches the pre-fan-out client:
                # the HTTP-status AlertmanagerError re-raises as-is, other
                # errors are wrapped with the original as __cause__.
                if isinstance(failure, AlertmanagerError):
                    raise failure
                raise AlertmanagerError(
                    f"Alertmanager POST failed: {failure}"
                ) from failure
            raise AlertmanagerError(
                f"Alertmanager POST failed on all {len(results)} replica(s): "
                f"{failure}"
            ) from failure
        for failure in failures:
            logger.warning(
                "Alertmanager replica post failed (%d/%d ok): %s",
                len(results) - len(failures),
                len(results),
                failure,
            )
        logger.debug(
            "Posted %d alert(s) to %d Alertmanager replica(s)",
            len(alerts),
            len(results) - len(failures),
        )

    @staticmethod
    async def _post_one(
        session: aiohttp.ClientSession, url: str, alerts: List[Dict[str, Any]]
    ) -> None:
        async with session.post(f"{url}/api/v2/alerts", json=alerts) as resp:
            if not 200 <= resp.status < 300:
                body = (await resp.text())[:300]
                raise AlertmanagerError(
                    f"Alertmanager returned HTTP {resp.status}: {body}"
                )
