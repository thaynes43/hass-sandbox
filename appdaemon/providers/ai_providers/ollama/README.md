# Ollama Provider

This provider family supplies local text and vision capabilities for AppDaemon:

- `simple_text`
- `multimodal`

It does not supply image generation in this package.

## Default Models

- `simple_text`: `qwen3.5:9b`
- `multimodal`: `qwen3.5:9b`

An alternate multimodal model is also allowed today:

- `qwen2.5vl:7b`

The allowed model set is enforced in [../provider_settings.py](../provider_settings.py).

## Implemented Capabilities

- Text to structured JSON via `/api/chat`
- Image plus text to structured JSON via `/api/chat`

## One request at a time per endpoint

Both providers take a process-wide slot from `_request_gate.gate_for(base_url)`
before they send, so AppDaemon has at most one request in flight per Ollama
endpoint, across every app and thread. The house endpoint
(`ollama-assist02`) is shared with Home Assistant's Ollama integration (an AI
Task entity), and haynes-ops#3450 measured it as a local voice model. It runs
`qwen3.5` one request at a time, so the gate keeps any other client from
queueing behind a burst of camera calls: such a request waits for at most the
one already running (haynes-ops#3450). HA's Assist pipelines ran on
llama-server, not here, when this was written (2026-10-06).

- Waiters queue in arrival order.
- The wait is bounded by `queue_wait_s` on `OllamaMultimodalConfig` /
  `OllamaSimpleTextConfig`. It defaults to the request's own `timeout_s`, which
  is 300 s unless a bundle sets `multimodal_timeout_s` /
  `simple_text_timeout_s`. A separate `queue_wait_s` cannot be set from bundle
  YAML or an app's `ai_provider_conf` today.
  Past the bound the request raises `OllamaQueueTimeout`, an
  `ExternalDataGenError`, without being sent; callers log a warning and skip it.
- The HTTP `timeout_s` starts only once the slot is held. The time spent
  queued is returned as `_meta.queue_wait_s`.
- The gate registry is module level, one per AppDaemon process. Restart
  AppDaemon rather than editing files under `/conf/apps/providers` in a running
  pod: a fresh import of `_request_gate` starts an empty registry, and two
  requests could be in flight with no log line.

## Limitations

- No text-to-image support
- No image-to-image support
- No inpainting support
- Model availability is external to this package; the Ollama host must already have the required model pulled and ready

## Notes

- The current multimodal path targets local Qwen models served by Ollama.
- The multimodal provider explicitly disables thinking for the current Qwen chat flow because hidden reasoning could consume the token budget without returning final JSON.
- Small nonzero `load_duration` values from Ollama are not treated as true cold starts in logs.

## Files

- [ollama_simple_text_provider.py](./ollama_simple_text_provider.py)
- [ollama_multimodal_text_provider.py](./ollama_multimodal_text_provider.py)
- [ollama_image_generation_provider.py](./ollama_image_generation_provider.py)
- [_request_gate.py](./_request_gate.py)
