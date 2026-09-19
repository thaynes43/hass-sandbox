"""Unit tests for AssistExposureClient.

Patches HaRestClient where exposure_client imports it — no real HA, no
WebSocket.  The token env var is a placeholder (security rule S5); it is read
at construction time by ``resolve_secret``.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo_root / "apps"))
sys.path.insert(0, str(_repo_root))

from providers.ha_provisioner.exposure_client import (  # noqa: E402
    CONVERSATION_ASSISTANT,
    AssistExposureClient,
)

_REST_CLIENT_PATH = "providers.ha_provisioner.exposure_client.HaRestClient"


class _FakeHaRestClient:
    """Stand-in for HaRestClient usable as ``async with``.

    ``responses`` is consumed in order, so a test can script a sequence of
    WebSocket replies; a single dict is reused for every call.
    """

    def __init__(self, responses: Any) -> None:
        self.sent: List[Dict[str, Any]] = []
        if isinstance(responses, list):
            self._queue = list(responses)
            self._single = None
        else:
            self._queue = []
            self._single = responses
        self.send_ws_command = AsyncMock(side_effect=self._send)

    async def _send(self, message: Dict[str, Any]) -> Any:
        self.sent.append(message)
        if self._single is not None:
            return self._single
        return self._queue.pop(0)

    async def __aenter__(self) -> "_FakeHaRestClient":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _patch_rest_client(fake: _FakeHaRestClient):
    return patch(_REST_CLIENT_PATH, MagicMock(return_value=fake))


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_client(monkeypatch) -> AssistExposureClient:
    monkeypatch.setenv("TOKEN", "test-token")
    return AssistExposureClient(ha_url="http://ha:8123", ha_token_env="TOKEN")


def _ok(result: Any) -> Dict[str, Any]:
    return {"id": 10, "type": "result", "success": True, "result": result}


# ---------------------------------------------------------------------------
# list_exposed_entities
# ---------------------------------------------------------------------------


def test_list_exposed_entities_returns_only_the_requested_assistant(monkeypatch) -> None:
    """HA's payload is the real shape: ``{entity_id: {assistant: True}}``."""
    fake = _FakeHaRestClient(
        _ok(
            {
                "exposed_entities": {
                    "light.kitchen": {"conversation": True},
                    "lock.front_door": {"cloud.alexa": True},
                    "cover.shades": {"conversation": True, "cloud.alexa": True},
                }
            }
        )
    )
    client = _make_client(monkeypatch)
    with _patch_rest_client(fake):
        exposed = _run(client.list_exposed_entities(CONVERSATION_ASSISTANT))

    assert exposed == ["cover.shades", "light.kitchen"]
    assert fake.sent == [{"type": "homeassistant/expose_entity/list"}]


def test_list_exposed_entities_skips_falsy_and_malformed_records(monkeypatch) -> None:
    fake = _FakeHaRestClient(
        _ok(
            {
                "exposed_entities": {
                    "light.kitchen": {"conversation": True},
                    "light.dead": {"conversation": False},
                    "light.weird": "not-a-dict",
                }
            }
        )
    )
    client = _make_client(monkeypatch)
    with _patch_rest_client(fake):
        assert _run(client.list_exposed_entities()) == ["light.kitchen"]


def test_list_exposed_entities_rejects_an_unexpected_payload(monkeypatch) -> None:
    fake = _FakeHaRestClient(_ok({"something_else": {}}))
    client = _make_client(monkeypatch)
    with _patch_rest_client(fake), pytest.raises(RuntimeError, match="exposed_entities"):
        _run(client.list_exposed_entities())


# ---------------------------------------------------------------------------
# list_entity_platforms
# ---------------------------------------------------------------------------


def test_list_entity_platforms_maps_entity_id_to_platform(monkeypatch) -> None:
    fake = _FakeHaRestClient(
        _ok(
            {
                "sensor.pool_ph": {"entity_id": "sensor.pool_ph", "platform": "IntelliCenter"},
                "light.kitchen": {"entity_id": "light.kitchen", "platform": "hue"},
                "sensor.no_platform": {"entity_id": "sensor.no_platform"},
                "binary_sensor.state_only": None,
            }
        )
    )
    client = _make_client(monkeypatch)
    ids = ["sensor.pool_ph", "light.kitchen", "sensor.no_platform", "binary_sensor.state_only", " "]
    with _patch_rest_client(fake):
        platforms = _run(client.list_entity_platforms(ids))

    assert platforms == {
        "sensor.pool_ph": "intellicenter",
        "light.kitchen": "hue",
        "sensor.no_platform": "",
    }
    # Only the exposed ids are requested — never the whole registry, whose
    # 9.5 MB frame broke the WebSocket client on the live instance (v1.18.0).
    assert fake.sent == [
        {
            "type": "config/entity_registry/get_entries",
            "entity_ids": ["sensor.pool_ph", "light.kitchen", "sensor.no_platform", "binary_sensor.state_only"],
        }
    ]


