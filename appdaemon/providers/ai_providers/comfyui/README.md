# ComfyUI Provider

Local image generation through ComfyUI workflows. This package supplies the
`image` capability only — no `simple_text`, no `multimodal`.

The graph that goes over the wire is chosen at **runtime** by *workflow
profile*, selected from Home Assistant. Switching the workflow, trialling a new
one on a single camera, promoting it, and rolling back are all operator actions
in the HA UI — no ComfyUI change, no AppDaemon release, no image rebuild.

## Implemented capabilities

- Image-to-image generation through the ComfyUI `/upload/image`, `/prompt`,
  `/history` and `/view` APIs, polling to completion
- Multi-image workflows: every reference frame that has a slot in the profile is
  uploaded and bound; unused slots are pruned from the graph
- Runtime workflow selection, per-zone trial, and one-shot fallback when a
  workflow is rejected

## Limitations

- No text-only or image-to-text structured output
- No text-to-image: every profile expects at least one reference image
- The caller cannot pass ComfyUI parameters directly — anything tunable is a
  profile `override`, which keeps the graph validated at load time

## Shipped workflow profiles

Declared in [`workflow_profiles.yaml`](./workflow_profiles.yaml). Timings are
measured on the live server with a warm model and a 1080p input frame.

| Profile | Frames | Steps / cfg | Scale | Timeout | Typical render | Use it for |
|---|---|---|---|---|---|---|
| `qwen2509-original` | 1 | 4 / 3.5 | 1.5 MP | 900 s | ~50 s | The rollback target — byte-for-byte what production sent before profiles existed |
| `qwen2509-tuned` | 1 | 4 / 1 | 1.0 MP | 900 s | ~50 s | Everyday use: same speed, visibly cleaner (cfg 3.5 comes out burnt and over-saturated) |
| `qwen2509-tuned-multiframe` | up to 3 | 4 / 1 | 1.0 MP | 1200 s | several times slower (≤ ~4 min) | Zones where a subject that appears in only one frame keeps getting dropped or invented |

`qwen2509-original` is the `default_profile`: a fresh deploy renders exactly
what the previous release did until someone changes the HA select.

Extra reference frames are expensive — a three-frame render costs roughly 4x a
single-frame one — so multi-frame is opt-in rather than the norm. A profile that
has fewer slots than the caller supplied frames uses the first N in order and
records the rest in result meta as `ignored_input_paths`.

All three run the distilled 4-step Lightning LoRA on purpose. There is no
full-step profile: a 20-step cfg-4 single-frame render was still going at 16
minutes on the current server, and because ComfyUI serves one queue that would
starve every other camera and trip the `ImageGenQueueStuck` page. Revisit it
after the ComfyUI upgrade.

### Required model files

Each profile's `required_models` is derived from its workflow's loader nodes
(`unet_name`, `clip_name`, `vae_name`, `lora_name`, `ckpt_name`) and reported in
result meta. All profiles need the base Qwen edit set on the ComfyUI server:

- `qwen_image_edit_2509_fp8_e4m3fn.safetensors` (UNet)
- `qwen_2.5_vl_7b_fp8_scaled.safetensors` (CLIP)
- `qwen_image_vae.safetensors` (VAE)

All three additionally need
`Qwen-Image-Edit-2509-Lightning-4steps-V1.0-bf16.safetensors`, which is what
makes switching between them free — see the caution below.

## Switching workflows

Three Home Assistant helpers, all self-provisioned by `detection_summary_app` on
startup — never create them by hand:

| Entity | Type | What it does |
|---|---|---|
| `input_select.comfyui_active_workflow` | global | The profile every camera zone uses |
| `input_select.comfyui_trial_workflow` | global | The profile a zone uses while its trial toggle is on |
| `input_boolean.<zone>_detection_summary_trial_workflow` | per zone | Opts that zone into the trial select |

Both selects list the registered profiles in YAML order and start on the
default. Options are reconciled against the registry on every AppDaemon start,
so a release that adds or retires a profile updates the dropdowns by itself; a
selection that no longer exists is reset to the default.

**Trial a profile on one camera**

1. Set `input_select.comfyui_trial_workflow` to the profile you want to try.
2. Turn on `input_boolean.<zone>_detection_summary_trial_workflow` for one zone
   (`garage`, say).
3. Trigger that camera and compare. Every other zone is untouched — it is still
   on the Active profile.

**Promote it**

4. Set `input_select.comfyui_active_workflow` to that profile.
5. Turn the zone's trial toggle back off, so it follows Active with everyone else.

**Roll back**

6. Set `input_select.comfyui_active_workflow` back to the previous profile
   (`qwen2509-original` restores the pre-profiles behaviour exactly).

Both changes take effect on the next detection — nothing restarts, nothing
redeploys. The profile that produced each image is recorded in the run bundle
(`generated_image.workflow_profile`) alongside where the name came from
(`workflow_profile_source`: `ha_active`, `ha_trial` or `yaml_default`), and on
the `image gen start` INFO log line.

### What a switch costs

- Single-frame profiles render in **~40-50 s** on the live server.
- The three-frame profile is several times slower. One measurement came in at
  ~4 min, taken while the GPU may have been thermally throttled, so treat that
  as an upper bound and time it yourself before promoting it.

Switching between the three shipped profiles is free: they load the same
diffusion model and the same LoRA, so ComfyUI keeps what it already has. A
profile that names a **different model file** makes ComfyUI load it on first
use — minutes when the file is cold on the NAS. While a trial of such a profile
runs on one zone and the rest stay on Active, ComfyUI swaps models every time
the two alternate, so expect slower renders for the length of the trial.

