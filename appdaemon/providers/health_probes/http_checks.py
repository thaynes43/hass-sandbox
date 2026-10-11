"""Async HTTP health probes with no AppDaemon dependency."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict

import aiohttp

logger = logging.getLogger(__name__)


async def http_check(url: str, timeout_s: int = 5) -> Dict[str, str]:
    """HTTP GET *url* and return a status dict.

    Only checks for a successful HTTP response (2xx).  Does not follow
    fragment identifiers (e.g. ``#/control-panel``); the fragment is
    client-side only.

    Returns::

        {"status": "ok", "detail": "200 OK"}
        {"status": "critical", "detail": "HTTP 503"}
        {"status": "critical", "detail": "Connection error: <msg>"}
    """
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, ssl=False) as resp:
                if 200 <= resp.status < 300:
                    logger.debug("http_check %s -> %s OK", url, resp.status)
                    return {"status": "ok", "detail": f"{resp.status} OK"}
                logger.debug("http_check %s -> HTTP %s", url, resp.status)
                return {"status": "critical", "detail": f"HTTP {resp.status}"}
    except asyncio.TimeoutError:
        logger.warning("http_check %s timed out after %ss", url, timeout_s)
        return {"status": "critical", "detail": "timeout"}
    except aiohttp.ClientError as exc:
        logger.warning("http_check %s client error: %s", url, exc)
        return {"status": "critical", "detail": f"Connection error: {exc}"}
    except Exception as exc:
        logger.warning("http_check %s error: %s", url, exc)
        return {"status": "critical", "detail": f"Error: {exc}"}


async def http_reachable_check(
    url: str, timeout_s: int = 5, label: str = "reachable"
) -> Dict[str, str]:
    """HTTP GET *url* and report whether the service answered at all.

    Unlike ``http_check``, any response below 500 is ok: an authenticated
    API answering 401/403 to an anonymous request proves the endpoint is up
    and reachable from this host without holding its credential.  A 5xx,
    timeout or connection error is critical.

    Returns::

        {"status": "ok", "detail": "reachable · 163ms"}
        {"status": "critical", "detail": "HTTP 503"}
        {"status": "critical", "detail": "timeout"}
    """
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    start = time.monotonic()
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                elapsed_ms = (time.monotonic() - start) * 1000
                if resp.status < 500:
                    logger.debug("http_reachable_check %s -> %s", url, resp.status)
                    return {"status": "ok", "detail": f"{label} · {elapsed_ms:.0f}ms"}
                logger.debug("http_reachable_check %s -> HTTP %s", url, resp.status)
                return {"status": "critical", "detail": f"HTTP {resp.status}"}
    except asyncio.TimeoutError:
        logger.warning("http_reachable_check %s timed out after %ss", url, timeout_s)
        return {"status": "critical", "detail": "timeout"}
    except aiohttp.ClientError as exc:
        logger.warning("http_reachable_check %s client error: %s", url, exc)
        return {"status": "critical", "detail": f"Connection error: {exc}"}
    except Exception as exc:
        logger.warning("http_reachable_check %s error: %s", url, exc)
        return {"status": "critical", "detail": f"Error: {exc}"}
