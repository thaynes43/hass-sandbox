"""Unit tests for VoiceHealthChecker and its probes (wyoming_check, http_reachable_check).

The Wyoming probe is exercised against a throwaway asyncio server on 127.0.0.1
(no external network); aiohttp is mocked for the reachability probe.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from conftest import closing_create_task

# ---------------------------------------------------------------------------
# Mock hassapi before importing the app
# ---------------------------------------------------------------------------
mock_hass = MagicMock()
mock_hass.Hass = type("_MockHass", (), {"__init__": lambda self, *a, **kw: None})
sys.modules["hassapi"] = mock_hass

_repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo_root / "apps"))
sys.path.insert(0, str(_repo_root / "apps" / "health_checks"))
sys.path.insert(0, str(_repo_root))

from health_checks.checker_apps.voice_health_checker.voice_health_checker import (
    VoiceHealthChecker,
)
from shared.check_utils import http_reachable_check, wyoming_check


def _run(coro):
    """Run a coroutine in a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# A minimal Wyoming server
# ---------------------------------------------------------------------------

ASR_INFO = {
    "asr": [{
        "name": "faster-whisper",
        "installed": True,
        "models": [{"name": "nemo-parakeet-tdt-0.6b-v2", "installed": True}],
    }],
    "tts": [],
}
TTS_INFO = {
    "asr": [],
    "tts": [{
        "name": "kokoro",
        "installed": True,
        "voices": [{"name": f"v{i}", "installed": True} for i in range(54)],
    }],
}


async def _probe(reply, service: str, *, timeout_s: float = 2):
    """Start a server that answers one connection with *reply*, run wyoming_check.

    *reply* is a callable (reader, writer, request_line) -> awaitable, so each
    test controls exactly what goes back on the wire.
    """
    requests = []
    handlers = []

    async def handle(reader, writer):
        handlers.append(asyncio.current_task())
        line = await reader.readline()
        requests.append(line)
        try:
            await reply(reader, writer, line)
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        result = await wyoming_check("127.0.0.1", port, service, timeout_s=timeout_s)
    finally:
        server.close()
        for task in handlers:
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)
        await server.wait_closed()
    return result, requests


def _info_reply(data: Dict[str, Any], *, inline: bool = False, event_type: str = "info"):
    async def reply(reader, writer, line):
        if inline:
            writer.write((json.dumps({"type": event_type, "data": data}) + "\n").encode())
        else:
            body = json.dumps(data).encode()
            writer.write(
                (json.dumps({"type": event_type, "version": "1.7.2", "data_length": len(body)}) + "\n").encode()
                + body
            )
        await writer.drain()
    return reply


