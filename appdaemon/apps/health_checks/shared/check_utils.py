"""Reusable async health-check primitives.

Provides ``ping_check`` and ``http_check`` for use by health-checker
AppDaemon apps.  These are lightweight wrappers around ``asyncio``
subprocesses and ``aiohttp`` that return a uniform result dict::

    {"status": "ok" | "critical", "detail": "<human-readable detail>"}

Also provides ``apply_cross_check`` for symmetric cross-check logic:
when a device has multiple health signals and only some fail, the
failing checks are downgraded to **warning** instead of **critical**.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from typing import Dict, List

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


# ------------------------------------------------------------------
# Cross-check helpers
# ------------------------------------------------------------------


def apply_cross_check(results: List[Dict[str, str]]) -> None:
    """Downgrade critical to warning when not all checks are failing.

    Mutates *results* in-place.  If every check is critical or unknown the
    device is truly down and statuses stay as-is.  If at least one check
    passes, any ``critical`` result is downgraded to ``warning`` (partial
    failure).

    A single-check result list is left unchanged — there is nothing to
    cross-check against.
    """
    if len(results) < 2:
        return
    all_bad = all(r["status"] in ("critical", "unknown") for r in results)
    if not all_bad:
        for r in results:
            if r["status"] == "critical":
                r["status"] = "warning"
                r["detail"] += " (partial failure)"


def apply_cross_check_per_device(
    results: List[Dict[str, str]],
    device_names: List[str],
) -> None:
    """Apply :func:`apply_cross_check` independently per device.

    Groups results by matching their ``name`` prefix against each device
    name, then applies the cross-check within each group.
    """
    for dev_name in device_names:
        dev_results = [r for r in results if r["name"].startswith(dev_name)]
        apply_cross_check(dev_results)
