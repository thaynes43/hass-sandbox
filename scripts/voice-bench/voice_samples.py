"""Synthesize one sentence in several TTS voices and save the audio inside the HA pod.

    scripts/voice-bench/run.sh voice_samples.py "ENGINE=tts.kokoro VOICES=bm_george,bm_daniel,bm_lewis,bm_fable LANG=en-GB OUT=/tmp/samples"
    kubectl cp home-automation/<ha pod>:/tmp/samples ./samples -c app     # then hand the files to the owner

Nothing plays anywhere: tts_get_url only synthesizes. Prints file name, size and time-to-audio per voice.
"""

import asyncio
import os
import time

import aiohttp

TOK = os.environ["HA_TOKEN"]
BASE = "http://localhost:8123"
H = {"Authorization": f"Bearer {TOK}"}
ENGINE = os.environ.get("ENGINE", "tts.kokoro")
VOICES = os.environ.get("VOICES", "bm_george").split(",")
LANG = os.environ.get("LANG_", os.environ.get("LANG", "en-GB"))
OUT = os.environ.get("OUT", "/tmp/samples")
SENTENCE = os.environ.get(
    "SENTENCE",
    "Good evening, sir. The garage is closed, the front door is locked, and the rumpus room is at seventy two degrees. "
    "I took the liberty of dimming the lights.",
)


async def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    async with aiohttp.ClientSession() as s:
        for v in VOICES:
            t0 = time.monotonic()
            body = {"engine_id": ENGINE, "message": SENTENCE, "language": LANG, "options": {"voice": v}, "cache": False}
            async with s.post(f"{BASE}/api/tts_get_url", headers=H, json=body) as r:
                if r.status != 200:
                    print(v, "tts_get_url", r.status, (await r.text())[:200])
                    continue
                path = (await r.json())["path"]
            async with s.get(f"{BASE}{path}") as r:
                data = await r.read()
                ext = "mp3" if "mpeg" in (r.headers.get("content-type") or "") or path.endswith(".mp3") else "wav"
            fn = f"{OUT}/jarvis-{v}.{ext}"
            with open(fn, "wb") as f:
                f.write(data)
            print(v, fn, f"{len(data)/1024:.0f} KB", f"{time.monotonic()-t0:.2f}s")


asyncio.run(main())
