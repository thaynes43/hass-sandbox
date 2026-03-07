# Gemini Provider

This provider family supplies all three AI capability groups used by AppDaemon:

- `simple_text`
- `multimodal`
- `image`

## Default Models

- `simple_text`: `gemini-2.5-flash-lite`
- `multimodal`: `gemini-2.5-flash`
- `image`: `gemini-2.5-flash-image`

The broader supported model set is enforced in [../provider_settings.py](../provider_settings.py).

## Implemented Capabilities

- Text to structured JSON via the simple-text provider
- Image plus text to structured JSON via the multimodal provider
- Text-to-image and image-to-image via the image provider

## Notes

- Gemini is a full-capability provider in this package, similar to OpenAI.
- The multimodal provider uses Gemini structured output support to return JSON.
- The image provider is a hosted API integration, not a local workflow engine.
- These providers require an API key.

## Files

- [gemini_simple_text_provider.py](./gemini_simple_text_provider.py)
- [gemini_multimodal_text_provider.py](./gemini_multimodal_text_provider.py)
- [gemini_image_generation_provider.py](./gemini_image_generation_provider.py)
