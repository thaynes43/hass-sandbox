# Light-count tool benchmark

Use `light_tool_bench.py` to vet Muse's handling of light-state questions before expanding the local voice trial. It calls the existing model server and supplies frozen Home Assistant results. It does not call Home Assistant or change devices.

The benchmark checks exposed light **entities**, including groups, rather than individual bulbs. One record with several aliases still counts once. Off, unknown, unavailable and unexposed records do not count as on. A room question and a whole-house question have different expected sets.

## Run and preserve evidence

Keep native prompts, tool profiles, reconstructed house context and model reports in private storage. Do not commit live inventory or credentials. A profile contains the model name, rendered system messages, available function schemas and relevant request settings. Record whether each part was captured or reconstructed, and identify omitted tools or substituted context.

The public `light_count_cases.json` contains eight scenarios and 12 user turns with synthetic entities. Merge it with a private `--profiles` file containing `phone` and `rumpus` profiles. The file may wrap that mapping in `profiles` and include `provenance` describing capture/reconstruction limits.

Validate the suite without calling the model:

```bash
python3 scripts/voice-bench/light_tool_bench.py validate \
  --suite scripts/voice-bench/light_count_cases.json \
  --profiles /private/native-profiles.json --out /private/expected.json
```

The run is deliberately bounded: at most 12 user turns, two tool rounds per turn, 90 seconds per turn, 512 generated tokens per completion and 600 seconds overall, sequentially. The token cap applies to these benchmark requests; native settings are retained in profile metadata. It executes only the selected profile's native GetLiveContext tool against fixture records: `homeassistant__GetLiveContext` for Phone or `assist__homeassistant__GetLiveContext` for Rumpus. All other tool calls are refused. Declare a short activity covering the shared model service before running it.

```bash
RUN_VOICE_TOOL_BENCH=1 \
BENCH_BASE_URL=http://llama-server.ai.svc.cluster.local:8080/v1 \
python3 scripts/voice-bench/light_tool_bench.py run \
  --suite scripts/voice-bench/light_count_cases.json \
  --profiles /private/native-profiles.json --out /private/model-report.json
```

If backend authentication is required, supply `BENCH_API_KEY` through the environment. Never put a key into the command line, suite or report.

Re-score an existing report without further inference:

```bash
python3 scripts/voice-bench/light_tool_bench.py score \
  --suite scripts/voice-bench/light_count_cases.json \
  --profiles /private/native-profiles.json --report /private/model-report.json \
  --out /private/scored-report.json
```

An empty final reply, incomplete stream, timeout, blocked tool call, unsupported inference or incorrect grounded count is a failure. Semantic cases such as follow-up interpretation, naming the right lights and acknowledging an unknown physical bulb total require human review; nonempty text alone does not pass them. Read all actual answers before drawing a rollout conclusion.

## Accuracy and speed are separate results

Fixture tests can reveal counting, tool-selection and conversation failures. When the native house inventory is replaced with a small fixture, the measured time belongs to that fixture. It is not a native Assist or microphone-to-speaker result. Preserve actual Assist traces separately to assess room routing, cold and cached requests, overlapping turns, transcription and speech playback.

The initial Phone Assist incident on 2026-10-13 returned a correct count for its available records: three of 49 exposed light entities were on. The first answer took 37.89 seconds, with visible text after 31.70 seconds; the repeated question took 7.46 seconds. The first request processed 17,690 prompt tokens. An overlapping new conversation processed another large prompt and slowed the first answer. The state tool itself was effectively instant.

Historical Home Assistant records had 26 light entities on at both count timestamps. That larger set included overlapping groups, member entities and indicators, so neither total establishes a physical bulb count. An answer needs to make the coverage and scope clear.

## Initial results, 2026-10-13

The original native Phone and Rumpus profiles answered all 12 fixture turns, with nine passes and three failures after reviewing every answer. Basic counts, alias handling and room scope were correct. The failures were a repeated state question answered without a fresh lookup, an unhelpful response to `?` after an answer, and an unsupported individual-light total inferred from controls that did not establish bulb counts.

The two local prompts now explicitly require a fresh lookup for each new state question, identify counts as visible lighting controls, acknowledge missing physical bulb data, and interpret a short follow-up in the preceding conversation. The complete prompt backups are in `agent-docs/voice-agent-prompts.md`. Model settings, tools, personas and pipeline routing are preserved.

The original run took 99.99 seconds across 12 turns, with a median of 6.40 seconds. Cold Phone and Rumpus fixture turns took 19.73 and 23.76 seconds. These are direct model results with synthetic state data and a reconstructed native tool profile, not microphone-to-speaker measurements. Preserve both the untouched baseline and separately reviewed reports; a correction must not erase its failures.

One targeted retest used the corrected native prompt prefixes, unchanged API instructions, tools and settings, and eight original turns. Seven passed after answer review: the repeat made a new lookup, `?` clarified the prior answer, counts remained correct, and room-to-house scope changed correctly. The individual-bulb case still failed: Muse confidently returned two individual lights even though fixture controls did not establish physical bulb counts. The prompt correction is therefore a partial improvement, not a reliable safeguard against that inference.

The targeted run took 75.51 seconds, with a median of 5.43 seconds. Its cold Phone count took 21.03 seconds and cold Rumpus bulb question 30.00 seconds. There were no empty replies or protocol failures. A full corrected 12-turn regression and physical Rumpus voice test have not been performed. Keep the local deployment as a trial; remaining design work is recorded in `backlog/005-muse-cold-turn-latency.md` and `backlog/006-grounded-light-counts.md`.

A small passing suite is evidence for the cases tested, not proof that all household voice requests are reliable. Keep the Rumpus proof-out and broader migration decision distinct from this direct-backend benchmark.

