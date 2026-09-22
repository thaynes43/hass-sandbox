# Local assist stack — resident LLM, local STT/TTS, and the tool tracks (cold start)

Written 2026-09-22. Read this before touching the local voice stack or the two tool tracks below.
Companion docs: `.agents/plans/voice-assist-rollout.md` (the OpenAI room agents, phases 1–4),
`.agents/plans/voice-assist-handoff.md` (room-by-room testing), `agent-docs/voice-agent-prompts.md`
(prompt backups incl. Regina's). Cluster side lives in haynes-ops (`kubernetes/main/apps/ai/`).

## What is live (2026-09-22)

| Piece | Where | Facts |
|---|---|---|
| Resident LLM | haynes-ops `ai/llama-server` on `talosw01` | llama.cpp `server-cuda-b11096`, **Muse Glimmer 30B Q4_K_M** (`Muse-Glimmer-30B-KQuant-17GB-Q4_K_M.gguf` on NFS `misc/llama/models/`), alias `muse-glimmer-30b`, `--parallel 2`, `--reasoning-effort low`, KV q8. `http://llama-server.ai.svc.cluster.local:8080/v1`. Also an Open WebUI connection (haynes-ops#3110). |
| GPU layout | `talosw01` 2× RTX 3090 | Tom's ruling 2026-09-22 (haynes-ops#2960): per-app pinning via `NVIDIA_VISIBLE_DEVICES`. **LLM on 3090 #1 (`GPU-d8a856f1…`), ComfyUI on 3090 #0 (`GPU-18bf6eab…`)** — swapped the same day because #0 throttles to 225 MHz within ~10 s of load (haynes-ops#3052; 87 °C, fan 100 %). Until airflow is fixed nothing latency-sensitive goes on #0. ComfyUI's AppDaemon and Open WebUI graphs load the same int8 Qwen-Image-2.1 files, so one resident copy serves both. |
| STT | haynes-ops `ai/whisper` on `talosm01` (A2000) | `ghcr.io/thaynes43/wyoming-whisper-gpu:3.8.1-2`, **NVIDIA Parakeet TDT 0.6B v2** via onnx-asr on CUDA, `whisper.ai.svc.cluster.local:10300`, HA entry `faster-whisper` → `stt.faster_whisper`. Measured in HA: 0.30 s cold / 0.05 s warm vs HA Cloud 1.14 / 0.86 s, same transcript with punctuation. |
| TTS | haynes-ops `ai/kokoro` on `talosm01` | `flight777/kokoro-wyoming-ml` (CUDA), Kokoro-82M, `kokoro.ai.svc.cluster.local:10210`, HA entry `01M34V1S24J5SBNZXXFW00JF1K` → `tts.kokoro`, 54 voices, **no streaming synthesis** (that image reports `supports_synthesize_streaming: False`). 0.28–0.50 s to first audio vs cloud 0.39–0.62 s. Piper is still there as fallback. A2000 total ≈ 8.1 GB of 12 (vexa 3.9 + Parakeet 3.4 + Kokoro 0.6). |
| HA agent | `llama_cpp` integration, entry `01M34TQMBVCKW0BG0KZW5JG5YM` | subentry `01M34TQMBVMNR9CJZX892KD8VJ` → **`conversation.muse_glimmer_30b`**, `llm_hass_api: [assist]`, prompt = Regina persona + the shared spoken-aloud block (backup in `agent-docs/voice-agent-prompts.md`; write back with `ha_config_set_helper(helper_type="config_subentry", …)`). |
| Pipeline | **Regina** `01jb8sg4njw0mh3gnpqt4j9h6x` | `stt.faster_whisper` (en) → `conversation.muse_glimmer_30b` → `tts.kokoro` `af_heart` (en-US), `prefer_local_intents: true`. **No satellite is assigned to it yet** — Tom picks a box to try it on. The four room pipelines are unchanged (OpenAI + HA Cloud). |
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
  ("cigar-journal", 35 tools), attached to Regina as `llm_hass_api: ["assist",
  "mcp-01M34ZKF449AB21P6K1EGW6880"]`. The tool schemas add **~28k tokens** to every turn (15k →
  43.6k), which first overflowed the 32k slot; llama-server now runs `--ctx-size 131072` = 64k per
  slot (haynes-ops#3113, +488 MiB VRAM).

**Results (2026-09-22, `bench.py MODE=pipe PIPELINE=<Regina>`, GPU in thermal throttle — prompt
eval ~300 tok/s, decode 8–9 tok/s):**

| Question | Tool chosen | Result size | Outcome |
|---|---|---|---|
| What cigars do I have in my humidor? | `get_my_inventory{}` ✓ | **~47k tokens** | request grew to 91,674 tokens → context error even at 64k |
| What was the last cigar I smoked? | `get_my_smokes{limit: 10}` ✓ | 5.2k tokens | correct spoken answer (name, date, rating) in 37 s |
| Find me a maduro in the catalog | `browse_catalog{q: "maduro", limit: 48}` ✓ | 18.7k tokens (62 s to ingest) | correct answer ("eighty five matches, examples …") in 91 s |
| Is the front door locked? | `assist…GetLiveContext{name: "Front Door Lock State"}` ✓ | small | correct in 17 s |

Conclusions: the 30B model **picks the right tool with sensible arguments 4/4**, mixing Assist and
MCP tools. What breaks the voice loop is (1) the throttled GPU (#3052 — 3.5× slower prompt eval,
4× slower decode than the same card cold) and (2) **tool result size**: cigar-journal's outputs are
sized for desktop agents, not a spoken turn. Before any room agent gets these tools: cap results in
the prompt ("ask for at most five results; never fetch the whole humidor"), and ask cigar-journal
for a compact/voice output mode (or per-tool `limit` defaults) — the model cannot shrink a 47k-token
result it never sees. Prompt caching does carry the 28k tool preamble across turns (only the first
turn after a restart pays ~30 s cold), but the cache is per slot and there are two slots.
The tools stay attached to Regina for further testing; nothing else uses them.
- Voice safety: the journal server exposes write tools (`save_smoke`, `record_purchase`, …). The test
  agent is Regina (no satellite). Before any room agent gets these tools, either scope the OAuth client
  to `catalog:read journal:read` or keep writes behind a confirmation in the prompt.

## Tool track 2 — Movie Room recommender (design needed)

Goal (Tom, 2026-09-22): tune an agent for the Movie Room that recommends new things to watch for him
and the family (Tom, Kellie, Jackson, Penelope).

What exists (surveyed 2026-09-22): hass-sandbox `appdaemon/providers/media_providers/` has Tautulli
(recently added, popular; `get_history`/`get_users` one wrapper away), TMDB (trending/discover/detail;
similar + watch-providers via `append_to_response`), mdblist ratings (**hard 1 req/s**), SerpAPI
showtimes (quota-bound). `media_dashboard_app` computes household rows only and is `disable: true` in
prod. In haynes-ops `media/`: Plex ×2 (`plex`, `plexops`), Tautulli ×2, **Seerr v3.4.1** (request path,
`/api/v1`), Radarr/Sonarr, Kometa (holds TMDB/mdblist keys). No media MCP server exists; no Trakt.
Movie Room players: `media_player.movie_room` (Sonos Port), the LG TV, the AVR.

Proposed shape (not yet ratified): a small **media MCP server** in haynes-ops (`ai/media-mcp` or under
`media/`), read-mostly, exposing 5–6 tools with tiny schemas: `recently_watched(person)`,
`library_search(query)`, `similar_to(title)`, `trending(kind)`, `ratings(title)`, and one guarded write
`request_title(title)` → Seerr. Ratings/trending pre-computed on a cadence (mdblist limit), so a voice
turn never waits on it. HA attaches it via the `mcp` integration (no auth → URL only) and the Movie
Room agent gets it in `llm_hass_api`. Person = spoken name ("for Penelope"), never inferred.

Questions for Tom (ask one at a time, at the moment each blocks work):
- Q-01 Which Plex/Tautulli pair is the family's (k8plex vs plexops)?
- Q-02 May the assistant *request* titles through Seerr by voice, or recommend only?
- Q-03 Which model drives the Movie Room agent for this — the local resident model or the OpenAI one?

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
