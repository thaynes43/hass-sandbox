# AI Providers

This package defines the AppDaemon-facing provider layer for three capability groups:

- `simple_text`: text input to structured JSON
- `multimodal`: image plus text input to structured JSON
- `image`: prompt plus optional reference image(s) to generated image output

The registry in [registry.py](./registry.py) resolves provider config from `ai_provider_conf`, validates model compatibility, and builds the concrete provider implementation for each capability.

## Capability Shape

The shared provider interfaces are:

- [simple_text_provider.py](./simple_text_provider.py)
- [multimodal_text_provider.py](./multimodal_text_provider.py)
- [image_generation_provider.py](./image_generation_provider.py)

At a high level:

- `simple_text` is used for narrative or text-only structured outputs
- `multimodal` is used for frame scoring and other image-to-JSON tasks
- `image` is used for image editing or generation workflows that write an output image file

## Provider Summary

| Provider | simple_text | multimodal | image |
| --- | --- | --- | --- |
| OpenAI | Yes | Yes | Yes |
| Gemini | Yes | Yes | Yes |
| Ollama | Yes | Yes | No |
| ComfyUI | No | No | Yes |

## Major Limitations

- Ollama does not provide image generation in this package.
- ComfyUI does not provide text-only or image-to-text structured output in this package.
- Capability switching is static per configured provider today. The registry does not dynamically choose different ComfyUI workflows or different image backends at runtime based on the number of input images or whether a request is text-to-image versus image-to-image.

## Model Defaults

Current default models in this package:

- OpenAI:
  - `simple_text`: `gpt-5.2`
  - `multimodal`: `gpt-5.2`
  - `image`: `gpt-image-1.5`
- Gemini:
  - `simple_text`: `gemini-2.5-flash-lite`
  - `multimodal`: `gemini-2.5-flash`
  - `image`: `gemini-2.5-flash-image`
- Ollama:
  - `simple_text`: `qwen3.5:9b`
  - `multimodal`: `qwen3.5:9b`
- ComfyUI:
  - `image`: `qwen-image-edit-2509`

## Per-Provider Docs

- [OpenAI](./openai/README.md)
- [Gemini](./gemini/README.md)
- [Ollama](./ollama/README.md)
- [ComfyUI](./comfyui/README.md)
