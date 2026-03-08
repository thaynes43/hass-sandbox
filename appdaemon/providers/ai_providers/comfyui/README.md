# ComfyUI Provider

This provider family supplies local image generation through ComfyUI workflows:

- `image`

It does not supply `simple_text` or `multimodal` structured JSON capabilities in this package.

## Default Model and Workflow

- `image`: `qwen-image-edit-2509`
- Default workflow bundle: `workflows/02_qwen_Image_edit_subgraphed_API.json`

The model bundle is defined in [../model_settings/comfyui.yaml](../model_settings/comfyui.yaml).

## Implemented Capabilities

- Image-to-image generation through the ComfyUI `/upload/image`, `/prompt`, `/history`, and `/view` APIs
- Workflow-driven output generation with polling for completion

## Limitations

- No text-only structured output
- No image-to-text structured output
- No text-to-image support in the current provider configuration
- The current provider implementation is built around a single uploaded reference image, even though the broader AppDaemon image interface accepts a sequence of input image paths
- Workflow selection is static today; the provider does not dynamically switch between different ComfyUI workspaces or workflow templates based on request shape

## Notes

- This provider is intended for local image editing workflows backed by a prepared ComfyUI workspace.
- The current Qwen edit path depends on the ComfyUI workflow node IDs configured in the bundle.
- Future expansion will likely require a richer workflow selection layer rather than a single static workflow mapping.

## TODO

The current provider should be treated as the first working ComfyUI integration, not the final provider architecture.

What we need next is a routing layer that can choose the correct ComfyUI workspace or workflow based on the request shape coming through the shared AppDaemon image-generation API.

Current AppDaemon image API shape:

- `input_image_paths`: zero or more reference images
- `prompt`: the requested generation or edit instruction
- `output_image_path`: destination for the generated image

That means ComfyUI eventually needs to support at least these runtime cases behind the same provider interface:

- text-to-image:
  - when `input_image_paths` is empty
  - route to a text-only workflow or workspace
- single-image edit:
  - when `input_image_paths` has one image
  - route to the current one-image Qwen edit workflow
- multi-image edit:
  - when `input_image_paths` has two or more images
  - route to a workflow that can bind multiple uploaded images into the graph

The important architectural point is that the shared `image` provider interface should not force AppDaemon apps to care about ComfyUI workflow details. The ComfyUI provider should decide how to translate a generic image request into the correct workflow.

Recommended direction:

- Introduce workflow descriptors in provider config rather than a single static `workflow_path`.
- Allow matching rules based on request shape:
  - `min_images`
  - `max_images`
  - `supports_text_to_image`
  - `supports_image_to_image`
  - optional model or workspace labels
- Let the provider select the best workflow descriptor at runtime.
- Support per-workflow upload node mappings instead of a single `load_image_node_id`.
- Keep prompt node, negative prompt node, save node, and sampler overrides per workflow.

The bundled three-image candidate for this future work is:

- [workflows/02_qwen_Image_edit_subgraphed_three_images_API.json](./workflows/02_qwen_Image_edit_subgraphed_three_images_API.json)

That workflow is a good reference for the multi-image path because it expects three image sources in the graph and can be used to design:

- multi-upload handling
- per-node image binding
- fallback behavior when fewer than the maximum number of images are supplied

Open design questions that still need implementation work:

- Should missing extra image slots be duplicated from the first image, left empty, or rejected?
- Should workflow selection stay model-centric, workspace-centric, or become a capability matrix?
- Should text-to-image and image-to-image be separate bundles, or a single provider bundle with multiple workflow variants?
- How should the provider expose unsupported combinations back to callers when no matching workflow exists?

## Files

- [comfyui_image_generation_provider.py](./comfyui_image_generation_provider.py)
- [workflows/02_qwen_Image_edit_subgraphed_API.json](./workflows/02_qwen_Image_edit_subgraphed_API.json)
- [workflows/02_qwen_Image_edit_subgraphed_three_images_API.json](./workflows/02_qwen_Image_edit_subgraphed_three_images_API.json)
