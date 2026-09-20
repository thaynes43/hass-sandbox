import asyncio, os, aiohttp
TOK=os.environ["HA_TOKEN"]
LR="media_player.living_room"; K="media_player.kitchen"
async def main():
    async with aiohttp.ClientSession() as s, s.ws_connect("http://localhost:8123/api/websocket") as ws:
        await ws.receive_json(); await ws.send_json({"type":"auth","access_token":TOK}); await ws.receive_json()
        n=[0]
        async def call(domain, service, data=None, target=None, resp=False):
            n[0]+=1; m={"id":n[0],"type":"call_service","domain":domain,"service":service,"service_data":data or {}}
            if target: m["target"]=target
            if resp: m["return_response"]=True
            await ws.send_json(m); r=await ws.receive_json(); return (r.get("result") or {}).get("response") if resp else (r.get("success"), (r.get("error") or {}).get("message"))
        async def show(label):
            n[0]+=1; await ws.send_json({"id":n[0],"type":"render_template","template":"LR={{ states('"+LR+"') }}@{{ state_attr('"+LR+"','volume_level') }} '{{ state_attr('"+LR+"','media_title') }}' grp={{ (state_attr('"+LR+"','group_members') or [])|count }} | K={{ states('"+K+"') }} '{{ state_attr('"+K+"','media_title') }}'"}); await ws.receive_json(); print(label, (await ws.receive_json())["event"]["result"])
        await show("start           :")
        print("play   ->", await call("music_assistant","play_media",{"media_id":"Daft Punk","media_type":"artist","enqueue":"replace"},{"entity_id":LR})); await asyncio.sleep(8); await show("playing LR      :")
        print("add    ->", await call("script","voice_group_music",{"source_area":"living_room","areas":["kitchen"],"action":"add"},resp=True)); await asyncio.sleep(8); await show("after add       :")
        print("remove ->", await call("script","voice_group_music",{"source_area":"living_room","areas":["kitchen"],"action":"remove"},resp=True)); await asyncio.sleep(12); await show("after remove    :")
asyncio.run(main())
