# ComfyUI Provider

Local image generation through ComfyUI workflows. This package supplies the
`image` capability only — no `simple_text`, no `multimodal`.

The graph that goes over the wire is chosen **by name** from the workflow
registry, in AppDaemon config. Changing which workflow a camera renders with is
a config change shipped as a normal AppDaemon release — there is no runtime
switch and nothing to click.

## Implemented capabilities

- Image-to-image generation through the ComfyUI `/upload/image`, `/prompt`,
  `/history` and `/view` APIs, polling to completion
- Multi-image workflows: every reference frame that has a slot is uploaded and
  bound; unused slots are pruned from the graph
- One-shot fallback to the registry default when a workflow is rejected

## Limitations

- No text-only or image-to-text structured output
- No text-to-image: every workflow expects at least one reference image
- The caller cannot pass ComfyUI parameters directly — anything tunable is a
  registry `override`, which keeps the graph validated at load time

## Choosing a workflow

The registry lives in [`workflow_registry.yaml`](./workflow_registry.yaml).
Every entry carries the metadata needed to tell it from its neighbours months
later: when the *model* was released, when the entry was added, and what to
expect from it.

| Workflow | Model released | Added | Frames | Expect |
|---|---|---|---|---|
| `qwen-image-2.1-2609-25step-edit` **(default)** | 2026-09-20 | 2026-09-21 | 1 | Clean, painterly restyles that keep the scene, people and vehicles where they are; natural colour, no burnt shadows. About 16 GB of VRAM and roughly 1.5-2.5 min per image on a 3090. |
| `qwen-image-edit-2509-lightning4-legacy` | 2025-09-22 | 2026-09-21 | 1 | What production sent until 2026-09: strong stylisation but over-saturated colour and crushed shadows, because cfg 3.5 fights the 4-step LoRA. About 40-50 s per image. |
| `qwen-image-edit-2509-lightning4-tuned` | 2025-09-22 | 2026-09-21 | 1 | Same model at the settings it was made for (cfg 1, 1 MP): clean colour, subject and lighting preserved. About 40-50 s per image. |
| `qwen-image-edit-2509-lightning4-tuned-3frame` | 2025-09-22 | 2026-09-21 | up to 3 | Tuned settings with up to three camera frames as references, for zones where a subject seen in only one frame goes missing. Several times slower per image. |

`qwen-image-2.1-2609-25step-edit` is the registry's `default_workflow`. It runs
Qwen-Image-2.1 (7B, one model for both generate and edit) on the official
ComfyUI template's settings, and **needs ComfyUI >= 0.37.0** for its
`TextEncodeQwenImage21` and `QwenImage21Cache` nodes — an older server rejects
the graph outright, which the provider reports as a workflow rejection and
falls back from.

It is slower than the 4-step 2509 workflows (minutes, not seconds) and wants
more VRAM. Rolling a camera — or everything — back to
`qwen-image-edit-2509-lightning4-legacy` is a one-line config change; that
entry's graph file is still kept as production had it.

The three 2509 workflows now set `weight_dtype: fp8_e4m3fn` on their own
UNETLoader. They used to get that from the server-wide `--fp8_e4m3fn-unet`
flag, which is being removed because it breaks the int8 checkpoints
Qwen-Image-2.1 uses; pinning it per workflow keeps their output identical once
the flag is gone.

Names follow `<model>-<model release yymm>-<sampling>-<variant>`. Spell the
model out — the name is what someone reads in a diff, long after the context is
gone.

### Set the default for every app

In [`../model_settings/comfyui.yaml`](../model_settings/comfyui.yaml), on the
`comfyui-qwen-edit` bundle:

```yaml
bundles:
  comfyui-qwen-edit:
    image_model: qwen-image-2.1
    provider_options:
      workflow: qwen-image-2.1-2609-25step-edit
```

`image_model` is the label reported in result meta; keep it in step with the
workflow's own `model`. The bundle is still called `comfyui-qwen-edit` because
every app references it by that name.

Omit `workflow` entirely to follow the registry's own `default_workflow`.

### Override it for one app

In `apps-prod.yaml` / `apps-dev.yaml`, alongside the capability refs:

```yaml
detection_summary_back_yard_pets:
  ai_provider_conf:
    simple_text: openai-default
    multimodal: openai-default
    image: comfyui-qwen-edit
    image_workflow: qwen-image-edit-2509-lightning4-tuned-3frame
```

`image_workflow` beats the bundle, and only for that app. It is also accepted
inside the capability's own scoped dict, which wins over the top-level key
when both are given:

```yaml
  ai_provider_conf:
    image:
      bundle: comfyui-qwen-edit
      image_workflow: qwen-image-edit-2509-lightning4-tuned-3frame
```

On an app whose image provider is not ComfyUI the key does nothing — there are
no workflows to name — and a WARNING says so rather than dropping it silently.

### Roll back

