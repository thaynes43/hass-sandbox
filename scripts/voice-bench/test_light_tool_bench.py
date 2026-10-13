"""Offline regression checks; no network, inference, or device mutations."""

import copy
import http.client
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

import light_tool_bench as bench


def entities():
    return [
        {"entity_id": "light.a", "domain": "light", "names": ["Desk", "Office lamp", "Iris"], "state": "on", "areas": ["Rumpus Room", "Rumpus"], "attributes": {"brightness": "176"}},
        {"entity_id": "light.b", "domain": "light", "names": ["Shelf"], "state": "off", "areas": ["Rumpus Room"]},
        {"entity_id": "light.c", "domain": "light", "names": ["Unavailable"], "state": "unavailable", "areas": ["Rumpus Room"]},
        {"entity_id": "light.d", "domain": "light", "names": ["Island"], "state": "on", "areas": ["Kitchen"]},
        {"entity_id": "light.group", "domain": "light", "names": ["Room group"], "state": "on", "areas": ["Rumpus Room"], "members": ["light.a", "light.b"]},
        {"entity_id": "light.hidden", "domain": "light", "names": ["Hidden"], "state": "on", "areas": ["Kitchen"], "exposed": False},
        {"entity_id": "switch.noise", "domain": "switch", "names": ["Fan"], "state": "on", "areas": ["Rumpus Room"]},
    ]


def suite(turns=None):
    return {
        "schema_version": 1,
        "profile": {
            "model": "fixture-model",
            "messages": [{"role": "system", "content": "Fixture test prompt\n" + bench.STATIC_MARKER}],
            "settings": {"temperature": 0.1, "max_tokens": 3000, "reasoning_effort": "low"},
            "tools": [
                {"type": "function", "function": {"name": bench.READ_TOOL, "description": "Native context schema fixture", "parameters": {"type": "object", "properties": {"domain": {"anyOf": [{}, {"type": "array", "items": {"type": "string"}}]}}, "additionalProperties": False}}},
                {"type": "function", "function": {"name": "HassTurnOn", "parameters": {"type": "object"}}},
            ],
        },
        "scenarios": [{"id": "fixture", "entities": entities(), "turns": turns or [turn()]}],
    }


def turn(**overrides):
    result = {"id": "count", "text": "Count fixture lights.", "scope": {"domain": "light"}}
    result.update(overrides)
    return result


def event(calls=None):
    return {"tool_calls": calls or [], "elapsed_seconds": 0.1, "ttft_seconds": None, "finish_reason": "stop"}


def read_record(args=None):
    args = args or {"domain": "light"}
    result, ids = bench.execute_fixture_tool(entities(), bench.READ_TOOL, args)
    return {"name": bench.READ_TOOL, "arguments": args, "result": result, "matched_entity_ids": ids, "blocked": False}


def result(answer="Three lights are on.", calls=None):
    return {"id": "count", "status": "complete", "final": answer, "events": [event(calls if calls is not None else [read_record()])]}


def assistant(content="", tool=None, arguments=None, finish="stop"):
    msg = {"role": "assistant", "content": content}
    if tool:
        msg["tool_calls"] = [{"id": "fixture-call", "type": "function", "function": {"name": tool, "arguments": json.dumps(arguments or {"domain": "light"})}}]
    ev = event()
    ev.update(ttft_seconds=0.01 if content else None, finish_reason=finish)
    return msg, ev


class FakeBackend:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def complete(self, payload, deadline):
        self.requests.append(copy.deepcopy(payload))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return copy.deepcopy(response)


