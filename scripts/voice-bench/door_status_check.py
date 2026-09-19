"""Polarity check for the door/garage status sensors: prints the real lock/cover states, then what
the room agents SAY about them (text-only, read-only). Any disagreement is a bug."""
import asyncio, json, os, aiohttp
TOK=os.environ["HA_TOKEN"]; H={"Authorization":f"Bearer {TOK}"}
QS=["Is the front door locked","Is the mudroom door locked","Is the mudroom door unlocked","Are any doors unlocked right now","Is the garage open","Is the Wagoneer garage door closed"]
async def main():
    async with aiohttp.ClientSession() as s:
        for e in ["lock.front_door_lock","lock.mudroom_door_lock","lock.side_door_lock","lock.bulkhead_lock","cover.ratgdov25i_4a0325_door","cover.ratgdov25i_dbfa50_door","sensor.front_door_lock_state","sensor.mudroom_door_lock_state","sensor.wagoneer_garage_door_state"]:
            async with s.get(f"http://localhost:8123/api/states/{e}", headers=H) as r:
                j=await r.json(); print(f"TRUTH {e} = {j.get('state')}")
        for agent in ["conversation.bedroom_assist","conversation.chatgpt_2","conversation.rumpus_room_chatgpt_4"]:
            for q in QS:
                async with s.post("http://localhost:8123/api/conversation/process", headers=H, json={"text":q,"agent_id":agent,"language":"en"}) as r:
                    j=await r.json()
                print(f"{agent.split('.')[1]:16s} | {q:34s} -> {j['response']['speech']['plain']['speech'][:150]}")
asyncio.run(main())