# Model A/B through an Assist pipeline

`model_ab.py` compares two conversation agents on Home Assistant's own path: the pipeline's intent stage, with the agent's real prompt, tools and settings. It runs inside the HA pod through `run.sh`. Use it before you change the model of a voice agent.

1. Create two test copies of the room agent with `ha_config_set_helper(helper_type="config_subentry", …)`, one per model, and leave every other field as the room agent has it. Do not test on the room's own agent: family use warms and loads it during the run.
2. Prove the copies match: `run.sh model_ab.py "ACTION=check SUBENTRIES=<room>,<copy A>,<copy B>"` prints each subentry's settings and its prompt's hash, never the prompt or a key. The official create flow guesses `city` again for OpenAI agents with user location on, so it can differ.
3. Run read-only questions through **Bench Local (test agent)**. It has `prefer_local_intents: false`, so every question reaches the model. Pass `DEVICE_ID` of the room's satellite so the agent knows its area.

```bash
scripts/voice-bench/run.sh model_ab.py "ACTION=run PIPELINE=01m3g1wwhs6a5w0egwwp7wvvc6 \
  AGENTS=conversation.<copy_a>,conversation.<copy_b> DEVICE_ID=<satellite device id> \
  STATE_AREA=kitchen STATE_ENTITIES=lock.front_door_lock COLD=1 ROUNDS=1 \
  QUERIES='What is the temperature in here?|Is the front door locked?'" > /private/ab.out
ACTION=summary python3 scripts/voice-bench/model_ab.py < /private/ab.out
```

Before each turn the driver points the pipeline at one agent, alternating which agent goes first. Every turn starts a new conversation. Afterwards the driver points the pipeline back at its previous agent, even when a run fails. It records when the first text arrived, the first tool call and the intent end, all in seconds after the text was sent, plus the tool calls and the answer. Text-to-speech is not run.

Pipeline text cannot carry an apostrophe through `run.sh`, so write "What is" rather than "What's".

The driver stops after any tool call that is not a read tool: `GetLiveContext`, `GetDateTime`, the weather and to-do reads, or OpenAI web search. A question can still make a model act, though, and the stop comes only after that turn. So ask only questions that could never sensibly lead to an action.

**Cold and warm.** `COLD=1`, or `COLD=<agent>,…`, labels an agent's first turn of that run as cold. Keep that agent idle for at least 15 minutes beforehand. OpenAI does not report whether a prefix came from its prompt cache, so "cold" means an agent that was idle that long, not a confirmed cache miss.

`STATE_*` prints the live states before and after a run, so you can check the answers. Read every answer: the summary counts speed and failures, not correctness.

Delete both test copies afterwards with `ha_remove_helpers_integrations(helper_type="config_subentry", …)`, and confirm that the pipeline points at `conversation.home_assistant` again.

## gpt-6.1-terra against gpt-5.6-terra, 2026-10-28

At the time of this run, the Kitchen, Bedroom, Movie Room and Rumpus Room ChatGPT agents ran `gpt-5.6-terra`, as did the Phone Assist OpenAI fallback. They all used `reasoning_effort: none`, `verbosity: low`, `service_tier: priority` and web search. Home Assistant's OpenAI integration offers `gpt-6.1-terra` only `low`, `medium`, `high`, `xhigh` or `max`, so the candidate ran `low`. A swap could not keep `none` either.

The test used two copies of Kitchen ChatGPT that differed only in the model and the reasoning effort. Both had the Kitchen prompt's hash and the Assist API, and both ran as the kitchen satellite through Bench Local. Each copy answered six read-only questions: temperature, lights, front door, tomorrow's weather, a pep talk and the last Celtics game. Turns alternated between the copies. The run had 56 turns, 28 per model: 3 cold turns, each after at least 15 idle minutes, and 25 warm. The times measure the intent stage of the text path, from submitting the text to the end of the answer. They exclude STT and TTS.

| Seconds to end of answer | gpt-5.6-terra (none) | gpt-6.1-terra (low) |
|---|---|---|
| Warm, all questions: median / p90 / max (n=25) | 2.79 / 4.15 / 4.81 | 3.49 / 7.01 / 19.31 |
| Warm, lights and door, one lookup each: median (n=10) | 2.67 | 3.14 |
| Cold, temperature question: median (n=3) | 3.87 | 5.31 |

- Neither model had errors, empty answers or actuating tool calls.
- The first tool call came at a median of about 1.3 s for both models. 6.1 then took longer to answer. It also made more lookups: 2 rather than 1 on the temperature question, and it chose web search for the weather.
- The 19.3 s maximum was a 6.1 weather turn that ran two web searches.

What the answers got right:

- **Lights and front door:** both models were right every time.
- **Temperature in here:** the kitchen has no exposed temperature sensor. 5.6 said so every time, and twice gave the outside temperature instead. 6.1 looked again every time and gave the First Floor Ecobee reading (70.3, live 70.2).
- **Weather tomorrow:** the HA forecast said sunny, 67/42. 5.6 twice gave about forty-four degrees, which was today's temperature, not tomorrow's. 6.1 always used web search for the copy's re-guessed city (Hudson NH) and said about seventy.
- **Last Celtics game:** 5.6 said it could not get the score on 3 of 4 warm turns, each time within about 2.2 s. 6.1 answered on all 4. When either answered, it gave the same score: a win over the Nets, 125-124. A separate web search could not confirm that score.
- **Dashes:** 4 of 5.6's 28 answers contained a dash, which the prompt's no-symbols rule forbids. None of 6.1's did.

Limitations: this was one evening, one room's persona, text only, with small samples (3 cold and 25 warm turns per model). OpenAI does not say whether a turn hit its prompt cache. The flow guessed a different web-search city for each copy.
