"""Door/garage status check (text-only, read-only).

1. Every status helper must equal the lock/cover it mirrors (a swapped pair would
   render valid words on the wrong door).
2. Every room agent is asked about every door BY NAME; its answer must contain the
   true word and not the opposite one. Any MISMATCH line is a bug.
"""
import asyncio, os, aiohttp

TOK = os.environ["HA_TOKEN"]; H = {"Authorization": f"Bearer {TOK}"}
BASE = "http://localhost:8123"
MIRRORS = {
    "sensor.front_door_lock_state": ("lock.front_door_lock", "front door"),
    "sensor.side_door_lock_state": ("lock.side_door_lock", "side door"),
    "sensor.bulkhead_lock_state": ("lock.bulkhead_lock", "bulkhead door"),
    "sensor.mudroom_door_lock_state": ("lock.mudroom_door_lock", "mudroom door"),
    "sensor.tesla_garage_door_state": ("cover.ratgdov25i_4a0325_door", "Tesla garage door"),
    "sensor.wagoneer_garage_door_state": ("cover.ratgdov25i_dbfa50_door", "Wagoneer garage door"),
}
AGENTS = {"Bedroom": "conversation.bedroom_assist", "Kitchen": "conversation.chatgpt_2",
          "Movie": "conversation.chatgpt_5", "Rumpus": "conversation.rumpus_room_chatgpt_4"}
OPPOSITE = {"locked": "unlocked", "unlocked": "locked", "open": "closed", "closed": "open"}


async def state(s, entity_id):
    async with s.get(f"{BASE}/api/states/{entity_id}", headers=H) as r:
        return (await r.json()).get("state")


def verdict(answer, truth, label):
    """Judge only the clause(s) that talk about THIS door.

    The prompts tell the agents an unlocked mudroom door is normal, which invites
    two-clause answers ("the mudroom door is unlocked, everything else is locked").
    Scanning the whole answer would flag that correct reply, so split on clause
    boundaries and keep the clauses naming the door (or the whole answer if none does).
    """
    import re

    key = label.lower().split()[0]  # front / side / bulkhead / mudroom / tesla / wagoneer
    clauses = [c for c in re.split(r"[.;,!?]| but | and ", answer.lower()) if c.strip()]
    relevant = [c for c in clauses if key in c] or clauses
    text = " ".join(relevant)
    # "unlocked" contains "locked": strip it before testing for the bare word.
    said = {
        "unlocked": "unlocked" in text,
        "locked": "locked" in text.replace("unlocked", ""),
        "open": "open" in text,
        "closed": "closed" in text,
    }
    if truth not in said:
        return "SKIP (transitional state)"
    return "ok" if said[truth] and not said[OPPOSITE[truth]] else "MISMATCH"


async def main():
    bad = 0
    async with aiohttp.ClientSession() as s:
        truth = {}
        for mirror, (source, label) in MIRRORS.items():
            m, t = await state(s, mirror), await state(s, source)
            truth[label] = t
            flag = "ok" if m == t else "MISMATCH"
            bad += flag == "MISMATCH"
            print(f"MIRROR {flag:8s} {mirror} = {m!r} | {source} = {t!r}")
        for name, agent in AGENTS.items():
            for label, t in truth.items():
                kind = "locked" if t in ("locked", "unlocked") else "open"
                q = f"Is the {label} {kind} right now"
                async with s.post(f"{BASE}/api/conversation/process", headers=H,
                                  json={"text": q, "agent_id": agent, "language": "en"}) as r:
                    j = await r.json()
                answer = j.get("response", {}).get("speech", {}).get("plain", {}).get("speech", "") or f"(no speech: {str(j)[:80]})"
                v = verdict(answer, t, label)
                bad += v == "MISMATCH"
                print(f"{name:8s} {v:8s} truth={t:9s} | {q:44s} -> {answer[:110]}")
    print(f"RESULT: {'ALL OK' if not bad else str(bad) + ' MISMATCH(ES)'}")



# Self-test of the judge (runs with the check; cheap and catches a broken heuristic):
assert verdict("The mudroom door is unlocked, everything else is locked.", "unlocked", "mudroom door") == "ok"
assert verdict("The mudroom door is locked.", "unlocked", "mudroom door") == "MISMATCH"
assert verdict("No, sir. The Tesla garage door is closed; the Wagoneer is open.", "closed", "Tesla garage door") == "ok"
assert verdict("Yes, the front door is locked.", "locked", "front door") == "ok"

asyncio.run(main())