class TestWyomingCheck:
    def test_asr_ok_reports_model(self):
        result, requests = _run(_probe(_info_reply(ASR_INFO), "asr"))
        assert result["status"] == "ok"
        assert result["detail"].startswith("nemo-parakeet-tdt-0.6b-v2 · ")
        assert json.loads(requests[0]) == {"type": "describe", "data": {}}

    def test_tts_ok_reports_voice_count(self):
        result, _ = _run(_probe(_info_reply(TTS_INFO), "tts"))
        assert result["status"] == "ok"
        assert result["detail"].startswith("kokoro, 54 voices · ")

    def test_inline_data_header_is_accepted(self):
        result, _ = _run(_probe(_info_reply(ASR_INFO, inline=True), "asr"))
        assert result["status"] == "ok"

    def test_wrong_service_is_critical(self):
        """A TTS-only server does not satisfy a speech-to-text check."""
        result, _ = _run(_probe(_info_reply(TTS_INFO), "asr"))
        assert result == {"status": "critical", "detail": "no speech-to-text installed"}

    def test_program_not_installed_is_critical(self):
        info = {"asr": [dict(ASR_INFO["asr"][0], installed=False)]}
        result, _ = _run(_probe(_info_reply(info), "asr"))
        assert result["status"] == "critical"

    def test_asr_without_installed_model_is_critical(self):
        info = {"asr": [dict(ASR_INFO["asr"][0], models=[{"name": "m", "installed": False}])]}
        result, _ = _run(_probe(_info_reply(info), "asr"))
        assert result["status"] == "critical"
        assert "no model installed" in result["detail"]

    def test_tts_without_voices_is_critical(self):
        info = {"tts": [dict(TTS_INFO["tts"][0], voices=[])]}
        result, _ = _run(_probe(_info_reply(info), "tts"))
        assert result["status"] == "critical"
        assert "no voice installed" in result["detail"]

    def test_unexpected_event_type_is_critical(self):
        result, _ = _run(_probe(_info_reply(ASR_INFO, event_type="error"), "asr"))
        assert result["status"] == "critical"
        assert "unexpected reply" in result["detail"]

    def test_closed_without_reply_is_critical(self):
        async def reply(reader, writer, line):
            return None
        result, _ = _run(_probe(reply, "asr"))
        assert result == {"status": "critical", "detail": "Connection error: closed without a reply"}

    def test_garbage_reply_is_critical(self):
        async def reply(reader, writer, line):
            writer.write(b"not json\n")
            await writer.drain()
        result, _ = _run(_probe(reply, "asr"))
        assert result["status"] == "critical"
        assert result["detail"].startswith("bad reply")

    def test_truncated_data_is_critical(self):
        async def reply(reader, writer, line):
            writer.write((json.dumps({"type": "info", "data_length": 500}) + "\n").encode() + b"{}")
            await writer.drain()
        result, _ = _run(_probe(reply, "asr"))
        assert result["status"] == "critical"
        assert result["detail"].startswith("Connection error")

    def test_silent_server_times_out(self):
        async def reply(reader, writer, line):
            await asyncio.sleep(5)
        result, _ = _run(_probe(reply, "asr", timeout_s=0.3))
        assert result == {"status": "critical", "detail": "timeout"}

    def test_refused_connection_is_critical(self):
        async def go():
            server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            server.close()
            await server.wait_closed()
            return await wyoming_check("127.0.0.1", port, "asr", timeout_s=1)
        result = _run(go())
        assert result["status"] == "critical"
        assert result["detail"].startswith("Connection error")

    def test_bad_service_raises(self):
        with pytest.raises(ValueError):
            _run(wyoming_check("127.0.0.1", 1, "wake"))


# ---------------------------------------------------------------------------
# http_reachable_check
# ---------------------------------------------------------------------------


def _session_answering(status: int = 200, exc: Exception | None = None) -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=None)
    mock_session = MagicMock()
    if exc is not None:
        mock_session.get = MagicMock(side_effect=exc)
    else:
        mock_session.get = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    return mock_session


class TestHttpReachableCheck:
    @pytest.mark.parametrize("status", [200, 401, 403, 404])
    def test_below_500_is_reachable(self, status):
        with patch("shared.check_utils.aiohttp.ClientSession", return_value=_session_answering(status)):
            result = _run(http_reachable_check("https://api.example.com/v1/models", label="OpenAI reachable"))
        assert result["status"] == "ok"
        assert result["detail"].startswith("OpenAI reachable · ")

    def test_5xx_is_critical(self):
        with patch("shared.check_utils.aiohttp.ClientSession", return_value=_session_answering(503)):
            result = _run(http_reachable_check("https://api.example.com"))
        assert result == {"status": "critical", "detail": "HTTP 503"}

    def test_timeout_is_critical(self):
        with patch("shared.check_utils.aiohttp.ClientSession", return_value=_session_answering(exc=asyncio.TimeoutError())):
            result = _run(http_reachable_check("https://api.example.com"))
        assert result == {"status": "critical", "detail": "timeout"}

    def test_connection_error_is_critical(self):
        err = aiohttp.ClientError("Connection refused")
        with patch("shared.check_utils.aiohttp.ClientSession", return_value=_session_answering(exc=err)):
            result = _run(http_reachable_check("https://api.example.com"))
        assert result["status"] == "critical"
        assert result["detail"].startswith("Connection error")


# ---------------------------------------------------------------------------
# VoiceHealthChecker
# ---------------------------------------------------------------------------

MODULE = "health_checks.checker_apps.voice_health_checker.voice_health_checker"

DEFAULT_ARGS: Dict[str, Any] = {
    "checker_id": "voice",
    "checker_name": "Voice",
    "check_interval_s": 120,
    "check_timeout_s": 5,
    "checks": [
        {"name": "Speech to Text", "type": "wyoming", "service": "asr", "host": "whisper", "port": 10300},
        {"name": "Text to Speech", "type": "wyoming", "service": "tts", "host": "kokoro", "port": 10210},
        {
            "name": "Assistant",
            "type": "agent",
            "entity_id": "conversation.phone_assist",
            "reachability_url": "https://api.openai.com/v1/models",
            "reachability_name": "OpenAI",
            "dependency": "cloud",
        },
    ],
}