class FixtureTests(unittest.TestCase):
    def test_zero_one_three_are_derived_from_entity_records(self):
        for count in (0, 1, 3):
            rows = copy.deepcopy(entities()[:3])
            for i, row in enumerate(rows):
                row["state"] = "on" if i < count else "off"
            self.assertEqual(bench.truth_for(rows, {"domain": "light"})["count"], count)

    def test_aliases_groups_hidden_and_other_domains_do_not_invent_bulb_truth(self):
        truth = bench.truth_for(entities(), {"domain": "light"})
        self.assertEqual(truth["count"], 3)
        self.assertEqual(truth["on_entity_ids"], ["light.a", "light.d", "light.group"])
        self.assertEqual(truth["unsettled_entity_ids"], ["light.c"])
        self.assertEqual(bench.truth_for(entities(), {"domain": "light", "area": "RUMPUS"})["count"], 1)
        self.assertEqual(bench.truth_for(entities(), {"domain": "light", "area": "Rumpus Room"})["count"], 2)
        bad = suite()
        bad["scenarios"][0]["count_unit"] = "physical_bulbs"
        with self.assertRaisesRegex(bench.Refused, "physical bulb"):
            bench.validate_suite(bad)

    def test_native_read_shape_and_exact_fixture_alias_filter(self):
        value, ids = bench.execute_fixture_tool(entities(), bench.READ_TOOL, {"domain": [" LIGHT "], "name": "iris"})
        self.assertEqual(ids, ["light.a"])
        self.assertEqual(set(value), {"success", "result"})
        self.assertIn('state: "on"', value["result"])
        self.assertNotIn("entity_id", value["result"])
        self.assertNotIn("light.hidden", value["result"])
        self.assertEqual(bench.execute_fixture_tool(entities(), bench.READ_TOOL, {"area": "Absent"})[0],
                         {"success": False, "error": "No exposed entities found in area 'Absent'"})

    def test_write_selection_and_invalid_read_arguments_are_refused(self):
        for name, args in (("HassTurnOn", {}), ("ha_get_state", {}), (bench.READ_TOOL, {"entity_id": "light.a"}),
                           (bench.READ_TOOL, {"domain": 4}), (bench.READ_TOOL, {"area": ["Rumpus"]})):
            with self.subTest(name=name, args=args), self.assertRaises(bench.Refused):
                bench.execute_fixture_tool(entities(), name, args)

    def test_suite_bounds_and_request_fields_fail_closed(self):
        self.assertEqual(bench.validate_suite(suite()), 1)
        for change in ("many", "headers", "duplicate", "user"):
            bad = suite()
            if change == "many":
                bad["scenarios"][0]["turns"] = [turn(id=str(i)) for i in range(13)]
            elif change == "headers":
                bad["profile"]["settings"]["headers"] = {"Authorization": "fixture-secret"}
            elif change == "duplicate":
                bad["profile"]["tools"].append(bad["profile"]["tools"][0])
            else:
                bad["profile"]["messages"].append({"role": "user", "content": "guess"})
            with self.subTest(change=change), self.assertRaises(bench.Refused):
                bench.validate_suite(bad)

    def test_committed_case_manifest_is_reproducible_without_private_house_data(self):
        manifest = json.loads(Path(__file__).with_name("light_count_cases.json").read_text())
        manifest["profiles"] = {"phone": suite()["profile"], "rumpus": suite()["profile"]}
        self.assertEqual(bench.validate_suite(manifest), 12)
        expected = {s["id"]: [bench.truth_for(s["entities"], t["scope"])["count"] for t in s["turns"]]
                    for s in manifest["scenarios"]}
        self.assertEqual(expected["zero_on"], [0])
        self.assertEqual(expected["one_on"], [1])
        self.assertEqual(expected["aliases_are_not_extra_lights"], [3])
        self.assertEqual(expected["scope_followup"], [1, 3, 3])
        self.assertEqual(sum("manual_criterion" in t for s in manifest["scenarios"] for t in s["turns"]), 3)
        for s in manifest["scenarios"]:
            self.assertTrue(all("members" not in e for e in s["entities"]))


