"""Assist latency bench. Runs INSIDE the HA pod; token arrives in env HA_TOKEN (never printed).

MODE=stt|tts|pipe|voice|conv (comma separated), see bottom and run.sh.
"""
import asyncio, json, os, subprocess, sys, time, uuid
import aiohttp

BASE = "http://localhost:8123"
TOK = os.environ["HA_TOKEN"]
H = {"Authorization": f"Bearer {TOK}"}
MODES = os.environ.get("MODE", "stt").split(",")
PIPELINE = os.environ.get("PIPELINE", "01jbaynz8ff9fd97wfy9240zr9")
SENTENCE = os.environ.get("SENTENCE", "What is the temperature in the bedroom?")
REPS = int(os.environ.get("REPS", "3"))
DEVICE_ID = os.environ.get("DEVICE_ID", "")  # a satellite's device id (pipe/voice modes)
WAV = "/tmp/bench_in.wav"
SPEECH_SECS = 0.0


async def gen_audio(s):
    """Piper -> 16 kHz mono s16 wav with 0.3 s lead-in and 2 s trailing silence."""
    global SPEECH_SECS
    async with s.post(f"{BASE}/api/tts_get_url", headers=H, json={"engine_id": "tts.piper", "message": SENTENCE}) as r:
        url = (await r.json())["url"]
    async with s.get(url) as r:
        raw = await r.read()
    open("/tmp/bench_raw.wav", "wb").write(raw)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", "/tmp/bench_raw.wav", "-af",
                    "adelay=300,apad=pad_dur=2", "-ar", "16000", "-ac", "1", "-sample_fmt", "s16", WAV], check=True)
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", "/tmp/bench_raw.wav"],
                         capture_output=True, text=True).stdout.strip()
    SPEECH_SECS = float(out) + 0.3
    print(f"audio: '{SENTENCE}' speech={SPEECH_SECS:.2f}s (+2s silence)")


async def bench_stt(s):
    data = open(WAV, "rb").read()
    for eng in os.environ.get("STT", "stt.faster_whisper,stt.home_assistant_cloud,stt.openai_stt,stt.speech_to_phrase").split(","):
        async with s.get(f"{BASE}/api/stt/{eng}", headers=H) as r:
            if r.status != 200:
                print(f"STT {eng}: info HTTP {r.status}")
                continue
            langs = (await r.json())["languages"]
        lang = "en-US" if "en-US" in langs else "en"
        hdr = dict(H)
        hdr["X-Speech-Content"] = f"format=wav; codec=pcm; sample_rate=16000; bit_rate=16; channel=1; language={lang}"
        ts = []
        text = None
        for _ in range(REPS):
            t0 = time.monotonic()
            async with s.post(f"{BASE}/api/stt/{eng}", headers=hdr, data=data) as r:
                body = await r.text()
            ts.append(time.monotonic() - t0)
            try:
                text = json.loads(body).get("text")
            except Exception:
                text = body[:120]
        print(f"STT {eng:28s} {' '.join(f'{t:5.2f}' for t in ts)} s  -> {text!r}")


async def bench_tts(s):
    msgs = {"short": "Done, the lights are off.",
            "long": "Right now it is seventy one degrees in the bedroom, the fan is off, and the nightstand lights are both on at about forty percent."}
    for eng in os.environ.get("TTS", "tts.home_assistant_cloud,tts.piper,tts.openai_tts").split(","):
        for label, msg in msgs.items():
            res = []
            for _ in range(REPS):
                m = f"{msg} {uuid.uuid4().hex[:4]}"
                t0 = time.monotonic()
                async with s.post(f"{BASE}/api/tts_get_url", headers=H, json={"engine_id": eng, "message": m, "cache": False}) as r:
                    j = await r.json()
                if "url" not in j:
                    res.append(f"ERR {j}")
                    continue
                t1 = time.monotonic()
                first = None
                n = 0
                async with s.get(j["url"]) as r:
                    async for chunk in r.content.iter_any():
                        if first is None:
                            first = time.monotonic()
                        n += len(chunk)
                t2 = time.monotonic()
                first = first or t2  # empty body: keep the row, let the sweep finish
                res.append(f"url {t1-t0:4.2f} ttfb {first-t0:4.2f} total {t2-t0:4.2f} ({n//1024}k)")
            print(f"TTS {eng:26s} {label:5s} | " + " | ".join(res))


