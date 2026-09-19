---
name: appdaemon-set-state-semantics
description: Exactly which set_state values AppDaemon 4.5.13 mangles or drops before they reach HA, read from site-packages — 0/False vanish, True becomes "true", empty lists and empty strings survive, and `state` is filtered too
metadata:
  type: reference
---

# `set_state` value filtering in AppDaemon 4.5.13

Read from the installed package (`appdaemon/utils.py`,
`appdaemon/plugins/hass/hassplugin.py`), 2026-09-18. Not derivable from this
repo — it is third-party internals, and the folklore version of it in repo
comments is imprecise.

## The path

`set_state` → `hassplugin.set_plugin_state` → `hassplugin.http_method`
(`~line 457`) → `utils.clean_http_kwargs(kwargs)` → `POST /api/states/<entity>`.

`clean_http_kwargs` is two steps:

1. `clean_kwargs(val, http=True)` — `case True if http: return "true"`. The
   literal pattern `True` matches by **identity**, so only a real `bool` `True`
   is stringified; an `int` `1` is untouched.
2. `remove_literals(cleaned, (None, False))` — recursive; a mapping keeps
   `k: v` only `if v not in (None, False)`, and `in` compares with `==`.

## What that actually means

| value | result |
|---|---|
| `True` | becomes the string `"true"` |
| `False` | **dropped** |
| `0`, `0.0` | **dropped** (`0 == False`) |
| `""` | kept |
| `[]`, `{}` | **kept** (`[] == False` is `False`) |
| `1`, `"0"`, `"false"` | kept as-is |

Two consequences people get wrong:

- The filter applies to the **top-level kwargs too**, not just `attributes`.
  So `set_state(entity, state=0)` sends no `state` field at all, and HA's
  `POST /api/states` rejects the body. Always stringify a numeric state.
- Repo comments claiming "an empty list is absent on read-back" are wrong
  about the mechanism (e.g.
  `apps/health_checks/checker_apps/network_protocol_checker/repairable_network_protocol_checker.py`
  `_parse_attempts`). Harmless there — the code does `for item in raw or []`
  either way — but do not build new logic on that belief.

## The rule to code by

Publish every attribute a card or a human will read as a **non-empty string**,
and stringify the state. See `apps/assist_exposure_guard`'s `_publish_status`
for the worked example, and test it — a mutation swapping `str(len(x))` for
`len(x)` must fail.

Related: [[appdaemon-testing-discipline]], [[appdaemon-probe-design]]
