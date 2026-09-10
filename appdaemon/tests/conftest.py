"""Pytest configuration and shared test doubles for the AppDaemon suite.

## Why there is a forced GC pass after every test

A coroutine that is created but never awaited raises
``RuntimeWarning: coroutine '...' was never awaited`` from its ``__del__``.
``__del__`` runs whenever the interpreter happens to finalise the object,
which for a coroutine trapped in a ``MagicMock``'s ``call_args`` (or in any
other reference cycle) means "the next time the cyclic garbage collector
runs" — i.e. during some *other*, entirely innocent test.

That misattribution is what made this class of defect impossible to fix: the
gated suite blamed ~40 tests that were not leaking anything. The autouse
fixture below forces ``gc.collect()`` in every test's teardown, so an
un-awaited coroutine is finalised while its own test is still the current
item and the warning lands on the test that actually leaked it.

Keep the fixture. It is what makes the ``filterwarnings`` gate in
``pytest.ini`` trustworthy.
"""

import gc
import inspect
from unittest.mock import DEFAULT, MagicMock

import pytest


@pytest.fixture(scope="session", autouse=True)
def _freeze_import_time_heap():
    """Move the import-time heap out of the collector's reach, once.

    Purely a speed measure for the per-test ``gc.collect()`` below. By the time
    the first test runs, pytest has imported every test module, so the heap is
    large and static; ``gc.freeze()`` parks all of it in the permanent
    generation, which full collections never scan. Objects created afterwards —
    including every coroutine a test might leak — are unaffected, so the gate
    is exactly as strict, it just stops re-walking the same 100+ modules 3355
    times. Measured on the full suite: 293s without, 187s with (baseline 180s).
    """
    gc.collect()
    gc.freeze()
    yield
    gc.unfreeze()


@pytest.fixture(autouse=True)
def _finalize_coroutines_in_own_test():
    """Force a GC pass in every test's teardown.

    Autouse fixtures are set up before explicitly requested ones, so this
    tears down *last* — after function-scoped fixtures have released their
    mocks. A coroutine reachable only from that function-scoped state is
    therefore collected here, and its "never awaited" RuntimeWarning is
    attributed to the test that leaked it rather than to whichever test the
    collector happened to interrupt.

    That covers every leak found so far, but it is not absolute. A coroutine
    still held by a module-, class- or session-scoped fixture outlives this
    teardown, and so does one pinned by the traceback pytest retains for a
    *failing* test. Either is finalised later and can still land on a
    bystander — so if a reported leak makes no sense for the test named,
    check for a wider-scoped holder before believing the attribution.
    """
    yield
    gc.collect()


def _close_coroutine_args(*args, **kwargs):
    """Close any coroutine passed positionally, then defer to the mock.

    Returning ``DEFAULT`` keeps ``MagicMock``'s normal return value, so a
    double built with this side effect behaves exactly like the bare
    ``MagicMock()`` it replaces — it just does not leak the coroutine.
    """
    for arg in args:
        if inspect.iscoroutine(arg):
            arg.close()
    return DEFAULT


def closing_create_task(**kwargs) -> MagicMock:
    """A ``create_task``/``run_in`` double that closes what it is handed.

    Use this in place of a bare ``MagicMock()`` whenever a test does not care
    whether the scheduled coroutine actually runs. If the test *does* care,
    capture the coroutine and await it instead — see the
    ``app.captured_tasks`` + ``_drive()`` pattern in
    ``test_repairable_network_protocol_checker.py``.
    """
    return MagicMock(side_effect=_close_coroutine_args, **kwargs)
