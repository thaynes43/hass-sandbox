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
