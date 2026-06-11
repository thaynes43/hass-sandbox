"""Unit tests for HaAdminClient.

Patches HaRestClient where ha_admin_client imports it — no real HA access.
The HA token env var is set via monkeypatch (resolve_secret runs at
construction time).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo_root / "apps"))
sys.path.insert(0, str(_repo_root))

from providers.ha_provisioner.ha_admin_client import HaAdminClient

_REST_CLIENT_PATH = "providers.ha_provisioner.ha_admin_client.HaRestClient"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeHaRestClient:
    """Stand-in for HaRestClient usable as ``async with``."""

    def __init__(self, get_result=None, post_result=None):
        self.get = AsyncMock(return_value=get_result)
        self.post = AsyncMock(return_value=post_result)

    async def __aenter__(self) -> "_FakeHaRestClient":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _patch_rest_client(fake: _FakeHaRestClient):
    return patch(_REST_CLIENT_PATH, MagicMock(return_value=fake))


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_client(monkeypatch) -> HaAdminClient:
    monkeypatch.setenv("TOKEN", "test-token")
    return HaAdminClient(ha_url="http://ha:8123", ha_token_env="TOKEN")


# ---------------------------------------------------------------------------
# Tests — construction / secret resolution
# ---------------------------------------------------------------------------

class TestConstruction:
    def test_missing_env_var_raises_value_error(self, monkeypatch):
        monkeypatch.delenv("MISSING_TOKEN", raising=False)
        with pytest.raises(ValueError, match="MISSING_TOKEN"):
            HaAdminClient(ha_url="http://ha:8123", ha_token_env="MISSING_TOKEN")

    def test_resolved_token_passed_to_rest_client(self, monkeypatch):
        client = _make_client(monkeypatch)
        fake = _FakeHaRestClient(get_result=[])

        with _patch_rest_client(fake) as mock_cls:
            _run(client.list_config_entries())

        mock_cls.assert_called_once_with("http://ha:8123", "test-token")


# ---------------------------------------------------------------------------
# Tests — list_config_entries
# ---------------------------------------------------------------------------

class TestListConfigEntries:
    def test_with_domain_passes_params(self, monkeypatch):
        client = _make_client(monkeypatch)
        entries = [
            {"entry_id": "abc123", "domain": "unifiprotect", "state": "loaded"},
        ]
        fake = _FakeHaRestClient(get_result=entries)

        with _patch_rest_client(fake):
            result = _run(client.list_config_entries(domain="unifiprotect"))

        assert result == entries
        fake.get.assert_awaited_once_with(
            "/api/config/config_entries/entry",
            params={"domain": "unifiprotect"},
        )

    def test_without_domain_passes_none_params(self, monkeypatch):
        client = _make_client(monkeypatch)
        fake = _FakeHaRestClient(get_result=[])

        with _patch_rest_client(fake):
            result = _run(client.list_config_entries())

        assert result == []
        fake.get.assert_awaited_once_with(
            "/api/config/config_entries/entry",
            params=None,
        )

    def test_returns_full_list(self, monkeypatch):
        client = _make_client(monkeypatch)
        entries = [
            {"entry_id": "e1", "domain": "unifiprotect", "state": "loaded"},
            {"entry_id": "e2", "domain": "geckospa", "state": "not_loaded"},
        ]
        fake = _FakeHaRestClient(get_result=entries)

        with _patch_rest_client(fake):
            result = _run(client.list_config_entries())

        assert result == entries

    def test_non_list_payload_raises_runtime_error(self, monkeypatch):
        client = _make_client(monkeypatch)
        fake = _FakeHaRestClient(get_result={"message": "unauthorized"})

        with _patch_rest_client(fake):
            with pytest.raises(RuntimeError, match="Unexpected config entries payload"):
                _run(client.list_config_entries())

    def test_non_list_payload_error_names_type(self, monkeypatch):
        client = _make_client(monkeypatch)
        fake = _FakeHaRestClient(get_result="oops")

        with _patch_rest_client(fake):
            with pytest.raises(RuntimeError, match="str"):
                _run(client.list_config_entries())


# ---------------------------------------------------------------------------
# Tests — reload_config_entry
# ---------------------------------------------------------------------------

class TestReloadConfigEntry:
    def test_posts_to_reload_endpoint(self, monkeypatch):
        client = _make_client(monkeypatch)
        fake = _FakeHaRestClient(post_result={"require_restart": False})

        with _patch_rest_client(fake):
            result = _run(client.reload_config_entry("abc123"))

        assert result == {"require_restart": False}
        fake.post.assert_awaited_once_with(
            "/api/config/config_entries/entry/abc123/reload"
        )


# ---------------------------------------------------------------------------
# Tests — render_template
# ---------------------------------------------------------------------------

class TestRenderTemplate:
    def test_posts_template_payload(self, monkeypatch):
        client = _make_client(monkeypatch)
        fake = _FakeHaRestClient(post_result="rendered text")

        template = "{{ integration_entities('unifiprotect') }}"
        with _patch_rest_client(fake):
            result = _run(client.render_template(template))

        assert result == "rendered text"
        fake.post.assert_awaited_once_with(
            "/api/template", json={"template": template}
        )

    def test_string_result_returned_as_is(self, monkeypatch):
        client = _make_client(monkeypatch)
        fake = _FakeHaRestClient(post_result="['binary_sensor.a', 'binary_sensor.b']")

        with _patch_rest_client(fake):
            result = _run(client.render_template("{{ states.binary_sensor }}"))

        assert result == "['binary_sensor.a', 'binary_sensor.b']"

    def test_parsed_list_result_serialised_to_json_string(self, monkeypatch):
        client = _make_client(monkeypatch)
        fake = _FakeHaRestClient(post_result=["binary_sensor.a", "binary_sensor.b"])

        with _patch_rest_client(fake):
            result = _run(client.render_template("{{ ... }}"))

        assert isinstance(result, str)
        assert result == '["binary_sensor.a", "binary_sensor.b"]'

    def test_parsed_dict_result_serialised_to_json_string(self, monkeypatch):
        client = _make_client(monkeypatch)
        fake = _FakeHaRestClient(post_result={"queue": 3})

        with _patch_rest_client(fake):
            result = _run(client.render_template("{{ ... }}"))

        assert isinstance(result, str)
        assert result == '{"queue": 3}'
