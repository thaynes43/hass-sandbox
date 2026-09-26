"""Attach haynesnetwork's "Watch history" MCP API to the Movie Room agent, or roll it back.

Runs INSIDE the Home Assistant pod via run.sh (the HA token arrives on stdin as HA_TOKEN and is
never printed). One ACTION per run:

    scripts/voice-bench/run.sh attach_watch_history.py "ACTION=status"      # read-only, the default
    scripts/voice-bench/run.sh attach_watch_history.py "ACTION=add-entry URL=http://haynesnetwork-mcp-hop.frontend.svc.cluster.local:8080/mcp"
    scripts/voice-bench/run.sh attach_watch_history.py "ACTION=attach ENTRY_ID=<mcp entry id>"
    scripts/voice-bench/run.sh attach_watch_history.py "ACTION=update ENTRY_ID=<mcp entry id> DRY_RUN=1"   # prints, changes nothing
    scripts/voice-bench/run.sh attach_watch_history.py "ACTION=update ENTRY_ID=<mcp entry id>"
    scripts/voice-bench/run.sh attach_watch_history.py "ACTION=update ENTRY_ID=<mcp entry id> PROMPT_FILE=<update backup in the HA pod> [DRY_RUN=1]"   # undoes an update
    scripts/voice-bench/run.sh attach_watch_history.py "ACTION=detach ENTRY_ID=<mcp entry id> [PROMPT_FILE=<attach backup in the HA pod>] [BLOCK_ONLY=1]"

How each action talks to HA (read against the HA 2026.9.3 source in the pod):

- status: reads /config/.storage/core.config_entries (as openai_agents.py does) and
  GET /api/config/config_entries/entry?domain=... for load state. Changes nothing.
- add-entry: the `mcp` config flow over REST (components/mcp/config_flow.py).
  POST /api/config/config_entries/flow {"handler": "mcp"} -> form step "user" (field `url`)
  -> POST /api/config/config_entries/flow/<flow_id> {"url": URL}. A server that answers the MCP
  handshake without a 401 gets its entry created right there, titled with the server's
  initialize `serverInfo.name`, data {"url": URL}. A 401 would divert into OAuth discovery
  (auth_discovery -> credentials_choice); this helper cancels that instead of following it.
- update: swaps an earlier WATCH HISTORY block for the current one, in place, when the block text
  changes. The live prompt must hold exactly one WATCH HISTORY block (its header line through the
  line before the next blank line) whose text is WATCH_BLOCK (nothing to do) or one of
  PREVIOUS_WATCH_BLOCKS, and llm_hass_api must already carry the entry's API; anything else is
  refused. llm_hass_api is resubmitted unchanged. DRY_RUN=1 prints the block before and after and
  the length change, and starts no flow. With PROMPT_FILE, the block swapped in is the one in that
  backup instead of WATCH_BLOCK (it too must be WATCH_BLOCK or one of PREVIOUS_WATCH_BLOCKS): that is
  how an update is undone, and the update prints the exact line.
- Undoing a change: attach, update and detach print the subentry data they replace (BACKUP_BEGIN ..
  BACKUP_END) and write it to /tmp in the HA pod, which a pod restart wipes, then print the line
  that undoes them. An attach made from a prompt with no block is undone by
  ACTION=detach PROMPT_FILE=<its backup> (the prompt byte for byte, the API removed); an update by
  ACTION=update PROMPT_FILE=<its backup> (its block back in place, the API kept). A detach has no
  exact undo: ACTION=attach attaches again with the current WATCH_BLOCK. Once /tmp is gone, restore
  the backup's prompt by hand in HA's UI (the agent's Instructions field).
- attach / update / detach: the OpenAI conversation subentry's reconfigure flow, REST only. HA has no
  websocket command for subentry flows, and the websocket `config_entries/subentries/update`
  changes the title only. (components/openai_conversation/config_flow.py OpenAISubentryFlowHandler)
  POST /api/config/config_entries/subentries/flow {"handler": [entry_id, "conversation"], "subentry_id": id}
  -> step "reconfigure" copies the stored data and shows form "init": prompt, llm_hass_api, recommended
  -> form "additional": chat_model, max_tokens, top_p, temperature, store_responses
  -> form "model": code_interpreter, reasoning_effort, pro_mode, verbosity, reasoning_summary,
     service_tier, web_search, search_context_size, user_location, inline_citations
     (the set depends on chat_model)
  -> abort "reconfigure_successful" (the subentry data is replaced by the flow's options).
  Every field of every form is submitted with its CURRENT value: an omitted field takes its schema
  default (max_tokens would fall to 3000). Each form's suggested values (HA's live copy) are checked
  against the .storage backup before anything is submitted. Only the final "model" submit writes. Any
  earlier stop DELETEs the flow and nothing changes.

Known limits:
- With web_search and user_location both on, the "model" submit re-derives city/region by asking
  OpenAI to reverse-geocode zone.home, and country/timezone from HA's config. No flow path skips
  that, so those four keys may be rewritten. The stored data is diffed afterwards and any drift is
  printed.
- Saving the subentry fires the OpenAI entry's update listener, which reloads the entry: every
  agent under it is briefly unavailable.
- attach and update need the mcp entry LOADED: the llm_hass_api selector only accepts registered
  API ids.
- HA's mcp integration reads the server's tool list once, when the entry is set up (its coordinator
  has no listeners, so the 30-minute refresh never runs). After the server gains or loses a tool,
  reload the mcp entry (homeassistant.reload_config_entry) or restart HA; this helper does neither.
"""

