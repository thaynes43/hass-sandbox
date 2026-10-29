"""Offline checks for model_ab.py; no Home Assistant, inference, or device mutations."""

import asyncio
import contextlib
import io
import json
import unittest
from unittest.mock import patch

import aiohttp

import model_ab as ab


def row(agent, kind, query, end, first=None, speech="ok", error=None, tools=(), response_type="action_done"):
    r = {"agent": agent, "kind": kind, "query": query, "intent_end_s": end, "first_text_s": first or end,
         "speech": speech, "error": error, "response_type": response_type,
         "tools": [{"name": t[0], "id": t[1], "args": t[2] if len(t) > 2 else {}} for t in tools]}
    return "ROW " + json.dumps(r) + "\n"


def run_summary(lines):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        ab.summary(lines)
    return out.getvalue()


def cells(text, agent, kind):
    line = next(l for l in text.splitlines() if agent in l and kind in l)
    return [c.split() for c in line.split("|")]


class Msg:
    def __init__(self, payload):
        self.type, self.data = aiohttp.WSMsgType.TEXT, json.dumps(payload)


class FakeWs:
    """Auth handshake, then the scripted messages; afterwards receive() hangs like a stalled run."""

    def __init__(self, script):
        self.script, self.sent, self.closed = list(script), [], False

    async def receive_json(self):
        return {"type": "auth_ok"}

    async def send_json(self, msg):
        self.sent.append(msg)

    async def receive(self):
        if self.script:
            return Msg(self.script.pop(0))
        await asyncio.sleep(3600)

    async def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, make):
        self.make = make

    async def ws_connect(self, url):
        return self.make()


class GuardTests(unittest.TestCase):
    def test_read_tools_pass_with_or_without_the_api_prefix(self):
        tools = [{"name": "homeassistant__GetLiveContext"}, {"name": "GetDateTime"}, {"name": "web_search_call"}]
        self.assertEqual(ab.blocked_tools(tools), [])

    def test_actuating_tools_are_blocked(self):
        tools = [{"name": "homeassistant__HassTurnOn"}, {"name": "script_lock_all_doors"}, {"name": "GetLiveContext"}]
        self.assertEqual(ab.blocked_tools(tools), ["homeassistant__HassTurnOn", "script_lock_all_doors"])

    def test_settings_hide_prompt_and_credentials_but_keep_max_tokens(self):
        data = {"prompt": "p", "api_key": "k", "token": "t", "access_token": "a", "password": "x",
                "chat_model": "m", "max_tokens": 150, "reasoning_effort": "low"}
        self.assertEqual(ab.public_settings(data), {"chat_model": "m", "max_tokens": 150, "reasoning_effort": "low"})

    def test_timed_out_turn_keeps_its_tool_calls(self):
        tool = {"event": {"type": "intent-progress", "data": {"chat_log_delta": {
            "tool_calls": [{"tool_name": "homeassistant__HassTurnOn", "id": "c1", "tool_args": {}}]}}}}
        ws = FakeWs([{"type": "result", "success": True}, tool])
        with patch.object(ab, "TURN_TIMEOUT", 0.05):
            r = asyncio.run(ab.turn(FakeSession(lambda: ws), "p1", "q", ""))
        self.assertTrue(r["error"].startswith("timeout"))
        self.assertEqual(ab.blocked_tools(r["tools"]), ["homeassistant__HassTurnOn"])
        self.assertTrue(ws.closed)

    def test_restore_reports_failure_when_ha_is_unreachable(self):
        class Down:
            async def ws_connect(self, url):
                raise ConnectionError("down")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(ab, "RESTORE_PAUSE", 0):
            ok = asyncio.run(ab.restore(Down(), {"id": "p1", "conversation_engine": "x"}, "conversation.home_assistant"))
        self.assertFalse(ok)
        self.assertIn("RESTORE FAILED: pipeline p1", out.getvalue())

    def test_cold_takes_all_or_only_listed_agents(self):
        agents = ["conversation.a", "conversation.b"]
        self.assertEqual(ab.cold_agents("1", agents), set(agents))
        self.assertEqual(ab.cold_agents("0", agents), set())
        self.assertEqual(ab.cold_agents("conversation.b,conversation.z", agents), {"conversation.b"})


class SummaryTests(unittest.TestCase):
    def test_nearest_rank_percentile(self):
        xs = list(range(1, 11))
        self.assertEqual(ab.pct(xs, 90), 9)
        self.assertEqual(ab.pct(xs, 100), 10)
        self.assertEqual(ab.pct([5.0], 90), 5.0)
        self.assertIsNone(ab.pct([], 90))

    def test_counts_failures_and_distinct_lookups(self):
        lines = [
            "PIPELINE ignored\n",
            row("conversation.a", "warm", "q1", 2.0, tools=[("GetLiveContext", "c1"), ("GetLiveContext", "c1")]),
            row("conversation.a", "warm", "q1", 4.0, tools=[("GetLiveContext", "c2"), ("GetLiveContext", "c3")]),
            row("conversation.a", "warm", "q2", None, speech="", error="timeout 90s"),
            row("conversation.a", "cold", "q1", 6.0),
        ]
        text = run_summary(lines)
        warm = cells(text, "conversation.a", "warm")
        self.assertEqual(warm[0][-1], "3")                       # samples, failure included
        self.assertEqual(warm[2][0], "3.00")                     # intent-end median of the two good turns
        self.assertEqual(warm[3], ["1", "1.50"])                 # one failure; (1 + 2) distinct lookups / 2
        self.assertIn("cold", text)

    def test_agent_error_answer_is_a_failure_in_both_tables(self):
        lines = [row("conversation.a", "warm", "q1", 2.0),
                 row("conversation.a", "warm", "q1", 9.0, speech="Sorry, I had a problem", response_type="error")]
        text = run_summary(lines)
        self.assertEqual(cells(text, "conversation.a", "warm")[3][0], "1")
        self.assertIn("n= 1 med   2.00", text)

    def test_calls_without_an_id_are_told_apart_by_arguments(self):
        lines = [row("conversation.a", "warm", "q1", 2.0,
                     tools=[("web_search_call", None, {"q": "a"}), ("web_search_call", None, {"q": "b"})])]
        self.assertEqual(cells(run_summary(lines), "conversation.a", "warm")[3], ["0", "2.00"])


if __name__ == "__main__":
    unittest.main()