def _make_app(extra_args: dict | None = None, states: dict | None = None) -> VoiceHealthChecker:
    app = VoiceHealthChecker(MagicMock(), MagicMock())
    args = dict(DEFAULT_ARGS)
    if extra_args:
        args.update(extra_args)
    app.args = args

    lookup = {"conversation.phone_assist": "2026-09-26T22:21:18+00:00"}
    if states is not None:
        lookup = states
    # Awaited in the app: an AsyncMock, so the read path really runs.
    app.get_state = AsyncMock(side_effect=lambda entity_id, **kw: lookup.get(entity_id))
    app.listen_event = MagicMock()
    app.fire_event = MagicMock()
    app.run_in = MagicMock()
    app.run_every = MagicMock()
    app.log = MagicMock()
    app.create_task = closing_create_task()
    return app


def _fired(app, command: str) -> Dict[str, Any]:
    calls = [c for c in app.fire_event.call_args_list if c.kwargs.get("command") == command]
    assert calls, f"no {command} event fired"
    return json.loads(calls[-1].kwargs["payload"])


def _results(app) -> Dict[str, Dict[str, str]]:
    return {r["name"]: r for r in _fired(app, "report_status")["results"]}


OK_WYOMING = AsyncMock(return_value={"status": "ok", "detail": "fine · 3ms"})
OK_REACH = AsyncMock(return_value={"status": "ok", "detail": "OpenAI reachable · 160ms"})


class TestConfig:
    def test_all_three_checks_parsed(self):
        app = _make_app()
        app.initialize()
        assert [c["name"] for c in app._checks] == ["Speech to Text", "Text to Speech", "Assistant"]

    @pytest.mark.parametrize("bad", [
        "not a dict",
        {"type": "wyoming", "service": "asr", "host": "h", "port": 1},        # no name
        {"name": "X", "type": "ping"},                                          # unknown type
        {"name": "X", "type": "wyoming", "service": "wake", "host": "h", "port": 1},
        {"name": "X", "type": "wyoming", "service": "asr", "port": 1},         # no host
        {"name": "X", "type": "agent"},                                         # no entity_id
    ])
    def test_malformed_entry_is_skipped(self, bad):
        app = _make_app({"checks": [bad, DEFAULT_ARGS["checks"][0]]})
        app.initialize()
        assert [c["name"] for c in app._checks] == ["Speech to Text"]
        assert any("Skipping" in str(c.args[0]) for c in app.log.call_args_list)


class TestRegistration:
    def test_register_declares_cloud_dependency_for_assistant_only(self):
        app = _make_app()
        app.initialize()
        app._register()
        payload = _fired(app, "register_checker")
        assert payload["checker_id"] == "voice"
        assert payload["check_names"] == ["Speech to Text", "Text to Speech", "Assistant"]
        assert payload["dependencies"] == [{"checker_id": "cloud", "affects_checks": ["Assistant"]}]

    def test_no_dependencies_key_without_dependencies(self):
        app = _make_app({"checks": DEFAULT_ARGS["checks"][:2]})
        app.initialize()
        app._register()
        assert "dependencies" not in _fired(app, "register_checker")


