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

- Text to structured JSON via `/api/generate`
- Image plus text to structured JSON via `/api/chat`

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