async def ws_run(s, start_stage, sentence=None):
    """Run the pipeline over WS; returns list of (dt, event_type, summary)."""
    async with s.ws_connect(f"{BASE}/api/websocket") as ws:
        await ws.receive_json()
        await ws.send_json({"type": "auth", "access_token": TOK})
        await ws.receive_json()
        msg = {"id": 1, "type": "assist_pipeline/run", "start_stage": start_stage, "end_stage": "tts", "pipeline": PIPELINE}
        msg["input"] = {"text": sentence} if start_stage == "intent" else {"sample_rate": 16000}
        if DEVICE_ID:  # act as that satellite: the agent is told its area, local intents prefer it
            msg["device_id"] = DEVICE_ID
        t0 = time.monotonic()
        await ws.send_json(msg)
        rows, first_delta, tts_url, speech_end = [], None, None, None
        feeder = None

        async def feed(handler):
            nonlocal speech_end
            raw = open(WAV, "rb").read()
            pcm = raw[raw.find(b"data", 12) + 8:]  # ffmpeg adds a LIST chunk, so the header is not 44 bytes
            step = 3200  # 100 ms
            start = time.monotonic()
            for i in range(0, len(pcm), step):
                await ws.send_bytes(bytes([handler]) + pcm[i:i + step])
                if speech_end is None and (i / 32000) >= SPEECH_SECS:
                    speech_end = time.monotonic()
                await asyncio.sleep(max(0, start + (i + step) / 32000 - time.monotonic()))
            await ws.send_bytes(bytes([handler]))

        while True:
            m = await ws.receive()
            if m.type != aiohttp.WSMsgType.TEXT:
                break
            j = json.loads(m.data)
            if j.get("type") == "result":
                if not j.get("success"):
                    print("  run rejected:", j.get("error"))
                    break
                continue
            ev = j["event"]
            et, d = ev["type"], ev.get("data") or {}
            now = time.monotonic()
            if et == "run-start":
                if start_stage == "stt":
                    feeder = asyncio.create_task(feed(d["runner_data"]["stt_binary_handler_id"]))
                if d.get("tts_output"):
                    tts_url = d["tts_output"].get("url")
            if et == "intent-progress":
                delta = d.get("chat_log_delta") or {}
                if first_delta is None and delta.get("content"):
                    first_delta = now
                    rows.append((now, "first-text-delta", ""))
                if delta.get("tool_calls"):
                    rows.append((now, "tool-call", ",".join(f"{t.get('tool_name', '?')}{json.dumps(t.get('tool_args', {}))}" for t in delta["tool_calls"])))
                continue
            summ = ""
            if et == "stt-end":
                summ = repr(d.get("stt_output", {}).get("text"))
            if et == "intent-end":
                io = d.get("intent_output", {})
                sp = io.get("response", {}).get("speech", {}).get("plain", {}).get("speech", "")
                summ = f"local={d.get('processed_locally')} {sp[:110]!r}"
            if et == "tts-end":
                tts_url = d.get("tts_output", {}).get("url") or tts_url
            if et == "error":
                summ = json.dumps(d)
            rows.append((now, et, summ))
            if et in ("run-end", "error"):
                break
        if feeder:
            feeder.cancel()
            try:
                await feeder
            except asyncio.CancelledError:
                pass
            except Exception as ex:  # a broken feed must not read as a slow STT engine
                print(f"  !! audio feeder failed, discard this run: {type(ex).__name__}: {ex}")
        ref = speech_end or t0
        label = "since end-of-speech" if speech_end else "since text submitted"
        print(f"  timeline ({label}):")
        for t, et, summ in rows:
            print(f"    {t-ref:+7.2f}s {et:18s} {summ}")
        if tts_url:
            t1 = time.monotonic()
            first, n = None, 0
            async with s.get(BASE + tts_url if tts_url.startswith("/") else tts_url) as r:
                async for chunk in r.content.iter_any():
                    if first is None:
                        first = time.monotonic()
                    n += len(chunk)
            first = first or time.monotonic()
            print(f"    tts fetch: first audio byte +{first-t1:.2f}s after request, complete +{time.monotonic()-t1:.2f}s ({n//1024}k)")


async def main():
    async with aiohttp.ClientSession() as s:
        if {"stt", "voice"} & set(MODES):
            await gen_audio(s)
        if "stt" in MODES:
            await bench_stt(s)
        if "tts" in MODES:
            await bench_tts(s)
        if "pipe" in MODES:
            for q in os.environ.get("QUERIES", SENTENCE).split("|"):
                for i in range(REPS):
                    print(f"PIPE text run {i+1}: {q!r}")
                    await ws_run(s, "intent", q)
        if "conv" in MODES:
            agent = os.environ["AGENT"]
            for q in os.environ.get("QUERIES", SENTENCE).split("|"):
                ts, sp = [], ""
                for _ in range(REPS):
                    t0 = time.monotonic()
                    async with s.post(f"{BASE}/api/conversation/process", headers=H, json={"text": q, "agent_id": agent, "language": "en"}) as r:
                        j = await r.json()
                    ts.append(time.monotonic() - t0)
                    resp = j.get("response", {})
                    sp = f"[{resp.get('response_type')}] " + resp.get("speech", {}).get("plain", {}).get("speech", "")[:90]
                print(f"CONV {os.environ.get('LABEL', agent):34s} {' '.join(f'{t:5.2f}' for t in ts)} s | {q[:28]!r} -> {sp!r}")
        if "voice" in MODES:
            for i in range(REPS):
                print(f"VOICE run {i+1}: {SENTENCE!r}")
                await ws_run(s, "stt")

asyncio.run(main())