import asyncio
import datetime
import json
import os
import re
import sys

import aiohttp

sys.stdout.reconfigure(errors="backslashreplace")

TOK = os.environ["HA_TOKEN"]
BASE = "http://localhost:8123"
H = {"Authorization": f"Bearer {TOK}"}
STORAGE = "/config/.storage/core.config_entries"

ACTION = os.environ.get("ACTION", "status")
URL = os.environ.get("URL", "http://haynesnetwork-mcp-hop.frontend.svc.cluster.local:8080/mcp")
ENTRY_ID = os.environ.get("ENTRY_ID", "")
PROMPT_FILE = os.environ.get("PROMPT_FILE", "")  # ACTION=update (an update backup) or detach (an attach backup) only
DRY_RUN = os.environ.get("DRY_RUN", "") not in ("", "0")  # ACTION=update only
# ACTION=detach only: this id's API is already gone (removed by hand) while another mcp API is attached;
# change only the prompt. Without it that state refuses, because a mistyped ENTRY_ID looks the same.
BLOCK_ONLY = os.environ.get("BLOCK_ONLY", "") not in ("", "0")

OPENAI_ENTRY = "01JK456T3JV6CPBG2ZQ2FS10GE"
SUBENTRY = "01JZ8DWMCRND9599AR8EFJVN0A"  # "Movie Room ChatGPT", type conversation
SUBFLOW = "/api/config/config_entries/subentries/flow"
FLOW = "/api/config/config_entries/flow"
EXPECTED_TITLE = "Watch history"

# Byte-identical copy of the fenced block under "Movie Room — watch history" in
# agent-docs/voice-agent-prompts.md. Change both together, and move the text it replaces, verbatim,
# into PREVIOUS_WATCH_BLOCKS so ACTION=update recognises the copy that is live in HA.
WATCH_BLOCK = """WATCH HISTORY
- The watch history tools know Tom's own Plex viewing on every server and cover only his account. Use them for anything about what he has or hasn't watched, never guess, and don't search the web for it. If someone else asks about their own viewing, say you only know Tom's.
- "What haven't I finished" or "what was I watching": use unfinished and name the next episode of each show you mention. "What should I watch": use recommend, with kind show or movie when he says which, and offset to hear more after the first answer. Say at most three titles, each with a few words on why.
- When he says he already watched something, use mark_watched with that title and say back the title and year it marked. If he also wants something new, use recommend right after. If a tool says a title is ambiguous, ask which one he meant.
- "Undo that" right after a change means undo_last_change. "Not interested" means dismiss. "That was the kids, not me" means dismiss with reason not_mine.
- His Plex watchlist: use watchlist to list it (not recommend) and set_watchlist to add or remove a title, and say back the title and year it names. If set_watchlist or undo_last_change says Seerr will or may request a title, always tell him it will download."""

# Every earlier WATCH_BLOCK, verbatim, oldest first. ACTION=update replaces one of these in place.
PREVIOUS_WATCH_BLOCKS = [
    # 2026-09-23: attached with the seven watch-history tools
    """WATCH HISTORY
- The watch history tools know Tom's own Plex viewing on every server and cover only his account. Use them for anything about what he has or hasn't watched, never guess, and don't search the web for it. If someone else asks about their own viewing, say you only know Tom's.
- "What haven't I finished" or "what was I watching": use unfinished and name the next episode of each show you mention. "What should I watch": use recommend, with kind show or movie when he says which, and offset to hear more after the first answer. Say at most three titles, each with a few words on why.
- When he says he already watched something, use mark_watched with that title and say back the title and year it marked. If he also wants something new, use recommend right after. If a tool says a title is ambiguous, ask which one he meant.
- "Undo that" right after a change means undo_last_change. "Not interested" means dismiss. "That was the kids, not me" means dismiss with reason not_mine.""",
]
WATCH_HEADER = WATCH_BLOCK.splitlines()[0]  # "WATCH HISTORY"
SEP = "\n\n"  # one blank line between the existing prompt and the block

