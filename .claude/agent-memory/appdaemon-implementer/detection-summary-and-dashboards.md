---
name: detection-summary-and-dashboards
description: detection_summary_app profile/entrance conventions, the 5-card detection-summary Lovelace view shape, and the HA MCP dashboard-editing gotchas (config_hash, python_transform)
metadata:
  type: reference
---

# Detection summary + HA dashboard editing

## `profiles.py` pattern

- Add built-in profiles as module-level constants after `PROFILE_VEHICLES`, before `BUILTIN_PROFILES`; register them in the `BUILTIN_PROFILES` dict.
- `PROFILE_ANIMALS`: animals `required_for_publish=True`, people `required_for_publish=False`, `DEFAULT_SCORE_FIELDS` only (no extras = 8 fields).
- `PROFILE_PACKAGES`: adds a `package_count` `ScoreFieldSpec`; all 3 categories `required_for_publish=True`.
- `PROFILE_VEHICLES`: adds `vehicle_count` + `vehicle_type` `ScoreFieldSpec`.

## Image prompt policy (`prompting/image_prompt_builder.py`)

The reference frames show the SAME people seconds apart, and a multi-image
edit model combines what each image shows, so the generated image repeated the
same person once per frame (fixed in 1.22.2). Keep the prompt saying one thing:
draw Image 1's moment, each subject once.
- `manager.py` sends only the best frame plus, per profile category, a frame
  showing MORE of that category than the best frame (by category total).
  Never re-add a min_refs-style "second best frame": in a one-person run that
  is the same person again.
- Never ask for a "composite" of the frames. Never put a later frame's own
  summary in the prompt (it places the same person somewhere else). Never put
  the run narrative in the prompt (a sequence of actions is drawn as that many
  people).
- Counts come from per-frame category totals (`consensus_<cat>_total`), so a
  person scored as a man, then a woman, stays one person.
- A multi-image edit is anchored on Image 1 by the prompt alone: the 2.1
  workflows start from an empty latent at denoise 1.0, and at cfg 1 the
  negative prompt does nothing.

Key files: `appdaemon/apps/detection_summary_app/profiles.py`,
`appdaemon/tests/test_detection_profiles.py` (import path via `sys.path.insert`
into `apps/`), `appdaemon/apps/detection_summary_app/README.md` (profiles table).

## `apps-dev.yaml`: detection entrances

- Each entrance needs `detection_summary_{bk}_dev` **and** `detection_viewer_{bk}_dev`.
- The viewer self-provisions: `input_select`, three `input_text` (selected / timing / cooldown), and its relay script.
- `best_min_person_score: 0` disables the legacy person gate — needed for package-only or animal-only publishing.
- `best_min_animal_count: 1` is required for animal-gated publishing alongside the profile.
- `debug_preserve_run_dirs: true` for dev apps (prevents cleanup).
- An animal-only profile uses `detection_profile: animals` (built-in, not inline).

## Dashboard editing over the HA MCP server

- Fetch `config_hash` fresh before **each** edit — it changes after every `ha_config_set_dashboard` call.
- Use `python_transform` for surgical view updates, not `jq_transform`.
- The transform is a single line; chain statements with `;`.
- Inner Jinja2 templates inside markdown content use single-quoted strings with escaped inner quotes.
- Dashboard `detection-summary` view order: 0 garage, 1 front-door, 2 bulkhead, 3 package, 4 back-deck-pets.

## The 5-card detection-summary view

Card order: bubble-card (nav) → summary markdown → generated img → best img →
timing/cooldown metadata.

- bubble-card entity: `input_select.{bk}_detection_summary_run_id`
- image paths: `/local/detection-summary/{path_segment}/viewer/{{ states('input_select...run_id') }}_generated.png`
- metadata template: `_Detection: {{ states('input_text.{bk}_detection_summary_timing') }}_\n\n_Cooldown: …cooldown…_\n\n_Selection updated: {{ states.input_text.{bk}_detection_summary_selected.last_updated }}_`
