"""Thin async HTTP wrapper for the Home Assistant REST API.

Pure-Python — no AppDaemon dependency.  Uses ``aiohttp`` for all I/O.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)


class HaRestClient:
    """Low-level authenticated REST client for the HA API."""

    def __init__(
        self,
        base_url: str,
        token: str,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._owns_session = session is None
        self._session = session

    async def __aenter__(self) -> "HaRestClient":
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError(
                "HaRestClient has no active session — "
                "use 'async with HaRestClient(...)' or pass a session"
            )
        return self._session

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    async def get(self, path: str) -> Any:
        url = f"{self.base_url}{path}"
        logger.debug("GET %s", path)
        async with self.session.get(url, headers=self._headers()) as resp:
            data = await resp.json()
            logger.debug("GET %s -> %d", path, resp.status)
            resp.raise_for_status()
            return data

    async def post(self, path: str, *, json: Any = None) -> Any:
        url = f"{self.base_url}{path}"
        logger.debug("POST %s  body=%s", path, json)
        async with self.session.post(url, json=json, headers=self._headers()) as resp:
            try:
                data = await resp.json()
            except aiohttp.ContentTypeError:
                data = await resp.text()
            logger.debug("POST %s -> %d", path, resp.status)
            resp.raise_for_status()
            return data