LOCATION_KEYS = ("city", "region", "country", "timezone")  # re-derived by the "model" step
# A key is credential-like when it contains one of CRED_WORDS or one of its _-separated words is in
# CRED_PARTS: a bare "token" or "key" field counts, "max_tokens" does not.
CRED_WORDS = ("api_key", "apikey", "password", "passwd", "secret", "access_token", "refresh_token")
CRED_PARTS = ("key", "token", "secret", "password")


class Refused(Exception):
    """Stop before (or instead of) changing anything."""


# ---------- reads ----------

def load_entries() -> list[dict]:
    with open(STORAGE) as f:
        return json.load(f)["data"]["entries"]


def get_subentry(entries: list[dict]) -> dict:
    for e in entries:
        if e["entry_id"] == OPENAI_ENTRY:
            for s in e.get("subentries", []):
                if s["subentry_id"] == SUBENTRY:
                    return s
            raise Refused(f"subentry {SUBENTRY} not found under {OPENAI_ENTRY}")
    raise Refused(f"config entry {OPENAI_ENTRY} not found")


def mcp_entries(entries: list[dict]) -> list[dict]:
    return [e for e in entries if e["domain"] == "mcp"]


def find_blocks(prompt: str) -> list[tuple[int, int]]:
    """(start, end) of each WATCH HISTORY block in the prompt: a line that is exactly the header,
    through the last line before a blank line, another header, or the end (no trailing newline)."""
    lines = prompt.split("\n")
    starts, pos = [], 0
    for line in lines:
        starts.append(pos)
        pos += len(line) + 1
    spans, i = [], 0
    while i < len(lines):
        if lines[i] != WATCH_HEADER:
            i += 1
            continue
        j = i
        while j + 1 < len(lines) and lines[j + 1].strip() and lines[j + 1] != WATCH_HEADER:
            j += 1
        spans.append((starts[i], starts[j] + len(lines[j])))
        i = j + 1
    return spans


def block_state(prompt: str) -> str:
    spans = find_blocks(prompt)
    if not spans:
        return "absent"
    if len(spans) > 1:
        return f"{len(spans)} WATCH HISTORY headers (unexpected: fix the prompt by hand)"
    text = prompt[spans[0][0]:spans[0][1]]
    if text == WATCH_BLOCK:
        return "present (current text)"
    if text in PREVIOUS_WATCH_BLOCKS:
        return "present, an earlier version this script knows (ACTION=update replaces it)"
    return "a WATCH HISTORY header is present but the text differs from every copy in this script"


async def req(s: aiohttp.ClientSession, method: str, path: str, body: dict | None = None):
    async with s.request(method, BASE + path, headers=H, json=body) as r:
        if "json" in (r.headers.get("content-type") or ""):
            return r.status, await r.json()
        return r.status, (await r.text())[:300]


async def entry_states(s: aiohttp.ClientSession, domain: str) -> dict[str, str]:
    st, data = await req(s, "GET", f"/api/config/config_entries/entry?domain={domain}")
    if st != 200 or not isinstance(data, list):
        raise Refused(f"listing {domain} entries failed: HTTP {st} {data}")
    return {e["entry_id"]: e.get("state", "?") for e in data}


async def wait_state(s: aiohttp.ClientSession, domain: str, entry_id: str, want: str, secs: int) -> str:
    state = "?"
    for _ in range(secs):
        state = (await entry_states(s, domain)).get(entry_id, "missing")
        if state == want:
            break
        await asyncio.sleep(1)
    return state


def shown_url(url) -> str:
    """A URL for printing: any query string (where a server token could ride) is left out."""
    url = str(url)
    return url.split("?", 1)[0] + ("?<query hidden>" if "?" in url else "")


def summarize(r) -> str:
    """A flow result without its data: type/step/reason/errors/field names only."""
    if not isinstance(r, dict):
        return str(r)[:300]
    out = {k: r[k] for k in ("type", "step_id", "reason", "errors", "menu_options") if r.get(k) is not None}
    if r.get("data_schema"):
        out["fields"] = [f.get("name") for f in r["data_schema"]]
    return json.dumps(out)