class ScoringTests(unittest.TestCase):
    def test_empty_first_response_is_a_failure_even_after_tool_success(self):
        scored, _ = bench.score_turn(turn(), result(""), entities())
        self.assertFalse(scored["pass"])
        self.assertFalse(scored["nonempty_final"])
        self.assertTrue(scored["grounded_scope"])

    def test_correct_number_without_read_does_not_pass(self):
        scored, _ = bench.score_turn(turn(), result(calls=[]), entities())
        self.assertFalse(scored["pass"])
        self.assertTrue(scored["count_correct"])
        self.assertFalse(scored["grounded_scope"])

    def test_truth_is_recomputed_and_transcript_provenance_is_verified(self):
        forged = read_record({"domain": "light", "area": "Kitchen"})
        forged["matched_entity_ids"] = [e["entity_id"] for e in entities()]
        scored, _ = bench.score_turn(turn(), result(calls=[forged]), entities())
        self.assertFalse(scored["grounded_scope"])
        forged["result"]["result"] = "invented context"
        scored, _ = bench.score_turn(turn(), result(calls=[forged]), entities())
        self.assertEqual(scored["invalid_calls"], 1)

    def test_scope_followup_can_use_prior_read_but_required_refresh_cannot(self):
        prior = set(read_record()["matched_entity_ids"])
        follow = turn(scope={"domain": "light", "area": "Rumpus Room"}, require_read=False)
        scored, _ = bench.score_turn(follow, result("Two lights are on.", calls=[]), entities(), prior)
        self.assertTrue(scored["pass"])
        follow["require_read"] = True
        scored, _ = bench.score_turn(follow, result("Two lights are on.", calls=[]), entities(), prior)
        self.assertFalse(scored["pass"])

    def test_grounded_correct_count_passes_and_alias_overcount_fails(self):
        self.assertTrue(bench.score_turn(turn(), result(), entities())[0]["pass"])
        self.assertFalse(bench.score_turn(turn(), result("Six lights are on."), entities())[0]["pass"])
        blocked = {"name": "HassTurnOn", "arguments": {}, "blocked": True}
        scored, _ = bench.score_turn(turn(), result(calls=[read_record(), blocked]), entities())
        self.assertFalse(scored["pass"])
        self.assertEqual(scored["blocked_calls"], 1)

    def test_count_parser_rejects_ambiguity_negation_and_reasoning_only(self):
        valid = {"Three lights are on.": 3, "There are 3 lights on and one unavailable.": 3,
                 "No lights are on.": 0, "All lights are off.": 0, "One is on.": 1,
                 "Two lights are off and three lights are on.": 3,
                 "2 lights in the Rumpus Room are currently on.": 2}
        for answer, expected in valid.items():
            with self.subTest(answer=answer):
                self.assertEqual(bench.stated_count(answer), expected)
        for answer in ("Not three lights are on.", "Maybe three lights are on.", "Three lights are off.",
                       "Three lights are on downstairs and two lights are on upstairs.",
                       "<think>Three lights are on.</think>", "3.0 lights are on.", "-3 lights are on.",
                       "There might be three lights on.", "No more than three lights are on.",
                       "Three lights are on, but two of those are off.", "Three lights might be on.",
                       "Three lights are on if the readings are current.", "There were three lights on."):
            with self.subTest(answer=answer):
                self.assertIsNone(bench.stated_count(answer))
        self.assertIsNone(bench.stated_count("1,003 lights are on."))
        self.assertEqual(bench.visible_text("<think>Three lights are on."), "")

    def test_explicit_wrong_scope_fails_and_unknown_scope_requires_review(self):
        scored, _ = bench.score_turn(turn(), result("Three lights are on in the Kitchen."), entities())
        self.assertFalse(scored["pass"])
        self.assertFalse(scored["stated_scope_correct"])
        scored, _ = bench.score_turn(turn(), result("Kitchen has three lights on."), entities())
        self.assertFalse(scored["pass"])
        self.assertFalse(scored["stated_scope_correct"])
        scored, _ = bench.score_turn(turn(), result("Three lights are on in the Garage."), entities())
        self.assertIsNone(scored["pass"])
        self.assertTrue(scored["manual_review_required"])
        scored, _ = bench.score_turn(turn(), result("There might be three lights on."), entities())
        self.assertIsNone(scored["pass"])
        self.assertTrue(scored["count_parse_review"])

    def test_report_rejects_native_pipeline_claim_and_missing_rows(self):
        for report in ({"mode": "native_ha_pipeline", "scenarios": []}, {"mode": bench.MODE, "scenarios": []}):
            with self.assertRaises(bench.Refused):
                bench.score_report(suite(), report)

    def test_semantic_followups_require_recorded_manual_judgment(self):
        follow = turn(expected_kind="nonempty", require_read=False, manual_criterion="Check named entities.")
        recorded = result("Desk and Island.", calls=[])
        scored, _ = bench.score_turn(follow, recorded, entities())
        self.assertIsNone(scored["pass"])
        self.assertTrue(scored["automatic_gates_pass"])
        recorded["manual_review"] = {"pass": False, "reason": "Missing the on group."}
        self.assertFalse(bench.score_turn(follow, recorded, entities())[0]["pass"])
        recorded["manual_review"] = {"pass": True, "reason": "All relevant on records are correctly named."}
        self.assertTrue(bench.score_turn(follow, recorded, entities())[0]["pass"])
        recorded["manual_review"] = {"pass": True, "reason": ""}
        with self.assertRaises(bench.Refused):
            bench.score_turn(follow, recorded, entities())


