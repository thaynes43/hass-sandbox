"""Door/garage status check (text-only, read-only).

1. Every status helper must equal the lock/cover it mirrors (a swapped pair would
   render valid words on the wrong door).
2. Every room agent is asked about every door BY NAME; its answer must contain the
   true word and not the opposite one. Any MISMATCH line is a bug.
"""
import asyncio, os, re

import aiohttp

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
    """Entity state, or None when the entity is missing/unreachable (never raises)."""
    try:
        async with s.get(f"{BASE}/api/states/{entity_id}", headers=H) as r:
            if r.status != 200:
                return None
            return (await r.json()).get("state")
    except Exception:
        return None


STATE_WORD = re.compile(r"\b(unlocked|locked|open|opened|closed|shut)\b")
CANON = {"opened": "open", "shut": "closed"}


def verdict(answer, truth, label):
    """Judge the state word that belongs to THIS door: the first one after the door's
    name, else the nearest one before it.  No clause splitting, so conjoined subjects
    ("the front, side and bulkhead doors are all locked") and two-part answers ("the
    mudroom door is unlocked, everything else is locked") both judge correctly; word
    boundaries keep "opener" from reading as "open".
    """
    if truth not in OPPOSITE:
        return "SKIP"
    text = answer.lower()
    key = label.lower().split()[0]  # front / side / bulkhead / mudroom / tesla / wagoneer
    words = [(m.start(), CANON.get(m.group(1), m.group(1))) for m in STATE_WORD.finditer(text)]
    if not words:
        return "MISMATCH"
    at = text.find(key)
    if at < 0:
        said = words[0][1] if len({w for _, w in words}) == 1 else None
    else:
        after = [w for pos, w in words if pos > at]
        said = after[0] if after else words[-1][1]
    return "ok" if said == truth else "MISMATCH"


async def main():
    counts = {"ok": 0, "MISMATCH": 0, "SKIP": 0, "MISSING": 0}
    async with aiohttp.ClientSession() as s:
        truth = {}
        for mirror, (source, label) in MIRRORS.items():
            m, t = await state(s, mirror), await state(s, source)
            truth[label] = t
            flag = "MISSING" if m is None or t is None else ("ok" if m == t else "MISMATCH")
            counts[flag] += 1
            print(f"MIRROR {flag:8s} {mirror} = {m!r} | {source} = {t!r}")
        for name, agent in AGENTS.items():
            for label, t in truth.items():
                if t is None:
                    counts["MISSING"] += 1
                    print(f"{name:8s} MISSING  truth=?         | {label}: source entity unavailable, not asked")
                    continue
                kind = "locked" if t in ("locked", "unlocked") else "open"
                q = f"Is the {label} {kind} right now"
                try:
                    async with s.post(f"{BASE}/api/conversation/process", headers=H,
                                      json={"text": q, "agent_id": agent, "language": "en"}) as r:
                        j = await r.json()
                    answer = j.get("response", {}).get("speech", {}).get("plain", {}).get("speech", "") or f"(no speech: {str(j)[:80]})"
                except Exception as ex:  # one failed call must not discard the rest
                    answer = f"(request failed: {type(ex).__name__})"
                v = verdict(answer, t, label)
                counts[v] += 1
                print(f"{name:8s} {v:8s} truth={str(t):9s} | {q:44s} -> {answer[:110]}")
    clean = counts["MISMATCH"] == 0 and counts["MISSING"] == 0 and counts["ok"] > 0
    print(f"RESULT: {'ALL OK' if clean else 'NOT OK'} {counts}")


# Self-test of the judge (runs with the check; cheap and catches a broken heuristic):
assert verdict("The mudroom door is unlocked, everything else is locked.", "unlocked", "mudroom door") == "ok"
assert verdict("The mudroom door is locked.", "unlocked", "mudroom door") == "MISMATCH"
assert verdict("No, sir. The Tesla garage door is closed; the Wagoneer is open.", "closed", "Tesla garage door") == "ok"
assert verdict("No, sir. The Tesla garage door is closed; the Wagoneer is open.", "open", "Wagoneer garage door") == "ok"
assert verdict("Yes, the front door is locked.", "locked", "front door") == "ok"
assert verdict("The front, side and bulkhead doors are all locked.", "locked", "front door") == "ok"
assert verdict("The front, side and bulkhead doors are all locked.", "locked", "side door") == "ok"
assert verdict("The Tesla garage door opener reports closed.", "closed", "Tesla garage door") == "ok"
assert verdict("The only unlocked door is the mudroom door.", "unlocked", "mudroom door") == "ok"
assert verdict("I could not find that.", "locked", "front door") == "MISMATCH"
assert verdict("It is opening.", "opening", "Tesla garage door") == "SKIP"

asyncio.run(main())
