"""A/B two conversation agents through one Assist pipeline. Runs INSIDE the HA pod (run.sh); HA_TOKEN in env.

ACTION=check  SUBENTRIES=<id>,<id>,...
    Print each openai_conversation subentry's settings without the prompt or any key, plus the prompt's
    sha256 and length, so test copies can be proven identical to the agent they copy. Reads .storage only.

ACTION=run  PIPELINE=<id> AGENTS=<entity>,<entity> QUERIES='q1|q2|...' [ROUNDS=1] [COLD=0|1] [DEVICE_ID=<satellite>]
    For each round, each query, each agent (order alternates every round): point the pipeline's agent at
    it, run the text through the pipeline's intent stage only, and print one ROW json line with:
    first text delta, first tool call and intent end (seconds after submit), tool calls, spoken answer.
    COLD=1 (or COLD=<entity>,...) marks each (listed) agent's first turn of this invocation as cold: the
    caller keeps that agent idle first.
    Every turn starts a new conversation. TTS is not run: it does not depend on the model.
    Stops after any tool call outside READ_ONLY_TOOLS. Then points the pipeline back where it was, on a fresh
    socket with retries; if that fails it prints RESTORE FAILED and exits 1. A killed run cannot restore.
    STATE_AREA=<area id> STATE_ENTITIES=<entity>,... print STATE lines before and after, to check answers against.

ACTION=summary  (locally, no HA)  python3 model_ab.py < saved run output
    Per agent and cold/warm: sample count, median, p90 (nearest rank) and max of first text and intent end,
    failures (error or empty answer) and lookups per turn; then medians per query. Read the answers yourself.
"""
import asyncio, hashlib, json, os, re, statistics, sys, time

BASE = "http://localhost:8123"
TOK = os.environ.get("HA_TOKEN", "")
H = {"Authorization": f"Bearer {TOK}"}
ACTION = os.environ.get("ACTION", "check")
READ_ONLY_TOOLS = {"GetLiveContext", "GetDateTime", "HassGetWeather", "HassGetCurrentDate", "HassGetCurrentTime",
                   "todo_get_items", "web_search", "web_search_call"}
STATE_AREA = os.environ.get("STATE_AREA", "")  # area whose lights and temperature sensors the answers concern
STATE_ENTITIES = [e for e in os.environ.get("STATE_ENTITIES", "").split(",") if e]
TURN_TIMEOUT = 90
WS_TIMEOUT = 15
RESTORE_PAUSE = 2  # seconds between restore attempts
SECRET_KEY = re.compile(r"(^|_)(key|token|api_?key|secret|password|access_token|refresh_token|auth|bearer)$")


def public_settings(data):
    """Subentry data without the prompt or anything that may hold a credential (max_tokens stays)."""
    return {k: v for k, v in data.items() if k != "prompt" and not SECRET_KEY.search(k.lower())}


def check():
    want = [s for s in os.environ.get("SUBENTRIES", "").split(",") if s]
    d = json.load(open("/config/.storage/core.config_entries"))
    for e in d["data"]["entries"]:
        if e["domain"] != "openai_conversation":
            continue
        for s in e.get("subentries", []):
            if s["subentry_id"] not in want:
                continue
            data = public_settings(s["data"])
            p = s["data"].get("prompt", "")
            print(f"{s['title']} | {s['subentry_id']} | prompt sha256 {hashlib.sha256(p.encode()).hexdigest()[:16]} "
                  f"len {len(p)} | {json.dumps(data, sort_keys=True)}")


class Ws:
    def __init__(self, ws):
        self.ws, self.n = ws, 0

    async def call(self, msg):
        self.n += 1
        await self.ws.send_json({"id": self.n, **msg})
        while True:
            j = await asyncio.wait_for(self.ws.receive_json(), timeout=WS_TIMEOUT)
            if j.get("id") == self.n and j.get("type") == "result":
                if not j.get("success"):
                    raise RuntimeError(f"{msg['type']}: {j.get('error')}")
                return j.get("result")


async def connect(s):
    ws = await s.ws_connect(f"{BASE}/api/websocket")
    await asyncio.wait_for(ws.receive_json(), timeout=WS_TIMEOUT)
    await ws.send_json({"type": "auth", "access_token": TOK})
    await asyncio.wait_for(ws.receive_json(), timeout=WS_TIMEOUT)
    return ws