class ExecutionTests(unittest.TestCase):
    def run_fixture(self, backend, input_suite=None):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "report.json"
            value = bench.run_suite(input_suite or suite(), backend, target)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(target.read_text()), value)
            return value

    def test_complete_loop_is_sequential_bounded_and_uses_exact_supplied_schemas(self):
        backend = FakeBackend([assistant(tool=bench.READ_TOOL), assistant("Three lights are on.")])
        report = self.run_fixture(backend)
        self.assertTrue(report["summary"]["all_pass"])
        self.assertFalse(report["summary"]["native_ha_pipeline"])
        self.assertEqual(len(backend.requests), 2)
        self.assertEqual(backend.requests[0]["tools"], suite()["profile"]["tools"])
        self.assertEqual(backend.requests[0]["temperature"], 0.1)
        self.assertEqual(backend.requests[0]["max_tokens"], 512)
        self.assertNotIn(bench.STATIC_MARKER, backend.requests[0]["messages"][0]["content"])
        self.assertNotIn('state: "on"', backend.requests[0]["messages"][0]["content"])
        self.assertEqual(backend.requests[1]["messages"][-1]["role"], "tool")

    def test_write_is_advertised_but_never_executed_or_counted_as_safe(self):
        backend = FakeBackend([assistant(tool="HassTurnOn"), assistant("Three lights are on.")])
        report = self.run_fixture(backend)
        scored = report["scenarios"][0]["turns"][0]["score"]
        self.assertEqual(scored["blocked_calls"], 1)
        self.assertFalse(scored["pass"])
        self.assertIn("Benchmark refused", backend.requests[1]["messages"][-1]["content"])

    def test_repeated_question_has_continuity_but_new_scenario_is_fresh(self):
        turns = [turn(), turn(id="repeat")]
        input_suite = suite(turns)
        second = copy.deepcopy(input_suite["scenarios"][0])
        second.update(id="fresh", turns=[turn()])
        input_suite["scenarios"].append(second)
        backend = FakeBackend([assistant(tool=bench.READ_TOOL), assistant("Three lights are on.")] * 3)
        self.run_fixture(backend, input_suite)
        self.assertEqual(sum(m["role"] == "user" for m in backend.requests[2]["messages"]), 2)
        self.assertEqual(sum(m["role"] == "user" for m in backend.requests[4]["messages"]), 1)

    def test_multiple_native_profiles_share_one_suite_bound(self):
        input_suite = suite()
        profile = input_suite.pop("profile")
        other = copy.deepcopy(profile)
        other["settings"]["temperature"] = 0.2
        other["tools"][0]["function"]["name"] = "assist__" + bench.READ_TOOL
        input_suite["profiles"] = {"phone": profile, "room": other}
        input_suite["scenarios"][0]["profile"] = "phone"
        second = copy.deepcopy(input_suite["scenarios"][0])
        second.update(id="room", profile="room")
        input_suite["scenarios"].append(second)
        backend = FakeBackend([assistant(tool=bench.READ_TOOL), assistant("Three lights are on."),
                               assistant(tool="assist__" + bench.READ_TOOL), assistant("Three lights are on.")])
        report = self.run_fixture(backend, input_suite)
        self.assertEqual([r["temperature"] for r in backend.requests], [0.1, 0.1, 0.2, 0.2])
        self.assertEqual([s["profile"] for s in report["scenarios"]], ["phone", "room"])
        self.assertTrue(report["summary"]["all_pass"])
        changed = copy.deepcopy(input_suite)
        changed["scenarios"][0]["entities"][0]["state"] = "off"
        with self.assertRaisesRegex(bench.Refused, "complete suite"):
            bench.score_report(changed, report)

    def test_exact_empty_regression_question_mark_and_repeat_are_individually_scored(self):
        input_suite = suite([turn(), turn(id="clarify", text="?", expected_kind="nonempty", require_read=False), turn(id="repeat")])
        backend = FakeBackend([assistant(tool=bench.READ_TOOL), assistant(""), assistant("I did not catch that."),
                               assistant(tool=bench.READ_TOOL), assistant("Three lights are on.")])
        report = self.run_fixture(backend, input_suite)
        self.assertEqual([t["score"]["pass"] for t in report["scenarios"][0]["turns"]], [False, True, True])
        self.assertEqual(report["summary"]["passed"], 2)

    def test_tool_round_limit_stops_after_two_fixture_dispatches(self):
        backend = FakeBackend([assistant(tool=bench.READ_TOOL)] * 3)
        report = self.run_fixture(backend)
        recorded = report["scenarios"][0]["turns"][0]
        self.assertEqual(recorded["status"], "tool_round_limit")
        self.assertEqual(recorded["tool_rounds"], 2)
        self.assertEqual(len(backend.requests), 3)
        self.assertTrue(recorded["events"][-1]["tool_calls"][0]["round_limit"])

    def test_timeout_cancellation_http_error_and_token_limit_persist_then_stop(self):
        cases = [
            (TimeoutError(), "timeout"), (KeyboardInterrupt(), "cancelled"),
            (http.client.IncompleteRead(b"partial", 20), "transport_error"),
            (urllib.error.HTTPError("http://fixture", 503, "failure", {}, None), "http_error"),
            (assistant("Three lights are on.", finish="length"), "output_token_limit"),
            (assistant(tool=bench.READ_TOOL, finish="length"), "output_token_limit"),
            (assistant("Three lights are on.", finish="content_filter"), "content_filter"),
            (assistant("Three lights are on.", finish="unexpected"), "unexpected_finish_reason"),
            (assistant(finish="tool_calls"), "backend_protocol_error"),
        ]
        for response, status in cases:
            with self.subTest(status=status):
                backend = FakeBackend([response])
                report = self.run_fixture(backend, suite([turn(), turn(id="later")]))
                self.assertEqual([t["status"] for t in report["scenarios"][0]["turns"]], [status, "not_run"])
                self.assertFalse(report["summary"]["all_pass"])
                self.assertEqual(len(backend.requests), 1)

    def test_expired_total_deadline_sends_no_request(self):
        backend = FakeBackend([])
        messages = copy.deepcopy(suite()["profile"]["messages"])
        response = bench.run_turn(backend, suite()["profile"], messages, turn(), entities(), 0)
        self.assertEqual(response["status"], "timeout")
        self.assertEqual(backend.requests, [])

    def test_dns_error_retains_safe_class_errno_and_unrun_turns_are_separate(self):
        backend = FakeBackend([urllib.error.URLError(bench.socket.gaierror(-3, "private diagnostic text"))])
        report = self.run_fixture(backend, suite([turn(), turn(id="later")]))
        recorded = report["scenarios"][0]["turns"][0]
        self.assertEqual(recorded["error"], {"exception_class": "URLError", "reason_class": "gaierror", "errno": -3})
        self.assertNotIn("private diagnostic", json.dumps(report))
        self.assertEqual(report["summary"]["failed"], 1)
        self.assertEqual(report["summary"]["not_run"], 1)
        self.assertEqual(report["summary"]["manual_pending"], 0)
        self.assertEqual(report["summary"]["completed_model_requests"], 0)


