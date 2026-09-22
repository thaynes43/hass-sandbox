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
- `capabilities.max_input_images` — the selected workflow's slot count, so a
  caller can trim before it renders (see below)
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
| `qwen-image-2.1-2609-25step-edit-3frame` **(registry default / fallback)** | 2026-09-20 | 2026-09-21 | up to 3 | Same clean, painterly result as the single-frame entry, with the extra frames used to confirm who and what is in the scene. About 16 GB of VRAM; roughly 50 s per image on a cool 3090, 2-3x that when the card is thermally throttled. |
| `qwen-image-2.1-2609-25step-edit-3frame-gpu1` **(what the bundle runs)** | 2026-09-20 | 2026-09-21 | up to 3 | Identical output to the plain three-frame entry. About 55-60 s per image even back-to-back, where the first GPU drops to ~115 s once it heats up. The first render after a ComfyUI restart or after switching cards pays a one-off model load of a few minutes. |
| `qwen-image-2.1-2609-25step-edit` | 2026-09-20 | 2026-09-21 | 1 | Clean, painterly restyles that keep the scene, people and vehicles where they are; natural colour, no burnt shadows. About 16 GB of VRAM and roughly 45 s per image on a cool 3090, 2-3x that when thermally throttled. |
| `qwen-image-edit-2509-lightning4-legacy` | 2025-09-22 | 2026-09-21 | 1 | What production sent until 2026-09: strong stylisation but over-saturated colour and crushed shadows, because cfg 3.5 fights the 4-step LoRA. About 40-50 s per image. |
| `qwen-image-edit-2509-lightning4-tuned` | 2025-09-22 | 2026-09-21 | 1 | Same model at the settings it was made for (cfg 1, 1 MP): clean colour, subject and lighting preserved. About 40-50 s per image. |
| `qwen-image-edit-2509-lightning4-tuned-3frame` | 2025-09-22 | 2026-09-21 | up to 3 | Tuned settings with up to three camera frames as references, for zones where a subject seen in only one frame goes missing. Several times slower per image. |

Two names matter here, and they are deliberately different:

- **`qwen-image-2.1-2609-25step-edit-3frame-gpu1`** is what the
  `comfyui-qwen-edit` bundle pins, so it is what every camera actually renders
  on.
