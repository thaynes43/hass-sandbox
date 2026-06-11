"""HaAdminClient — admin-level HA REST operations beyond provisioning.

Wraps :class:`HaRestClient` with config-entry inspection/reload and
server-side template rendering.  Mirrors :class:`HAProvisioner`'s
construction pattern (``ha_url`` + ``ha_token_env``) and, like the
provisioner, requires a long-lived admin access token.

Used by health checkers that need to discover integration entities at
runtime (``render_template`` + ``integration_entities``) or heal a wedged
integration by reloading its config entry.  Reloading via REST (rather
than ``call_service``) keeps the timeout under our control — AppDaemon
cancels service calls after ~60s.
"""

from __future__ import annotations

import json
import logging
from typing import Any, List, Optional

from .ha_rest_client import HaRestClient

logger = logging.getLogger(__name__)


class HaAdminClient:
    """Authenticated client for HA config-entry and template REST APIs."""

    def __init__(self, ha_url: str, ha_token_env: str) -> None:
        """ha_url is the resolved HA base URL; ha_token stays env-backed to avoid UI exposure."""
        from providers.secrets import resolve_secret

        self._url = ha_url
        self._token = resolve_secret(ha_token_env)

    async def list_config_entries(
        self, domain: Optional[str] = None
    ) -> List[dict]:
        """Return config entries, optionally filtered by integration domain.

        Each entry includes ``entry_id``, ``domain``, ``title``, ``state``
        (e.g. ``loaded``, ``not_loaded``) and ``source`` (e.g. ``user``,
        ``ignore``).
        """
        params = {"domain": domain} if domain else None
        async with HaRestClient(self._url, self._token) as client:
            entries = await client.get(
                "/api/config/config_entries/entry", params=params
            )
        if not isinstance(entries, list):
            raise RuntimeError(
                f"Unexpected config entries payload: {type(entries).__name__}"
            )
        return entries

    async def reload_config_entry(self, entry_id: str) -> Any:
        """Reload a config entry by ID and return HA's response payload."""
        logger.info("Reloading config entry %s", entry_id)
        async with HaRestClient(self._url, self._token) as client:
            return await client.post(
                f"/api/config/config_entries/entry/{entry_id}/reload"
            )

    async def render_template(self, template: str) -> str:
        """Render a Jinja2 template server-side and return the result as text.

        HA returns the rendered template as plain text; if a proxy or HA
        version returns parsed JSON instead, it is re-serialised so callers
        always receive a string.
        """
        async with HaRestClient(self._url, self._token) as client:
            result = await client.post("/api/template", json={"template": template})
        if isinstance(result, str):
            return result
        return json.dumps(result)
