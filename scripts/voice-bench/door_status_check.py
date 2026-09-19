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


def verdict(answer, truth):
    text = answer.lower()
    # "unlocked" contains "locked": test the longer word first.
    says_unlocked = "unlocked" in text
    says_locked = "locked" in text.replace("unlocked", "")
    said = {"unlocked": says_unlocked, "locked": says_locked, "open": "open" in text, "closed": "closed" in text}
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
                v = verdict(answer, t)
                bad += v == "MISMATCH"
                print(f"{name:8s} {v:8s} truth={t:9s} | {q:44s} -> {answer[:110]}")
    print(f"RESULT: {'ALL OK' if not bad else str(bad) + ' MISMATCH(ES)'}")

asyncio.run(main())
