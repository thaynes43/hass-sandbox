"""Async HTTP client for the Vestaboard local API.

The Vestaboard local API runs on port 7000 on the device's LAN IP.
Auth is via ``X-Vestaboard-Local-Api-Key`` header.

Pure-Python — no AppDaemon dependency.  Uses ``aiohttp`` for all I/O.
"""

from __future__ import annotations

import logging
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

_LOCAL_API_PORT = 7000
_MESSAGE_PATH = "/local-api/message"


class VestaboardClient:
    """Low-level authenticated client for the Vestaboard local API."""

    def __init__(
        self,
        ip: str,
        api_key: str,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> None:
        self._base_url = f"http://{ip}:{_LOCAL_API_PORT}"
        self._api_key = api_key
        self._owns_session = session is None
        self._session = session

    # ------------------------------------------------------------------
    # Async context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "VestaboardClient":
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @property
    def _active_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError(
                "VestaboardClient has no active session — "
                "use 'async with VestaboardClient(...)' or pass a session"
            )
        return self._session

    def _headers(self) -> dict[str, str]:
        return {
            "X-Vestaboard-Local-Api-Key": self._api_key,
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def write_frame(self, characters: list[list[int]]) -> bool:
        """POST a 6x22 character grid to the board.

        Parameters
        ----------
        characters:
            A 6-row x 22-column list of integer character codes.

        Returns
        -------
        bool
            ``True`` if the board accepted the frame (2xx response),
            ``False`` on any error.
        """
        url = f"{self._base_url}{_MESSAGE_PATH}"
        logger.debug("write_frame POST %s", url)
        try:
            async with self._active_session.post(
                url, json=characters, headers=self._headers()
            ) as resp:
                logger.debug("write_frame -> %d", resp.status)
                return resp.status < 300
        except Exception as exc:  # noqa: BLE001
            logger.error("write_frame failed: %s", exc)
            return False

    async def read_current(self) -> list[list[int]] | None:
        """GET the current character grid from the board.

        Returns
        -------
        list[list[int]] | None
            The 6x22 grid currently displayed, or ``None`` on any error.
        """
        url = f"{self._base_url}{_MESSAGE_PATH}"
        logger.debug("read_current GET %s", url)
        try:
            async with self._active_session.get(
                url, headers=self._headers()
            ) as resp:
                logger.debug("read_current -> %d", resp.status)
                if resp.status >= 300:
                    logger.warning("read_current non-2xx: %d", resp.status)
                    return None
                data = await resp.json(content_type=None)
                return data
        except Exception as exc:  # noqa: BLE001
            logger.error("read_current failed: %s", exc)
            return None
