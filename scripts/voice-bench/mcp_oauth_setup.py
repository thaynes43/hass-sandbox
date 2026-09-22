"""Register Home Assistant as an OAuth client of an MCP server and store the credentials.

Run INSIDE the HA pod via run.sh (the HA token arrives over stdin, never argv):
    scripts/voice-bench/run.sh mcp_oauth_setup.py "MCP_URL=https://cigars.haynesnetwork.com/mcp NAME=cigar-journal"

Steps: read the server's RFC 9728 protected-resource metadata -> its RFC 8414 authorization-server
metadata -> RFC 7591 dynamic client registration with HA's `my.home-assistant.io` redirect ->
`application_credentials/create` for the `mcp` domain over HA's websocket. Prints NO secrets: only a
client_id prefix and the credential id. After this, adding the "Model Context Protocol" integration in
HA's UI with the same URL skips straight to the server's consent page.
"""

import asyncio
import os

import aiohttp

TOK = os.environ["HA_TOKEN"]
BASE = "http://localhost:8123"
MCP_URL = os.environ["MCP_URL"]
NAME = os.environ.get("NAME", "mcp-server")
REDIRECT = "https://my.home-assistant.io/redirect/oauth"


async def main() -> None:
    origin = MCP_URL.split("/mcp")[0]
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{origin}/.well-known/oauth-protected-resource") as r:
            prm = await r.json()
        auth_server = prm["authorization_servers"][0].rstrip("/")
        async with s.get(f"{auth_server}/.well-known/oauth-authorization-server") as r:
            asm = await r.json()
        reg_url = asm.get("registration_endpoint")
        if not reg_url:
            print("no registration_endpoint: register a client by hand and add application credentials")
            return
        body = {
            "client_name": f"Home Assistant Assist ({NAME})",
            "redirect_uris": [REDIRECT],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_post",
            "scope": " ".join(prm.get("scopes_supported", [])),
        }
        async with s.post(reg_url, json=body) as r:
            status = r.status
            reg = await r.json()
        if status not in (200, 201):
            print("register failed", status, {k: v for k, v in reg.items() if "secret" not in k})
            return
        cid = reg["client_id"]
        csec = reg.get("client_secret") or ""
        print(
            "registered client_id", cid[:6] + "…", "secret", "yes" if csec else "NO (public client)",
            "auth_method", reg.get("token_endpoint_auth_method"), "scope", reg.get("scope"),
        )
        async with s.ws_connect(f"{BASE}/api/websocket") as ws:
            await ws.receive_json()
            await ws.send_json({"type": "auth", "access_token": TOK})
            r = await ws.receive_json()
            assert r["type"] == "auth_ok", r
            await ws.send_json({"id": 1, "type": "application_credentials/list"})
            existing = (await ws.receive_json()).get("result") or []
            for c in existing:
                if c.get("domain") == "mcp" and c.get("name") == NAME:
                    print("credential already exists", c["id"], "- leaving it; delete it in HA first to re-register")
                    return
            await ws.send_json(
                {
                    "id": 2,
                    "type": "application_credentials/create",
                    "domain": "mcp",
                    "client_id": cid,
                    "client_secret": csec,
                    "name": NAME,
                }
            )
            r = await ws.receive_json()
            print("application credential", "ok" if r.get("success") else r.get("error"), (r.get("result") or {}).get("id"))


asyncio.run(main())
