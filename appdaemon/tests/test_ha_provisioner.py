"""Tests for ha_provisioner shared library."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers.ha_provisioner.provisioner import HAProvisioner
from providers.ha_provisioner.ha_rest_client import HaRestClient


def _make_404() -> aiohttp.ClientResponseError:
    """Build a ClientResponseError with status 404."""
    return aiohttp.ClientResponseError(
        request_info=MagicMock(),
        history=(),
        status=404,
        message="Not Found",
    )


def _make_500() -> aiohttp.ClientResponseError:
    """Build a ClientResponseError with status 500."""
    return aiohttp.ClientResponseError(
        request_info=MagicMock(),
        history=(),
        status=500,
        message="Internal Server Error",
    )


# ---------------------------------------------------------------------------
# HaRestClient
# ---------------------------------------------------------------------------

class TestHaRestClient:
    def test_headers_contain_bearer_token(self):
        client = HaRestClient("http://ha:8123", "tok-123", session=MagicMock())
        headers = client._headers()
        assert headers["Authorization"] == "Bearer tok-123"
        assert headers["Content-Type"] == "application/json"

    def test_base_url_trailing_slash_stripped(self):
        client = HaRestClient("http://ha:8123/", "tok", session=MagicMock())
        assert client.base_url == "http://ha:8123"


# ---------------------------------------------------------------------------
# HAProvisioner._helper_slug
# ---------------------------------------------------------------------------

class TestHelperSlug:
    def test_simple_name(self):
        assert HAProvisioner._helper_slug("input_boolean", "My Toggle") == "my_toggle"

    def test_special_characters(self):
        assert HAProvisioner._helper_slug("input_text", "App - Data (v2)") == "app_data_v2"

    def test_consecutive_spaces(self):
        assert HAProvisioner._helper_slug("input_button", "a   b") == "a_b"


# ---------------------------------------------------------------------------
# HAProvisioner.ensure_script
# ---------------------------------------------------------------------------

class TestEnsureScript:
    @pytest.mark.asyncio
    async def test_creates_script_when_missing(self):
        with patch("providers.secrets.resolve_secret", return_value="tok"):
            prov = HAProvisioner(ha_url="http://ha:8123", ha_token_env="TOKEN")
        mock_get = AsyncMock(side_effect=_make_404())
        mock_post = AsyncMock(return_value={})

        with patch.object(HaRestClient, "get", mock_get), \
             patch.object(HaRestClient, "post", mock_post), \
             patch.object(HaRestClient, "close", new_callable=AsyncMock):
            created = await prov.ensure_script("my_relay", {"alias": "Relay"})

        assert created is True
        mock_post.assert_called_once_with(
            "/api/config/script/config/my_relay",
            json={"alias": "Relay"},
        )

    @pytest.mark.asyncio
    async def test_propagates_non_404_errors(self):
        with patch("providers.secrets.resolve_secret", return_value="tok"):
            prov = HAProvisioner(ha_url="http://ha:8123", ha_token_env="TOKEN")
        mock_get = AsyncMock(side_effect=_make_500())

        with patch.object(HaRestClient, "get", mock_get), \
             patch.object(HaRestClient, "post", new_callable=AsyncMock), \
             patch.object(HaRestClient, "close", new_callable=AsyncMock):
            with pytest.raises(aiohttp.ClientResponseError, match="500"):
                await prov.ensure_script("my_relay", {"alias": "Relay"})

    @pytest.mark.asyncio
    async def test_skips_when_script_exists(self):
        with patch("providers.secrets.resolve_secret", return_value="tok"):
            prov = HAProvisioner(ha_url="http://ha:8123", ha_token_env="TOKEN")
        mock_get = AsyncMock(return_value={"entity_id": "script.my_relay", "state": "off"})
        mock_post = AsyncMock()

        with patch.object(HaRestClient, "get", mock_get), \
             patch.object(HaRestClient, "post", mock_post), \
             patch.object(HaRestClient, "close", new_callable=AsyncMock):
            created = await prov.ensure_script("my_relay", {"alias": "Relay"})

        assert created is False
        mock_post.assert_not_called()


# ---------------------------------------------------------------------------
# HAProvisioner.ensure_helper
# ---------------------------------------------------------------------------

class TestEnsureHelper:
    @pytest.mark.asyncio
    async def test_creates_helper_via_config_flow(self):
        with patch("providers.secrets.resolve_secret", return_value="tok"):
            prov = HAProvisioner(ha_url="http://ha:8123", ha_token_env="TOKEN")
        mock_get = AsyncMock(side_effect=_make_404())
        flow_init = {"flow_id": "flow-abc", "type": "form"}
        flow_complete = {"type": "create_entry", "title": "My Toggle"}
        mock_post = AsyncMock(side_effect=[flow_init, flow_complete])

        with patch.object(HaRestClient, "get", mock_get), \
             patch.object(HaRestClient, "post", mock_post), \
             patch.object(HaRestClient, "close", new_callable=AsyncMock):
            created = await prov.ensure_helper(
                "input_boolean", "My Toggle", icon="mdi:toggle"
            )

        assert created is True
        assert mock_post.call_count == 2
        # First call starts the config entry flow
        mock_post.assert_any_call(
            "/api/config/config_entries/flow",
            json={"handler": "input_boolean", "show_advanced_options": False},
        )
        # Second call completes the flow with data
        mock_post.assert_any_call(
            "/api/config/config_entries/flow/flow-abc",
            json={"name": "My Toggle", "icon": "mdi:toggle"},
        )

    @pytest.mark.asyncio
    async def test_skips_when_helper_exists(self):
        with patch("providers.secrets.resolve_secret", return_value="tok"):
            prov = HAProvisioner(ha_url="http://ha:8123", ha_token_env="TOKEN")
        mock_get = AsyncMock(return_value={"entity_id": "input_boolean.my_toggle", "state": "off"})
        mock_post = AsyncMock()

        with patch.object(HaRestClient, "get", mock_get), \
             patch.object(HaRestClient, "post", mock_post), \
             patch.object(HaRestClient, "close", new_callable=AsyncMock):
            created = await prov.ensure_helper("input_boolean", "My Toggle")

        assert created is False
        mock_post.assert_not_called()

    @pytest.mark.asyncio
    async def test_raises_on_unexpected_flow_result(self):
        with patch("providers.secrets.resolve_secret", return_value="tok"):
            prov = HAProvisioner(ha_url="http://ha:8123", ha_token_env="TOKEN")
        mock_get = AsyncMock(side_effect=_make_404())
        flow_init = {"flow_id": "flow-abc", "type": "form"}
        flow_fail = {"type": "abort", "reason": "already_configured"}
        mock_post = AsyncMock(side_effect=[flow_init, flow_fail])

        with patch.object(HaRestClient, "get", mock_get), \
             patch.object(HaRestClient, "post", mock_post), \
             patch.object(HaRestClient, "close", new_callable=AsyncMock):
            with pytest.raises(RuntimeError, match="Unexpected config entry flow"):
                await prov.ensure_helper("input_boolean", "My Toggle")

    @pytest.mark.asyncio
    async def test_raises_on_missing_flow_id(self):
        with patch("providers.secrets.resolve_secret", return_value="tok"):
            prov = HAProvisioner(ha_url="http://ha:8123", ha_token_env="TOKEN")
        mock_get = AsyncMock(side_effect=_make_404())
        mock_post = AsyncMock(return_value={"type": "error"})

        with patch.object(HaRestClient, "get", mock_get), \
             patch.object(HaRestClient, "post", mock_post), \
             patch.object(HaRestClient, "close", new_callable=AsyncMock):
            with pytest.raises(RuntimeError, match="did not return flow_id"):
                await prov.ensure_helper("input_boolean", "Test")
