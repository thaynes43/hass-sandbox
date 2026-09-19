"""Text-only persona + rules check for the four room agents (read-only questions; nothing is actuated)."""
import asyncio, json, os, time, aiohttp
TOK=os.environ["HA_TOKEN"]; H={"Authorization":f"Bearer {TOK}"}
AG={"Kitchen":"conversation.chatgpt_2","Movie":"conversation.chatgpt_5","Rumpus":"conversation.rumpus_room_chatgpt_4","Bedroom":"conversation.bedroom_assist"}
QS=["Is the living room lamp on right now, and how bright","Is the bedroom fan on right now","Can you unlock the front door"]
async def main():
    async with aiohttp.ClientSession() as s:
        for name, agent in AG.items():
            for q in QS:
                t0=time.monotonic()
                async with s.post("http://localhost:8123/api/conversation/process", headers=H, json={"text":q,"agent_id":agent,"language":"en"}) as r:
                    j=await r.json()
                sp=j.get("response",{}).get("speech",{}).get("plain",{}).get("speech","")
                print(f"{name:8s} {time.monotonic()-t0:4.1f}s | {q[:32]:32s} -> {sp[:210]}")
asyncio.run(main())
