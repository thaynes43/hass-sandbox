"""Reusable async health-check primitives.

Provides ``ping_check``, ``http_check``, ``http_reachable_check`` and
``wyoming_check`` for use by health-checker AppDaemon apps.  These are lightweight wrappers around ``asyncio``
subprocesses and ``aiohttp`` that return a uniform result dict::

    {"status": "ok" | "critical", "detail": "<human-readable detail>"}

Also provides ``apply_cross_check`` for symmetric cross-check logic:
when a device has multiple health signals and only some fail, the
failing checks are downgraded to **warning** instead of **critical**.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from typing import Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)


async def ping_check(
    host: str,
    timeout_s: int = 2,
    attempts: int = 1,
    retry_delay_s: float = 0.5,
) -> Dict[str, str]:
    """ICMP-ping *host* and return a status dict.

    Uses the system ``ping`` command.  Detects macOS (``-t``) vs
    Linux (``-W``) for the timeout flag so it works both in local dev
    and in the production Kubernetes container.

    ``attempts`` > 1 retries after a miss (with ``retry_delay_s`` between
    tries) and returns ok on the first success — useful for IoT devices in
    Wi-Fi power-save that routinely drop a single ping.  The failure detail
    reports the attempt count when more than one was made.

    Returns::

        {"status": "ok", "detail": "2.1ms"}
        {"status": "critical", "detail": "timeout"}
        {"status": "critical", "detail": "timeout (3 attempts)"}
        {"status": "critical", "detail": "ping failed: <error>"}
    """
    attempts = max(1, int(attempts))
    last_result: Dict[str, str] = {"status": "critical", "detail": "timeout"}

    for attempt in range(attempts):
        if attempt > 0:
            await asyncio.sleep(retry_delay_s)
        last_result = await _ping_once(host, timeout_s)
        if last_result["status"] == "ok":
            return last_result

    if attempts > 1:
        last_result = {
            "status": last_result["status"],
            "detail": f"{last_result['detail']} ({attempts} attempts)",
        }
    return last_result


async def _ping_once(host: str, timeout_s: int) -> Dict[str, str]:
    """Single ICMP ping — see ``ping_check``."""
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


# Largest Wyoming ``info`` body accepted (a TTS server lists every voice; the
# 54-voice Kokoro server answers with a few tens of KB).
_WYOMING_MAX_DATA = 4 * 1024 * 1024


async def wyoming_check(
    host: str, port: int, service: str, timeout_s: int = 5
) -> Dict[str, str]:
    """Wyoming ``describe`` handshake against *host*:*port*.

    Sends the same ``describe`` event Home Assistant's Wyoming integration
    sends, reads the ``info`` reply and checks that it lists an installed
    program for *service*: ``"asr"`` (speech-to-text) or ``"tts"``
    (text-to-speech).  A Wyoming event is one JSON header line, optionally
    followed by ``data_length`` bytes of JSON data (older servers inline
    ``data`` in the header instead) and ``payload_length`` bytes of payload.

    Returns::

        {"status": "ok", "detail": "nemo-parakeet-tdt-0.6b-v2 · 5ms"}
        {"status": "ok", "detail": "kokoro, 54 voices · 3ms"}
        {"status": "critical", "detail": "no speech-to-text installed"}
        {"status": "critical", "detail": "timeout"}
        {"status": "critical", "detail": "Connection error: <msg>"}
    """
    if service not in ("asr", "tts"):
        raise ValueError(f"wyoming_check service must be 'asr' or 'tts', not {service!r}")
    kind = "speech-to-text" if service == "asr" else "text-to-speech"

    async def _describe():
        """Connect, send describe, read the reply event. None = closed without a reply."""
        reader, writer = await asyncio.open_connection(host, port, limit=_WYOMING_MAX_DATA)
        try:
            writer.write((json.dumps({"type": "describe", "data": {}}) + "\n").encode())
            await writer.drain()
            line = await reader.readline()
            if not line:
                return None
            header = json.loads(line)
            if not isinstance(header, dict):
                raise ValueError("header is not a JSON object")
            data = header.get("data") or {}
            if not isinstance(data, dict):
                raise ValueError("data is not a JSON object")
            data = dict(data)
            data_length = int(header.get("data_length") or 0)
            if data_length > _WYOMING_MAX_DATA:
                raise ValueError(f"info too large ({data_length} bytes)")
            if data_length:
                extra = json.loads(await reader.readexactly(data_length))
                if not isinstance(extra, dict):
                    raise ValueError("data is not a JSON object")
                data.update(extra)
            return header, data
        finally:
            writer.close()

    start = time.monotonic()
    try:
        # One deadline for the whole handshake, so check_timeout_s bounds the probe.
        reply = await asyncio.wait_for(_describe(), timeout=timeout_s)
    except asyncio.TimeoutError:
        logger.warning("wyoming_check %s:%s timed out after %ss", host, port, timeout_s)
        return {"status": "critical", "detail": "timeout"}
    except (OSError, asyncio.IncompleteReadError) as exc:
        logger.warning("wyoming_check %s:%s connection error: %s", host, port, exc)
        return {"status": "critical", "detail": f"Connection error: {exc}"}
    except (ValueError, TypeError) as exc:
        logger.warning("wyoming_check %s:%s bad reply: %s", host, port, exc)
        return {"status": "critical", "detail": f"bad reply: {exc}"}
    if reply is None:
        return {"status": "critical", "detail": "Connection error: closed without a reply"}
    header, data = reply
    elapsed_ms = (time.monotonic() - start) * 1000

    if header.get("type") != "info":
        return {"status": "critical", "detail": f"unexpected reply: {header.get('type')!r}"}

    # A listed program, model or voice without an "installed" field counts as installed.
    programs = [
        p for p in (data.get(service) or []) if isinstance(p, dict) and p.get("installed", True)
    ]
    if not programs:
        return {"status": "critical", "detail": f"no {kind} installed"}
    items_key = "models" if service == "asr" else "voices"
    for program in programs:
        items = [
            i for i in program.get(items_key) or []
            if isinstance(i, dict) and i.get("installed", True)
        ]
        if not items:
            continue
        if service == "asr":
            return {"status": "ok", "detail": f"{items[0].get('name', '?')} · {elapsed_ms:.0f}ms"}
        name = program.get("name", kind)
        return {"status": "ok", "detail": f"{name}, {len(items)} voices · {elapsed_ms:.0f}ms"}
    name = programs[0].get("name", kind)
    what = "model" if service == "asr" else "voice"
    return {"status": "critical", "detail": f"{name}: no {what} installed"}


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


def is_implausible_battery_drop(
    prev_good_value: Optional[float],
    curr_value: float,
    healthy_floor: float,
    low_threshold: float,
) -> bool:
    """Return True when *curr_value* looks like an RF/gateway disconnect, not a real battery drain.

    A genuine battery drains gradually; a value that plunges from a healthy
    baseline (``>= healthy_floor``) straight to at-or-below ``low_threshold``
    in a single reading is the PowerView-style "0% when RF-unreachable"
    signature, not a physically plausible discharge (real batteries never
    lose 35+ percentage points between two consecutive readings).

    ``prev_good_value=None`` (no healthy baseline observed yet — e.g. cold
    start or a gradually-declining battery that was already below
    ``healthy_floor``) never counts as implausible: there is nothing
    trustworthy to compare against, so a low reading is treated as a
    genuine low battery rather than a disconnect.
    """
    if prev_good_value is None:
        return False
    return curr_value <= low_threshold and prev_good_value >= healthy_floor


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
