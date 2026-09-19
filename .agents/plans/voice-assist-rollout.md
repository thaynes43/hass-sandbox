# Voice assistant tuning and rollout (Voice PE + OpenAI)

Owner ask (Tom, 2026-09-18): make the Home Assistant Voice PE satellites respond quickly with
OpenAI-backed agents, bedroom first, then expose things safely, fix music, roll out to the
other three satellites, and research MCP tool sources.

**Status: phase 1 done 2026-09-18** — bedroom retuned, Tom voice-tested it ("fast enough now")
and approved the agent cleanup. Nothing outside the bedroom pipeline has been changed.
Phases 2–5 not started.

## Inventory (verified 2026-09-18, HA core-2026.9.2)

| Satellite | Pipeline select | Pipeline | Agent |
|---|---|---|---|
| `assist_satellite.primary_bedroom_voice_assist_satellite` | `select.primary_bedroom_voice_assistant` | Bedroom Assistant `01jbaynz8ff9fd97wfy9240zr9` | `conversation.bedroom_assist` (gpt-5.4-mini, none, priority) |
| rumpus_room | — | Rumpus Room Assist `01jtvee3cf1vfbczk2dmst64qy` | `conversation.rumpus_room_chatgpt_4` (gpt-5-mini, low) |
| kitchen_assist | — | Kitchen Assist `01jbqv0j9wjz49e4rnz3wptffh` | `conversation.chatgpt_2` (gpt-5-mini, **medium**) |
| movie_room | — | Movie Room Assist `01jk451rswcggg0xt1d5yfxr7b` | `conversation.chatgpt_5` (gpt-5-mini, **medium**) |

`openai_conversation` has two config entries on the same OpenAI account
(`01JBM33KVTM1FHF795G01R2C4X`: Music Assistant / Kitchen / Bedroom agents;
`01JK456T3JV6CPBG2ZQ2FS10GE`: Movie Room / Rumpus Room). Agents are **subentries**; the MCP
options probe cannot show their model fields, so read them from inside the HA pod
(`.storage/core.config_entries`, read-only, drop `api_key` and `prompt` before printing) and
write them with `ha_config_set_helper(helper_type="config_subentry", entry_id, subentry_type="conversation", subentry_id, config={...})`.

36 entities are exposed to Assist, so prompt size is **not** a latency factor today.

## Phase 1a — the "Bedroom GPT-5.6" error (fixed)

- Real error (HA system log): `404 model_not_found` for `GPT-Realtime-2.1`, then `gpt-realtime`.
- The account **does** list `gpt-realtime`, `gpt-realtime-2.1`, `gpt-realtime-2.1-mini`, … The 404
  is an endpoint mismatch, not a typo or an access problem: HA's integration calls the
  Responses API, and realtime (speech-to-speech) models are only served by the Realtime API.
  No HA core integration speaks the Realtime API, so that model family cannot be used here.
- Subentry `01M2TXW9MYTV11R6B8S138SY22` now runs `gpt-5.4-mini`, `reasoning_effort: none`,
  `verbosity: low`, `service_tier: priority`, web search on (Tom had enabled it).
- Cleanup (Tom approved): the subentry/device is now titled "Bedroom Assist", entity id
  `conversation.bedroom_assist` (was `conversation.bedroom_gpt_5_6`); the unused gpt-4o-mini
  agent "Bedroom ChatGPT" (`conversation.chatgpt_3`, subentry `01JZ8DWMCRGRJJ5P5YEJGSSBJJ`,
  `recommended: true`, same prompt) was deleted after confirming nothing referenced it.
  Subentry titles are renamed with the WS command `config_entries/subentries/update`.
