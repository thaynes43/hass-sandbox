"""List openai_conversation agent subentries (no prompt, no key) and the model ids the account can see.

Reads .storage read-only; the API key is used for one GET /v1/models and never printed.
"""
import json, urllib.request
d = json.load(open("/config/.storage/core.config_entries"))
for e in d["data"]["entries"]:
    if e["domain"] != "openai_conversation":
        continue
    print("== entry", e["entry_id"], e["title"])
    for s in e.get("subentries", []):
        data = {k: v for k, v in s["data"].items() if k != "prompt"}
        print("  ", s["subentry_type"], "|", s["title"], "|", s["subentry_id"], "|", json.dumps(data))
    key = e["data"]["api_key"]
    req = urllib.request.Request("https://api.openai.com/v1/models", headers={"Authorization": "Bearer " + key})
    try:
        r = json.load(urllib.request.urlopen(req, timeout=20))
        ids = sorted(m["id"] for m in r["data"])
        print("  models (%d):" % len(ids), ", ".join(ids))
    except Exception as ex:
        print("  models list failed:", type(ex).__name__, str(ex)[:200])
