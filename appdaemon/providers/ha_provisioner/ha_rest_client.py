"""Thin async HTTP + WebSocket wrapper for the Home Assistant API.

Pure-Python — no AppDaemon dependency.  Uses ``aiohttp`` for all I/O.

REST is used for scripts and state checks.  WebSocket is used for helper
creation/deletion (the ``{domain}/create`` / ``{domain}/delete`` commands)
because HA removed helpers from the Config Entry Flow REST API.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)

_MSG_ID_START = 10


class HaRestClient:
    """Low-level authenticated client for the HA REST and WebSocket APIs."""

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

    # ------------------------------------------------------------------
    # REST
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # WebSocket (short-lived connection per call)
    # ------------------------------------------------------------------

    def _ws_url(self) -> str:
        base = self.base_url
        if base.startswith("https://"):
            return "wss://" + base[len("https://"):] + "/api/websocket"
        if base.startswith("http://"):
            return "ws://" + base[len("http://"):] + "/api/websocket"
        return base + "/api/websocket"

    async def send_ws_command(self, message: dict[str, Any]) -> dict[str, Any]:
        """Open a WebSocket, authenticate, send one command, return result, close.

        This mirrors the pattern ha-mcp uses for helper CRUD (``{domain}/create``,
        ``{domain}/delete``, ``{domain}/list``).
        """
        ws_url = self._ws_url()
        logger.debug("WS connect %s", ws_url)
        async with self.session.ws_connect(ws_url) as ws:
            # auth_required
            auth_req = await ws.receive_json()
            if auth_req.get("type") != "auth_required":
                raise RuntimeError(f"Expected auth_required, got: {auth_req}")

            await ws.send_json({"type": "auth", "access_token": self._token})
            auth_resp = await ws.receive_json()
            if auth_resp.get("type") != "auth_ok":
                raise RuntimeError(f"WebSocket auth failed: {auth_resp}")

            msg_id = _MSG_ID_START
            payload = {"id": msg_id, **message}
            logger.debug("WS send %s", message.get("type"))
            await ws.send_json(payload)

            result = await ws.receive_json()
            logger.debug("WS recv id=%s success=%s", result.get("id"), result.get("success"))
            return result