async def cancel(s: aiohttp.ClientSession, flow_path: str, flow_id: str) -> None:
    st, _ = await req(s, "DELETE", f"{flow_path}/{flow_id}")
    print(f"   flow {flow_id} cancelled (HTTP {st}); nothing was changed")


# ---------- actions ----------

async def status(s: aiohttp.ClientSession) -> None:
    entries = load_entries()
    states = await entry_states(s, "mcp")
    openai_state = (await entry_states(s, "openai_conversation")).get(OPENAI_ENTRY, "missing")
    sub = get_subentry(entries)
    apis = sub["data"].get("llm_hass_api") or []
    print("== mcp config entries")
    mcps = mcp_entries(entries)
    if not mcps:
        print("   (none)")
    for e in mcps:
        eid = e["entry_id"]
        tag = " | ATTACHED to the Movie Room agent" if f"mcp-{eid}" in apis else ""
        print(f"   {eid} | {e['title']} | url={shown_url(e['data'].get('url'))} | state={states.get(eid, '?')} | llm api mcp-{eid}{tag}")
    print(f"== {OPENAI_ENTRY} (state={openai_state}) subentry {SUBENTRY} ({sub['subentry_type']}) {sub['title']!r}")
    print("   llm_hass_api:", json.dumps(apis))
    prompt = sub["data"].get("prompt", "")
    print("   prompt length:", len(prompt))
    print("   watch-history block:", block_state(prompt))
    print("   prompt tail (last 200 chars, repr):", repr(prompt[-200:]))


async def add_entry(s: aiohttp.ClientSession) -> None:
    for e in mcp_entries(load_entries()):
        if e["data"].get("url") == URL:
            print(f"already exists: {e['entry_id']} | {e['title']} | url={shown_url(URL)}; nothing done")
            return
    st, r = await req(s, "POST", FLOW, {"handler": "mcp", "show_advanced_options": False})
    flow_id = r.get("flow_id") if isinstance(r, dict) else None
    if st != 200 or not isinstance(r, dict) or r.get("type") != "form" or r.get("step_id") != "user":
        if flow_id:
            await cancel(s, FLOW, flow_id)
        raise Refused(f"unexpected mcp flow start: HTTP {st} {summarize(r)}")
    print(f"   mcp flow {flow_id}: step user, submitting url={shown_url(URL)}")
    st, r = await req(s, "POST", f"{FLOW}/{flow_id}", {"url": URL})
    kind = r.get("type") if isinstance(r, dict) else None
    if st == 200 and kind == "create_entry":
        entry = r["result"]
        eid = entry["entry_id"]
        print(f"created mcp entry {eid} | title {entry['title']!r} | state {entry.get('state')}")
        if entry["title"] != EXPECTED_TITLE:
            print(f"   NOTE: title is {entry['title']!r}, not {EXPECTED_TITLE!r} (it is the server's serverInfo.name)")
        state = await wait_state(s, "mcp", eid, "loaded", 30)
        print(f"   state {state} | LLM API id mcp-{eid}")
        print(f"   next: ACTION=attach ENTRY_ID={eid}")
        return
    if st == 200 and kind == "abort":
        raise Refused(f"mcp flow aborted: {r.get('reason')}")  # an aborted flow is already gone
    await cancel(s, FLOW, flow_id)
    if kind in ("menu", "external") or (isinstance(r, dict) and r.get("step_id") not in (None, "user")):
        raise Refused(f"the server demanded auth (HTTP 401 path: {summarize(r)}); this helper only adds no-auth servers")
    raise Refused(f"no entry created: HTTP {st} {summarize(r)}")


def credential_like(key: str) -> bool:
    k = key.lower()
    return any(w in k for w in CRED_WORDS) or any(p in CRED_PARTS for p in re.split(r"[^a-z0-9]+", k))


def backup(data: dict, undo: str) -> None:
    """Print the subentry data and write it to /tmp in the pod. `undo` says how to reverse the
    change; "{path}" in it becomes the file's path."""
    creds = [k for k in data if credential_like(k)]
    if creds:
        raise Refused(f"subentry data has credential-like keys {creds}; not printing a backup, not changing anything")
    line = json.dumps(data, ensure_ascii=True, sort_keys=True)
    print("BACKUP_BEGIN")
    print(line)
    print("BACKUP_END")
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%S.%fZ")  # a backup never overwrites another
    path = f"/tmp/watch-history-backup-{SUBENTRY}-{stamp}.json"
    try:
        with open(path, "x") as f:
            f.write(line + "\n")
        print(f"   (also written in the HA pod at {path}, until the pod restarts)")
        print("   to undo:", undo.format(path=path))
    except OSError as ex:
        print(f"   (could not write the in-pod copy: {ex}; save the lines above)")
        print("   to undo:", undo.format(path="<a file in the HA pod holding the lines above>"))


