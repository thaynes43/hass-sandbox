# Local assist stack — resident LLM, local STT/TTS, and the tool tracks (cold start)

Written 2026-09-22. Read this before touching the local voice stack or the two tool tracks below.
Companion docs: `.agents/plans/voice-assist-rollout.md` (the OpenAI room agents, phases 1–4),
`.agents/plans/voice-assist-handoff.md` (room-by-room testing), `agent-docs/voice-agent-prompts.md`
(prompt backups incl. Jarvis's). Cluster side lives in haynes-ops (`kubernetes/main/apps/ai/`).

## What is live (2026-09-22)

| Piece | Where | Facts |
|---|---|---|
| Resident LLM | haynes-ops `ai/llama-server` on `talosw01` | llama.cpp `server-cuda-b11096`, **Muse Glimmer 30B Q4_K_M** (`Muse-Glimmer-30B-KQuant-17GB-Q4_K_M.gguf` on NFS `misc/llama/models/`), alias `muse-glimmer-30b`, `--parallel 2`, `--reasoning-effort low`, KV q8. `http://llama-server.ai.svc.cluster.local:8080/v1`. Also an Open WebUI connection (haynes-ops#3110). |
| GPU layout | `talosw01` 2× RTX 3090 | Tom's ruling 2026-09-22 (haynes-ops#2960): per-app pinning via `NVIDIA_VISIBLE_DEVICES`. **LLM on 3090 #1 (`GPU-d8a856f1…`), ComfyUI on 3090 #0 (`GPU-18bf6eab…`)** — swapped the same day because #0 throttles to 225 MHz within ~10 s of load (haynes-ops#3052; 87 °C, fan 100 %). Until airflow is fixed nothing latency-sensitive goes on #0. ComfyUI's AppDaemon and Open WebUI graphs load the same int8 Qwen-Image-2.1 files, so one resident copy serves both. |
| STT | haynes-ops `ai/whisper` on `talosm01` (A2000) | `ghcr.io/thaynes43/wyoming-whisper-gpu:3.8.1-2`, **NVIDIA Parakeet TDT 0.6B v2** via onnx-asr on CUDA, `whisper.ai.svc.cluster.local:10300`, HA entry `faster-whisper` → `stt.faster_whisper`. Measured in HA: 0.30 s cold / 0.05 s warm vs HA Cloud 1.14 / 0.86 s, same transcript with punctuation. |
| TTS | haynes-ops `ai/kokoro` on `talosm01` | `flight777/kokoro-wyoming-ml` (CUDA), Kokoro-82M, `kokoro.ai.svc.cluster.local:10210`, HA entry `01M34V1S24J5SBNZXXFW00JF1K` → `tts.kokoro`, 54 voices, **no streaming synthesis** (that image reports `supports_synthesize_streaming: False`). 0.28–0.50 s to first audio vs cloud 0.39–0.62 s. Piper is still there as fallback. A2000 total ≈ 8.1 GB of 12 (vexa 3.9 + Parakeet 3.4 + Kokoro 0.6). |
| HA agent | `llama_cpp` integration, entry `01M34TQMBVCKW0BG0KZW5JG5YM` | subentry `01M34TQMBVMNR9CJZX892KD8VJ` → **`conversation.muse_glimmer_30b`**, `llm_hass_api: [assist]`, prompt = JARVIS persona + the shared spoken-aloud block (backup in `agent-docs/voice-agent-prompts.md`; write back with `ha_config_set_helper(helper_type="config_subentry", …)`). |
| Pipeline | **Jarvis** `01jb8sg4njw0mh3gnpqt4j9h6x` (renamed from Regina the same evening) | `stt.faster_whisper` (en) → `conversation.muse_glimmer_30b` (JARVIS persona) → `tts.kokoro` `bm_george` (en-GB, provisional), `prefer_local_intents: true`. **Rumpus Room Voice PE** runs it with wake word **Hey Jarvis** (`select.rumpus_room_voice_assistant` / `select.rumpus_room_voice_wake_word`; revert = "Rumpus Room Assist" / "Okay Nabu"). Text-tested as that satellite 13/13 correct. **Kitchen** pipeline moved to local STT + Kokoro `af_sarah` with its OpenAI agent kept (it carried the cigar-journal API for one evening; removed again the same night — ~2 s per turn, see Tool track 1). Bedroom and Movie Room pipelines unchanged (OpenAI + HA Cloud). |
| Removed | — | `ollama-assist01` (haynes-ops#3108, PVC orphan haynes-ops#3109) and its HA entry (had zero models). `ollama-assist02` on the RTX 2000 Ada still serves AppDaemon's detection summaries (`qwen3.5:9b`). |

### What the numbers mean

- The Assist prompt is **~15k tokens per turn** (115 exposed entities + ~30 tool schemas). llama-server's
  prefix cache reuses 98–99.9 % of it, so per-turn prompt cost is ~0.2–0.5 s; a changed system prompt costs
  one full re-eval (~13 s at 1,100 tok/s) once. Slots are 32k so a longer chat or more tools fit.
- On a healthy card the model decodes ~38 tok/s; a two-round tool question (tool call → answer) is ~2.5 s.
  If answers drift to 10 s+, **sample the card's clocks before touching prompts**
  (`kubectl exec -n observability <nvidia-gpu-exporter pod on talosw01> -- nvidia-smi -i N
  --query-gpu=temperature.gpu,clocks.sm,clocks_throttle_reasons.active --format=csv -l 1`).
- Bench harness: `scripts/voice-bench/run.sh bench.py "MODE=conv AGENT=conversation.muse_glimmer_30b
  QUERIES='…'"` (text, read-only questions are safe) and `MODE=stt,tts STT=… TTS=…` (synthesis only).
  Never `MODE=voice` against a room.

## Model bake-off still owed

Tom named two candidates; only one is deployed. `qwen3.8:27b` (18 GB Q4, thinking on by default — needs
llama-server `--reasoning-effort low`, which is why Ollama was not used) should be benched against Muse
Glimmer on the same card with the same `QUERIES` and the cigar-journal tool test below. Fetch pattern:
haynes-ops `kubernetes/main/apps/ai/llama-server/resources/fetch-model-job.yaml` (12 Gi limit — the first
fetch OOM-paged Tom at 6 Gi). Swapping models = change `-m` and `--alias` in the HR; one PR each way.

## Tool track 1 — cigar-journal as the tool-use test (in progress)

- cigar-journal is a full OAuth 2.1 server (`/.well-known/oauth-protected-resource`, PKCE, refresh,
  dynamic registration). `scripts/voice-bench/mcp_oauth_setup.py` registered HA as a client and stored
  the credential (`application_credentials` domain `mcp`, name `cigar-journal`, id `mcp_ee4a70…`) —
  **but the link fails**: cigar-journal's authorize endpoint requires PKCE S256 and RFC 8707
  `resource`, and HA's `mcp` integration (2026.9.2) sends neither (plain `LocalOAuth2Implementation`,
  no `code_challenge`), so the server reflects an error to the callback and my.home-assistant.io shows
  "Account linking rejected". Verified in `apps/web/app/oauth/authorize/route.ts`. The credential is
  left in place for when HA adds PKCE.
- **Ruling (Tom, 2026-09-22):** in-cluster header hop instead. The MCP app's bearer gate is the only
  auth path (`packages/mcp/src/auth.ts`), and the HA pod already reaches
  `cigar-journal-mcp.frontend.svc.cluster.local:8081` (no policy), so haynes-ops runs an nginx hop
  `cigar-mcp-hop.home-automation.svc.cluster.local:8080/mcp` that adds `Authorization: Bearer
  $CIGAR_JOURNAL_TOKEN` (the dev-env consumer's full-scope service token, same 1Password field).
  HA's MCP entry points at the hop with no auth. Test scope; a dedicated read-only `home-assistant`
  service token (ADR-011 CLI in the cigar-journal pod, pasted into 1Password by Tom) is the follow-up
  if it becomes permanent.
- Live since 2026-09-22: haynes-ops#3112 (hop), HA `mcp` entry `01M34ZKF449AB21P6K1EGW6880`
  ("cigar-journal", 35 tools). It was first attached to the local agent as `llm_hass_api: ["assist",
  "mcp-01M34ZKF449AB21P6K1EGW6880"]` for the test below; who holds it now is in the safety note at the
  end of this section. The tool schemas add **~28k tokens** to every turn (15k →
  43.6k), which first overflowed the 32k slot; llama-server now runs `--ctx-size 131072` = 64k per
  slot (haynes-ops#3113, +488 MiB VRAM).

**Results (2026-09-22, `bench.py MODE=pipe PIPELINE=<Jarvis, then still named Regina>`, GPU in thermal throttle — prompt
eval ~300 tok/s, decode 8–9 tok/s):**

| Question | Tool chosen | Result size | Outcome |
|---|---|---|---|
| What cigars do I have in my humidor? | `get_my_inventory{}` ✓ | **~47k tokens** | request grew to 91,674 tokens → context error even at 64k |
| What was the last cigar I smoked? | `get_my_smokes{limit: 10}` ✓ | 5.2k tokens | correct spoken answer (name, date, rating) in 37 s |
| Find me a maduro in the catalog | `browse_catalog{q: "maduro", limit: 48}` ✓ | 18.7k tokens (62 s to ingest) | correct answer ("eighty five matches, examples …") in 91 s |
| Is the front door locked? | `assist…GetLiveContext{name: "Front Door Lock State"}` ✓ | small | correct in 17 s |

Conclusions: the 30B model **picks the right tool with sensible arguments 4/4**, mixing Assist and
MCP tools. What breaks the voice loop is (1) the throttled GPU (haynes-ops#3052 — 3.5× slower prompt eval,
4× slower decode than the same card cold) and (2) **tool result size**: cigar-journal's outputs are
sized for desktop agents, not a spoken turn. Before any room agent gets these tools: cap results in
the prompt ("ask for at most five results; never fetch the whole humidor"), and ask cigar-journal
for a compact/voice output mode (or per-tool `limit` defaults) — the model cannot shrink a 47k-token
result it never sees. Prompt caching does carry the 28k tool preamble across turns (only the first
turn after a restart pays ~30 s cold), but the cache is per slot and there are two slots.
- **Who holds the cigar-journal API (2026-09-22 evening):**
  - the **Kitchen** OpenAI agent had it for one evening (Tom's tool tests) and **lost it again the same
    night**: the 35 schemas cost ~2 s per turn on OpenAI (isolation bench, bedroom as control), which
    Tom ruled unaffordable for a room agent — `conversation.chatgpt_2` is Assist-only again;
  - a second `llama_cpp` subentry (also titled "Muse Glimmer 30b") on **no pipeline**, for text tests;
  - **not** the main Jarvis agent `01M34TQMBVMNR9CJZX892KD8VJ` (Assist only — keeps the room turn
    28k tokens lighter). Net: **no satellite-backed agent holds the cigar-journal API**; only the
    pipeline-less test subentry does.
- **Voice safety, as it stands:** the journal server exposes write tools (`save_smoke`,
  `record_purchase`, …) and the hop carries the dev-env consumer's **full-scope** token. Today the
  only holder is the pipeline-less test subentry, so no spoken request can reach them. The guard
  used while the kitchen had them was a **prompt bullet** ("ask for at most five results … by voice
  the journal is read-only") — a prompt rule, not an enforced scope; it came out of the live kitchen
  prompt together with the API, and is parked in *Kitchen — additions* in
  `agent-docs/voice-agent-prompts.md`. Before any room agent gets
  the API again: a dedicated read-only `home-assistant` service token (`catalog:read journal:read`)
  behind the hop, a compact/voice output mode on the server so results stop being 5k–47k tokens,
  and the ~2 s-per-turn schema cost on OpenAI (measured) needs to be acceptable or reduced.

## Tool track 2 — Movie Room recommender (RATIFIED 2026-09-23: built in haynesnetwork, not here)

Goal (Tom, 2026-09-22): tune an agent for the Movie Room that recommends new things to watch. Refined
by Tom on 2026-09-23: "specialized agents for specialized things", the Movie Room agent knows **his
watch history on all servers**, answers "what series haven't I finished" and "what should I watch
next" without repeating anything he has seen, and accepts "I already watched X, recommend a new show";
dev-env gets the same tools. He pointed it at **haynesnetwork**, which already holds the Tautulli trio,
the Plex owner tokens, the ledger with ratings and genres, and his identity.

**Where it lives now:** haynesnetwork ADR-087 (the in-cluster `/api/mcp` surface and its hop),
ADR-088 (watch history read-model, Watch Marks, Plex write-back), ADR-089 (deterministic
recommendations), DESIGN-049, PLAN-068, OPS-015. The standalone `ai/media-mcp` proposal above this
line is retired.

**What HA sees:** an `mcp` entry named "Watch history" at
`http://haynesnetwork-mcp-hop.frontend.svc.cluster.local:8080/mcp` (the hop injects the consumer token;
HA holds no credential), granted to the **Movie Room agent only** (`conversation.chatgpt_5`), with a
WATCH HISTORY block in its prompt (`agent-docs/voice-agent-prompts.md`). Seven tools at first, nine
since 2026-09-26 (`watchlist` and `set_watchlist`, below); `tools/list` ≤ 4 KB (3 KB until the
watchlist tools), spoken-text results ≤ 1,200 characters — the voice budget that keeps this from repeating the
cigar-journal 2 s-per-turn cost (Tool track 1). With two APIs on the agent, HA namespaces every tool:
`watch_history__recommend`, and Assist's become `assist__…`.

Answers to the three questions:
- Q-01 (which Plex/Tautulli pair): **all of them** — Tom, 2026-09-23 ("find my watch history via plex or
  Tautulli (all servers)"). HaynesTower holds his history since 2023-09, HaynesOps since 2026-07; plex.tv
  view-state sync is on for his account.
- Q-02 (Seerr requests by voice): **resolved 2026-09-25** (Tom: "Add it, say it downloads"). Requests go
  through his Plex watchlist, which Seerr already auto-requests: `set_watchlist` adds the title and its
  answer says "It isn't on Plex yet, so Seerr will request it." haynesnetwork never calls Seerr itself.
- Q-03 (local or OpenAI): v1 runs on the room's existing OpenAI agent (`gpt-5.6-terra`, reasoning none),
  because the owner's 2026-09-22 latency ruling rules out the throttled local card for a room agent
  (tool turns 8–34 s). Revisit after the Qwen bake-off.
- New, open: household persons by spoken name (Kellie, Penelope, Jackson) — haynesnetwork PRD Q-12. v1
  answers for Tom's account only, and the prompt says so.

Measured cost (voice bench, text as the agent, 3 reps, `conv` mode, 2026-09-23; pass = at most 0.5 s
median added latency on questions that use no watch tool, haynesnetwork R-245):

| Question (no watch tool) | Assist only, median | Assist + Watch history, median |
|---|---|---|
| Is it warm in the movie room? | 4.62 s | 3.12 s (3.87 / 3.12 / 2.69) |
| Give me a one line movie trivia fact. | 1.87 s | 1.57 s (1.95 / 1.29 / 1.57) |
| Are the movie room lights on? | 2.98 s | 2.96 s (3.23 / 2.96 / 2.57) |

No median rose: the seven-tool list is 2,712 bytes and every result is spoken text under 1,200
characters, so the per-turn schema cost that sank Tool track 1 does not appear. The three US-13
questions, asked as text to `conversation.chatgpt_5`: "What shows haven't I finished?" 3.5 s
("Our Flag Means Death, next up season two, episode one. Spider-Noir has three left…"); "What should
I watch next?" 3.1 s (Arcane, Daredevil, Terminator 2, each with a reason); "I already watched
Severance, recommend a new show" 3.6 s (marked, then Arcane / FROM) and "Undo that" 3.4 s ("Undone.
Severance is no longer marked watched."). Attached with `scripts/voice-bench/attach_watch_history.py`
(mcp entry `01M381GTWER1BG9K4MWG3GDEGR`, LLM API `mcp-01M381GTWER1BG9K4MWG3GDEGR`). One side effect of
the OpenAI reconfigure flow: it re-derives the agent's `city` from `zone.home` on every save
(Northborough → Bedford → Grafton → Leominster across three saves); the value only feeds web-search
localisation and cannot be pinned through the flow.

**Watchlist tools (2026-09-26).** The server gained `watchlist` (read: his plex.tv watchlist, newest
first, each title marked on Plex or not) and `set_watchlist` (write: add or remove one title on his real
plex.tv watchlist; `undo_last_change` reverses it). Adding a title that is not on Plex makes Seerr
download it. Two things learned putting them live:

- **HA loads an MCP server's tool list once, at entry setup.** The `mcp` integration's
  DataUpdateCoordinator has no listeners, so its 30-minute `UPDATE_INTERVAL` never fires. A server that
  gains or loses tools needs `homeassistant.reload_config_entry` on the mcp entry
  (`01M381GTWER1BG9K4MWG3GDEGR`) or an HA restart. After the reload the Movie Room agent used the new
  tools with no prompt change, but it paraphrased their answers down to a few words (it dropped "It's on
  Plex."), so the WATCH HISTORY block gained a last bullet that makes it say back the title and year and
  always say when a title will download. The helper puts a changed block live with
  `ACTION=update ENTRY_ID=01M381GTWER1BG9K4MWG3GDEGR` (`DRY_RUN=1` first): it swaps a known earlier block
  in place and leaves `llm_hass_api` alone. It prints its own undo, the same action with
  `PROMPT_FILE=<its backup>`, which puts the old block back while the pod's `/tmp` lasts.
- **Nine tools still cost nothing measurable.** Same bench on 2026-09-26 at 10:40Z, after the entry reload and
  with the new prompt line live (the agent answered "Is FROM on my watchlist?" from `watchlist` just before),
  text as `conversation.chatgpt_5`, 3 reps, `conv` mode: every median is below both 2026-09-23 medians (table
  below). An earlier run that day (08:36Z) was published here as the nine-tool column, but it ran before the
  reload, so the agent still had seven tools; it is now its own column.

| Question (no watch tool) | Assist only (2026-09-23), median | Seven tools (2026-09-23), median | Seven tools (2026-09-26 08:36Z, before the reload), median | Nine tools (2026-09-26 10:40Z), median |
|---|---|---|---|---|
| Is it warm in the movie room? | 4.62 s | 3.12 s | 2.74 s | 2.67 s |
| Give me a one line movie trivia fact. | 1.87 s | 1.57 s | 1.35 s | 1.36 s |
| Are the movie room lights on? | 2.98 s | 2.96 s | 2.67 s | 2.89 s |

The watchlist questions, asked after the reload: "Add The Matrix to my watchlist." 4.45 s
(`set_watchlist` called, the add written), "Undo that." 2.60 s, "What is on my watchlist?" 2.61 s (the
`watchlist` tool), "Is FROM on my watchlist?" 2.70 s.

## Tool track 3 — voice dispatch to dev-env agents (design needed)

Goal (Tom, 2026-09-22): "dispatch off work to dev-env agents and relay the results".

What exists: dev-env-ops (`upgrade-agent` namespace) consumes a **ConfigMap queue**
`upgrade-work-orders` (`work-order-watch.sh`, 60 s poll; lanes `wo-` quiet, `esc-` pages on spawn,
`rem-` silent), single-flight, budgeted, deduped, with Pushover pages, a daily digest and GitHub issue
reports. `agent-run` in the dev-env pod is human-driven only. **No agent→HA push channel exists.**

Proposed shape (not yet ratified): an HA tool (`script.voice_dispatch_task` or an MCP tool) that files a
`wo-voice-<ts>` order with the spoken request as the work order body and a fixed target repo set; the
existing lane runs it; results come back through a new, capped notify path (dev-env-ops → HA
`notify.mobile_app_*` and/or a queued spoken announcement on the originating satellite). Guardrails to
copy from the existing lanes: claim record, spend envelope separate from the upgrade budget, page cap,
deny-list of verbs (no deploys, no merges from voice), and a confirmation read-back before filing.

Questions for Tom:
- Q-04 Which repos may voice-dispatched work touch, and may it open PRs or only report?
- Q-05 Where should results land — phone push, spoken on the box that asked, or both?
- Q-06 Who may dispatch: any voice in the house, or only requests that name Tom's satellite/PIN?
