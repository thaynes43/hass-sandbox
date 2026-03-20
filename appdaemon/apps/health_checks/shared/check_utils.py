"""Reusable async health-check primitives.

Provides ``ping_check`` and ``http_check`` for use by health-checker
AppDaemon apps.  These are lightweight wrappers around ``asyncio``
subprocesses and ``aiohttp`` that return a uniform result dict::

    {"status": "ok" | "critical", "detail": "<human-readable detail>"}
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from typing import Dict

import aiohttp

logger = logging.getLogger(__name__)


async def ping_check(host: str, timeout_s: int = 2) -> Dict[str, str]:
    """ICMP-ping *host* once and return a status dict.

    Uses the system ``ping`` command.  Detects macOS (``-t``) vs
    Linux (``-W``) for the timeout flag so it works both in local dev
    and in the production Kubernetes container.

    Returns::

        {"status": "ok", "detail": "2.1ms"}
        {"status": "critical", "detail": "timeout"}
        {"status": "critical", "detail": "ping failed: <error>"}
    """
    if sys.platform == "darwin":
        timeout_flag = "-t"
    else:
        timeout_flag = "-W"

    cmd = ["ping", "-c", "1", timeout_flag, str(timeout_s), host]

    try:
        start = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_s + 5
        )
        elapsed_ms = (time.monotonic() - start) * 1000

        if proc.returncode == 0:
            logger.debug("ping %s succeeded in %.1fms", host, elapsed_ms)
            return {"status": "ok", "detail": f"{elapsed_ms:.0f}ms"}

        logger.debug(
            "ping %s failed (rc=%s): %s",
            host,
            proc.returncode,
            stderr.decode(errors="replace").strip(),
        )
        return {"status": "critical", "detail": "timeout"}

    except asyncio.TimeoutError:
        logger.warning("ping %s timed out after %ss", host, timeout_s + 5)
        return {"status": "critical", "detail": "timeout"}
    except Exception as exc:
        logger.warning("ping %s error: %s", host, exc)
        return {"status": "critical", "detail": f"ping failed: {exc}"}


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
