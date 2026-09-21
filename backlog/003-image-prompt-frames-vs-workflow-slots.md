# 003 — The image prompt's frame notes do not match the frames actually sent

**Status:** Proposed
**Size:** Small code change, but needs a provider-interface decision first
**Raised:** 2026-09-21, while making `qwen-image-2.1-2609-25step-edit-3frame` the
default ComfyUI workflow (AppDaemon 1.21.0)

## Problem

Two related mismatches between what the image prompt says about the reference
frames and what the ComfyUI provider actually uploads. Both are **pre-existing**
and both got *smaller*, not larger, in 1.21.0 — which is why neither was folded
into that change.

### (a) The count can be wrong

`detection_summary_app` picks 2-4 reference frames per run
(`manager.py`, `min_refs = 2` / `max_refs = 4`) and tells the image model how
many it is looking at:

```
- You are provided {input_paths_count} image(s) captured close in time during ONE
  motion detection event.
```

`input_paths_count` is `len(input_paths)` — the number of frames the *app*
selected, not the number the *workflow* can accept. The ComfyUI provider uploads
only as many frames as the selected workflow has image slots and reports the rest
as `ignored_input_paths`. So on a 4-frame run against a 3-slot workflow the
prompt claims four references while three arrive.

Before 1.21.0 the default was a one-slot workflow, so a 4-frame run claimed four
and sent one. It is now at worst 4 claimed / 3 sent.

### (b) The frame notes are in a different order from the frames

The per-frame `notes` block is appended to the prompt under *"Frame notes (for
the provided references)"* and labelled with filenames (`frame_003.jpg`) that
the model never receives — the uploads are renamed `<zone>-slot<N><suffix>`.

The two lists are built in **different orders**:

- `input_paths` (`manager.py:1048-1060`) follows `candidate_idxs`, which is
  **rank order**: best frame, then best-animals, best-males, best-females.
- `notes` (`manager.py:1092`) iterates `sorted({best_idx} | set(candidate_idxs))`
  — **frame-index order**, i.e. chronological.

They therefore disagree whenever the best-scoring frame is not the earliest one,
which is the common case. With a one-slot default this was invisible: one frame
went over and the notes were read as loose context. With three references the
model can reasonably map note 1 to `images.image_1` and get the wrong frame, and
a 4-candidate run additionally carries a note for a frame that was never sent.

Fixing (b) alone (ordering the notes to match `input_paths`) is easy but leaves
the count lie from (a); the coherent fix is one change that makes the notes
describe exactly the frames sent, in the order sent — which is blocked on the
same interface decision below.

## Current state

- `appdaemon/apps/detection_summary_app/manager.py` — `candidate_idxs` and
  `input_paths` are built *before* the image provider is constructed, so nothing
  there knows the slot count; `notes` is built after, but from `candidate_idxs`
  in a different (chronological) order.
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
   limit; ComfyUI returns the resolved workflow's `max_images`), truncate
   `candidate_idxs` to it once the provider is built, and derive **both**
   `input_paths` and `notes` from that one ordered list. Fixes (a) and (b)
   together and keeps them from drifting apart again — but it puts a
   ComfyUI-shaped concept on the shared protocol, so it needs a call on whether
   that belongs there.
2. **Clamp only the count passed to the prompt builder**, leaving `input_paths`
   and `notes` alone. One line, but the notes still describe a frame the model
   never sees and still in the wrong order, so it only half-fixes (a) and does
   nothing for (b).
3. **Lower `max_refs` to 3 in the app**, and sort `notes` by `candidate_idxs`
   rank instead of frame index. Trivial, needs no interface change, and makes
   both lists agree today — but the count half silently re-breaks the moment a
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
- Should the notes name the reference slot they describe ("reference 1: …")
  rather than a `frame_NNN.jpg` filename the model never receives? That would
  make the ordering contract explicit in the prompt instead of implied, but it
  assumes every image provider presents references in a stable order.
