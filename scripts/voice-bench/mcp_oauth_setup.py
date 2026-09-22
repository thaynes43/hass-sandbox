"""Register Home Assistant as an OAuth client of an MCP server and store the credentials.

Run INSIDE the HA pod via run.sh (the HA token arrives over stdin, never argv):
    scripts/voice-bench/run.sh mcp_oauth_setup.py "MCP_URL=https://cigars.haynesnetwork.com/mcp NAME=cigar-journal"

Steps: check HA for an existing `mcp` application credential of that NAME (re-runs must not orphan a
client on the server) -> read the server's RFC 9728 protected-resource metadata (path-inserted form
first, then the root form) -> its RFC 8414 authorization-server metadata -> RFC 7591 dynamic client
registration with HA's `my.home-assistant.io` redirect -> `application_credentials/create` over HA's
websocket. Prints NO secrets: only a client_id prefix and the credential id.

Known limit (2026-09-22): HA's `mcp` integration sends neither PKCE nor RFC 8707 `resource`, so an
OAuth 2.1-strict server (cigar-journal) rejects the link even with a valid credential. Kept for the
day HA adds PKCE; see .agents/plans/local-assist-stack.md.
"""

import asyncio
import os
from urllib.parse import urlsplit

import aiohttp

TOK = os.environ["HA_TOKEN"]
BASE = "http://localhost:8123"
MCP_URL = os.environ["MCP_URL"]
NAME = os.environ.get("NAME", "mcp-server")
REDIRECT = "https://my.home-assistant.io/redirect/oauth"
SENSITIVE = ("secret", "token", "key")


def redact(d: dict) -> dict:
    return {k: v for k, v in d.items() if not any(w in k.lower() for w in SENSITIVE)}


async def get_json(s: aiohttp.ClientSession, url: str) -> dict | None:
    async with s.get(url) as r:
        if r.status != 200 or "json" not in (r.headers.get("content-type") or ""):
            return None
        return await r.json()


async def main() -> None:
    parts = urlsplit(MCP_URL)
    origin = f"{parts.scheme}://{parts.netloc}"
    path = parts.path.rstrip("/")

    async with aiohttp.ClientSession() as s:
        # 1. Do not register twice: the credential is what HA keeps, so check it first.
        async with s.ws_connect(f"{BASE}/api/websocket") as ws:
            await ws.receive_json()
            await ws.send_json({"type": "auth", "access_token": TOK})
            r = await ws.receive_json()
            assert r["type"] == "auth_ok", r
            await ws.send_json({"id": 1, "type": "application_credentials/list"})
            resp = await ws.receive_json()
            if not resp.get("success"):
                print("application_credentials/list failed, refusing to register blind:", redact(resp.get("error") or {}))
                return
            existing = resp.get("result") or []
            for c in existing:
                if c.get("domain") == "mcp" and c.get("name") == NAME:
                    print("credential already exists", c["id"], "- nothing registered; delete it in HA first to re-register")
                    return

        # 2. Discovery. RFC 9728 puts a path-bearing resource's metadata at
        #    /.well-known/oauth-protected-resource<path>; the root form is the no-path case.
        prm = None
        for candidate in ([f"{origin}/.well-known/oauth-protected-resource{path}"] if path else []) + [
            f"{origin}/.well-known/oauth-protected-resource"
        ]:
            prm = await get_json(s, candidate)
            if prm:
                break
        if not prm:
            print("server publishes no protected-resource metadata: it is not an OAuth-protected MCP server (or needs a bearer proxy)")
            return
        servers = prm.get("authorization_servers") or []
        if not servers:
            print("protected-resource metadata names no authorization server")
            return
        auth_server = servers[0].rstrip("/")
        asm = await get_json(s, f"{auth_server}/.well-known/oauth-authorization-server")
        if not asm:
            print("authorization server publishes no RFC 8414 metadata at", auth_server)
            return
        reg_url = asm.get("registration_endpoint")
        if not reg_url:
            print("no registration_endpoint: register a client by hand and add application credentials in HA")
            return

        # 3. Register.
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
            reg = await r.json(content_type=None) if "json" in (r.headers.get("content-type") or "") else {}
        if status not in (200, 201):
            print("register failed", status, redact(reg))
            return
        cid = reg.get("client_id")
        if not cid:
            print("registration returned", status, "but no client_id:", redact(reg))
            return
        csec = reg.get("client_secret") or ""
        if not csec:
            print(
                "server issued a PUBLIC client (no client_secret); HA's application_credentials needs a confidential one."
                " Nothing stored; the registration is unusable — revoke it on the server."
            )
            return
        print(
            "registered client_id", cid[:6] + "…", "auth_method", reg.get("token_endpoint_auth_method"), "scope", reg.get("scope"),
        )

        # 4. Store in HA.
        async with s.ws_connect(f"{BASE}/api/websocket") as ws:
            await ws.receive_json()
            await ws.send_json({"type": "auth", "access_token": TOK})
            r = await ws.receive_json()
            assert r["type"] == "auth_ok", r
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
            if r.get("success"):
                print("application credential ok", (r.get("result") or {}).get("id"))
            else:
                print("credential store FAILED:", redact(r.get("error") or {}))
                print("a client was registered on the server as", cid[:6] + "… and is now orphaned - revoke it there before re-running")


asyncio.run(main())
