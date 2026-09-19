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
    primitive HA offers.  Requires an admin token.  Validated all-or-nothing,
    so malformed ids are filtered out here and reported back to the caller in
    an :class:`ExposureChange` rather than silently dropped.

``config/entity_registry/get_entries``
    Takes the required ``entity_ids`` list and returns ``{entity_id: entry}``
    with the extended registry entry, or ``None`` for an entity that has no
    registry entry.  Called in chunks for exactly the exposed ids — never
    ``config/entity_registry/list``, whose whole-registry reply is too large for
    one WebSocket frame on this instance.  Only ``platform`` (the supplying
    integration) is used: the entry's ``device_class`` is merely the user
    override, so callers that need the effective class read it from state.

All of it is plain ``aiohttp`` through :class:`HaRestClient`; no AppDaemon
import, so it is unit-testable on its own.  Like the rest of
``ha_provisioner`` the token never appears in config — only the *name* of the
environment variable holding it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List

from .ha_rest_client import HaRestClient

logger = logging.getLogger(__name__)

#: The assistant id used by HA's built-in "Assist" conversation pipelines.
CONVERSATION_ASSISTANT = "conversation"


@dataclass
class ExposureChange:
    """What :meth:`AssistExposureClient.set_exposure` actually did.

    ``sent`` holds the normalised ids that went to Home Assistant in the
    command it accepted; ``skipped`` holds the ids left out because they are
    not a well-formed ``domain.object_id`` and would have made HA reject the
    whole batch.

    Both are **normalised** (``strip().lower()``, de-duplicated, order
    preserved), so a caller that normalised its own ids the same way can test
    membership directly.

    Returning this rather than a count is the point: a caller that assumes
    "no exception means everything applied" will report an entity as
    un-exposed while it is still exposed — which on this app's durable
    enforcement record is a false all-clear on a security boundary.

    There is deliberately no ``__len__`` or ``__bool__``: collapsing this back
    to one number is the habit that produced the bug it exists to prevent.
    Callers read ``sent`` and ``skipped``.
    """

    sent: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)


#: Registry entries requested per WebSocket call (an extended entry is ~1-2 KB,
#: so 200 stays far below the 4 MB frame limit).  Each call opens and
#: authenticates its own WebSocket, so do not shrink this without reason: the
#: cost of a smaller chunk is one more connection per chunk, every check.
REGISTRY_CHUNK_SIZE = 200

#: HA validates ``entity_ids`` all-or-nothing, so one malformed id would fail the
#: whole chunk — and with it the whole check.  Ids that do not look like
#: ``domain.object_id`` are skipped here instead (they get an empty platform).
_ENTITY_ID_RE = re.compile(r"^(?!.+__)(?!_)[\da-z_]+(?<!_)\.(?!_)[\da-z_]+(?<!_)$")

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

        Ids are normalised (``strip().lower()``, de-duplicated) before the
        request and the returned dict is keyed by the NORMALISED id — callers
        must look results up with the same normalisation.

        Uses ``config/entity_registry/get_entries`` for just these ids, in
        chunks: ``config/entity_registry/list`` returns the WHOLE registry,
        which on this instance (~15k entities) is a 9.5 MB frame — over the
        WebSocket client's 4 MB limit (the v1.18.0 startup failure).
        """
        candidates = [str(entity_id).strip().lower() for entity_id in entity_ids]
        ids = list(dict.fromkeys(text for text in candidates if _ENTITY_ID_RE.match(text)))
        skipped = sorted({text for text in candidates if text and not _ENTITY_ID_RE.match(text)})
        if skipped:
            logger.warning(
                "Skipping %d malformed entity id(s) in the registry lookup (no platform "
                "will be known for them): %s",
                len(skipped),
                ", ".join(skipped[:10]),
            )
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
    ) -> ExposureChange:
        """Expose or un-expose ``entity_ids`` for ``assistant`` in one command.

        Returns an :class:`ExposureChange` naming what was ``sent`` and what
        was ``skipped`` — **not** a count.  The distinction is load-bearing:
        ids are normalised and de-duplicated like the read path, and malformed
        ones are left out (HA validates ``entity_ids`` all-or-nothing, so one
        bad id would make it reject the whole batch and nothing at all would
        change).  A caller that treats "no exception" as "everything applied"
        would then report a still-exposed entity as handled.

        An empty ``sent`` is a no-op: HA accepts an empty list, but skipping
        the round trip keeps a clean run free of WebSocket traffic.  ``skipped``
        is still populated, so the caller can report those ids.
        """
        candidates = [str(entity_id).strip().lower() for entity_id in entity_ids]
        ids = list(dict.fromkeys(text for text in candidates if _ENTITY_ID_RE.match(text)))
        malformed = sorted({text for text in candidates if text and not _ENTITY_ID_RE.match(text)})
        if malformed:
            # HA validates ``entity_ids`` all-or-nothing: one malformed id would
            # make it reject the whole command, and then NOTHING is un-exposed
            # that run — a garage opener in the same batch included.
            logger.warning(
                "Leaving %d malformed entity id(s) out of the exposure change (HA would "
                "reject the whole batch): %s",
                len(malformed),
                ", ".join(malformed[:10]),
            )
        if not ids:
            return ExposureChange(sent=[], skipped=malformed)
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
        # Only reached when HA answered success — _ws_result raises otherwise —
        # so every id in `ids` really was applied.
        return ExposureChange(sent=ids, skipped=malformed)

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