Set the name back — to `qwen-image-edit-2509-lightning4-legacy` for the
pre-2.1 behaviour — and ship it. One line in the bundle rolls every camera
back; one line in an app rolls back just that one. Nothing else changes: the
other entries' graph files are untouched by a rollback.

### A bad name stops the app

An unregistered name raises at `initialize()`, naming the workflow, where it
was set (`app_config` or `bundle`), and every registered name:

```
ComfyUI workflow 'qwen-tunedd' is not registered (set via app_config).
Registered workflows: ['qwen-image-edit-2509-lightning4-legacy', ...]
```

A config typo therefore never reaches a render. The one gap: an app with
`external_image_gen_enabled: false` builds no image provider at all, so its
workflow name is not checked until image generation is switched on.

### What it costs to switch

Measured on the live server, warm, with a 1080p input frame on a 3090:

- `qwen-image-2.1-2609-25step-edit` — 1.5-2.5 min per image, about 16 GB VRAM
- the 2509 workflows — 40-50 s single frame, several times that for three

ComfyUI serves one queue, so a slow workflow starves every other camera —
worth weighing before pointing every zone at a slow one.

Moving between the three 2509 workflows costs nothing extra: they load the same
diffusion model and the same LoRA. Moving between 2.1 and 2509 loads a
different model, so ComfyUI re-patches and reloads — about 5 minutes on the
first render after the change. One-off, not a per-render tax.

### Required model files

Each entry's `required_models` is derived from its graph's loader nodes
(`unet_name`, `clip_name`, `vae_name`, `lora_name`, `ckpt_name`) and reported
in result meta. On the ComfyUI server:

`qwen-image-2.1-2609-25step-edit`

- `qwen_image_2.1_int8_convrot.safetensors` (UNet)
- `qwen3vl_8b_int8_convrot.safetensors` (CLIP)
- `qwen_image_2.1_vae_bf16.safetensors` (VAE)

the three `qwen-image-edit-2509-*` workflows

- `qwen_image_edit_2509_fp8_e4m3fn.safetensors` (UNet)
- `qwen_2.5_vl_7b_fp8_scaled.safetensors` (CLIP)
- `qwen_image_vae.safetensors` (VAE)
- `Qwen-Image-Edit-2509-Lightning-4steps-V1.0-bf16.safetensors` (LoRA)

## Adding a workflow

1. Put the API-format graph JSON in [`workflows/`](./workflows/). Export it
   from ComfyUI with *Workflow → Export (API)*. Replace any personal input
   filename with the neutral placeholder `input.png`.
2. Add an entry to [`workflow_registry.yaml`](./workflow_registry.yaml):

   ```yaml
   qwen-image-edit-2509-lightning4-myvariant:
     model: qwen-image-edit-2509       # label reported in result meta["model"]
     model_released: 2025-09-22        # when the MODEL WEIGHTS shipped
     added: 2026-10-01                 # when this entry was added
     description: "What the graph is."
     expect: "What the output looks like, and roughly how long it takes."
     graph: workflows/my_graph_API.json
     timeout_s: 900
     bindings:
       prompt: {node: "115:111", input: prompt}
       negative_prompt: {node: "115:110", input: prompt}   # optional; set to ""
       seed: {node: "115:3", input: seed}                  # optional; per-run
       output: {node: "60"}                                # SaveImage node
       images:                                             # ordered; slot 0 required
         - {node: "78", input: image}
         - node: "120"
           input: image
           unlink: ["115:111.image2", "115:110.image2"]
     overrides:
       "115:3": {cfg: 1, steps: 4}
   ```

3. Run the tests. Validation is strict and happens at load: the dates must
   parse as `YYYY-MM-DD`, every node id and input name must exist in the graph,
   every `unlink` target must currently link to that slot's node, and
   `default_workflow` must be registered. A typo is a startup error, never a
   silent no-op against a live ComfyUI.
4. Point a bundle or an app at the new name and ship it.

`unlink` is what makes a multi-slot graph safe with fewer images: when slot N
goes unused, the provider deletes that slot's `LoadImage` node **and** each
listed `<node>.<input>` that consumed it. Those are optional inputs on
`TextEncodeQwenImageEditPlus`; leaving one pointing at a deleted node makes
ComfyUI reject the whole graph.

## Bundle options

Set under `provider_options` in [`comfyui.yaml`](../model_settings/comfyui.yaml).

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `workflow` | `str \| null` | `null` | Workflow name for every app on this bundle. `null` follows the registry's `default_workflow`. An unregistered name fails at app startup. Overridden per app by `ai_provider_conf.image_workflow`, or by `image_workflow` nested inside the scoped `image:` dict — the nested one wins if both are set. |
| `upload_namespace` | `str \| null` | `null` | Prefix for uploaded input filenames. The app sets this per camera zone; `null` means `comfyui`. |
| `min_input_pixels` | `int \| null` | `null` | Log a warning when the first input image's width × height is below this. Does not block generation. |
| `poll_interval_s` | `float` | `1.0` | Seconds between `/history` polls. |