- **`qwen-image-2.1-2609-25step-edit-3frame`** is the registry's
  `default_workflow`, which is also `fallback_workflow_name` for every
  provider. It stays **GPU-agnostic on purpose** — see
  [Why the registry default names no GPU](#why-the-registry-default-names-no-gpu).

`qwen-image-2.1-2609-25step-edit-3frame` runs Qwen-Image-2.1 (7B, one model for
both generate and edit) on the official ComfyUI template's settings, and
**needs ComfyUI >= 0.37.0** for its `TextEncodeQwenImage21` and
`QwenImage21Cache` nodes.

An older server rejects both 2.1 graphs outright, and the provider reports that
as a workflow rejection — but **the registry default cannot fall back**,
because the default is what everything falls back *to*
(`fallback_workflow_name` is the registry default, and a fallback equal to the
requested workflow is not retried). So on a downgraded ComfyUI the bundle's
entry is rejected, its one retry on the default is rejected too, and the render
fails rather than quietly landing on an older graph; the fix is to point the
bundle at a 2509 entry and ship it.

It takes up to three reference frames because that is what the callers have:
every detection_summary camera app picks 2-4 candidate frames, trims them to
this entry's three slots and tells the model how many it is looking at. Unused
slots are pruned, so a single-frame caller renders the same graph the one-slot
entry does — picking a one-slot entry narrows the app to the best frame alone,
which is a real rollback and not just a cost saving. On this 7B model the
extra frames are close to free — about 50 s against 45 s for one — which is why
three frames is the default rather than an opt-in. (On the 20B 2509 model they
cost several times more, so its three-frame entry stayed opt-in.)

Two rollbacks, both one line:

- `qwen-image-2.1-2609-25step-edit` — the same model and settings, one frame
  only. Use this if multi-frame ever turns out to confuse a zone.
- `qwen-image-edit-2509-lightning4-legacy` — the pre-2.1 model, byte-for-byte
  what production sent before 2026-09.

The three 2509 workflows now set `weight_dtype: fp8_e4m3fn` on their own
UNETLoader. They used to get that from the server-wide `--fp8_e4m3fn-unet`
flag, which is being removed because it breaks the int8 checkpoints
Qwen-Image-2.1 uses; pinning it per workflow keeps their output identical once
the flag is gone.

Names follow `<model>-<model release yymm>-<sampling>-<variant>`. Spell the
model out — the name is what someone reads in a diff, long after the context is
gone. A variant that only changes where the graph runs says so in the same
slot: `…-3frame-gpu1` is the three-frame graph pinned to the second card.

### The `gpu1` variant

The ComfyUI host has two RTX 3090s, and ComfyUI puts everything on `gpu:0`.
That card sits in the hotter slot: a cool render takes 43 s, but back-to-back
work throttles it to about 115 s. `gpu:1` throttles far more gently — the same
graph holds 55-60 s render after render, which is why the bundle points there.

`qwen-image-2.1-2609-25step-edit-3frame-gpu1` is the three-frame graph with
three core ComfyUI 0.37.0 device-selection nodes added and four wires moved:

| Node | Class | Places |
|---|---|---|
| `20` | `SelectModelDevice` | the diffusion model (`2` → `5`) |
| `21` | `SelectCLIPDevice` | the text encoder (`3` → `6`) |
| `22` | `SelectVAEDevice` | the VAE (`4` → `6` and `8`) |

Nothing else differs, so the output is identical — only the card changes. The
`device` options this server offers are `default`, `cpu`, `gpu:0` and `gpu:1`
(the VAE node has no `cpu`).

Because the device nodes sit between the *loaders* and their consumers, and the
prunable reference slots are `LoadImage` nodes, frame pruning is untouched: one
or two frames prune exactly as they do on the plain entry.

The first render after a ComfyUI restart, or after moving between cards, still
pays the one-off model load of a few minutes.

#### Why the registry default names no GPU

`build_image_provider` always passes the registry's `default_workflow` as
`fallback_workflow_name`, and a fallback equal to the requested workflow is
never retried. So the GPU pin lives on the **bundle**, not on the registry
default. If `gpu:1` ever leaves the bus, ComfyUI rejects the graph at
`POST /prompt` with HTTP 400 `value_not_in_list` on `device` — exactly the
deterministic rejection the one-shot fallback exists for — and the retry
renders on the GPU-agnostic default instead. Pinning the card on the registry
default would mean both the requested graph and its fallback fail, and the zone
goes dark.

#### Going back to the plain entry

Set the bundle's `workflow` back to `qwen-image-2.1-2609-25step-edit-3frame`
and ship it. That is the whole rollback: the graph files are untouched, and the
render moves back to whichever card ComfyUI chooses (`gpu:0`).

### Set the default for every app

In [`../model_settings/comfyui.yaml`](../model_settings/comfyui.yaml), on the
`comfyui-qwen-edit` bundle:

```yaml
bundles:
  comfyui-qwen-edit:
    image_model: qwen-image-2.1
    provider_options:
      workflow: qwen-image-2.1-2609-25step-edit-3frame-gpu1
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
    image_workflow: qwen-image-2.1-2609-25step-edit
```

`image_workflow` beats the bundle, and only for that app. It is also accepted
inside the capability's own scoped dict, which wins over the top-level key
when both are given:

```yaml
  ai_provider_conf:
    image:
      bundle: comfyui-qwen-edit
      image_workflow: qwen-image-2.1-2609-25step-edit
```

On an app whose image provider is not ComfyUI the key does nothing — there are
no workflows to name — and a WARNING says so rather than dropping it silently.

### Roll back

Set the name back and ship it — to `qwen-image-2.1-2609-25step-edit-3frame` to
keep the same render but let ComfyUI choose the card, to
`qwen-image-2.1-2609-25step-edit` to keep the model but send one frame, or to
`qwen-image-edit-2509-lightning4-legacy` for the pre-2.1 behaviour. One line in
the bundle rolls every camera back; one line in an app rolls back just that
one. Nothing else changes: the other entries' graph files are untouched by a
rollback.

### A bad name stops the app

An unregistered name raises at `initialize()`, naming the workflow, where it
was set (`app_config` or `bundle`), and every registered name:

```
ComfyUI workflow 'qwen-tunedd' is not registered (set via app_config).
Registered workflows: ['qwen-image-edit-2509-lightning4-legacy', ...]
```

A config typo therefore never reaches a render. The provider's constructor
enforces the same rule for both `workflow_name` and `fallback_workflow_name`,
so a caller that builds a `ComfyUIImageGenerationConfig` directly — bypassing
`build_image_provider` — gets the same `ValueError` rather than a per-render
warning and an image quietly rendered on the default:

```
ComfyUI workflow_name 'qwen-tunedd' is not a registered workflow;
registered workflows: ['qwen-image-edit-2509-lightning4-legacy', ...]
```

An empty name still means "unset" in both places: no `workflow_name` resolves
to the registry's `default_workflow`, and no `fallback_workflow_name` means a
rejected workflow is not retried.

The one gap: an app with `external_image_gen_enabled: false` builds no image
provider at all, so its workflow name is not checked until image generation is
switched on.

### What it costs to switch

Measured on the live server, warm, with 1080p input frames on a 3090:

- `qwen-image-2.1-2609-25step-edit-3frame` — about 50 s per image, 16 GB VRAM
- `qwen-image-2.1-2609-25step-edit` — about 45 s per image, 16 GB VRAM
- the 2509 workflows — 40-50 s single frame, several times that for three

All of those are cool-card numbers. A thermally throttled 3090 takes 2-3x as
long, which is where the older "1.5-2.5 min" figure for the 2.1 workflows came
from. On this host that throttling is what `gpu:0` does under back-to-back
work (43 s cool → ~115 s hot), and it is the whole reason
`qwen-image-2.1-2609-25step-edit-3frame-gpu1` exists: the same graph on `gpu:1`
holds 55-60 s render after render.

Frame count is what separates the two models here: on Qwen-Image-2.1 (7B) a
third reference frame costs about 5 s, while on Qwen-Image-Edit-2509 (20B) it
multiplies the render. That is why the 2.1 default sends three frames and the
2509 three-frame entry is opt-in.

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

all three `qwen-image-2.1-2609-25step-edit*` workflows (the `gpu1` variant only
moves where they are loaded, not what)

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
listed `<node>.<input>` that consumed it. Those are the encoder's optional
image inputs; leaving one pointing at a deleted node makes ComfyUI reject the
whole graph. Load-time validation also works the other direction: every input
in the graph that consumes a prunable slot's node must appear in that slot's
`unlink` list, so forgetting one is a startup error rather than a 400 at
render time.

An `unlink` target is split on its **first** dot: everything before it is the
node id, everything after is the input name. The input name may itself contain
dots — `TextEncodeQwenImage21` calls its slots `images.image_2` and
`images.image_3`, so the 2.1 three-frame entry writes
`unlink: ["6.images.image_2"]`. Node ids may contain `:` (the 2509 subgraph
ids, `115:111`) but never `.`.

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

That budget is the budget for the **render**, enforced by the `/history` poll
deadline — it is not a socket timeout. The short calls (upload, `POST /prompt`,
the output download) are capped at 60 s each, and `/history` polls at 30 s, so
a half-open connection fails in a minute instead of stalling for the full
15-20 minutes. The upload and `POST /prompt` run inside the namespace's upload
lock, so a stall there would hold that lock as well as the worker thread; the
output download runs after the lock is released and costs only the thread. A
budget shorter than the cap still wins: the cap is a ceiling, never a floor.

## Uploads and concurrency

Input frames upload as `<upload_namespace>-slot<N><suffix>`, sanitised to
`[A-Za-z0-9._-]`, with `overwrite=true`. That bounds the server's input
directory to (zones × slots) files while removing a real corruption path: every
zone used to upload `best.jpg`, and because `LoadImage` resolves filenames at
*execution* time, two zones firing together could render each other's frame.

Within one namespace the provider also holds a lock from the first upload until
the prompt reaches a terminal state, so two runs for the same zone cannot
interleave. Different namespaces never block each other.

### How many frames go over

`capabilities.max_input_images` is the resolved workflow's slot count — 3 on
the three-frame entries, 1 on the rest. It is **per instance**, not per class,
because it is only knowable once the workflow is; the class attribute leaves it
`None` and carries only the workflow-independent flags. It reports the workflow
that will be *requested*, so a one-shot fallback onto a narrower entry is not
reflected in it.

Callers that describe their references in the prompt — `detection_summary_app`
does, with a count and one note per image — must read it and trim before
calling `edit_image`, or the prompt describes frames the model never receives.

`edit_image` truncates anyway (`in_paths[: workflow.max_images]`) and records
the remainder as `ignored_input_paths`. That is a safety net, not the contract:
it covers a caller that did not trim and a fallback onto a workflow with fewer
slots. On a trimmed run nothing is dropped and `ignored_input_paths` is absent.

## Errors and fallback

`ComfyUIWorkflowRejectedError` (a subclass of `ExternalImageGenError`) means
ComfyUI refused the graph *before running it*, so the same request can never
succeed as sent. Exactly two failures qualify:

- a registered workflow whose graph can no longer be turned into a request —
  its JSON unreadable, or missing a node/input the entry binds
- HTTP 400 from `POST /prompt` — ComfyUI's graph validation. A missing model
  file arrives this way as `node_errors[*].errors[*].type == "value_not_in_list"`;
  the provider surfaces the `input_name` and the `received_value` in the message

An *unregistered* name is not one of these: it is rejected when the provider is
constructed (see [A bad name stops the app](#a-bad-name-stops-the-app)), so it
never reaches a render and never falls back.

Those, and only those, fall back: when the configured workflow differs from
`fallback_workflow_name` (the registry default), the failure logs a WARNING and
retries **exactly once** with the fallback. Result meta then carries
`workflow_name` (what actually produced the image), `workflow_name_requested`,
and `workflow_fallback_reason`.

That difference is live in the shipped config: the bundle asks for
`qwen-image-2.1-2609-25step-edit-3frame-gpu1` while the registry default — and
so the fallback — is the GPU-agnostic `qwen-image-2.1-2609-25step-edit-3frame`.
A `gpu:1` that has left the bus is a `value_not_in_list` 400 on `device`, and
the retry renders on `gpu:0` instead of the zone going dark.

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
| `ignored_input_paths` | Inputs beyond the workflow's slot count; absent when the caller trimmed to `max_input_images` first |
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
- [workflows/qwen_image_2_1_edit_3frame_API.json](./workflows/qwen_image_2_1_edit_3frame_API.json) — `qwen-image-2.1-2609-25step-edit-3frame`
- [workflows/qwen_image_2_1_edit_3frame_gpu1_API.json](./workflows/qwen_image_2_1_edit_3frame_gpu1_API.json) — `qwen-image-2.1-2609-25step-edit-3frame-gpu1`
- [workflows/qwen_image_2_1_edit_API.json](./workflows/qwen_image_2_1_edit_API.json) — `qwen-image-2.1-2609-25step-edit`
- [workflows/02_qwen_Image_edit_subgraphed_API.json](./workflows/02_qwen_Image_edit_subgraphed_API.json) — `…-lightning4-legacy`
- [workflows/qwen_image_edit_2509_single_image_API.json](./workflows/qwen_image_edit_2509_single_image_API.json) — `…-lightning4-tuned`
- [workflows/02_qwen_Image_edit_subgraphed_three_images_API.json](./workflows/02_qwen_Image_edit_subgraphed_three_images_API.json) — `…-lightning4-tuned-3frame`
