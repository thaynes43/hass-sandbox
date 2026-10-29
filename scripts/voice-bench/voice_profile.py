"""Set a voice agent's model and web-search location through haynes-ops' hnet_watch_maintenance. Runs INSIDE the HA pod (run.sh).

The component must be mounted first (haynes-ops runbook hnet-watch-subentry-maintenance.md). It writes only fixed profiles
(terra61_westford, or the rollback terra56_westford) into the five OpenAI room agents and checks the full-data hash.
Nothing here prints a prompt, a location or any other configuration value: only statuses and hashes.

ACTION=load                       add the maintenance config entry through the official flow (no agent changes)
ACTION=status  [AGENTS=a,b]       hash of each agent's current subentry data
ACTION=apply   PROFILE=<profile> [AGENTS=a,b]   set the profile on each agent, one at a time, compare-and-set
ACTION=unload                     delete the maintenance config entry (its commands go away)
AGENTS defaults to all five: kitchen,bedroom,movie,rumpus,phone.
"""
import asyncio, json, os
import aiohttp

BASE = "http://localhost:8123"
TOK = os.environ["HA_TOKEN"]
H = {"Authorization": f"Bearer {TOK}"}
DOMAIN = "hnet_watch_maintenance"
ACTION = os.environ.get("ACTION", "status")
AGENTS = [a for a in os.environ.get("AGENTS", "kitchen,bedroom,movie,rumpus,phone").split(",") if a]


async def entries(s):
    async with s.get(f"{BASE}/api/config/config_entries/entry", headers=H) as r:
        return [e for e in await r.json() if e["domain"] == DOMAIN]


async def ws_call(ws, n, msg):
    await ws.send_json({"id": n, **msg})
    while True:
        j = await asyncio.wait_for(ws.receive_json(), timeout=60)
        if j.get("id") == n and j.get("type") == "result":
            return j


async def main():
    if ACTION not in ("load", "status", "apply", "unload"):
        raise SystemExit(f"unknown ACTION {ACTION}: use load, status, apply or unload")
    async with aiohttp.ClientSession() as s:
        if ACTION == "load":
            if await entries(s):
                print("maintenance entry already present")
                return
            async with s.post(f"{BASE}/api/config/config_entries/flow", headers=H, json={"handler": DOMAIN}) as r:
                flow = await r.json()
            async with s.post(f"{BASE}/api/config/config_entries/flow/{flow['flow_id']}", headers=H, json={}) as r:
                res = await r.json()
            print("flow result:", res.get("type"), "| entries now:", [(e["entry_id"], e["state"]) for e in await entries(s)])
            return
        if ACTION == "unload":
            for e in await entries(s):
                async with s.delete(f"{BASE}/api/config/config_entries/entry/{e['entry_id']}", headers=H) as r:
                    print("deleted", e["entry_id"], r.status)
            print("entries now:", await entries(s))
            return
        async with s.ws_connect(f"{BASE}/api/websocket") as ws:
            await ws.receive_json()
            await ws.send_json({"type": "auth", "access_token": TOK})
            await ws.receive_json()
            n = 0
            failed = False
            for agent in AGENTS:
                n += 1
                st = await ws_call(ws, n, {"type": f"{DOMAIN}/status", "agent": agent})
                if not st.get("success"):
                    print(agent, "status error:", st.get("error"))
                    failed = True
                    continue
                before = st["result"]["hash"]
                if ACTION == "status":
                    print(agent, "hash", before)
                    continue
                n += 1
                res = await ws_call(ws, n, {"type": f"{DOMAIN}/set_voice_profile", "agent": agent,
                                             "profile": os.environ["PROFILE"], "expected_sha256": before})
                if res.get("success"):
                    print(agent, "before", before[:16], "->", res["result"]["status"], res["result"]["hash"][:16])
                else:
                    print(agent, "REFUSED:", res.get("error"))
                    failed = True
            if failed:
                raise SystemExit(1)


asyncio.run(main())
