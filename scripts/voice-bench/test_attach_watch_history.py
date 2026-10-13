"""Watch helper guards with fake HA transports and deterministic save/reload reads."""

import asyncio
from contextlib import redirect_stdout
from copy import deepcopy
import io
import os
import runpy
import unittest
from unittest.mock import AsyncMock, Mock, patch

with patch.dict(os.environ, {"HA_TOKEN": "unit-test-token"}):
    import attach_watch_history as helper


def success(result):
    return {"id": 1, "type": "result", "success": True, "result": result}


def failure(code):
    return {"id": 1, "type": "result", "success": False,
            "error": {"code": code, "message": "private-provider-detail"}}


class FakeWebsocket:
    def __init__(self, response):
        self.responses = [{"type": "auth_required"}, {"type": "auth_ok"}, response]
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def receive_json(self):
        return self.responses.pop(0)

    async def send_json(self, message):
        self.sent.append(message)


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.websockets = []

    def ws_connect(self, url, **kwargs):
        if not self.responses:
            raise AssertionError("Unexpected websocket request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        ws = FakeWebsocket(response)
        self.websockets.append(ws)
        return ws

    @property
    def commands(self):
        return [ws.sent[-1] for ws in self.websockets if ws.sent and ws.sent[-1].get("id")]


class WatchMaintenanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.enterContext(redirect_stdout(self.output))
        self.backup = self.enterContext(patch.object(helper, "backup"))
        self.loaded = self.enterContext(patch.object(helper, "require_conversation_loaded", AsyncMock()))
        self.sleep = self.enterContext(patch.object(helper.asyncio, "sleep", AsyncMock()))
        self.wait_state = self.enterContext(patch.object(helper, "wait_state", AsyncMock(return_value="loaded")))
        self.req = self.enterContext(patch.object(helper, "req", AsyncMock()))
        self.current = {
            "prompt": "Original prompt",
            "llm_hass_api": ["assist", helper.WATCH_API, "mcp-another-api"],
            "chat_model": "test-model",
            "web_search": True,
            "user_location": True,
            "city": "Saved city",
            "region": "Saved region",
            "country": "CA",
            "timezone": "America/Toronto",
            "max_tokens": 7777,
            "temperature": 0.7,
            "nested": {"unicode": "Zürich", "enabled": True},
        }

    def status(self, data=None):
        return success({"status": "loaded", "hash": helper.data_hash(data or self.current)})

    def updated(self, target, changed=True):
        return success({"status": "updated" if changed else "unchanged", "changed": changed,
                        "hash": helper.data_hash(target)})

    def stored(self, data):
        return [{"entry_id": helper.CONVERSATION_ENTRY, "subentries": [
            {"subentry_id": helper.SUBENTRY, "data": data}]}]

    async def change(self, session, target):
        await helper.change(session, self.current, target["llm_hass_api"], target["prompt"], [], "undo")

    def test_canonical_hash_sorts_keys_and_escapes_unicode(self):
        import hashlib
        expected = hashlib.sha256(b'{"a":1,"z":"Z\\u00fcrich"}').hexdigest()
        self.assertEqual(helper.data_hash({"z": "Zürich", "a": 1}), expected)
        self.assertEqual(helper.data_hash({"a": 1, "z": "Zürich"}), expected)
        self.assertNotEqual(helper.data_hash({"a": True}), helper.data_hash({"a": 1}))

    async def test_unsafe_openai_without_maintenance_refuses_before_flow_or_backup(self):
        target = dict(self.current, prompt="Updated prompt")
        session = FakeSession(failure("unknown_command"))
        flow = self.enterContext(patch.object(helper, "reconfigure", AsyncMock()))
        with self.assertRaisesRegex(helper.Refused, "official reconfigure flow.*not been started"):
            await self.change(session, target)
        flow.assert_not_awaited()
        self.backup.assert_not_called()
        self.req.assert_not_awaited()
        self.assertEqual(session.commands, [{"id": 1, "type": "hnet_watch_maintenance/status", "agent": helper.AGENT}])

    async def test_direct_flow_guard_covers_both_location_flag_spellings_and_target(self):
        for key in ("user_location", "web_search_user_location"):
            for unsafe_side in ("current", "target"):
                with self.subTest(key=key, side=unsafe_side):
                    safe = {"web_search": True, "prompt": "old"}
                    unsafe = dict(safe, **{key: True})
                    current, target = (unsafe, safe) if unsafe_side == "current" else (safe, unsafe)
                    with self.assertRaises(helper.Refused):
                        await helper.reconfigure(FakeSession(), current, target, [])
        self.req.assert_not_awaited()

    async def test_safe_openai_and_jarvis_keep_guarded_flow_fallback(self):
        flow = self.enterContext(patch.object(helper, "reconfigure", AsyncMock()))
        verify = self.enterContext(patch.object(helper, "verify", AsyncMock()))
        cases = (("openai_conversation", False), ("llama_cpp", True))
        for domain, location in cases:
            with self.subTest(domain=domain), patch.object(helper, "AGENT_DOMAIN", domain):
                self.current["user_location"] = location
                target = dict(self.current, prompt="Updated prompt")
                await self.change(FakeSession(failure("unknown_command")), target)
                flow.assert_awaited_with(unittest.mock.ANY, self.current, target, [])
                verify.assert_awaited_with(unittest.mock.ANY, self.current, target)
        self.assertEqual(flow.await_count, 2)

    async def test_local_rumpus_selects_own_subentry_and_uses_guarded_native_flow(self):
        with patch.dict(os.environ, {"HA_TOKEN": "unit-test-token", "AGENT": "rumpus_local"}):
            selected = runpy.run_path(helper.__file__, run_name="watch_helper_selection")
        entry, subentry, _ = selected["AGENTS"]["rumpus_local"]
        cloud_entry, cloud_subentry, _ = selected["AGENTS"]["rumpus"]
        entries = [
            {"entry_id": cloud_entry, "subentries": [{"subentry_id": cloud_subentry, "data": {"prompt": "rollback"}}]},
            {"entry_id": entry, "subentries": [{"subentry_id": subentry, "data": {"prompt": "live"}}]},
        ]
        self.assertEqual(selected["get_subentry"](entries)["data"]["prompt"], "live")
        self.assertEqual(selected["AGENT_DOMAIN"], "llama_cpp")
        with patch.object(helper, "AGENT", "rumpus_local"), patch.object(helper, "AGENT_DOMAIN", selected["AGENT_DOMAIN"]):
            flow = self.enterContext(patch.object(helper, "reconfigure", AsyncMock()))
            verify = self.enterContext(patch.object(helper, "verify", AsyncMock()))
            maintenance = self.enterContext(patch.object(helper, "maintenance_command", AsyncMock()))
            target = dict(self.current, prompt="Updated prompt")
            await self.change(FakeSession(), target)
            flow.assert_awaited_once_with(unittest.mock.ANY, self.current, target, [])
            verify.assert_awaited_once_with(unittest.mock.ANY, self.current, target)
            maintenance.assert_not_awaited()

    async def test_watch_update_preserves_local_persona_and_trailing_tool_explanations(self):
        prefix = "Jarvis persona and Rumpus room rules.\n\n"
        tail = "\n\nWhen Tom asks about tools, explain actual supplied definitions and recorded results."
        old = helper.PREVIOUS_WATCH_BLOCKS[0]
        current = {"prompt": prefix + old + tail, "llm_hass_api": ["assist", helper.WATCH_API],
                   "chat_model": "muse-glimmer-30b", "recommended": True}
        entries = self.stored(current)
        change = self.enterContext(patch.object(helper, "change", AsyncMock()))
        self.enterContext(patch.object(helper, "load_entries", return_value=entries))
        self.enterContext(patch.object(helper, "need_mcp_entry"))
        self.enterContext(patch.object(helper, "require_mcp_loaded", AsyncMock()))
        self.enterContext(patch.object(helper, "ENTRY_ID", helper.WATCH_API.removeprefix("mcp-")))
        self.enterContext(patch.object(helper, "PROMPT_FILE", ""))
        self.enterContext(patch.object(helper, "DRY_RUN", False))
        await helper.update(FakeSession())
        args = change.await_args.args
        self.assertEqual(args[1], current)
        self.assertEqual(args[2], current["llm_hass_api"])
        self.assertEqual(args[3], prefix + helper.WATCH_BLOCK + tail)

    async def test_original_agents_use_native_snapshot_hash_and_full_readback(self):
        target = dict(self.current, prompt="Updated prompt")
        original = deepcopy(self.current)
        flow = self.enterContext(patch.object(helper, "reconfigure", AsyncMock()))
        for agent in ("movie", "rumpus", "phone", "jarvis"):
            with self.subTest(agent=agent), patch.object(helper, "AGENT", agent):
                session = FakeSession(self.status(), self.updated(target))
                with patch.object(helper, "load_entries", return_value=self.stored(target)):
                    await self.change(session, target)
                self.assertEqual(session.commands[1], {
                    "id": 1, "type": "hnet_watch_maintenance/replace_prompt", "agent": agent,
                    "prompt": "Updated prompt", "expected_sha256": helper.data_hash(self.current),
                })
                self.assertEqual(set(session.commands[0]), {"id", "type", "agent"})
                self.assertNotIn("city", session.commands[1])
                self.assertNotIn("data", session.commands[1])
        self.assertEqual(self.current, original)
        self.assertIn("complete subentry data matches the target", self.output.getvalue())
        flow.assert_not_awaited()
        self.req.assert_not_awaited()

    async def test_native_attachment_is_atomic_and_preserves_other_api_order_and_data(self):
        flow = self.enterContext(patch.object(helper, "reconfigure", AsyncMock()))
        for action in ("attach", "detach"):
            with self.subTest(action=action):
                before = ["assist", "mcp-first", "mcp-last"]
                if action == "detach":
                    before.insert(2, helper.WATCH_API)
                self.current["llm_hass_api"] = before
                wanted = [*before, helper.WATCH_API] if action == "attach" else [api for api in before if api != helper.WATCH_API]
                target = dict(self.current, prompt="Updated prompt", llm_hass_api=wanted)
                session = FakeSession(self.status(), self.updated(target))
                with patch.object(helper, "load_entries", return_value=self.stored(target)):
                    await self.change(session, target)
                self.assertEqual(session.commands[1]["watch_attachment"], action)
                self.assertNotIn("llm_hass_api", session.commands[1])
                self.assertEqual([api for api in target["llm_hass_api"] if api != helper.WATCH_API],
                                 ["assist", "mcp-first", "mcp-last"])
                for key in set(self.current) - {"prompt", "llm_hass_api"}:
                    self.assertEqual(target[key], self.current[key])
        flow.assert_not_awaited()

    async def test_native_rejects_arbitrary_api_change_before_backup_or_mutation(self):
        target = dict(self.current, prompt="Updated prompt", llm_hass_api=[*self.current["llm_hass_api"], "mcp-new"])
        session = FakeSession(self.status())
        with self.assertRaisesRegex(helper.Refused, "fixed Watch history attachment"):
            await self.change(session, target)
        self.backup.assert_not_called()
        self.assertEqual(len(session.commands), 1)
        self.req.assert_not_awaited()

    async def test_native_status_stale_hash_rejects_before_backup_or_mutation(self):
        stale = dict(self.current, city="Concurrent location")
        session = FakeSession(self.status(stale))
        with self.assertRaisesRegex(helper.Refused, "hash differs from the snapshot"):
            await self.change(session, dict(self.current, prompt="Updated prompt"))
        self.backup.assert_not_called()
        self.assertEqual(len(session.commands), 1)
        self.req.assert_not_awaited()

    async def test_native_concurrent_conflict_has_no_flow_fallback_or_error_detail(self):
        session = FakeSession(self.status(), failure("conflict"))
        verify = self.enterContext(patch.object(helper, "verify", AsyncMock()))
        with self.assertRaisesRegex(helper.Refused, "stale or concurrent") as raised:
            await self.change(session, dict(self.current, prompt="Updated prompt"))
        self.assertNotIn("private-provider-detail", str(raised.exception))
        self.assertNotIn("private-provider-detail", self.output.getvalue())
        verify.assert_not_awaited()
        self.req.assert_not_awaited()

    async def test_native_disappearing_after_preflight_never_falls_back_to_flow(self):
        session = FakeSession(self.status(), failure("unknown_command"))
        with self.assertRaisesRegex(helper.Refused, "no official reconfigure flow attempted"):
            await self.change(session, dict(self.current, prompt="Updated prompt"))
        self.req.assert_not_awaited()

    async def test_native_wrong_result_hash_cannot_claim_success(self):
        target = dict(self.current, prompt="Updated prompt")
        session = FakeSession(self.status(), self.updated(dict(target, city="Unexpected drift")))
        verify = self.enterContext(patch.object(helper, "verify", AsyncMock()))
        with self.assertRaisesRegex(helper.Refused, "does not match the complete target"):
            await self.change(session, target)
        verify.assert_not_awaited()
        self.req.assert_not_awaited()

    async def test_full_stored_readback_rejects_unrelated_field_drift(self):
        target = dict(self.current, prompt="Updated prompt")
        session = FakeSession(self.status(), self.updated(target))
        drift = dict(target, city="Unexpected drift")
        with patch.object(helper, "load_entries", return_value=self.stored(drift)):
            with self.assertRaisesRegex(helper.Refused, "complete subentry readback.*city") as raised:
                await self.change(session, target)
        self.assertNotIn("Unexpected drift", str(raised.exception))
        self.assertNotIn("Unexpected drift", self.output.getvalue())
        self.wait_state.assert_not_awaited()
        self.req.assert_not_awaited()

    async def test_readback_waits_for_storage_save_then_checks_complete_data(self):
        target = dict(self.current, prompt="Updated prompt")
        with patch.object(helper, "load_entries", side_effect=[self.stored(self.current), self.stored(target)]):
            await helper.verify(FakeSession(), self.current, target)
        self.assertEqual(self.sleep.await_count, 2)
        self.wait_state.assert_awaited_once()

    async def test_readback_timeout_refuses_instead_of_warning_success(self):
        target = dict(self.current, prompt="Updated prompt")
        with patch.object(helper, "load_entries", return_value=self.stored(self.current)):
            with self.assertRaisesRegex(helper.Refused, "did not match the target within 20 s"):
                await helper.verify(FakeSession(), self.current, target)
        self.assertEqual(self.sleep.await_count, 20)
        self.wait_state.assert_not_awaited()

    async def test_transport_timeout_is_fail_closed_even_for_safe_openai(self):
        self.current["user_location"] = False
        with self.assertRaisesRegex(helper.Refused, "no official reconfigure flow attempted"):
            await self.change(FakeSession(TimeoutError("private-provider-detail")),
                              dict(self.current, prompt="Updated prompt"))
        self.backup.assert_not_called()
        self.req.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