async def restore(s, base, original, attempts=3):
    """Point the pipeline back at its original agent on a fresh socket each try: the run's socket may be dead."""
    for attempt in range(1, attempts + 1):
        try:
            ws = await connect(s)
            try:
                w = Ws(ws)
                await set_agent(w, base, original)
                after = await w.call({"type": "assist_pipeline/pipeline/get", "pipeline_id": base["id"]})
            finally:
                await ws.close()
            if after["conversation_engine"] == original:
                print("PIPELINE agent restored:", original, flush=True)
                return True
            print(f"restore attempt {attempt}: pipeline reads {after['conversation_engine']}", flush=True)
        except Exception as ex:
            print(f"restore attempt {attempt} failed: {type(ex).__name__}: {ex}", flush=True)
        await asyncio.sleep(RESTORE_PAUSE)
    print(f"RESTORE FAILED: pipeline {base['id']} may still point at a test agent; set it back to {original}", flush=True)
    return False


async def set_agent(w, base, agent):
    body = {k: v for k, v in base.items() if k != "id"}
    body["conversation_engine"] = agent
    await w.call({"type": "assist_pipeline/pipeline/update", "pipeline_id": base["id"], **body})


async def turn(s, pipeline, text, device_id):
    import aiohttp
    ws = await connect(s)
    try:
        msg = {"id": 1, "type": "assist_pipeline/run", "start_stage": "intent", "end_stage": "intent",
               "pipeline": pipeline, "input": {"text": text}}
        if device_id:
            msg["device_id"] = device_id
        t0 = time.monotonic()
        await ws.send_json(msg)
        r = {"first_text_s": None, "first_tool_s": None, "intent_end_s": None, "tools": [], "speech": "",
             "response_type": None, "error": None}
        while True:
            left = TURN_TIMEOUT - (time.monotonic() - t0)
            try:
                m = await asyncio.wait_for(ws.receive(), timeout=max(left, 0.01))
            except asyncio.TimeoutError:  # keep the tool calls seen so far: the guard must still see them
                r["error"] = f"timeout {TURN_TIMEOUT}s"
                break
            if m.type != aiohttp.WSMsgType.TEXT:
                r["error"] = f"socket {m.type}"
                break
            j = json.loads(m.data)
            if j.get("type") == "result":
                if not j.get("success"):
                    r["error"] = f"rejected {j.get('error')}"
                    break
                continue
            ev = j["event"]
            et, d = ev["type"], ev.get("data") or {}
            dt = round(time.monotonic() - t0, 3)
            if et == "intent-progress":
                delta = d.get("chat_log_delta") or {}
                if r["first_text_s"] is None and delta.get("content"):
                    r["first_text_s"] = dt
                for t in delta.get("tool_calls") or []:
                    if r["first_tool_s"] is None:
                        r["first_tool_s"] = dt
                    r["tools"].append({"name": t.get("tool_name"), "id": t.get("id"), "args": t.get("tool_args"), "s": dt})
            elif et == "intent-end":
                r["intent_end_s"] = dt
                resp = (d.get("intent_output") or {}).get("response") or {}
                r["response_type"] = resp.get("response_type")
                r["speech"] = ((resp.get("speech") or {}).get("plain") or {}).get("speech", "")
                r["processed_locally"] = d.get("processed_locally")
            elif et == "error":
                r["error"] = json.dumps(d)
            if et in ("run-end", "error"):
                break
        return r
    finally:
        await ws.close()


async def states(s):
    if not (STATE_AREA or STATE_ENTITIES):
        return
    tpl = ""
    if STATE_AREA:
        tpl += ("{% set k = area_entities('" + STATE_AREA + "') %}"
                "lights_on={{ k | select('match', 'light[.]') | select('is_state', 'on') | list }} "
                "temps={{ k | select('match', 'sensor[.]') | select('is_state_attr', 'device_class', 'temperature')"
                " | map('states') | list }} ")
    for e in STATE_ENTITIES:
        tpl += e + "={{ states('" + e + "') }} "
    async with s.post(f"{BASE}/api/template", headers=H, json={"template": tpl}) as r:
        print("STATE", time.strftime("%H:%M:%S"), (await r.text()).strip())


def cold_agents(env, agents):
    """COLD=1 means every agent; otherwise a comma list of the agents that were kept idle."""
    return set(agents) if env == "1" else {a for a in env.split(",") if a in agents}