ComfyUI has one queue. A slow profile delays every other camera's render, so
time a candidate on the live server before making it Active.

## Adding a profile

1. Put the API-format workflow JSON in [`workflows/`](./workflows/). Export it
   from ComfyUI with *Workflow → Export (API)*. Replace any personal input
   filename with the neutral placeholder `input.png`.
2. Add an entry to [`workflow_profiles.yaml`](./workflow_profiles.yaml):

   ```yaml
   my-profile:
     description: "One line — it shows up in docs and logs."
     model: qwen-image-edit-2509   # the label reported in result meta["model"]
     workflow: workflows/my_workflow_API.json
     timeout_s: 900
     bindings:
       prompt: {node: "115:111", input: prompt}
       negative_prompt: {node: "115:110", input: prompt}   # optional; set to ""
       seed: {node: "115:3", input: seed}                  # optional; per-run seed
       output: {node: "60"}                                # SaveImage node
       images:                                             # ordered; slot 0 required
         - {node: "78", input: image}
         - node: "120"
           input: image
           unlink: ["115:111.image2", "115:110.image2"]
     overrides:
       "115:3": {cfg: 1, steps: 4}
   ```

3. Run the tests. Validation is strict and happens at load: every node id and
   input name must exist in the workflow, every `unlink` target must currently
   link to that slot's node, and `default_profile` must be registered. A typo is
   a startup `ValueError`, never a silent no-op against a live ComfyUI.
4. Deploy. The new name appears in both selects on the next AppDaemon start.

`unlink` is what makes a multi-slot workflow safe with fewer images: when slot N
goes unused, the provider deletes that slot's `LoadImage` node **and** each
listed `<node>.<input>` that consumed it. Those are optional inputs on
`TextEncodeQwenImageEditPlus`; leaving one pointing at a deleted node makes
ComfyUI reject the whole graph.

## Bundle options

Set under `provider_options` in [`comfyui.yaml`](../model_settings/comfyui.yaml).

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `workflow_profile` | `str \| null` | `null` | Pin this bundle to one profile. `null` follows the registry's `default_profile`. An unregistered name fails the build. |
| `upload_namespace` | `str \| null` | `null` | Prefix for uploaded input filenames. The app sets this per camera zone; `null` means `comfyui`. |
| `min_input_pixels` | `int \| null` | `null` | Log a warning when the first input image's width × height is below this. Does not block generation. |
| `poll_interval_s` | `float` | `1.0` | Seconds between `/history` polls. |

Timeouts belong to the profile. The bundle deliberately sets no
`image_timeout_s` — the old 300 s default could never survive a cold start,
where the first render after a ComfyUI restart takes ~10.5 minutes loading the
model from NFS. Setting `image_timeout_s` on the bundle overrides *every*
profile, so do it only to deliberately shorten one bundle.

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

`ComfyUIWorkflowRejectedError` (a subclass of `ExternalImageGenError`) means the
*workflow* was refused and would be refused again as sent:

- an unknown or invalid profile name
- HTTP 400 from `POST /prompt` — ComfyUI's graph validation. A missing model
  file arrives this way as `node_errors[*].errors[*].type == "value_not_in_list"`;
  the provider surfaces the `input_name` and the `received_value` in the message
- `execution_error` reported in `/history`

When the requested profile differs from `fallback_workflow_profile` (the
registry default), one such failure logs a WARNING and retries **exactly once**
with the fallback. Result meta then carries `workflow_profile` (the profile that
produced the image), `workflow_profile_requested`, and
`workflow_profile_fallback_reason`.

Timeouts and connection errors are deliberately **not** in this class. A slow or
unreachable server says nothing about the workflow, and a fallback render would
only burn another ten minutes.

## Result meta

`edit_image()` returns the usual provider meta (`elapsed_s`, `model`,
`output_path`, `prompt_id`, `input_paths`, `request`, `response`,
`input_dimensions`) plus:

| Key | Meaning |
|---|---|
| `workflow_profile` | The profile that produced the image |
| `workflow_profile_requested` | The profile that was asked for |
| `workflow_profile_fallback_reason` | Present only when it fell back |
| `workflow` | The workflow JSON path that profile used |
| `required_models` | Model files that graph loads |
| `uploaded_input_names` | Every uploaded input, slot order |
| `uploaded_input_name` | The first upload (kept for older readers) |
| `ignored_input_paths` | Inputs beyond the profile's slot count |
| `timeout_s` | The budget actually applied |

`model` is the producing profile's `model` label, so a bundle's recorded model
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

- [comfyui_image_generation_provider.py](./comfyui_image_generation_provider.py) — transport + profile binding
- [workflow_profiles.py](./workflow_profiles.py) — profile registry, strict load-time validation
- [workflow_profiles.yaml](./workflow_profiles.yaml) — the registry itself
- [workflow_profile_selector.py](./workflow_profile_selector.py) — HA helper provisioning + runtime selection (app layer only; the provider never reads HA)
- [comfyui_status_client.py](./comfyui_status_client.py)
- [workflows/02_qwen_Image_edit_subgraphed_API.json](./workflows/02_qwen_Image_edit_subgraphed_API.json) — `qwen2509-original`
- [workflows/qwen_image_edit_2509_single_image_API.json](./workflows/qwen_image_edit_2509_single_image_API.json) — `qwen2509-tuned`
- [workflows/02_qwen_Image_edit_subgraphed_three_images_API.json](./workflows/02_qwen_Image_edit_subgraphed_three_images_API.json) — `qwen2509-tuned-multiframe`