class TestRunChecks:
    def test_all_ok(self):
        app = _make_app()
        app.initialize()
        with patch(f"{MODULE}.wyoming_check", OK_WYOMING), patch(f"{MODULE}.http_reachable_check", OK_REACH):
            _run(app._run_checks())
        results = _results(app)
        assert {r["status"] for r in results.values()} == {"ok"}
        assert results["Assistant"]["detail"] == "OpenAI reachable · 160ms"
        app.get_state.assert_awaited_with("conversation.phone_assist")

    def test_wyoming_args_passed_through(self):
        app = _make_app()
        app.initialize()
        wy = AsyncMock(return_value={"status": "ok", "detail": "x"})
        with patch(f"{MODULE}.wyoming_check", wy), patch(f"{MODULE}.http_reachable_check", OK_REACH):
            _run(app._run_checks())
        assert wy.await_args_list[0].args == ("whisper", 10300, "asr")
        assert wy.await_args_list[1].args == ("kokoro", 10210, "tts")
        assert wy.await_args_list[0].kwargs == {"timeout_s": 5}

    def test_one_server_down_stays_critical(self):
        """No cross-check: one broken piece breaks voice, so it is not downgraded."""
        app = _make_app()
        app.initialize()
        wy = AsyncMock(side_effect=[
            {"status": "critical", "detail": "timeout"},
            {"status": "ok", "detail": "kokoro, 54 voices · 3ms"},
        ])
        with patch(f"{MODULE}.wyoming_check", wy), patch(f"{MODULE}.http_reachable_check", OK_REACH):
            _run(app._run_checks())
        results = _results(app)
        assert results["Speech to Text"] == {"name": "Speech to Text", "status": "critical", "detail": "timeout"}
        assert results["Text to Speech"]["status"] == "ok"

    @pytest.mark.parametrize("state, detail", [
        (None, "conversation.phone_assist not found"),
        ("unavailable", "conversation.phone_assist unavailable"),
    ])
    def test_agent_not_loaded_is_critical_without_probing(self, state, detail):
        app = _make_app(states={"conversation.phone_assist": state})
        app.initialize()
        reach = AsyncMock()
        with patch(f"{MODULE}.wyoming_check", OK_WYOMING), patch(f"{MODULE}.http_reachable_check", reach):
            _run(app._run_checks())
        assert _results(app)["Assistant"] == {"name": "Assistant", "status": "critical", "detail": detail}
        reach.assert_not_awaited()

    def test_agent_unknown_state_counts_as_loaded(self):
        """A never-used agent reads "unknown" — loaded, not broken."""
        app = _make_app(states={"conversation.phone_assist": "unknown"})
        app.initialize()
        with patch(f"{MODULE}.wyoming_check", OK_WYOMING), patch(f"{MODULE}.http_reachable_check", OK_REACH):
            _run(app._run_checks())
        assert _results(app)["Assistant"]["status"] == "ok"

    def test_agent_without_url_only_checks_entity(self):
        checks = [{"name": "Assistant", "type": "agent", "entity_id": "conversation.phone_assist"}]
        app = _make_app({"checks": checks})
        app.initialize()
        reach = AsyncMock()
        with patch(f"{MODULE}.http_reachable_check", reach):
            _run(app._run_checks())
        assert _results(app)["Assistant"] == {"name": "Assistant", "status": "ok", "detail": "agent loaded"}
        reach.assert_not_awaited()

    def test_reachability_label_uses_name(self):
        app = _make_app()
        app.initialize()
        reach = AsyncMock(return_value={"status": "ok", "detail": "x"})
        with patch(f"{MODULE}.wyoming_check", OK_WYOMING), patch(f"{MODULE}.http_reachable_check", reach):
            _run(app._run_checks())
        assert reach.await_args.args == ("https://api.openai.com/v1/models",)
        assert reach.await_args.kwargs == {"timeout_s": 5, "label": "OpenAI reachable"}

    def test_unexpected_exception_becomes_critical(self):
        app = _make_app()
        app.initialize()
        wy = AsyncMock(side_effect=RuntimeError("boom"))
        with patch(f"{MODULE}.wyoming_check", wy), patch(f"{MODULE}.http_reachable_check", OK_REACH):
            _run(app._run_checks())
        results = _results(app)
        assert results["Speech to Text"] == {"name": "Speech to Text", "status": "critical", "detail": "Error: boom"}
        assert results["Assistant"]["status"] == "ok"
        assert any(c.kwargs.get("level") == "ERROR" for c in app.log.call_args_list)


class TestLifecycle:
    def test_startup_registers_and_listens(self):
        app = _make_app()
        app.initialize()
        _run(app._async_startup())
        events = [c.args[1] for c in app.listen_event.call_args_list]
        assert events == ["health_check_controller_ready", "health_check_recheck"]
        _fired(app, "register_checker")
        app.run_in.assert_any_call(app._first_check, 5)

    def test_first_check_starts_periodic_timer(self):
        app = _make_app()
        app.initialize()
        with patch(f"{MODULE}.wyoming_check", OK_WYOMING), patch(f"{MODULE}.http_reachable_check", OK_REACH):
            app._first_check({})
        app.run_every.assert_called_once_with(app._check_tick, "now+120", 120)