- What Tom "probably meant": `gpt-realtime-2.1` — a real, voice-native model, but
  `v1/realtime`-only (OpenAI's model page lists Responses/Chat Completions as not supported).
  Realtime + Assist exists only as young custom components (`matt123p/ha-gemini-live` on stock
  firmware; `TristanBrotherton/voicepe-realtime` reflashes the box); core's pluggable
  voice-engine work is merged to `main` but in no 2026.9.x release. Not worth it today.

Reasoning-effort values the installed integration offers: `gpt-5.6*` none…max, `gpt-5.2–5.5`
none…xhigh, `gpt-5.1` none…high, `gpt-5`/`gpt-5-mini` **minimal**…high (no `none`).
`recommended: true` still means `gpt-4o-mini`. `gpt-6-astra` cannot be used on 2026.9.2 (the
integration sends `temperature`/`top_p`, which Astra rejects) and has no `none` effort anyway.

Price per 1M tokens in/out (OpenAI pricing page, 2026-09-18): gpt-5.4-mini 0.75/4.50,
gpt-5.4-nano 0.20/1.25 (no priority tier), gpt-5.6-luna 0.20/1.20, -terra 2/12, -sol 4/20,
gpt-5-mini 0.25/2.00, gpt-4o-mini 0.15/0.60. Priority tier = 2x; at voice volumes that is cents
a month and it trims the long tail.

## Phase 1b — measurements

Harness: `scripts/voice-bench/` (`run.sh bench.py "MODE=voice|pipe|conv|stt|tts …"`,
`run.sh debug_runs.py` for the real runs a satellite made, `run.sh openai_agents.py` for agent
settings + account model ids) — scripts run inside the HA pod and drive `assist_pipeline/run` over the WebSocket
API with a Piper-generated utterance streamed in real time, plus the `/api/stt/<engine>`,
`/api/tts_get_url` and `/api/conversation/process` REST endpoints for per-engine numbers.
Times are seconds **after the end of speech**, query "What is the temperature in the bedroom?"
(needs one `GetLiveContext` tool round trip).

| Stage | Before | After |
|---|---|---|
| End-of-speech detection (VAD, "default") | 0.9 | 0.9 |
| STT | faster_whisper 1.65 | HA Cloud 0.05 (streams while you talk) |
| LLM incl. tool round trip | gpt-4o-mini 2.4–4.1, outliers to 15 s | gpt-5.4-mini/none/priority 1.7–1.9 |
| TTS first audio (HA Cloud) | 0.2–0.8 | unchanged |
| **Total to reply text** | **5.0–6.7** | **2.6–2.8** (one 6.2 s outlier in 4 runs) |

Tom's real runs on the box after the change (debug runs, seconds after end-of-speech to reply
text): fuzzy command "kill the closet lights" 2.2 (light reacts at 1.4), temperature question
4.5, web-search question 3.9; STT 0.04–0.07 every time.

Engine comparison: STT REST round trip faster_whisper 1.7 s, HA Cloud 1.0 s, OpenAI 0.7–1.8 s
(no streaming, variable); TTS first byte HA Cloud 0.2–0.3 s, Piper 0.7–2.4 s, OpenAI 1.0–2.9 s.
LLM totals for the tool-call query: gpt-5.4-mini/none ≈ 2.0, gpt-5.4-nano/none ≈ 2.0 (no
faster, weaker), gpt-5.6-luna/none ≈ 2.3, gpt-4o-mini ≈ 2.7 with a long tail.

## Phase 1c — applied to "Bedroom Assistant"

| Setting | Was (rollback) | Now |
|---|---|---|
| `conversation_engine` | `conversation.chatgpt_3` (since deleted — recreate a `recommended: true` agent to go back) | `conversation.bedroom_assist` |
| `stt_engine` / `stt_language` | `stt.faster_whisper` / `en` | `stt.home_assistant_cloud` / `en-US` |
| TTS | `tts.home_assistant_cloud`, `JennyNeural||assistant` | unchanged (already the fastest) |
| `prefer_local_intents` | `true` | unchanged |

Left alone: `select.primary_bedroom_voice_finished_speaking_detection` = `default` (≈ 0.9 s of
silence). `aggressive` would save ~0.5 s but can cut off a mid-sentence pause; Tom was happy
without it.

How `prefer_local_intents` really behaves with an LLM agent (read from the installed
`assist_pipeline/pipeline.py` + `conversation/default_agent.py`, and confirmed live): exact-match
sentences are handled locally in ~0.1 s **except** `HassGetState` and
`HassMediaSearchAndPlay`, which are always handed to the LLM when the agent supports control.
So "turn on the closet lights" never touches OpenAI; state questions and music requests always
do, and each costs two model round trips (tool call, then the spoken answer). Replies under 60
characters are not TTS-streamed (`STREAM_RESPONSE_CHARS`), so time-to-last-token is what counts.

## Findings parked for phase 2 (exposure)

- The built-in agent answers "what is the temperature in the bedroom" with *"can't get the
  temperature for 'Set Primary Bedroom to Relax' (a script)"* — an exposed script in the area
  shadows the temperature sensor. Harmless on the LLM pipelines (state questions go to the LLM
  anyway) but it is what the default "Home Assistant" pipeline says.
- Hard limits to design around: more than 128 exposed scripts/tools breaks the conversation
  engine outright; HA's own guidance for small local models is under 25 exposed entities.

## Phases 2–5

Not started. 2: safe exposure workshop → written plan here. 3: Music Assistant + its agent
(`conversation.chatgpt`, gpt-5-mini low, `max_tokens: 150`). 4: roll out to rumpus/kitchen/movie.
5: MCP servers as LLM tool sources.