Timeouts belong to the workflow. The bundle deliberately sets no
`image_timeout_s` — the old 300 s default could never survive a cold start,
where the first render after a ComfyUI restart takes ~10.5 minutes loading the
model from NFS. Setting `image_timeout_s` on the bundle overrides *every*
workflow, so do it only to deliberately shorten one bundle.

## Uploads and concurrency

Input frames upload as `<upload_namespace>-slot<N><suffix>`, sanitised to
`[A-Za-z0-9._-]`, with `overwrite=true`. That bounds the server's input
directory to (zones × slots) files while removing a real corruption path: every
zone used to upload `best.jpg`, and because `LoadImage` resolves filenames at
*execution* time, two zones firing together could render each other's frame.

Within one namespace the provider also holds a lock from the first upload until
the prompt reaches a terminal state, so two runs for the same zone cannot
interleave. Different namespaces never block each other.

## Errors and fallback

`ComfyUIWorkflowRejectedError` (a subclass of `ExternalImageGenError`) means
ComfyUI refused the graph *before running it*, so the same request can never
succeed as sent. Exactly two failures qualify:

- an unknown or invalid workflow name
- HTTP 400 from `POST /prompt` — ComfyUI's graph validation. A missing model
  file arrives this way as `node_errors[*].errors[*].type == "value_not_in_list"`;
  the provider surfaces the `input_name` and the `received_value` in the message

Those, and only those, fall back: when the configured workflow differs from
`fallback_workflow_name` (the registry default), the failure logs a WARNING and
retries **exactly once** with the fallback. Result meta then carries
`workflow_name` (what actually produced the image), `workflow_name_requested`,
and `workflow_fallback_reason`.

Everything else raises a plain `ExternalImageGenError` and does **not** fall
back:

- **timeouts and connection errors** — a slow or unreachable server says
  nothing about the graph, and a fallback render would only burn another ten
  minutes
- **`execution_error` in `/history`** — the graph validated and started, so the
  failure is a runtime one (a CUDA OOM, most likely). Retrying on a different
  workflow would quietly render a zone that was deliberately rolled back to an
  older workflow on the current default instead
- **a missing SaveImage output, or a failed download** — transport faults

## Result meta

`edit_image()` returns the usual provider meta (`elapsed_s`, `model`,
`output_path`, `prompt_id`, `input_paths`, `request`, `response`,
`input_dimensions`) plus:

| Key | Meaning |
|---|---|
| `workflow_name` | The workflow that produced the image |
| `workflow_name_requested` | The workflow the config asked for |
| `workflow_source` | Where that name came from: `app_config`, `bundle`, `registry_default` |
| `workflow_fallback_reason` | Present only when it fell back |
| `workflow_graph` | The graph JSON path that workflow used |
| `workflow_model_released` | When those model weights were released |
| `required_models` | Model files that graph loads |
| `uploaded_input_names` | Every uploaded input, slot order |
| `uploaded_input_name` | The first upload (kept for older readers) |
| `ignored_input_paths` | Inputs beyond the workflow's slot count |
| `timeout_s` | The budget actually applied |

`model` is the producing workflow's `model` label, so a bundle's recorded model
always matches the graph that rendered it.

## ComfyUIStatusClient

`comfyui_status_client.py` is a lightweight read-only client, deliberately
separate from the image-generation provider:

- `await fetch_queue_remaining() -> int` — polls `GET /prompt` and returns
  `exec_info.queue_remaining` (queued + in-flight jobs)
- All failures (unreachable, non-200, malformed payload) raise
  `ComfyUIStatusError`, so callers can map unreachability to a health status
  instead of crashing
- ComfyUI's queue is in-memory: a restart resets the counter to 0

Used by `apps/health_checks/checker_apps/imagegen_health_checker` to detect a
dead or wedged ComfyUI instance (page-only watchdog — see its README).

## Files

- [comfyui_image_generation_provider.py](./comfyui_image_generation_provider.py) — transport + graph binding
- [workflow_registry.py](./workflow_registry.py) — the registry, with strict load-time validation
- [workflow_registry.yaml](./workflow_registry.yaml) — the registry itself
- [comfyui_status_client.py](./comfyui_status_client.py)
- [workflows/qwen_image_2_1_edit_API.json](./workflows/qwen_image_2_1_edit_API.json) — `qwen-image-2.1-2609-25step-edit`
- [workflows/02_qwen_Image_edit_subgraphed_API.json](./workflows/02_qwen_Image_edit_subgraphed_API.json) — `…-lightning4-legacy`
- [workflows/qwen_image_edit_2509_single_image_API.json](./workflows/qwen_image_edit_2509_single_image_API.json) — `…-lightning4-tuned`
- [workflows/02_qwen_Image_edit_subgraphed_three_images_API.json](./workflows/02_qwen_Image_edit_subgraphed_three_images_API.json) — `…-lightning4-tuned-3frame`
