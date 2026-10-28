"""Offline checks for model_ab.py; no Home Assistant, inference, or device mutations."""

import contextlib
import io
import json
import unittest

import model_ab as ab


def row(agent, kind, query, end, first=None, speech="ok", error=None, tools=()):
    r = {"agent": agent, "kind": kind, "query": query, "intent_end_s": end, "first_text_s": first or end,
         "speech": speech, "error": error, "tools": [{"name": n, "id": i} for n, i in tools]}
    return "ROW " + json.dumps(r) + "\n"


class GuardTests(unittest.TestCase):
    def test_read_tools_pass_with_or_without_the_api_prefix(self):
        tools = [{"name": "homeassistant__GetLiveContext"}, {"name": "GetDateTime"}, {"name": "web_search_call"}]
        self.assertEqual(ab.blocked_tools(tools), [])

    def test_actuating_tools_are_blocked(self):
        tools = [{"name": "homeassistant__HassTurnOn"}, {"name": "script_lock_all_doors"}, {"name": "GetLiveContext"}]
        self.assertEqual(ab.blocked_tools(tools), ["homeassistant__HassTurnOn", "script_lock_all_doors"])

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
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ab.summary(lines)
        text = out.getvalue()
        warm = next(l for l in text.splitlines() if "conversation.a" in l and "warm" in l)
        cells = [c.split() for c in warm.split("|")]
        self.assertEqual(cells[0][-1], "3")                      # samples, failure included
        self.assertEqual(cells[2][0], "3.00")                    # intent-end median of the two good turns
        self.assertEqual(cells[3], ["1", "1.50"])                # one failure; (1 + 2) distinct lookups / 2
        self.assertIn("cold", text)


if __name__ == "__main__":
    unittest.main()
