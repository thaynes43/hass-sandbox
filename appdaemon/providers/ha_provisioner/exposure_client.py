"""AssistExposureClient — read and change Home Assistant's Assist exposure list.

Exposure is the **only** security boundary in front of Home Assistant's LLM
voice agents: every conversation tool call runs through
``intent.async_match_targets``, which silently drops entities that are not
exposed.  There is no per-pipeline, per-agent or per-satellite exposure, no
permission layer and no confirmation step, so "what is exposed" *is* "what a
voice can do".

This client wraps the three admin WebSocket commands that make that list
readable and writable from AppDaemon:

``homeassistant/expose_entity/list``
    Returns ``{"exposed_entities": {entity_id: {assistant: True}}}``.  Only
    assistants with ``should_expose`` truthy are present, so an entity that is
    absent (or whose dict lacks the assistant) is simply not exposed.

``homeassistant/expose_entity``
    Takes ``assistants``, ``entity_ids`` and ``should_expose`` — the only bulk
    primitive HA offers.  Requires an admin token.

``config/entity_registry/get_entries``
    Returns the registry entries; each carries ``entity_id`` and ``platform``
    (the integration that supplies it).  It does **not** carry
    ``device_class`` — that lives on the entity state — so callers that need a
    device class read it from state, not from here.

All of it is plain ``aiohttp`` through :class:`HaRestClient`; no AppDaemon
import, so it is unit-testable on its own.  Like the rest of
``ha_provisioner`` the token never appears in config — only the *name* of the
environment variable holding it.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List

from .ha_rest_client import HaRestClient

logger = logging.getLogger(__name__)

#: The assistant id used by HA's built-in "Assist" conversation pipelines.
CONVERSATION_ASSISTANT = "conversation"


#: Registry entries requested per WebSocket call (an extended entry is ~1-2 KB).
REGISTRY_CHUNK_SIZE = 200

class AssistExposureClient:
    """Authenticated client for HA's voice-assistant exposure WebSocket API."""

    def __init__(self, ha_url: str, ha_token_env: str) -> None:
        """ha_url is the resolved HA base URL; ha_token stays env-backed to avoid UI exposure."""
        from providers.secrets import resolve_secret

        self._url = ha_url
        self._token = resolve_secret(ha_token_env)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def list_exposed_entities(
        self, assistant: str = CONVERSATION_ASSISTANT
    ) -> List[str]:
        """Return the entity ids exposed to ``assistant``, sorted.

        HA only includes assistants whose ``should_expose`` is truthy, so the
        membership test below is both the documented and the defensive read.
        """
        result = await self._ws_result({"type": "homeassistant/expose_entity/list"})
        exposed = (result or {}).get("exposed_entities")
        if not isinstance(exposed, dict):
            raise RuntimeError(
                "Unexpected expose_entity/list payload: expected an "
                f"'exposed_entities' mapping, got {type(exposed).__name__}"
            )
        return sorted(
            entity_id
            for entity_id, assistants in exposed.items()
            if isinstance(assistants, dict) and assistants.get(assistant)
        )

    async def list_entity_platforms(self, entity_ids: Iterable[str]) -> Dict[str, str]:
        """Return ``{entity_id: platform}`` for the given entities.

        ``platform`` is the integration domain (e.g. ``intellicenter``,
        ``gecko``, ``hue``) — the field the guard's integration deny rules
        match on.  Entities that exist only as state (no registry entry) are
        absent; callers should default them to an empty platform.

        Uses ``config/entity_registry/get_entries`` for just these ids, in
        chunks: ``config/entity_registry/list`` returns the WHOLE registry,
        which on this instance (~15k entities) is a 9.5 MB frame — over the
        WebSocket client's 4 MB limit (the v1.18.0 startup failure).
        """
        ids = [str(entity_id).strip() for entity_id in entity_ids if str(entity_id).strip()]
        platforms: Dict[str, str] = {}
        for offset in range(0, len(ids), REGISTRY_CHUNK_SIZE):
            chunk = ids[offset : offset + REGISTRY_CHUNK_SIZE]
            result = await self._ws_result(
                {"type": "config/entity_registry/get_entries", "entity_ids": chunk}
            )
            if not isinstance(result, dict):
                raise RuntimeError(
                    "Unexpected entity_registry/get_entries payload: expected a dict, "
                    f"got {type(result).__name__}"
                )
            for entity_id, entry in result.items():
                if not isinstance(entry, dict):
                    continue  # None = no registry entry (state-only entity)
                platforms[str(entity_id)] = str(entry.get("platform") or "").strip().lower()
        return platforms

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def set_exposure(
        self,
        entity_ids: Iterable[str],
        should_expose: bool,
        assistant: str = CONVERSATION_ASSISTANT,
    ) -> int:
        """Expose or un-expose ``entity_ids`` for ``assistant`` in one command.

        Returns the number of entity ids sent.  An empty list is a no-op — HA
        accepts it, but skipping the round trip keeps a clean run free of
        WebSocket traffic.
        """
        ids = [str(entity_id).strip() for entity_id in entity_ids if str(entity_id).strip()]
        if not ids:
            return 0
        logger.info(
            "Setting Assist exposure should_expose=%s for %d entity(s) on %r",
            bool(should_expose),
            len(ids),
            assistant,
        )
        await self._ws_result(
            {
                "type": "homeassistant/expose_entity",
                "assistants": [assistant],
                "entity_ids": ids,
                "should_expose": bool(should_expose),
            }
        )
        return len(ids)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _ws_result(self, message: Dict[str, Any]) -> Any:
        """Send one WebSocket command and return its ``result`` payload.

        Raises ``RuntimeError`` when HA answers ``success: false`` so a failed
        enforcement never looks like a successful one.  HA's error payload
        carries no credentials (the token travels in the auth frame, never in
        a response), so it is safe to surface.
        """
        async with HaRestClient(self._url, self._token) as client:
            response = await client.send_ws_command(message)
        if not isinstance(response, dict):
            raise RuntimeError(
                f"Unexpected WebSocket response for {message.get('type')!r}: "
                f"{type(response).__name__}"
            )
        if not response.get("success"):
            error = response.get("error")
            detail = error if isinstance(error, dict) else {}
            raise RuntimeError(
                f"HA WebSocket command {message.get('type')!r} failed: "
                f"{detail.get('code', 'unknown')} — {detail.get('message', response)}"
            )
        return response.get("result")
