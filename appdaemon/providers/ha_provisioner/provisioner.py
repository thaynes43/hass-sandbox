"""HAProvisioner — auto-create HA scripts and helpers from AppDaemon.

Usage from an AppDaemon app::

    from ha_provisioner import HAProvisioner

    async def _async_startup(self):
        prov = HAProvisioner(
            ha_url=self.args["ha_url"],
            ha_token=self.args["ha_token"],
        )
        await prov.ensure_script("my_app_relay", { ...config... })
        await prov.ensure_helper("input_boolean", "My Toggle", icon="mdi:toggle")

The provisioner is idempotent — calling ``ensure_*`` when the entity
already exists is a no-op (logged at DEBUG).
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from .ha_rest_client import HaRestClient

logger = logging.getLogger(__name__)


class HAProvisioner:
    """Create HA entities via the REST API if they don't already exist."""

    def __init__(self, ha_url: str, ha_token_env: str) -> None:
        """ha_url comes from !secret (secrets.yaml); ha_token from env to avoid UI exposure."""
        from providers.secrets import resolve_secret

        self._url = ha_url
        self._token = resolve_secret(ha_token_env)

    async def ensure_script(self, script_id: str, config: dict[str, Any]) -> bool:
        """Ensure ``script.<script_id>`` exists, creating it if missing.

        Returns True if the script was created, False if it already existed.
        """
        entity_id = f"script.{script_id}"
        async with HaRestClient(self._url, self._token) as client:
            if await self._entity_exists(client, entity_id):
                logger.debug("Script %s already exists — skipping", entity_id)
                return False

            logger.info("Creating script %s", entity_id)
            await client.post(
                f"/api/config/script/config/{script_id}",
                json=config,
            )
            logger.info("Script %s created successfully", entity_id)
            return True

    async def ensure_helper(
        self,
        helper_type: str,
        name: str,
        **kwargs: Any,
    ) -> bool:
        """Ensure a helper entity exists, creating it via Config Entry Flow if missing.

        Supported helper_type values: ``input_boolean``, ``input_text``,
        ``input_number``, ``input_select``, ``input_button``, ``input_datetime``,
        ``counter``, ``timer``.

        Returns True if the helper was created, False if it already existed.
        """
        slug = self._helper_slug(helper_type, name)
        entity_id = f"{helper_type}.{slug}"

        async with HaRestClient(self._url, self._token) as client:
            if await self._entity_exists(client, entity_id):
                logger.debug("Helper %s already exists — skipping", entity_id)
                return False

            logger.info("Creating helper %s (type=%s)", entity_id, helper_type)
            flow = await client.post(
                "/api/config/config_entries/flow",
                json={"handler": helper_type, "show_advanced_options": False},
            )
            flow_id = flow.get("flow_id")
            if not flow_id:
                raise RuntimeError(f"Config entry flow did not return flow_id: {flow}")

            step_data: dict[str, Any] = {"name": name, **kwargs}
            result = await client.post(
                f"/api/config/config_entries/flow/{flow_id}",
                json=step_data,
            )
            if result.get("type") == "create_entry":
                logger.info("Helper %s created successfully", entity_id)
                return True

            raise RuntimeError(
                f"Unexpected config entry flow result for {entity_id}: {result}"
            )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    async def _entity_exists(client: HaRestClient, entity_id: str) -> bool:
        """Check if an entity exists in HA by querying its state.

        Returns False only for HTTP 404 (entity genuinely missing).
        Re-raises on network errors, auth failures, and other HTTP errors
        so the caller can distinguish "not found" from "HA unreachable".
        """
        try:
            await client.get(f"/api/states/{entity_id}")
            return True
        except aiohttp.ClientResponseError as exc:
            if exc.status == 404:
                return False
            raise

    @staticmethod
    def _helper_slug(helper_type: str, name: str) -> str:
        """Derive the expected entity slug from a helper name.

        HA generates entity IDs by lower-casing the name and replacing
        non-alphanumeric chars with underscores.
        """
        slug = name.lower()
        out: list[str] = []
        for ch in slug:
            out.append(ch if ch.isalnum() else "_")
        cleaned = "_".join(part for part in "".join(out).split("_") if part)
        return cleaned
