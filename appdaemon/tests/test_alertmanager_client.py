"""Unit tests for AlertmanagerClient.

Mocks aiohttp.ClientSession — no real HTTP and no real Alertmanager required.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

_repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo_root / "apps"))
sys.path.insert(0, str(_repo_root))

from providers.alertmanager.alertmanager_client import (
    AlertmanagerClient,
    AlertmanagerError,
)


# ---------------------------------------------------------------------------
# Helpers — fake aiohttp session/response (async context managers)
# ---------------------------------------------------------------------------

class _FakeResponse:
    """Stand-in for aiohttp.ClientResponse usable as ``async with``."""

    def __init__(self, status: int = 200, text_data: str = "") -> None:
        self.status = status
        self._text_data = text_data

    async def text(self) -> str:
        return self._text_data

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSession:
    """Stand-in for aiohttp.ClientSession usable as ``async with``."""

    def __init__(self, response: _FakeResponse | None = None, exc: Exception | None = None) -> None:
        self._response = response
        self._exc = exc
        self.post_calls: list[tuple[str, dict]] = []

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def post(self, url: str, **kwargs):
        self.post_calls.append((url, kwargs))
        if self._exc is not None:
            raise self._exc
        return self._response


class _RoutingFakeSession(_FakeSession):
    """Session whose outcome depends on the replica URL being posted to.

    ``by_replica`` maps a replica base URL to either a ``_FakeResponse`` or
    an ``Exception`` to raise — so one replica can be down while another
    accepts the batch.
    """

    def __init__(self, by_replica: dict[str, object]) -> None:
        super().__init__()
        self._by_replica = by_replica

    def post(self, url: str, **kwargs):
        self.post_calls.append((url, kwargs))
        outcome = next(
            o for base, o in self._by_replica.items() if url.startswith(base)
        )
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _patch_session(session: _FakeSession):
    return patch(
        "providers.alertmanager.alertmanager_client.aiohttp.ClientSession",
        return_value=session,
    )


def _patch_replicas(client: AlertmanagerClient, urls: list[str]):
    """Pin the resolved replica list (DNS is exercised separately below)."""
    return patch.object(
        client, "_resolve_replica_urls", AsyncMock(return_value=urls)
    )


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _run_with_dns(coro_factory, addrs: list[str] | None = None, exc=None):
    """Run *coro_factory()* on a loop whose ``getaddrinfo`` is faked.

    ``_resolve_replica_urls`` resolves through the running loop, so faking
    the loop's own ``getaddrinfo`` is the seam — no global patching.
    Returns ``(result, getaddrinfo_mock)``.
    """
    loop = asyncio.new_event_loop()
    if exc is not None:
        loop.getaddrinfo = AsyncMock(side_effect=exc)
    else:
        loop.getaddrinfo = AsyncMock(
            return_value=[
                (2, 1, 6, "", (addr, 9093)) for addr in (addrs or [])
            ]
        )
    try:
        return loop.run_until_complete(coro_factory()), loop.getaddrinfo
    finally:
        loop.close()


def _sample_alerts() -> list[dict]:
    return [
        {
            "labels": {"alertname": "ProtectWebsocketDown", "severity": "critical"},
            "annotations": {"summary": "Protect websocket dead"},
            "startsAt": "2026-06-10T12:00:00+00:00",
        },
        {
            "labels": {"alertname": "ComfyUIQueueStuck", "severity": "warning"},
            "annotations": {"summary": "queue not draining"},
            "startsAt": "2026-06-10T12:00:00+00:00",
            "endsAt": "2026-06-10T12:05:00+00:00",
        },
    ]


# ---------------------------------------------------------------------------
# Tests — construction
# ---------------------------------------------------------------------------

class TestConstruction:
    def test_base_url_trailing_slash_stripped(self):
        client = AlertmanagerClient("http://alertmanager.test:9093/")
        assert client.base_url == "http://alertmanager.test:9093"

    def test_base_url_without_trailing_slash_unchanged(self):
        client = AlertmanagerClient("http://alertmanager.test:9093")
        assert client.base_url == "http://alertmanager.test:9093"


# ---------------------------------------------------------------------------
# Tests — post_alerts
# ---------------------------------------------------------------------------

class TestPostAlerts:
    def test_posts_to_v2_alerts_with_exact_payload(self):
        client = AlertmanagerClient("http://alertmanager.test:9093")
        alerts = _sample_alerts()
        session = _FakeSession(response=_FakeResponse(status=200))

        with _patch_session(session):
            _run(client.post_alerts(alerts))

        assert len(session.post_calls) == 1
        url, kwargs = session.post_calls[0]
        assert url == "http://alertmanager.test:9093/api/v2/alerts"
        assert kwargs == {"json": alerts}

    def test_trailing_slash_base_url_builds_clean_url(self):
        client = AlertmanagerClient("http://alertmanager.test:9093/")
        session = _FakeSession(response=_FakeResponse(status=200))

        with _patch_session(session):
            _run(client.post_alerts(_sample_alerts()))

        url, _ = session.post_calls[0]
        assert url == "http://alertmanager.test:9093/api/v2/alerts"

    def test_empty_list_makes_no_http_call(self):
        client = AlertmanagerClient("http://alertmanager.test:9093")
        session = _FakeSession(response=_FakeResponse(status=200))

        with _patch_session(session) as mock_session_cls:
            _run(client.post_alerts([]))

        mock_session_cls.assert_not_called()
        assert session.post_calls == []

    def test_2xx_non_200_is_success(self):
        client = AlertmanagerClient("http://alertmanager.test:9093")
        session = _FakeSession(response=_FakeResponse(status=201))

        with _patch_session(session):
            _run(client.post_alerts(_sample_alerts()))  # should not raise

    def test_timeout_config_passed_to_session(self):
        client = AlertmanagerClient("http://alertmanager.test:9093", timeout_s=7)
        session = _FakeSession(response=_FakeResponse(status=200))

        with _patch_session(session) as mock_session_cls:
            _run(client.post_alerts(_sample_alerts()))

        timeout = mock_session_cls.call_args.kwargs["timeout"]
        assert isinstance(timeout, aiohttp.ClientTimeout)
        assert timeout.total == 7


# ---------------------------------------------------------------------------
# Tests — error handling
# ---------------------------------------------------------------------------

class TestErrors:
    def test_non_2xx_raises_with_status_in_message(self):
        client = AlertmanagerClient("http://alertmanager.test:9093")
        session = _FakeSession(response=_FakeResponse(status=500, text_data="boom"))

        with _patch_session(session):
            with pytest.raises(AlertmanagerError, match="HTTP 500"):
                _run(client.post_alerts(_sample_alerts()))

    def test_non_2xx_message_includes_body(self):
        client = AlertmanagerClient("http://alertmanager.test:9093")
        session = _FakeSession(
            response=_FakeResponse(status=400, text_data="invalid label set")
        )

        with _patch_session(session):
            with pytest.raises(AlertmanagerError, match="invalid label set"):
                _run(client.post_alerts(_sample_alerts()))

    def test_300_status_is_an_error(self):
        client = AlertmanagerClient("http://alertmanager.test:9093")
        session = _FakeSession(response=_FakeResponse(status=300))

        with _patch_session(session):
            with pytest.raises(AlertmanagerError, match="HTTP 300"):
                _run(client.post_alerts(_sample_alerts()))

    def test_http_error_not_double_wrapped(self):
        """The HTTP-status AlertmanagerError must re-raise as-is, not get
        re-wrapped into the generic 'POST failed' message."""
        client = AlertmanagerClient("http://alertmanager.test:9093")
        session = _FakeSession(response=_FakeResponse(status=503, text_data=""))

        with _patch_session(session):
            with pytest.raises(AlertmanagerError) as excinfo:
                _run(client.post_alerts(_sample_alerts()))

        assert "POST failed" not in str(excinfo.value)
        assert "HTTP 503" in str(excinfo.value)

    def test_connection_error_wrapped(self):
        client = AlertmanagerClient("http://alertmanager.test:9093")
        session = _FakeSession(exc=aiohttp.ClientError("connection refused"))

        with _patch_session(session):
            with pytest.raises(AlertmanagerError, match="POST failed"):
                _run(client.post_alerts(_sample_alerts()))

    def test_connection_error_chains_cause(self):
        client = AlertmanagerClient("http://alertmanager.test:9093")
        original = aiohttp.ClientError("connection refused")
        session = _FakeSession(exc=original)

        with _patch_session(session):
            with pytest.raises(AlertmanagerError) as excinfo:
                _run(client.post_alerts(_sample_alerts()))

        assert excinfo.value.__cause__ is original

    def test_timeout_wrapped(self):
        client = AlertmanagerClient("http://alertmanager.test:9093")
        session = _FakeSession(exc=asyncio.TimeoutError())

        with _patch_session(session):
            with pytest.raises(AlertmanagerError, match="POST failed"):
                _run(client.post_alerts(_sample_alerts()))


# ---------------------------------------------------------------------------
# Tests — replica fan-out
# ---------------------------------------------------------------------------

REPLICA_A = "http://10.42.0.1:9093"
REPLICA_B = "http://10.42.0.2:9093"


class TestReplicaFanOut:
    """Alertmanager HA pairs gossip silences and the notification log — not
    the alert set.  A poster that hits only one replica leaves the other with
    a partial view, dedup never lines up and both replicas page (2026-08-31:
    one incident, two page streams).  So every replica gets every batch.
    """

    def test_posts_the_batch_to_every_replica(self):
        client = AlertmanagerClient("http://alertmanager-operated.test:9093")
        alerts = _sample_alerts()
        session = _FakeSession(response=_FakeResponse(status=200))

        with _patch_session(session), _patch_replicas(client, [REPLICA_A, REPLICA_B]):
            _run(client.post_alerts(alerts))

        assert sorted(u for u, _ in session.post_calls) == [
            f"{REPLICA_A}/api/v2/alerts",
            f"{REPLICA_B}/api/v2/alerts",
        ]
        # Identical payload to each — same alert on every replica.
        assert [kw for _, kw in session.post_calls] == [{"json": alerts}] * 2

    def test_one_replica_down_still_succeeds(self):
        """A single replica refusing the batch must never lose the page."""
        client = AlertmanagerClient("http://alertmanager-operated.test:9093")
        session = _RoutingFakeSession(
            {
                REPLICA_A: _FakeResponse(status=200),
                REPLICA_B: _FakeResponse(status=500, text_data="boom"),
            }
        )

        with _patch_session(session), _patch_replicas(client, [REPLICA_A, REPLICA_B]):
            _run(client.post_alerts(_sample_alerts()))  # must not raise

        assert len(session.post_calls) == 2

    def test_one_replica_unreachable_still_succeeds(self):
        client = AlertmanagerClient("http://alertmanager-operated.test:9093")
        session = _RoutingFakeSession(
            {
                REPLICA_A: aiohttp.ClientError("connection refused"),
                REPLICA_B: _FakeResponse(status=200),
            }
        )

        with _patch_session(session), _patch_replicas(client, [REPLICA_A, REPLICA_B]):
            _run(client.post_alerts(_sample_alerts()))  # must not raise

        assert len(session.post_calls) == 2

    def test_all_replicas_failing_raises(self):
        client = AlertmanagerClient("http://alertmanager-operated.test:9093")
        session = _RoutingFakeSession(
            {
                REPLICA_A: _FakeResponse(status=500, text_data="boom"),
                REPLICA_B: aiohttp.ClientError("connection refused"),
            }
        )

        with _patch_session(session), _patch_replicas(client, [REPLICA_A, REPLICA_B]):
            with pytest.raises(AlertmanagerError, match="all 2 replica"):
                _run(client.post_alerts(_sample_alerts()))

    def test_single_replica_keeps_the_old_error_contract(self):
        """One address = the pre-fan-out client, error messages included."""
        client = AlertmanagerClient("http://alertmanager.test:9093")
        session = _FakeSession(response=_FakeResponse(status=503))

        with _patch_session(session), _patch_replicas(client, [client.base_url]):
            with pytest.raises(AlertmanagerError) as excinfo:
                _run(client.post_alerts(_sample_alerts()))

        assert "HTTP 503" in str(excinfo.value)
        assert "replica" not in str(excinfo.value)


class TestReplicaResolution:
    def test_multiple_addresses_become_per_replica_urls(self):
        client = AlertmanagerClient("http://alertmanager-operated.test:9093")

        urls, getaddrinfo = _run_with_dns(
            client._resolve_replica_urls, addrs=["10.42.0.2", "10.42.0.1"]
        )

        # Sorted for a stable post order across resolutions.
        assert urls == [REPLICA_A, REPLICA_B]
        getaddrinfo.assert_awaited_once_with("alertmanager-operated.test", 9093)

    def test_single_address_falls_back_to_the_base_url(self):
        client = AlertmanagerClient("http://alertmanager.test:9093")

        urls, _ = _run_with_dns(client._resolve_replica_urls, addrs=["10.42.0.1"])

        assert urls == ["http://alertmanager.test:9093"]

    def test_duplicate_addresses_are_deduplicated(self):
        """One address returned once per socket type is still one replica."""
        client = AlertmanagerClient("http://alertmanager.test:9093")

        urls, _ = _run_with_dns(
            client._resolve_replica_urls, addrs=["10.42.0.1", "10.42.0.1"]
        )

        assert urls == ["http://alertmanager.test:9093"]

    def test_ipv6_addresses_are_bracketed(self):
        client = AlertmanagerClient("http://alertmanager-operated.test:9093")

        urls, _ = _run_with_dns(
            client._resolve_replica_urls, addrs=["fd00::1", "fd00::2"]
        )

        assert urls == ["http://[fd00::1]:9093", "http://[fd00::2]:9093"]

    def test_https_without_a_port_resolves_on_443(self):
        client = AlertmanagerClient("https://alertmanager-operated.test")

        urls, getaddrinfo = _run_with_dns(
            client._resolve_replica_urls, addrs=["10.42.0.1", "10.42.0.2"]
        )

        getaddrinfo.assert_awaited_once_with("alertmanager-operated.test", 443)
        assert urls == ["https://10.42.0.1:443", "https://10.42.0.2:443"]

    def test_ip_literal_host_is_never_resolved(self):
        client = AlertmanagerClient("http://10.42.0.1:9093")

        urls, getaddrinfo = _run_with_dns(client._resolve_replica_urls, addrs=[])

        assert urls == ["http://10.42.0.1:9093"]
        getaddrinfo.assert_not_awaited()

    def test_resolution_failure_falls_back_to_the_base_url(self):
        """DNS trouble must never block an alert post."""
        client = AlertmanagerClient("http://alertmanager-operated.test:9093")

        urls, _ = _run_with_dns(
            client._resolve_replica_urls, exc=OSError("Name or service not known")
        )

        assert urls == ["http://alertmanager-operated.test:9093"]
