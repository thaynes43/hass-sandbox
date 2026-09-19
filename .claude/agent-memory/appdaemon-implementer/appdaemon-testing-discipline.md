---
name: appdaemon-testing-discipline
description: Mutation-check every new test cheaply; give mocked run_in/create_task distinct handles; and never let a test encode a sequence the real producer cannot emit
metadata:
  type: feedback
---

# Testing discipline for this repo

## Mutation-check new tests (cheap and worth it)

**Why:** the repo's vacuous-test trap is real — a `MagicMock` where an
`AsyncMock` belongs sends the code down an `except` branch and every assertion
passes for the wrong reason.

**How to apply:** script it. For each `(file, anchor, broken replacement, test
selector)`, apply the edit, run pytest, restore in a `finally`, and assert the
return code is non-zero. 16 mutations over the photo-frame change ran in ~3 s
total; 21 over `assist_exposure_guard` ran in ~4 s. It has caught a real
missing `switch_allowlist` check and a wrong-timer cancel.

## Mocked `run_in`/`create_task` returning one shared handle hides cancel bugs

`MagicMock()` returns the **same** `return_value` for every call, so two
handles stored from two `run_in` calls compare equal. Every
`assert handle in cancel_timer.call_args_list` then passes even when the code
cancels the wrong timer — a mutation check caught exactly this.

Give the double distinct handles:

```python
handles = itertools.count()
app.run_in = MagicMock(side_effect=lambda *a, **k: f"handle-{next(handles)}")
```

## Watch for tests that encode timing that cannot happen

Two round-3 tests fired `batch_ready("Album B")` without changing the files on
disk — impossible in reality (the fetcher writes B's files before announcing
B). They only passed because routing was timing-based. Round 4's content-based
routing correctly reclassified them, and the fix was to make the scenario
realistic, **not** to relax the assertion. Before asserting on an event
sequence, read the producer's actual ordering rather than assuming events and
state move independently.

Related: [[health-checks-test-idioms]], [[appdaemon-async-lifecycle-bugs]]
