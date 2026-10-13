"""Bounded direct-backend evaluation with frozen HA entity fixtures.

This does not call Home Assistant. Only GetLiveContext executes, against fixture
records; every other advertised tool is refused. Native Assist traces remain a
separate evidence source. The suite supplies prompts and captured tool schemas.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import socket
import threading
import time
import urllib.error
import urllib.request


READ_TOOL = "homeassistant__GetLiveContext"
READ_TOOLS = {READ_TOOL, "assist__" + READ_TOOL}
STATIC_MARKER = "{{FIXTURE_STATIC_CONTEXT}}"
MAX_TURNS = 12
MAX_TOOL_ROUNDS = 2
TURN_TIMEOUT = 90
TOTAL_TIMEOUT = 600
MAX_OUTPUT_TOKENS = 512
MODE = "direct_backend_frozen_fixtures"
BACKEND_URL = "http://llama-server.ai.svc.cluster.local:8080/v1"
SETTINGS = {
    "temperature", "top_p", "top_k", "min_p", "repeat_penalty",
    "presence_penalty", "frequency_penalty", "reasoning_effort", "seed",
    "chat_template_kwargs", "parallel_tool_calls", "cache_prompt", "max_tokens", "stream",
}


class Refused(ValueError):
    """Invalid input or a tool selection outside the fixed read-only boundary."""


def validate_profile(profile):
    if not isinstance(profile.get("model"), str) or not profile["model"]:
        raise Refused("profile.model is required")
    if not isinstance(profile.get("messages"), list) or not profile["messages"]:
        raise Refused("profile.messages must contain the captured prompt")
    for msg in profile["messages"]:
        if msg.get("role") not in ("system", "developer") or not isinstance(msg.get("content"), str):
            raise Refused("profile.messages may contain only system/developer text")
    tools = profile.get("tools", [])
    names = []
    for tool in tools:
        if tool.get("type") != "function" or not isinstance(tool.get("function"), dict):
            raise Refused("profile.tools must contain captured function schemas")
        name = tool["function"].get("name")
        if not isinstance(name, str) or not name:
            raise Refused("each tool needs a name")
        names.append(name)
    if len(set(names) & READ_TOOLS) != 1 or len(names) != len(set(names)):
        raise Refused("tools must include GetLiveContext once and have unique names")
    settings = profile.get("settings", {})
    if not isinstance(settings, dict) or set(settings) - SETTINGS:
        raise Refused("profile.settings contains unsupported request fields")
    if "max_tokens" in settings and (type(settings["max_tokens"]) is not int or settings["max_tokens"] < 1):
        raise Refused("max_tokens must be a positive integer")
    if "stream" in settings and settings["stream"] is not True:
        raise Refused("the benchmark requires streaming for visible-text timing")
def profile_for(suite, scenario):
    if "profiles" not in suite:
        return suite["profile"]
    name = scenario.get("profile")
    if name not in suite["profiles"]:
        raise Refused("scenario.profile must select a supplied native profile")
    return suite["profiles"][name]


def read_tool_for(profile):
    return next(t["function"]["name"] for t in profile["tools"] if t["function"]["name"] in READ_TOOLS)


def suite_hash(suite):
    return hashlib.sha256(json.dumps(suite, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def validate_suite(suite):
    if suite.get("schema_version") != 1:
        raise Refused("schema_version must be 1")
    if ("profile" in suite) == ("profiles" in suite):
        raise Refused("supply either profile or profiles")
    profiles = suite.get("profiles", {"default": suite.get("profile")})
    if not isinstance(profiles, dict) or not profiles:
        raise Refused("profiles must be a nonempty mapping")
    for profile in profiles.values():
        validate_profile(profile)
    scenario_ids = set()
    turn_count = 0
    for scenario in suite["scenarios"]:
        profile_for(suite, scenario)
        if not isinstance(scenario.get("id"), str) or scenario["id"] in scenario_ids:
            raise Refused("scenario ids must be unique strings")
        scenario_ids.add(scenario["id"])
        if scenario.get("count_unit", "exposed_light_entities") != "exposed_light_entities":
            raise Refused("physical bulb counts cannot be inferred from exposed entity records")
        ids = set()
        for entity in scenario["entities"]:
            eid = entity["entity_id"]
            if not isinstance(eid, str) or eid in ids or "." not in eid:
                raise Refused("fixture entity ids must be unique")
            ids.add(eid)
            if entity.get("domain") != eid.split(".", 1)[0]:
                raise Refused("fixture domain must agree with entity_id")
            if not isinstance(entity.get("state"), str):
                raise Refused("fixture states must be strings")
            for field in ("names", "areas"):
                values = entity.get(field, [])
                if not isinstance(values, list) or any(not isinstance(v, str) or not v for v in values):
                    raise Refused(f"{field} must contain nonempty strings")
            if not entity.get("names"):
                raise Refused("fixture entities require names")
            if type(entity.get("exposed", True)) is not bool:
                raise Refused("exposed must be a boolean")
            attributes = entity.get("attributes", {})
            if not isinstance(attributes, dict) or any(
                not isinstance(k, str) or isinstance(v, (dict, list)) for k, v in attributes.items()
            ):
                raise Refused("fixture attributes must be scalar fields")
        turn_ids = set()
        if not scenario.get("turns"):
            raise Refused("each scenario needs at least one user turn")
        for turn in scenario["turns"]:
            if not isinstance(turn.get("id"), str) or turn["id"] in turn_ids:
                raise Refused("turn ids must be unique strings within a scenario")
            turn_ids.add(turn["id"])
            if not isinstance(turn.get("text"), str) or not turn["text"].strip():
                raise Refused("turn text is required")
            if turn.get("expected_kind", "count") not in ("count", "nonempty"):
                raise Refused("expected_kind must be count or nonempty")
            scope = turn.get("scope", {})
            if scope.get("domain") != "light" or set(scope) - {"domain", "area"}:
                raise Refused("count scope must explicitly select light and optional area")
            if "area" in scope and (not isinstance(scope["area"], str) or not scope["area"]):
                raise Refused("scope.area must be a nonempty string")
            if type(turn.get("require_read", True)) is not bool:
                raise Refused("require_read must be a boolean")
            if "manual_criterion" in turn and (not isinstance(turn["manual_criterion"], str) or not turn["manual_criterion"].strip()):
                raise Refused("manual_criterion must be nonempty text")
            turn_count += 1
    if not scenario_ids or turn_count > MAX_TURNS:
        raise Refused(f"suite must contain 1–{MAX_TURNS} user turns")
    return turn_count


def in_scope(entity, scope):
    return (
        entity.get("exposed", True)
        and entity["domain"] == scope["domain"]
        and ("area" not in scope or scope["area"].casefold() in [a.casefold() for a in entity.get("areas", [])])
    )


def truth_for(entities, scope):
    scoped = [e for e in entities if in_scope(e, scope)]
    return {
        "count_unit": "exposed_light_entities",
        "count": sum(e["state"] == "on" for e in scoped),
        "entity_ids": sorted(e["entity_id"] for e in scoped),
        "on_entity_ids": sorted(e["entity_id"] for e in scoped if e["state"] == "on"),
        "unsettled_entity_ids": sorted(e["entity_id"] for e in scoped if e["state"] not in ("on", "off")),
    }


def scalar(value):
    if value is None:
        return ""
    # JSON quoting is also YAML quoting; entity states are always explicit strings.
    return json.dumps(value, ensure_ascii=False)


def context_text(entities, include_state=True):
    rows = []
    for e in entities:
        rows.extend([f"- names: {scalar(', '.join(e['names']))}", f"  domain: {scalar(e['domain'])}"])
        if include_state:
            rows.append(f"  state: {scalar(e['state'])}")
        if e.get("areas"):
            rows.append(f"  areas: {scalar(', '.join(e['areas']))}")
        if include_state and e.get("attributes"):
            rows.append("  attributes:")
            rows.extend(f"    {scalar(k)}: {scalar(v)}" for k, v in e["attributes"].items())
    label = "Live" if include_state else "Static"
    return f"{label} Context: An overview of the areas and the devices in this smart home:\n" + ("\n".join(rows) + "\n" if rows else "[]\n")


def execute_fixture_tool(entities, name, args):
    if name not in READ_TOOLS:
        raise Refused("tool is outside the exact GetLiveContext allowlist")
    if not isinstance(args, dict) or set(args) - {"name", "domain", "area"}:
        raise Refused("GetLiveContext arguments must match the captured schema")
    for field in ("name", "area"):
        if field in args and not isinstance(args[field], str):
            raise Refused(f"{field} must be a string")
    domains = args.get("domain")
    if isinstance(domains, str):
        domains = [domains]
    if domains is not None and (not isinstance(domains, list) or any(not isinstance(d, str) for d in domains)):
        raise Refused("domain must be a string or a list of strings")
    if domains is not None:
        domains = [d.strip().lower() for d in domains if d.strip()]
    exposed = [e for e in entities if e.get("exposed", True)]
    if not exposed:
        return {"success": False, "error": "No entities are exposed"}, []
    selected = exposed
    # Fixture names/area aliases use exact, case-insensitive matches. This is not
    # a replacement for HA's full native intent target matcher.
    for field, key in (("name", "names"), ("area", "areas")):
        if args.get(field):
            selected = [e for e in selected if args[field].casefold() in [n.casefold() for n in e.get(key, [])]]
            if not selected:
                error = (f"No exposed entities matched name '{args[field]}'" if field == "name"
                         else f"No exposed entities found in area '{args[field]}'")
                return {"success": False, "error": error}, []
    if domains:
        selected = [e for e in selected if e["domain"] in domains]
        if not selected:
            return {"success": False, "error": f"No exposed entities found in domain(s): {', '.join(domains)}"}, []
    return {"success": True, "result": context_text(selected)}, sorted(e["entity_id"] for e in selected)


NUMBER_WORDS = {
    "zero": 0, "no": 0, "none": 0, "one": 1, "two": 2, "three": 3,
    "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20,
}
NUM = r"(?:\d+|" + "|".join(NUMBER_WORDS) + r")"
COUNT_PATTERNS = [
    re.compile(rf"(?=(?<![\w.,])(?P<n>{NUM})\s+(?:of\s+(?:the\s+)?)?light(?:s|\s+entities)?\b(?P<middle>[^.;!?\n]{{0,70}}?)\bon\b)", re.I),
    re.compile(rf"(?<![\w.,])(?P<n>{NUM})\s+(?:are|is)\s+(?:currently\s+|still\s+)?on\b", re.I),
]


def visible_text(content):
    text = re.sub(r"<think>.*?</think>", "", content or "", flags=re.S)
    return "" if "<think>" in text else text.strip()


def stated_count(answer):
    """Return an unambiguous positive on-count, else None for human review."""
    candidates = []
    text = visible_text(answer)
    hedge = r"\b(?:not|maybe|perhaps|might|could|about|around|approximately|least|most|more|fewer|less|than|if|or|were|was|yesterday|previously|earlier)\b"
    if re.search(hedge, text, re.I):
        return None
    if re.search(r"\b(?:but|however|actually|except)\b.*\b(?:off|not)\b", text, re.I):
        return None
    for pattern in COUNT_PATTERNS:
        for match in pattern.finditer(text):
            prefix = re.split(r"[.;!?\n]", text[:match.start()])[-1]
            if prefix.endswith("-") or re.search(hedge, prefix, re.I):
                continue
            middle = match.groupdict().get("middle", "")
            if re.search(r"\b(?:off|unavailable|unknown|not|were|was|lights?)\b", middle, re.I):
                continue
            word = match.group("n").lower()
            candidates.append(int(word) if word.isdigit() else NUMBER_WORDS[word])
    if re.search(r"\b(?:all|the)\s+lights\s+(?:are\s+)?(?:currently\s+)?off\b", text, re.I):
        if not re.search(hedge, text, re.I):
            candidates.append(0)
    return candidates[0] if candidates and len(set(candidates)) == 1 else None


def stated_scope(answer, scope, entities):
    """Check explicit scope claims; leave unfamiliar/compound claims for review."""
    text = visible_text(answer)
    area_claims = set()
    for entity in entities:
        for area in entity.get("areas", []):
            escaped = re.escape(area)
            if (re.search(rf"\b(?:in|throughout|across)\s+(?:the\s+)?{escaped}\b", text, re.I)
                    or re.search(rf"\b{escaped}\b\s+(?:currently\s+)?(?:has|have|contains)\b", text, re.I)
                    or re.search(rf"\b{escaped}\b\s*:", text, re.I)):
                area_claims.add(area.casefold())
    house_claim = bool(re.search(r"\b(?:whole\s+house|entire\s+house|housewide|everywhere)\b", text, re.I))
    if len(area_claims) > 1 or (area_claims and house_claim):
        return None
    if "area" not in scope:
        if area_claims:
            return False
    elif house_claim:
        return False
    elif area_claims:
        equivalent = {scope["area"].casefold()}
        for entity in entities:
            if scope["area"].casefold() in [a.casefold() for a in entity.get("areas", [])]:
                equivalent.update(a.casefold() for a in entity.get("areas", []))
        return bool(area_claims <= equivalent)
    if re.search(r"\b(?:in|throughout|across)\s+(?!total\b|all\b|fact\b)", text, re.I) and not (area_claims or house_claim):
        return None
    return True


def score_turn(turn, result, entities, previous_evidence=None, read_tool=READ_TOOL):
    truth = truth_for(entities, turn["scope"])
    evidence = set(previous_evidence or []) if not turn.get("require_read", True) else set()
    invalid = 0
    blocked = 0
    for event in result.get("events", []):
        for call in event.get("tool_calls", []):
            if call.get("blocked") or call.get("name") != read_tool:
                blocked += 1
                continue
            try:
                expected_result, ids = execute_fixture_tool(entities, call["name"], call["arguments"])
            except (Refused, KeyError, TypeError):
                invalid += 1
                continue
            # Never trust transcript-provided entity IDs or successful-read flags.
            if call.get("result") != expected_result:
                invalid += 1
            elif expected_result.get("success"):
                evidence.update(ids)
    answer = visible_text(result.get("final", ""))
    said = stated_count(answer)
    count_case = turn.get("expected_kind", "count") == "count"
    scope_claim = stated_scope(answer, turn["scope"], entities) if count_case else True
    grounded = bool(truth["entity_ids"]) and set(truth["entity_ids"]) <= evidence
    gates_pass = (
        result.get("status") == "complete" and bool(answer) and not blocked and not invalid
        and (not count_case or ((said is None or said == truth["count"]) and grounded and scope_claim is not False))
    )
    manual = result.get("manual_review")
    if manual is not None and (not isinstance(manual, dict) or type(manual.get("pass")) is not bool
                               or not isinstance(manual.get("reason"), str) or not manual["reason"].strip()):
        raise Refused("manual_review needs a boolean pass and a concrete reason")
    needs_manual = "manual_criterion" in turn or (count_case and (said is None or scope_claim is None))
    success = (None if needs_manual and manual is None and gates_pass else
               gates_pass and (not needs_manual or (manual is not None and manual["pass"])))
    if result.get("status") == "not_run":
        success = None
    score = {
        "pass": success, "automatic_gates_pass": gates_pass,
        "manual_review_required": needs_manual, "manual_criterion": turn.get("manual_criterion"),
        "manual_review": manual, "status": result.get("status"), "nonempty_final": bool(answer),
        "expected": truth, "stated_count": said,
        "count_correct": said == truth["count"] if count_case and said is not None else None,
        "stated_scope_correct": scope_claim if count_case else None,
        "grounded_scope": grounded if count_case else None,
        "blocked_calls": blocked, "invalid_calls": invalid,
        "count_parse_review": count_case and bool(answer) and said is None,
    }
    return score, evidence


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Refused("backend redirects are refused")


def safe_error(exception):
    result = {"exception_class": type(exception).__name__}
    reason = exception.reason if isinstance(exception, urllib.error.URLError) else exception
    result["reason_class"] = type(reason).__name__
    if isinstance(reason, OSError) and reason.errno is not None:
        result["errno"] = reason.errno
    if isinstance(exception, urllib.error.HTTPError):
        result["http_status"] = exception.code
    return result


class Backend:
    def __init__(self, base_url, api_key="", opener=None):
        if base_url.rstrip("/") != BACKEND_URL:
            raise Refused("inference is restricted to the verified in-cluster llama-server endpoint")
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def complete(self, payload, deadline):
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers=headers)
        started = time.monotonic()
        remaining = deadline - started
        if remaining <= 0:
            raise TimeoutError("turn deadline exceeded")
        content = []
        calls = {}
        event = {"tool_calls": [], "ttft_seconds": None, "first_payload_seconds": None,
                 "usage": None, "timings": None, "finish_reason": None}
        timed_out = threading.Event()
        timer = None
        with self.opener.open(request, timeout=remaining) as response:
            transport_socket = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
            if transport_socket is not None:
                def expire():
                    timed_out.set()
                    try:
                        transport_socket.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("turn deadline exceeded")
                timer = threading.Timer(remaining, expire)
                timer.daemon = True
                timer.start()
            try:
                stream = self._lines(response, deadline, transport_socket)
                for raw in stream:
                    self._consume_chunk(raw, started, event, content, calls)
                    if raw[5:].strip() == b"[DONE]" and raw.startswith(b"data:"):
                        break
            except OSError:
                if timed_out.is_set():
                    raise TimeoutError("turn deadline exceeded") from None
                raise
            finally:
                if timer:
                    timer.cancel()
            if timed_out.is_set():
                raise TimeoutError("turn deadline exceeded")
        message = {"role": "assistant", "content": "".join(content)}
        if event["first_payload_seconds"] is None or event["finish_reason"] is None:
            raise Refused("backend stream ended without a terminal completion")
        if calls:
            ids = [call["id"] for call in calls.values()]
            if any(not isinstance(i, str) or not i for i in ids) or len(ids) != len(set(ids)):
                raise Refused("completed tool calls require unique nonempty IDs")
            message["tool_calls"] = [calls[k] for k in sorted(calls)]
        event["elapsed_seconds"] = time.monotonic() - started
        event["response_content"] = message["content"]
        return message, event

    @staticmethod
    def _lines(response, deadline, transport_socket):
        buffer = b""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("turn deadline exceeded")
            if transport_socket is not None:
                transport_socket.settimeout(remaining)
            block = response.read1(8192)
            if not block:
                return
            buffer += block
            if len(buffer) > 1024 * 1024:
                raise Refused("stream line exceeds the bounded protocol budget")
            while b"\n" in buffer:
                raw, buffer = buffer.split(b"\n", 1)
                yield raw

    @staticmethod
    def _consume_chunk(raw, started, event, content, calls):
        now = time.monotonic()
        if not raw.startswith(b"data:"):
            return
        data = raw[5:].strip()
        if data == b"[DONE]":
            return
        chunk = json.loads(data)
        if "error" in chunk:
            raise Refused("backend returned a streaming error")
        if event["first_payload_seconds"] is None:
            event["first_payload_seconds"] = now - started
        if chunk.get("usage") is not None:
            event["usage"] = chunk["usage"]
        if chunk.get("timings") is not None:
            event["timings"] = chunk["timings"]
        choices = chunk.get("choices", [])
        if len(choices) > 1:
            raise Refused("only one completion choice is supported")
        for choice in choices:
            choice_index = choice.get("index", 0)
            if type(choice_index) is not int or choice_index != 0:
                raise Refused("only completion choice index zero is supported")
            if choice.get("finish_reason") is not None:
                event["finish_reason"] = choice["finish_reason"]
            delta = choice.get("delta", {})
            if delta.get("content"):
                if event["ttft_seconds"] is None:
                    event["ttft_seconds"] = now - started
                content.append(delta["content"])
            for incoming in delta.get("tool_calls", []):
                if incoming.get("type", "function") != "function" or type(incoming.get("index")) is not int or incoming["index"] < 0:
                    raise Refused("stream tool calls must be indexed functions")
                call = calls.setdefault(incoming["index"], {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                if incoming.get("id"):
                    call["id"] = incoming["id"]
                function = incoming.get("function", {})
                call["function"]["name"] += function.get("name", "")
                call["function"]["arguments"] += function.get("arguments", "")

def run_turn(backend, profile, messages, turn, entities, total_deadline):
    started = time.monotonic()
    deadline = min(started + TURN_TIMEOUT, total_deadline)
    messages.append({"role": "user", "content": turn["text"]})
    result = {"id": turn["id"], "text": turn["text"], "events": [], "final": "", "status": "complete"}
    tool_rounds = 0
    read_tool = read_tool_for(profile)
    try:
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("turn deadline exceeded")
            payload = copy.deepcopy(profile.get("settings", {}))
            payload.update(model=profile["model"], messages=copy.deepcopy(messages), tools=profile["tools"], stream=True,
                           stream_options={"include_usage": True},
                           max_tokens=min(payload.get("max_tokens", MAX_OUTPUT_TOKENS), MAX_OUTPUT_TOKENS))
            call_started = time.monotonic()
            message, event = backend.complete(payload, deadline)
            event["started_after_turn_seconds"] = call_started - started
            result["events"].append(event)
            messages.append(message)
            calls = message.get("tool_calls", [])
            if event.get("finish_reason") not in ("stop", "tool_calls", "length"):
                result["final"] = message.get("content", "")
                result["status"] = ("content_filter" if event.get("finish_reason") == "content_filter"
                                    else "unexpected_finish_reason")
                break
            if event.get("finish_reason") == "tool_calls" and not calls:
                result["status"] = "backend_protocol_error"
                break
            if event.get("finish_reason") == "length":
                result["final"] = message.get("content", "")
                event["tool_calls"] = [{"name": c["function"]["name"], "arguments_raw": c["function"]["arguments"],
                                        "blocked": c["function"]["name"] != read_tool, "output_token_limit": True}
                                       for c in calls]
                result["status"] = "output_token_limit"
                break
            if not calls:
                result["final"] = message.get("content", "")
                break
            if tool_rounds >= MAX_TOOL_ROUNDS:
                event["tool_calls"] = [
                    {"name": c["function"]["name"], "arguments_raw": c["function"]["arguments"],
                     "blocked": c["function"]["name"] != read_tool, "round_limit": True}
                    for c in calls
                ]
                result["status"] = "tool_round_limit"
                break
            tool_rounds += 1
            for call in calls:
                name = call["function"]["name"]
                record = {"name": name, "arguments_raw": call["function"]["arguments"], "blocked": name != read_tool}
                try:
                    arguments = json.loads(record["arguments_raw"])
                    record["arguments"] = arguments
                    if record["blocked"]:
                        raise Refused("tool is not the profile's exact advertised GetLiveContext")
                    tool_result, ids = execute_fixture_tool(entities, name, arguments)
                    record.update(result=tool_result, matched_entity_ids=ids)
                except (Refused, json.JSONDecodeError, TypeError):
                    record["invalid"] = True
                    tool_result = {"success": False, "error": "Benchmark refused this tool call"}
                    record["result"] = tool_result
                event["tool_calls"].append(record)
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(tool_result, ensure_ascii=False)})
    except urllib.error.HTTPError as ex:
        result.update(status="http_error", http_status=ex.code, error=safe_error(ex))
    except TimeoutError as ex:
        result.update(status="timeout", error=safe_error(ex))
    except urllib.error.URLError as ex:
        result["status"] = "timeout" if isinstance(ex.reason, TimeoutError) else "transport_error"
        result["error"] = safe_error(ex)
    except (OSError, http.client.HTTPException) as ex:
        result.update(status="transport_error", error=safe_error(ex))
    except (Refused, ValueError, KeyError, TypeError) as ex:
        result.update(status="backend_protocol_error", error=safe_error(ex))
    except KeyboardInterrupt as ex:
        result.update(status="cancelled", error=safe_error(ex))
    result["tool_rounds"] = tool_rounds
    result["elapsed_seconds"] = time.monotonic() - started
    first_text = [e["ttft_seconds"] + e["started_after_turn_seconds"]
                  for e in result["events"] if e.get("ttft_seconds") is not None]
    result["first_visible_text_seconds"] = first_text[0] if first_text else None
    return result


def score_report(suite, report):
    validate_suite(suite)
    if report.get("mode") != MODE:
        raise Refused("only direct backend frozen-fixture reports are scored here")
    if report.get("suite_sha256") != suite_hash(suite):
        raise Refused("report must match the complete suite, including fixtures and profiles")
    expected_ids = [s["id"] for s in suite["scenarios"]]
    if [s.get("id") for s in report.get("scenarios", [])] != expected_ids:
        raise Refused("report scenarios must exactly match the suite")
    passed = 0
    failed = 0
    pending = 0
    not_run = 0
    total = 0
    for scenario, recorded in zip(suite["scenarios"], report["scenarios"]):
        if [t.get("id") for t in recorded.get("turns", [])] != [t["id"] for t in scenario["turns"]]:
            raise Refused("report turns must exactly match the suite")
        evidence = set()
        for turn, result in zip(scenario["turns"], recorded["turns"]):
            result["score"], new_evidence = score_turn(turn, result, scenario["entities"], evidence,
                                                      read_tool_for(profile_for(suite, scenario)))
            evidence.update(new_evidence)
            passed += result["score"]["pass"] is True
            failed += result["score"]["pass"] is False
            pending += result["score"]["pass"] is None and result.get("status") != "not_run"
            not_run += result.get("status") == "not_run"
            total += 1
    report["summary"] = {"passed": passed, "failed": failed, "manual_pending": pending,
                         "not_run": not_run, "attempted_turns": total - not_run,
                         "completed_model_requests": sum(len(t.get("events", [])) for s in report["scenarios"] for t in s["turns"]),
                         "total": total, "all_pass": passed == total,
                         "mode": MODE, "native_ha_pipeline": False, "state_source": "frozen_fixture"}
    return report


def save_private(path, value):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def run_suite(suite, backend, output):
    validate_suite(suite)
    report = {"schema_version": 1, "mode": MODE, "suite_sha256": suite_hash(suite),
              "profiles": copy.deepcopy(suite.get("profiles", {"default": suite.get("profile")})),
              "limits": {"max_user_turns": MAX_TURNS, "max_tool_rounds": MAX_TOOL_ROUNDS,
                         "turn_timeout_seconds": TURN_TIMEOUT, "total_timeout_seconds": TOTAL_TIMEOUT,
                         "max_output_tokens": MAX_OUTPUT_TOKENS}, "scenarios": []}
    total_deadline = time.monotonic() + TOTAL_TIMEOUT
    stopped = False
    for scenario in suite["scenarios"]:
        profile = profile_for(suite, scenario)
        static = context_text([e for e in scenario["entities"] if e.get("exposed", True)], include_state=False)
        messages = copy.deepcopy(profile["messages"])
        for msg in messages:
            msg["content"] = msg["content"].replace(STATIC_MARKER, static)
        recorded = {"id": scenario["id"], "profile": scenario.get("profile", "default"),
                    "turns": [], "initial_messages": copy.deepcopy(messages)}
        report["scenarios"].append(recorded)
        for turn in scenario["turns"]:
            if stopped or time.monotonic() >= total_deadline:
                result = {"id": turn["id"], "text": turn["text"], "status": "not_run", "final": "", "events": []}
            else:
                result = run_turn(backend, profile, messages, turn, scenario["entities"], total_deadline)
                if result["status"] != "complete":
                    stopped = True
            recorded["turns"].append(result)
            save_private(output, report)  # persist even cancellation, empty speech or partial runs
    score_report(suite, report)
    save_private(output, report)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("validate", "score", "run"))
    parser.add_argument("--suite", required=True)
    parser.add_argument("--profiles")
    parser.add_argument("--report")
    parser.add_argument("--out")
    args = parser.parse_args()
    suite = json.loads(Path(args.suite).read_text())
    if args.profiles:
        if "profile" in suite or "profiles" in suite:
            raise Refused("--profiles may only complete a case manifest without profiles")
        supplied = json.loads(Path(args.profiles).read_text())
        suite["profiles"] = supplied.get("profiles", supplied)
        if "provenance" in supplied:
            suite["profile_provenance"] = supplied["provenance"]
    count = validate_suite(suite)
    if args.mode == "validate":
        summary = {"mode": "fixture_validation_only", "user_turns": count, "model_called": False,
                   "expected": [{"scenario": s["id"], "turn": t["id"], **truth_for(s["entities"], t["scope"])}
                                for s in suite["scenarios"] for t in s["turns"]]}
        if args.out:
            save_private(args.out, summary)
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    if not args.out:
        raise Refused("--out is required for durable private evidence")
    if args.mode == "score":
        if not args.report:
            raise Refused("--report is required")
        report = score_report(suite, json.loads(Path(args.report).read_text()))
        save_private(args.out, report)
    else:
        if os.environ.get("RUN_VOICE_TOOL_BENCH") != "1":
            raise Refused("RUN_VOICE_TOOL_BENCH=1 is required before model inference")
        backend = Backend(os.environ["BENCH_BASE_URL"], os.environ.get("BENCH_API_KEY", ""))
        report = run_suite(suite, backend, args.out)
    print(json.dumps(report["summary"]))
    return 0 if report["summary"]["all_pass"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (Refused, KeyError, ValueError) as ex:
        # Configuration diagnostics contain no transport headers or backend bodies.
        print(f"Refused: {ex}")
        raise SystemExit(2)