def load_backup_prompt(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        if "BACKUP_BEGIN" in text:
            text = text.split("BACKUP_BEGIN", 1)[1].split("BACKUP_END", 1)[0]
        obj = json.loads(text)
    except (OSError, ValueError) as ex:
        raise Refused(f"cannot read a backup from {path} in the HA pod ({type(ex).__name__}: {ex}); a pod restart wipes /tmp")
    if not isinstance(obj, dict):
        raise Refused(f"{path} does not hold a JSON object")
    if isinstance(obj.get("data"), dict):
        obj = obj["data"]
    prompt = obj.get("prompt")
    if not isinstance(prompt, str):
        raise Refused(f"{path} has no string 'prompt'")
    return prompt


async def reconfigure(s: aiohttp.ClientSession, current: dict, target: dict, need_apis: list[str]) -> None:
    """Drive the subentry reconfigure flow, submitting `target` for every field each form shows."""
    st, r = await req(s, "POST", SUBFLOW, {"handler": [OPENAI_ENTRY, "conversation"], "subentry_id": SUBENTRY})
    if st != 200 or not isinstance(r, dict):
        raise Refused(f"subentry flow start failed: HTTP {st} {summarize(r)}")
    flow_id = r.get("flow_id")
    finished = False
    try:
        for _ in range(6):
            kind = r.get("type")
            if kind == "abort":
                finished = True
                if r.get("reason") == "reconfigure_successful":
                    return
                raise Refused(f"subentry flow aborted: {r.get('reason')}")
            if kind != "form":
                raise Refused(f"unexpected subentry flow result: {summarize(r)}")
            step = r["step_id"]
            if r.get("errors"):
                raise Refused(f"step {step} returned errors {r['errors']}")
            payload = {}
            for f in r.get("data_schema") or []:
                name = f["name"]
                desc = f.get("description") or {}
                if name not in target:
                    if "default" in f or f.get("required"):
                        raise Refused(f"step {step} shows {name!r}, which the stored data lacks; submitting would add it")
                    continue  # optional, no default: leaving it out adds nothing
                live = desc.get("suggested_value", current.get(name))
                if name in current and name != "llm_hass_api" and live != current[name]:
                    raise Refused(f"step {step}: HA's live {name!r} differs from .storage; re-run to take a fresh backup")
                if name == "llm_hass_api":
                    opts = {o["value"]: o["label"] for o in (f.get("selector", {}).get("select", {}).get("options") or [])}
                    missing = [a for a in need_apis if a not in opts]
                    if missing:
                        raise Refused(f"LLM API {missing} is not registered in HA (is the mcp entry loaded?)")
                    print("   llm apis:", ", ".join(f"{a} = {opts.get(a, '(not registered)')!r}" for a in target[name]))
                payload[name] = target[name]
            print(f"   step {step}: submitting {sorted(payload)}")
            st, r = await req(s, "POST", f"{SUBFLOW}/{flow_id}", payload)
            if st != 200 or not isinstance(r, dict):
                raise Refused(f"step {step} rejected: HTTP {st} {summarize(r)}")
        raise Refused("subentry flow did not finish within 6 steps")
    finally:
        if not finished and flow_id:
            await cancel(s, SUBFLOW, flow_id)


async def verify(s: aiohttp.ClientSession, before: dict, target: dict) -> None:
    after: dict = {}
    for _ in range(20):  # config entries are saved to .storage about 1 s after a change
        await asyncio.sleep(1)
        try:
            after = dict(get_subentry(load_entries())["data"])
        except ValueError:  # caught the file mid-write: read again next second
            continue
        if after.get("llm_hass_api") == target["llm_hass_api"] and after.get("prompt") == target["prompt"]:
            break
    else:
        print("WARNING: .storage did not show the new values within 20 s; run ACTION=status")
    print("   llm_hass_api now:", json.dumps(after.get("llm_hass_api")))
    print(f"   prompt length now: {len(after.get('prompt', ''))} (was {len(before.get('prompt', ''))})")
    drift = sorted(k for k in set(before) | set(after)
                   if k not in ("prompt", "llm_hass_api") and before.get(k, "<absent>") != after.get(k, "<absent>"))
    if not drift:
        print("   every other key kept its value")
    for k in drift:
        why = "re-derived by the flow's location lookup" if k in LOCATION_KEYS else "UNEXPECTED"
        print(f"   CHANGED {k}: {before.get(k, '<absent>')!r} -> {after.get(k, '<absent>')!r} ({why})")
    print("   OpenAI entry after its reload:", await wait_state(s, "openai_conversation", OPENAI_ENTRY, "loaded", 60))


async def require_openai_loaded(s: aiohttp.ClientSession) -> None:
    openai_state = (await entry_states(s, "openai_conversation")).get(OPENAI_ENTRY)
    if openai_state != "loaded":
        raise Refused(f"OpenAI entry is {openai_state}; its subentry flow aborts unless the entry is loaded")


async def require_mcp_loaded(s: aiohttp.ClientSession) -> None:
    state = (await entry_states(s, "mcp")).get(ENTRY_ID)
    if state != "loaded":
        raise Refused(f"mcp entry {ENTRY_ID} is {state}; HA only offers a loaded entry's API to the agent")


async def change(s: aiohttp.ClientSession, current: dict, new_api: list, new_prompt: str, need_apis: list[str],
                 undo: str) -> None:
    await require_openai_loaded(s)
    backup(current, undo)
    target = dict(current, llm_hass_api=new_api, prompt=new_prompt)
    await reconfigure(s, current, target, need_apis)
    print("subentry updated")
    await verify(s, current, target)


def need_entry_id() -> str:
    if not ENTRY_ID:
        raise Refused("ENTRY_ID=<mcp entry id> is required (ACTION=status lists them)")
    return f"mcp-{ENTRY_ID}"


def need_mcp_entry(entries: list[dict]) -> None:
    if not any(e["entry_id"] == ENTRY_ID for e in mcp_entries(entries)):
        raise Refused(f"{ENTRY_ID} is not an mcp config entry")


def one_block(prompt: str, fix: str) -> tuple[int, int, str] | None:
    """The prompt's only WATCH HISTORY block as (start, end, text), or None; two or more refuse."""
    spans = find_blocks(prompt)
    if len(spans) > 1:
        raise Refused(f"the prompt has {len(spans)} WATCH HISTORY headers; {fix}")
    if not spans:
        return None
    start, end = spans[0]
    return start, end, prompt[start:end]


def print_block(label: str, text: str) -> None:
    print(f"   {label}:")
    for line in text.split("\n"):
        print(f"   | {line}")


def block_version(text: str) -> str:
    if text == WATCH_BLOCK:
        return "the current block"
    return f"earlier version {PREVIOUS_WATCH_BLOCKS.index(text) + 1} of {len(PREVIOUS_WATCH_BLOCKS)}"


def run_line(action: str, extra: str = "") -> str:
    return f'scripts/voice-bench/run.sh attach_watch_history.py "ACTION={action} ENTRY_ID={ENTRY_ID}{extra}"'


def other_mcp_apis(api: list, api_id: str) -> list:
    return [a for a in api if a.startswith("mcp-") and a != api_id]


def detach_then_attach(api: list, api_id: str) -> str:
    """The way out when the prompt holds an earlier block and llm_hass_api lacks api_id (the same
    line from attach, update and detach's own refusal)."""
    extra = " BLOCK_ONLY=1" if other_mcp_apis(api, api_id) else ""
    return (f"first {run_line('detach', extra)} (it removes the block), then {run_line('attach')} "
            "(the API and the current block)")


async def attach(s: aiohttp.ClientSession) -> None:
    api_id = need_entry_id()
    entries = load_entries()
    need_mcp_entry(entries)
    current = dict(get_subentry(entries)["data"])
    api = list(current.get("llm_hass_api") or [])
    prompt = current.get("prompt", "")
    block = one_block(prompt, "fix it by hand")
    if block and block[2] in PREVIOUS_WATCH_BLOCKS:
        if api_id in api:
            raise Refused("the prompt carries an earlier WATCH HISTORY block and the API is attached; "
                          "ACTION=update replaces the block in place")
        raise Refused(f"the prompt carries an earlier WATCH HISTORY block and llm_hass_api lacks {api_id}; "
                      + detach_then_attach(api, api_id))
    if block and block[2] != WATCH_BLOCK:
        raise Refused("the prompt already has a WATCH HISTORY block with different text; fix it by hand or detach with PROMPT_FILE first")
    if block:
        new_prompt = prompt
    elif not prompt:
        new_prompt = WATCH_BLOCK
    else:
        trailing = len(prompt) - len(prompt.rstrip("\n"))
        if trailing:
            print(f"   NOTE: the prompt ends with {trailing} newline(s); only a PROMPT_FILE detach restores them byte-for-byte")
        new_prompt = prompt + "\n" * max(0, len(SEP) - trailing) + WATCH_BLOCK
    new_api = api if api_id in api else api + [api_id]
    if new_api == api and new_prompt == prompt:
        print(f"already attached: llm_hass_api={json.dumps(api)}, block present; nothing done")
        return
    if new_api != ["assist", api_id]:
        print(f"   NOTE: llm_hass_api will be {json.dumps(new_api)} (existing APIs kept)")
    if block:  # only the API changes; the prompt already held the current block
        undo = (f"{run_line('detach')} removes the API and the block together (no helper action puts back "
                "a block without its API; the backup above holds the prompt as it was)")
    else:
        undo = f"{run_line('detach', ' PROMPT_FILE={path}')} restores this prompt byte for byte and removes the API"
    await require_mcp_loaded(s)
    await change(s, current, new_api, new_prompt, [api_id], undo)


async def update(s: aiohttp.ClientSession) -> None:
    api_id = need_entry_id()
    entries = load_entries()
    need_mcp_entry(entries)
    current = dict(get_subentry(entries)["data"])
    api = list(current.get("llm_hass_api") or [])
    prompt = current.get("prompt", "")
    known = [WATCH_BLOCK, *PREVIOUS_WATCH_BLOCKS]
    target = WATCH_BLOCK
    if PROMPT_FILE:  # undoing an update: swap the backup's block back in
        backed_up = one_block(load_backup_prompt(PROMPT_FILE), "it is not a backup this helper wrote")
        if backed_up is None:
            raise Refused(f"{PROMPT_FILE} holds no WATCH HISTORY block, so there is none to swap back; a backup "
                          "that attach took from a prompt with no block is restored by ACTION=detach with this PROMPT_FILE")
        target = backed_up[2]
        if target not in known:
            raise Refused(f"{PROMPT_FILE}'s WATCH HISTORY block matches no copy in this script; restore it by hand")
    block = one_block(prompt, "fix it by hand")
    if api_id not in api:
        if block and block[2] in PREVIOUS_WATCH_BLOCKS:
            raise Refused(f"llm_hass_api is {json.dumps(api)}, without {api_id}, and the prompt holds an earlier "
                          "WATCH HISTORY block; " + detach_then_attach(api, api_id))
        if block and block[2] != WATCH_BLOCK:
            raise Refused(f"llm_hass_api is {json.dumps(api)}, without {api_id}, and the prompt holds a WATCH HISTORY "
                          "block that matches no copy in this script; fix it by hand (ACTION=status shows the prompt "
                          "tail), or ACTION=detach with PROMPT_FILE=<an attach backup> removes it")
        raise Refused(f"llm_hass_api is {json.dumps(api)}, without {api_id}; ACTION=attach adds the API "
                      + ("(the block is already current)" if block else "and the current block together"))
    if block is None:
        raise Refused("the prompt has no WATCH HISTORY block; ACTION=attach appends the current one")
    start, end, old = block
    if old == target:
        print(f"already {'as in ' + PROMPT_FILE if PROMPT_FILE else 'current'}: the WATCH HISTORY block is "
              f"{block_version(old)}, llm_hass_api={json.dumps(api)}; nothing done")
        return
    if old not in known:
        raise Refused("the prompt's WATCH HISTORY block matches neither the current text nor any earlier one in this script; "
                      "fix it by hand (ACTION=status shows the prompt tail)")
    new_prompt = prompt[:start] + target + prompt[end:]
    where = "at the end of the prompt" if end == len(prompt) else f"at characters {start} to {end} of {len(prompt)}"
    source = f" from {PROMPT_FILE}" if PROMPT_FILE else ""
    print(f"   the live block is {block_version(old)}, {where}; replacing it in place with {block_version(target)}{source}")
    print_block("block now", old)
    print_block("block after", target)
    print(f"   prompt length: {len(prompt)} -> {len(new_prompt)} ({len(new_prompt) - len(prompt):+d} characters)")
    print(f"   llm_hass_api stays {json.dumps(api)}")
    await require_mcp_loaded(s)
    if DRY_RUN:
        await require_openai_loaded(s)
        print("DRY_RUN: no flow started, nothing changed")
        return
    undo = f"{run_line('update', ' PROMPT_FILE={path}')} puts this block back in place and keeps the API"
    await change(s, current, api, new_prompt, [api_id], undo)


async def detach(s: aiohttp.ClientSession) -> None:
    api_id = need_entry_id()
    current = dict(get_subentry(load_entries())["data"])
    api = list(current.get("llm_hass_api") or [])
    prompt = current.get("prompt", "")
    new_api = [a for a in api if a != api_id]
    others = other_mcp_apis(api, api_id)
    if BLOCK_ONLY and api_id in api:
        raise Refused(f"BLOCK_ONLY says {api_id}'s API was already removed by hand, but llm_hass_api is "
                      f"{json.dumps(api)} and still holds it, so this run would remove it too; "
                      f"{run_line('detach')} removes the API and the block together")
    if api_id not in api and others and (PROMPT_FILE or find_blocks(prompt)) and not BLOCK_ONLY:
        # Nothing to remove for this id while another mcp API is attached: ENTRY_ID usually names the wrong
        # entry (a typo or another server's), and detaching would strip the block and leave that API's tools
        # unguided. The state alone cannot tell that from "this id is right and its API was removed by hand",
        # so BLOCK_ONLY=1 is the explicit way through. An id whose entry is gone, with no mcp API left, is the
        # legitimate "entry already deleted" case and needs no flag.
        raise Refused(f"llm_hass_api has no {api_id} to remove but still holds {others}; detaching would strip "
                      "the prompt block and leave those tools attached. Check ENTRY_ID (ACTION=status lists the "
                      f"entries and marks the attached one); if {api_id} is right and its API was removed by hand, "
                      f"{run_line('detach', (f' PROMPT_FILE={PROMPT_FILE}' if PROMPT_FILE else '') + ' BLOCK_ONLY=1')} "
                      "changes only the prompt")
    if PROMPT_FILE:
        new_prompt = load_backup_prompt(PROMPT_FILE)
        if blk := one_block(new_prompt, "it is not a backup this helper wrote"):
            if blk[2] not in (WATCH_BLOCK, *PREVIOUS_WATCH_BLOCKS):
                raise Refused(f"{PROMPT_FILE} holds a WATCH HISTORY block that matches no copy in this script; "
                              "restore it by hand")
            raise Refused(f"{PROMPT_FILE} holds a WATCH HISTORY block, and a detach never leaves one behind: "
                          f"{run_line('detach')} removes the API and the block together, and ACTION=update with "
                          "this PROMPT_FILE puts that block back and keeps the API")
    elif block := one_block(prompt, "pass PROMPT_FILE"):
        start, end, text = block
        if text != WATCH_BLOCK and text not in PREVIOUS_WATCH_BLOCKS:
            raise Refused("the prompt has a WATCH HISTORY block whose text differs from every copy in this script; pass PROMPT_FILE")
        if prompt[:start].endswith(SEP):
            start -= len(SEP)
        if end != len(prompt):
            print("   NOTE: the block was not at the end of the prompt; removed it where it was")
        new_prompt = prompt[:start] + prompt[end:]
    else:
        new_prompt = prompt
    if new_api == api and new_prompt == prompt:
        print(f"already detached: llm_hass_api={json.dumps(api)}, {block_state(prompt)}; nothing done")
        return
    if new_api == api:
        print(f"   NOTE: {api_id} is not in llm_hass_api {json.dumps(api)}; only the prompt changes")
    elif new_api != ["assist"]:
        print(f"   NOTE: llm_hass_api will be {json.dumps(new_api)} (only {api_id} removed)")
    undo = (f"{run_line('attach')} attaches again with this script's current block (not byte for byte: "
            "the backup above holds the prompt as it was)")
    await change(s, current, new_api, new_prompt, [], undo)


async def main() -> int:
    actions = {"status": status, "add-entry": add_entry, "attach": attach, "update": update, "detach": detach}
    if ACTION not in actions:
        print(f"unknown ACTION={ACTION!r}; one of {', '.join(actions)}")
        return 2
    if DRY_RUN and ACTION not in ("status", "update"):
        print(f"DRY_RUN is only for ACTION=update; ACTION={ACTION} has no dry run. Nothing done.")
        return 2
    if BLOCK_ONLY and ACTION != "detach":
        print(f"BLOCK_ONLY is only for ACTION=detach; ACTION={ACTION} would ignore it. Nothing done.")
        return 2
    if PROMPT_FILE and ACTION not in ("update", "detach"):
        print(f"PROMPT_FILE is only for ACTION=update (an update backup) and ACTION=detach (an attach "
              f"backup); ACTION={ACTION} would ignore it. Nothing done.")
        return 2
    async with aiohttp.ClientSession() as s:
        try:
            await actions[ACTION](s)
        except Refused as ex:
            print("REFUSED:", ex)
            return 1
    return 0


sys.exit(asyncio.run(main()))
