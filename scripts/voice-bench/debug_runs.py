"""Print stage timings of the last N real Assist runs of a pipeline (HA keeps 10, in memory)."""
import asyncio, json, os, aiohttp
from datetime import datetime
TOK=os.environ["HA_TOKEN"]; PID=os.environ.get("PIPELINE", "01jbaynz8ff9fd97wfy9240zr9")
async def main():
    async with aiohttp.ClientSession() as s, s.ws_connect("http://localhost:8123/api/websocket") as ws:
        await ws.receive_json(); await ws.send_json({"type":"auth","access_token":TOK}); await ws.receive_json()
        await ws.send_json({"id":1,"type":"assist_pipeline/pipeline_debug/list","pipeline_id":PID})
        runs=(await ws.receive_json())["result"]["pipeline_runs"][-int(os.environ.get("N","4")):]
        for i,r in enumerate(runs, start=2):
            await ws.send_json({"id":i,"type":"assist_pipeline/pipeline_debug/get","pipeline_id":PID,"pipeline_run_id":r["pipeline_run_id"]})
            ev=(await ws.receive_json())["result"]["events"]
            ts={}
            print("RUN", r["timestamp"])
            vad_end=None
            for e in ev:
                t=datetime.fromisoformat(e["timestamp"]); d=e.get("data") or {}
                if e["type"]=="stt-vad-end": vad_end=t
                ts.setdefault(e["type"], t)
            ref=vad_end or datetime.fromisoformat(ev[0]["timestamp"])
            seen_delta=False
            for e in ev:
                t=datetime.fromisoformat(e["timestamp"]); d=e.get("data") or {}; info=""
                if e["type"]=="intent-progress":
                    dl=d.get("chat_log_delta") or {}
                    if dl.get("tool_calls"): info="tool:"+",".join(x.get("tool_name","?") for x in dl["tool_calls"])
                    elif dl.get("content") and not seen_delta: seen_delta=True; info="first text"
                    else: continue
                if e["type"]=="run-start": info=f"pipeline={d.get('pipeline')} tts_stream={ (d.get('tts_output') or {}).get('stream_response')}"
                if e["type"]=="stt-start": info=f"engine={d.get('engine')}"
                if e["type"]=="stt-end": info=repr(d.get("stt_output",{}).get("text"))
                if e["type"]=="intent-end":
                    sp=d.get("intent_output",{}).get("response",{}).get("speech",{}).get("plain",{}).get("speech","")
                    info=f"local={d.get('processed_locally')} {sp[:100]!r}"
                if e["type"]=="error": info=json.dumps(d)
                print(f"   {(t-ref).total_seconds():+7.2f}s {e['type']:16s} {info}")
asyncio.run(main())