class StreamTests(unittest.TestCase):
    def test_fragmented_native_tool_calls_usage_and_visible_text_timing(self):
        chunks = [
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "tool-1", "function": {"name": bench.READ_TOOL, "arguments": '{"domain":'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"light"}'}}]}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 40, "completion_tokens": 12}, "timings": {"predicted_n": 12}},
        ]
        response = io.BytesIO(b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks) + b"data: [DONE]\n\n")
        opener = unittest.mock.Mock()
        opener.open.return_value = response
        backend = bench.Backend(bench.BACKEND_URL, api_key="fixture-secret", opener=opener)
        msg, ev = backend.complete({}, bench.time.monotonic() + 10)
        self.assertEqual(json.loads(msg["tool_calls"][0]["function"]["arguments"]), {"domain": "light"})
        self.assertEqual(ev["usage"]["prompt_tokens"], 40)
        self.assertIsNone(ev["ttft_seconds"])
        self.assertIsNotNone(ev["first_payload_seconds"])
        self.assertNotIn("fixture-secret", json.dumps(ev))

    def test_non_stream_body_is_a_protocol_error(self):
        opener = unittest.mock.Mock()
        opener.open.return_value = io.BytesIO(b'{"choices": []}')
        with self.assertRaises(bench.Refused):
            bench.Backend(bench.BACKEND_URL, opener=opener).complete({}, bench.time.monotonic() + 10)

    def test_stream_requires_a_terminal_finish_reason(self):
        opener = unittest.mock.Mock()
        chunk = {"choices": [{"delta": {"content": "Three lights are on."}}]}
        for ending in (b"", b"data: [DONE]\n\n"):
            opener.open.return_value = io.BytesIO(b"data: " + json.dumps(chunk).encode() + b"\n\n" + ending)
            with self.assertRaisesRegex(bench.Refused, "terminal completion"):
                bench.Backend(bench.BACKEND_URL, opener=opener).complete({}, bench.time.monotonic() + 10)

    def test_malformed_stream_choices_and_completed_call_ids_fail_closed(self):
        valid = {"index": 0, "id": "id-a", "type": "function", "function": {"name": bench.READ_TOOL, "arguments": '{}'}}
        cases = [
            [{"index": 1, "delta": {}, "finish_reason": "stop"}],
            [{"index": 0, "delta": {}, "finish_reason": "stop"}] * 2,
            [{"delta": {"tool_calls": [dict(valid, type="unsupported")]}, "finish_reason": "tool_calls"}],
            [{"delta": {"tool_calls": [dict(valid, id="")]}, "finish_reason": "tool_calls"}],
            [{"delta": {"tool_calls": [valid, dict(valid, index=1)]}, "finish_reason": "tool_calls"}],
        ]
        for choices in cases:
            opener = unittest.mock.Mock()
            opener.open.return_value = io.BytesIO(b"data: " + json.dumps({"choices": choices}).encode() + b"\n\ndata: [DONE]\n\n")
            with self.subTest(choices=choices), self.assertRaises(bench.Refused):
                bench.Backend(bench.BACKEND_URL, opener=opener).complete({}, bench.time.monotonic() + 10)

    def test_backend_destination_and_redirects_are_refused(self):
        for url in ("http://example.test/v1", bench.BACKEND_URL + "?key=value", "https://llama-server.ai.svc.cluster.local:8080/v1"):
            with self.subTest(url=url), self.assertRaises(bench.Refused):
                bench.Backend(url)
        with self.assertRaises(bench.Refused):
            bench.NoRedirect().redirect_request(None, None, 302, "redirect", {}, "http://example.test")

    def test_stream_reads_recompute_remaining_timeout_without_load_or_sleep(self):
        sock = unittest.mock.Mock()
        response = io.BytesIO(b"data: one\n\n")
        with patch.object(bench.time, "monotonic", side_effect=[1, 11]):
            lines = bench.Backend._lines(response, 10, sock)
            self.assertEqual(next(lines), b"data: one")
            self.assertEqual(next(lines), b"")
            with self.assertRaises(TimeoutError):
                next(lines)
        sock.settimeout.assert_called_once_with(9)


if __name__ == "__main__":
    unittest.main()
