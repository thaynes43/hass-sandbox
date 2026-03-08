# OpenAI Provider

This provider family supplies all three AI capability groups used by AppDaemon:

- `simple_text`
- `multimodal`
- `image`

## Default Models

- `simple_text`: `gpt-5.2`
- `multimodal`: `gpt-5.2`
- `image`: `gpt-image-1.5`

Other models are allowed where validation permits them. The exact allow-list is enforced in [../provider_settings.py](../provider_settings.py).

## Implemented Capabilities

- Text to structured JSON via the simple-text provider
- Image plus text to structured JSON via the multimodal provider
- Text-to-image and image-to-image via the image provider
- Inpainting support is exposed as supported in the image capability metadata

## Notes

- OpenAI is the broadest-featured provider in this package.
- The image provider supports OpenAI-specific options such as size, quality, and output format.
- These providers require an API key.

## Files

- [openai_simple_text_provider.py](./openai_simple_text_provider.py)
- [openai_multimodal_text_provider.py](./openai_multimodal_text_provider.py)
- [openai_image_generation_provider.py](./openai_image_generation_provider.py)