def blocked_tools(tools):
    """Names of tool calls outside READ_ONLY_TOOLS. HA prefixes the API id (homeassistant__GetLiveContext)."""
    return [t["name"] for t in tools if str(t["name"]).split("__")[-1] not in READ_ONLY_TOOLS]


async def run():
    import aiohttp
    pipeline = os.environ["PIPELINE"]
    agents = [a for a in os.environ["AGENTS"].split(",") if a]
    queries = [q for q in os.environ["QUERIES"].split("|") if q]
    rounds = int(os.environ.get("ROUNDS", "1"))
    cold = cold_agents(os.environ.get("COLD", "0"), agents)
    device_id = os.environ.get("DEVICE_ID", "")
    used = set()
    restored = True
    async with aiohttp.ClientSession() as s:
        raw = await connect(s)
        try:
            w = Ws(raw)
            base = await w.call({"type": "assist_pipeline/pipeline/get", "pipeline_id": pipeline})
            original = base["conversation_engine"]
            print("PIPELINE", base["name"], "agent before:", original, "prefer_local_intents:", base.get("prefer_local_intents"))
            await states(s)
            stop = False
            try:
                for rnd in range(rounds):
                    for qi, q in enumerate(queries):
                        order = agents if (rnd + qi) % 2 == 0 else agents[::-1]
                        for agent in order:
                            await set_agent(w, base, agent)
                            kind = "cold" if agent in cold and agent not in used else "warm"
                            used.add(agent)
                            r = await turn(s, pipeline, q, device_id)
                            row = {"ts": time.strftime("%H:%M:%S"), "round": rnd + 1, "agent": agent, "kind": kind,
                                   "query": q, **r}
                            print("ROW", json.dumps(row), flush=True)
                            bad = blocked_tools(r.get("tools", []))
                            if bad:
                                print("STOP: tool call outside the read-only list:", bad, flush=True)
                                stop = True
                                break
                        if stop:
                            break
                    if stop:
                        break
            finally:
                restored = await restore(s, base, original)
            await states(s)
        finally:
            await raw.close()
    if not restored:
        raise SystemExit(1)


def pct(xs, q):
    xs = sorted(xs)
    return xs[max(0, -(-len(xs) * q // 100) - 1)] if xs else None


def summary(lines):
    rows = [json.loads(l[4:]) for l in lines if l.startswith("ROW ")]
    med = lambda xs: statistics.median(xs) if xs else None
    good = lambda r: not r.get("error") and r.get("speech") and r.get("response_type") != "error"
    call_key = lambda t: t.get("id") or (t.get("name"), json.dumps(t.get("args"), sort_keys=True))
    fmt = lambda v: "   -  " if v is None else f"{v:6.2f}"
    print(f"{'agent':34s} {'kind':4s}  n | first text: med   p90   max | intent end: med   p90   max | fail lookups")
    for agent in sorted({r["agent"] for r in rows}):
        for kind in ("cold", "warm"):
            rs = [r for r in rows if r["agent"] == agent and r["kind"] == kind]
            if not rs:
                continue
            ok = [r for r in rs if good(r)]
            ft = [r["first_text_s"] for r in ok if r.get("first_text_s") is not None]
            ie = [r["intent_end_s"] for r in ok if r.get("intent_end_s") is not None]
            look = sum(len({call_key(t) for t in r.get("tools", [])}) for r in ok) / max(1, len(ok))
            print(f"{agent:34s} {kind:4s} {len(rs):2d} |       {fmt(med(ft))}{fmt(pct(ft, 90))}{fmt(max(ft) if ft else None)}"
                  f" |       {fmt(med(ie))}{fmt(pct(ie, 90))}{fmt(max(ie) if ie else None)} | {len(rs) - len(ok):4d} {look:6.2f}")
    print()
    for q in dict.fromkeys(r["query"] for r in rows):
        line = f"{q[:40]:40s}"
        for agent in sorted({r["agent"] for r in rows}):
            ie = [r["intent_end_s"] for r in rows
                  if r["agent"] == agent and r["query"] == q and good(r) and r.get("intent_end_s") is not None]
            line += f" | {agent.split('.')[-1][:22]:22s} n={len(ie):2d} med {fmt(med(ie))}"
        print(line)


if __name__ == "__main__":
    if ACTION == "check":
        check()
    elif ACTION == "run":
        asyncio.run(run())
    elif ACTION == "summary":
        summary(sys.stdin)
    else:
        raise SystemExit(f"unknown ACTION {ACTION}")
