# 003 — The image prompt's frame count can exceed the workflow's slot count

**Status:** Proposed
**Size:** Small code change, but needs a provider-interface decision first
**Raised:** 2026-09-21, while making `qwen-image-2.1-2609-25step-edit-3frame` the
default ComfyUI workflow (AppDaemon 1.21.0)

## Problem

`detection_summary_app` picks 2-4 reference frames per run
(`manager.py`, `min_refs = 2` / `max_refs = 4`) and tells the image model how
many it is looking at:

```
- You are provided {input_paths_count} image(s) captured close in time during ONE
  motion detection event.
```

`input_paths_count` is `len(input_paths)` — the number of frames the *app*
selected, not the number the *workflow* can accept. The per-frame `notes` block
is built the same way, from `candidate_idxs`.

The ComfyUI provider uploads only as many frames as the selected workflow has
image slots and reports the rest as `ignored_input_paths`. So on a 4-frame run
against a 3-slot workflow the prompt claims four references and describes four
frames while three arrive. The model is being told about a picture it cannot
see — the same failure class this release fixed at the other end.

This is **pre-existing and got smaller, not larger**, in 1.21.0: the default was
a one-slot workflow, so a 4-frame run used to claim four and send one. It is now
at worst 4 claimed / 3 sent. It is not a regression, which is why it was not
folded into that change.

## Current state

- `appdaemon/apps/detection_summary_app/manager.py` — `candidate_idxs` /
  `input_paths` / `notes` are built before the image provider is constructed,
  so nothing there knows the slot count.
- `appdaemon/apps/detection_summary_app/prompting/image_prompt_builder.py:39,76`
  — `input_paths_count: int = 1` is taken on trust.
- `appdaemon/providers/ai_providers/comfyui/comfyui_image_generation_provider.py:401`
  — `used_paths = in_paths[: workflow.max_images]`, the truncation that creates
  the mismatch. `Workflow.max_images` is the slot count.
- No image provider exposes a slot count publicly. Gemini, OpenAI and Ollama
  image providers take every frame they are given, so the concept only exists
  for ComfyUI today.

## Candidate approaches

1. **Add `max_input_images` to the image-provider interface** (`None` = no
   limit; ComfyUI returns the resolved workflow's `max_images`), then clamp in
   `manager.py` before the prompt is built. Cleanest, and the only option that
   keeps `notes` honest too — but it puts a ComfyUI-shaped concept on the shared
   protocol, so it needs a call on whether that belongs there.
2. **Clamp only the count passed to the prompt builder**, leaving `input_paths`
   and `notes` alone. One line, but the notes still describe a frame the model
   never sees, so it only half-fixes it.
3. **Lower `max_refs` to 3 in the app.** Trivial, needs no interface change, and
   makes the two numbers agree today — but it silently re-breaks the moment a
   workflow with a different slot count becomes the default, which is exactly
   how this drifted in the first place.

## Open questions

- Does the protocol get `max_input_images`, or does `detection_summary_app`
  special-case ComfyUI the way it already does for `upload_namespace`?
- When frames are dropped, drop the lowest-ranked (`candidate_idxs` tail order)
  or re-rank? The tail order is currently "best, then best-animal, best-male,
  best-female", so the tail is not obviously the least useful frame.
- Should a drop log a WARNING? It is a config-shaped condition (the app's
  `max_refs` outgrew the workflow), not a per-run fault, so once at startup may
  be the better place.
