---
name: appdaemon-async-lifecycle-bugs
description: Five recurring async/lifecycle bug shapes found in AppDaemon review rounds — callback-released latches, fixed timeout margins, mutable "latest" fields read late, arrival-order metadata, and memory-only counters across reloads
metadata:
  type: reference
---

# Recurring async / lifecycle bug shapes (photo_frame_viewer review rounds, 2026-09-18)

## 1. A latch released only by a callback needs an absolute-deadline watchdog

A boolean that blocks work while an async chain runs, released only when the
chain's result callback fires, and whose timeout is *evaluated inside that same
callback*. If the chain dies (lost `run_in` bounce, cancelled task), the
timeout can never fire and the flag blocks the feature until AppDaemon
restarts.

Fix is two-part: (1) try/except the hand-back and release the flag there,
guarded on the id you own so a newer in-flight job is not clobbered; (2) a
`run_in` timer armed at `timeout + margin` when the work starts, carrying the
job id, no-op for a superseded id and for a non-owner instance (which must
still release its own flag). Cancel it on success, on give-up, in
`terminate()`, and when a new job starts.

**Exception:** a latch released in a `finally` inside the *same* coroutine
(no callback hop) cannot strand — no watchdog needed. Say so in a comment.

## 2. Timeout constants must be derived when the inputs they race are tunable

A watchdog armed at `timeout + FIXED_MARGIN` only outran the normal give-up
path at the *default* retry interval. Raise the documented (unclamped)
`stage_verify_interval_s` and the backstop fires first, killing a healthy job
and logging a phantom fault.

Rule: when a backstop timer must lose a race against a normal path, compute its
delay from the same live values the normal path uses (`timeout +
retry_interval + probe_timeout + named_slack`), and import the probe timeout
from the provider that owns it rather than duplicating the literal. Test the
*invariant* across several interval values, not the default.

## 3. Don't let a long-running async window read a mutable "latest" field

`_on_batch_ready` wrote `_staged_filter_name`, and the settle step read that
live field minutes later — so a generation got published under the *next*
album's title once verification stretched the window from 3 s to minutes.
Invisible while the window was short.

Pattern: snapshot the value into the job's own context when the job starts and
free the live field for the next job. On failure, hand the snapshot back only
`if not <live field>` so a newer arrival still wins. Clear the snapshot in the
context teardown, and read it into a local *before* teardown where the settle
path clears first.

## 4. Attribute event metadata by content fingerprint, not arrival order

The round-3 snapshot fix opened the mirror-image bug. `immich_fetcher` writes
all files → publishes a sensor (HA round trip) → fires `batch_ready(filter=…)`.
The viewer's periodic poll can stage that album's COMPLETE files inside that
gap, so the title arrives *after* the generation it describes was already
staged with an empty title — and nothing re-stages afterwards (fingerprint
already matches current, every poll returns early), so the wrong album name
sticks forever.

Fix shape: on a titled event, fingerprint what is on disk and route the title to
whichever generation those files belong to (in-flight / pending / current /
next). Arrival time is ambiguous in BOTH directions; content is not.

Generalise: whenever a producer writes data and *then* announces it, any
consumer that also polls has a window where the announcement arrives after it
already acted. Match announcement to data by content, never by timing.

## 5. AppDaemon reloads construct a NEW instance — memory-only counters collide

`photo_frame_viewer` allocated generation ids from `_next_gen_counter`, seeded
from the currently DISPLAYED gen, and `initialize()` always re-stages. Every
reload (module change *or* an HA-websocket reconnect) makes a fresh instance,
so two of them coming up during a staging window handed out the SAME id. Seen
in prod 2026-09-18: `staging gen=19` and `staging gen=22` each logged twice,
seconds apart, from two instances.

Consequences with a detached HA-side worker: the loser's cleanup deletes a
directory the winner already verified and promoted, and a reused id lets an
existence probe answer 200 from a leftover directory holding old content.

Fix: persist the counter in the app's existing `state_dir/state.json` and
advance it **before** the side effect that uses the id; on init take
`max(recovered-from-live-state, persisted)`. Validate the persisted value
(missing / non-int / negative / absurd → fall back, never raise) or one bad
write poisons the counter forever. Any id or sequence an AppDaemon app hands
out must be persisted, not just held in `self.`. The ownership guard
(`_is_active_owner`) belongs on the single funnel into allocation too, not only
on the individual callbacks. `_save_runtime_state()` rebuilds the whole JSON
document from live fields on every call, so adding a key is automatically
preserved by all other writers — worth checking before adding a field to a
shared state file.

Related: [[appdaemon-service-calls-and-staging]], [[appdaemon-testing-discipline]]
