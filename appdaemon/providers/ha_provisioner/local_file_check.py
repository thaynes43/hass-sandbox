"""Existence probe for Home Assistant ``/local/...`` static files.

Home Assistant serves everything under ``/config/www`` at ``/local/...`` as
**unauthenticated** static content.  This helper therefore deliberately sends
no ``Authorization`` header — the long-lived token must never be attached to a
static-asset request (security policy S3/S6).

It lives in ``providers/`` because security policy S2 forbids outbound HTTP
from ``appdaemon/apps/``.

Typical use: an app that asks Home Assistant to stage files on its own
filesystem cannot trust the ``shell_command`` return value (HA kills shell
commands at 60s while a detached worker may still be finishing).  Probing the
exact URL the frontend will load is the only trustworthy signal.
"""

from __future__ import annotations

import logging
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 5.0


def build_local_url(ha_url: str, url_path: str) -> str:
    """Join an HA base URL with a ``/local/...`` path.

    Returns an empty string when either side is missing, so callers can treat
    "not configured" the same as "not there".
    """
    base = str(ha_url or "").strip().rstrip("/")
    path = str(url_path or "").strip()
    if not base or not path:
        return ""
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{base}{path}"


def _timeout(timeout_s: float) -> aiohttp.ClientTimeout:
    try:
        total = float(timeout_s)
    except (TypeError, ValueError):
        total = DEFAULT_TIMEOUT_S
    if total <= 0:
        total = DEFAULT_TIMEOUT_S
    return aiohttp.ClientTimeout(total=total)


async def _head_status(
    session: aiohttp.ClientSession,
    url: str,
    timeout: aiohttp.ClientTimeout,
) -> int:
    # No ``headers`` kwarg, on purpose: /local/... is unauthenticated static
    # content and must never receive the HA token.
    async with session.head(url, timeout=timeout, allow_redirects=False) as resp:
        return int(resp.status)


async def local_file_exists(
    ha_url: str,
    url_path: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    session: Optional[aiohttp.ClientSession] = None,
) -> bool:
    """Return ``True`` only when HA answers ``HEAD <ha_url><url_path>`` with 200.

    Never raises.  A 404, a redirect, a connection error, a timeout or a
    malformed URL all return ``False`` — this is a poll-until-ready probe, and
    a failure to answer is indistinguishable from "not there yet".
    """
    url = build_local_url(ha_url, url_path)
    if not url:
        logger.debug(
            "local_file_exists: unusable url (ha_url=%r url_path=%r)",
            ha_url,
            url_path,
        )
        return False

    timeout = _timeout(timeout_s)
    try:
        if session is not None:
            status = await _head_status(session, url, timeout)
        else:
            async with aiohttp.ClientSession() as owned:
                status = await _head_status(owned, url, timeout)
    except Exception as exc:
        logger.debug("local_file_exists: HEAD %s failed: %r", url, exc)
        return False

    logger.debug("local_file_exists: HEAD %s -> %s", url, status)
    return status == 200
