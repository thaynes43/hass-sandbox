"""Pytest configuration for AppDaemon tests."""

import pytest


def pytest_configure(config):
    """Add filter to suppress 'coroutine was never awaited' warnings.

    These arise when tests mock create_task/run_in: the app passes coroutines
    to them, but the mocks don't schedule them, so they're never awaited.
    The app code is fine; this is a test artifact.
    """
    config.addinivalue_line(
        "filterwarnings",
        "ignore:coroutine .* was never awaited:RuntimeWarning",
    )
