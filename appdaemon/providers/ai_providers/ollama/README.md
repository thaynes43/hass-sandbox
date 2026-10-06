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
(`ollama-assist02`) also serves Home Assistant's voice model, and the gate
keeps the camera pipeline to one of its slots so voice always has the other
(haynes-ops#3450).

- Waiters queue in arrival order.
- `queue_wait_s` on `OllamaMultimodalConfig` / `OllamaSimpleTextConfig` bounds the
  wait: a code default of 300 s, not settable from bundle YAML or an app's
  `ai_provider_conf` today.
  Past it the request raises `OllamaQueueTimeout`, an `ExternalDataGenError`,
  without being sent; callers log a warning and skip it.
- The HTTP `timeout_s` starts only once the slot is held. The time spent
  queued is returned as `_meta.queue_wait_s`.

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