def test_list_entity_platforms_requests_in_bounded_chunks(monkeypatch) -> None:
    from providers.ha_provisioner.exposure_client import REGISTRY_CHUNK_SIZE

    ids = [f"light.l{i}" for i in range(REGISTRY_CHUNK_SIZE + 5)]
    fake = _FakeHaRestClient(
        [
            _ok({i: {"platform": "hue"} for i in ids[:REGISTRY_CHUNK_SIZE]}),
            _ok({i: {"platform": "mqtt"} for i in ids[REGISTRY_CHUNK_SIZE:]}),
        ]
    )
    client = _make_client(monkeypatch)
    with _patch_rest_client(fake):
        platforms = _run(client.list_entity_platforms(ids))

    assert [len(m["entity_ids"]) for m in fake.sent] == [REGISTRY_CHUNK_SIZE, 5]
    assert all(m["type"] == "config/entity_registry/get_entries" for m in fake.sent)
    assert len(platforms) == len(ids) and platforms[ids[-1]] == "mqtt"


def test_list_entity_platforms_skips_malformed_ids_instead_of_failing_the_chunk(monkeypatch) -> None:
    """HA validates entity_ids all-or-nothing; one bad id must not fail the whole check."""
    fake = _FakeHaRestClient(_ok({"light.kitchen": {"platform": "hue"}}))
    client = _make_client(monkeypatch)
    with _patch_rest_client(fake):
        platforms = _run(
            client.list_entity_platforms(["light.kitchen", "not an id", "nodomain", "light.", ".x", "Light.Kitchen "])
        )
    assert platforms == {"light.kitchen": "hue"}
    assert fake.sent[0]["entity_ids"] == ["light.kitchen"]  # normalised and de-duplicated


def test_list_entity_platforms_with_nothing_exposed_sends_nothing(monkeypatch) -> None:
    fake = _FakeHaRestClient(_ok({}))
    client = _make_client(monkeypatch)
    with _patch_rest_client(fake):
        assert _run(client.list_entity_platforms([])) == {}
    assert fake.sent == []


def test_list_entity_platforms_rejects_a_non_dict_payload(monkeypatch) -> None:
    fake = _FakeHaRestClient(_ok([{"entity_id": "light.kitchen"}]))
    client = _make_client(monkeypatch)
    with _patch_rest_client(fake), pytest.raises(RuntimeError, match="expected a dict"):
        _run(client.list_entity_platforms(["light.kitchen"]))


# ---------------------------------------------------------------------------
# set_exposure
# ---------------------------------------------------------------------------


def test_set_exposure_sends_one_command_with_every_entity(monkeypatch) -> None:
    fake = _FakeHaRestClient(_ok(None))
    client = _make_client(monkeypatch)
    with _patch_rest_client(fake):
        count = _run(
            client.set_exposure(
                ["lock.front_door", " cover.garage_door ", ""], should_expose=False
            )
        )

    assert count == 2
    assert fake.sent == [
        {
            "type": "homeassistant/expose_entity",
            "assistants": ["conversation"],
            "entity_ids": ["lock.front_door", "cover.garage_door"],
            "should_expose": False,
        }
    ]


def test_set_exposure_on_an_empty_list_makes_no_call(monkeypatch) -> None:
    fake = _FakeHaRestClient(_ok(None))
    client = _make_client(monkeypatch)
    with _patch_rest_client(fake):
        assert _run(client.set_exposure([], should_expose=False)) == 0
    assert fake.sent == []


def test_set_exposure_honours_a_non_default_assistant(monkeypatch) -> None:
    fake = _FakeHaRestClient(_ok(None))
    client = _make_client(monkeypatch)
    with _patch_rest_client(fake):
        _run(client.set_exposure(["light.a"], True, assistant="cloud.alexa"))
    assert fake.sent[0]["assistants"] == ["cloud.alexa"]
    assert fake.sent[0]["should_expose"] is True


# ---------------------------------------------------------------------------
# Error envelopes
# ---------------------------------------------------------------------------


def test_an_unsuccessful_command_raises(monkeypatch) -> None:
    """A failed un-expose must never look like a successful one."""
    fake = _FakeHaRestClient(
        {
            "id": 10,
            "type": "result",
            "success": False,
            "error": {"code": "unauthorized", "message": "Unauthorized"},
        }
    )
    client = _make_client(monkeypatch)
    with _patch_rest_client(fake), pytest.raises(RuntimeError, match="unauthorized"):
        _run(client.set_exposure(["lock.front_door"], should_expose=False))


def test_a_non_dict_response_raises(monkeypatch) -> None:
    fake = _FakeHaRestClient("nonsense")
    client = _make_client(monkeypatch)
    with _patch_rest_client(fake), pytest.raises(RuntimeError, match="Unexpected"):
        _run(client.list_exposed_entities())


def test_the_token_is_never_echoed_in_an_error(monkeypatch) -> None:
    fake = _FakeHaRestClient(
        {"id": 10, "type": "result", "success": False, "error": {"code": "x"}}
    )
    client = _make_client(monkeypatch)
    with _patch_rest_client(fake):
        try:
            _run(client.list_exposed_entities())
        except RuntimeError as exc:
            assert "test-token" not in str(exc)
        else:  # pragma: no cover - the call above must raise
            pytest.fail("expected RuntimeError")
