"""Unit tests for ImageGenHealthChecker.

Mocks AppDaemon methods and the ComfyUI status client — no real HTTP or
Home Assistant access required.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Mock hassapi before importing the app
# ---------------------------------------------------------------------------
mock_hass = MagicMock()
mock_hass.Hass = type("_MockHass", (), {"__init__": lambda self, *a, **kw: None})
sys.modules["hassapi"] = mock_hass

_repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo_root / "apps"))
sys.path.insert(0, str(_repo_root))

from health_checks.checker_apps.imagegen_health_checker.imagegen_health_checker import (
    ImageGenHealthChecker,
    API_CHECK,
    QUEUE_CHECK,
)
from providers.ai_providers.comfyui.comfyui_status_client import ComfyUIStatusError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DEFAULT_ARGS: Dict[str, Any] = {
    "checker_id": "imagegen",
    "checker_name": "Image Gen",
    "comfyui_url": "http://comfyui.test:8188",
    "check_interval_s": 120,
    "request_timeout_s": 10,
    "queue_stuck_after_s": 1800,
    "unreachable_after_s": 900,
}


def _make_app(extra_args: dict | None = None) -> ImageGenHealthChecker:
    ad = MagicMock()
    config = MagicMock()
    app = ImageGenHealthChecker(ad, config)

    args = dict(DEFAULT_ARGS)
    if extra_args:
        args.update(extra_args)
    app.args = args

    app.get_state = MagicMock(return_value=None)
    app.set_state = MagicMock()
    app.call_service = MagicMock()
    app.listen_state = MagicMock()
    app.listen_event = MagicMock()
    app.fire_event = MagicMock()
    app.run_in = MagicMock()
    app.run_every = MagicMock()
    app.cancel_timer = MagicMock()
    app.log = MagicMock()
    app.create_task = MagicMock()

    return app


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _init_only(app: ImageGenHealthChecker) -> None:
    """Call initialize without running async startup."""
    app.initialize()


def _startup(app: ImageGenHealthChecker) -> None:
    app.initialize()
    _run(app._async_startup())


def _mock_client(
    app: ImageGenHealthChecker,
    return_value: Any = None,
    side_effect: Any = None,
) -> MagicMock:
    """Replace the app's ComfyUI client with a mock (no real HTTP)."""
    client = MagicMock()
    client.fetch_queue_remaining = AsyncMock(
        return_value=return_value, side_effect=side_effect
    )
    app._client = client
    return client


def _by_name(results: list, name: str) -> dict:
    matches = [r for r in results if r["name"] == name]
    assert len(matches) == 1, f"expected exactly one {name!r} result"
    return matches[0]


# ---------------------------------------------------------------------------
# Tests — Lifecycle & registration
# ---------------------------------------------------------------------------

class TestLifecycle:
    def test_initialize_calls_run_in(self):
        app = _make_app()
        app.initialize()
        app.run_in.assert_called_once()

    def test_startup_registers_event_listeners(self):
        app = _make_app()
        _startup(app)
        event_names = [c[0][1] for c in app.listen_event.call_args_list]
        assert "health_check_controller_ready" in event_names
        assert "health_check_recheck" in event_names

    def test_startup_registers_with_supports_repair_false(self):
        """Page-only checker: supports_repair must be False."""
        app = _make_app()
        _startup(app)
        register_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "register_checker"
        ]
        assert len(register_calls) == 1
        payload = json.loads(register_calls[0][1]["payload"])
        assert payload["supports_repair"] is False
        assert payload["checker_id"] == "imagegen"
        assert API_CHECK in payload["check_names"]
        assert QUEUE_CHECK in payload["check_names"]

    def test_registration_alerting_passthrough(self):
        """alerting arg is forwarded verbatim in the registration payload."""
        alerting = {"pushover_priority": 1, "page": True}
        app = _make_app({"alerting": alerting})
        _startup(app)
        register_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "register_checker"
        ]
        payload = json.loads(register_calls[0][1]["payload"])
        assert payload["alerting"] == alerting

    def test_registration_omits_alerting_when_unset(self):
        app = _make_app()
        _startup(app)
        register_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "register_checker"
        ]
        payload = json.loads(register_calls[0][1]["payload"])
        assert "alerting" not in payload

    def test_controller_ready_reregisters(self):
        app = _make_app()
        _startup(app)
        app.fire_event.reset_mock()
        app._on_controller_ready("health_check_controller_ready", {}, {})
        register_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "register_checker"
        ]
        assert len(register_calls) == 1

    def test_recheck_creates_task(self):
        app = _make_app()
        _startup(app)
        app.create_task.reset_mock()
        app._on_recheck("health_check_recheck", {}, {})
        app.create_task.assert_called_once()


# ---------------------------------------------------------------------------
# Tests — Queue polling
# ---------------------------------------------------------------------------

class TestQueuePolling:
    def test_queue_empty_is_healthy_and_resets_timers(self):
        app = _make_app()
        _init_only(app)
        _mock_client(app, return_value=0)
        # Backdate state to prove an empty queue resets the movement timer
        old = datetime.datetime.now() - datetime.timedelta(seconds=3600)
        app._last_queue_value = 0
        app._queue_unchanged_since = old

        results = _run(app._poll_comfyui())

        api = _by_name(results, API_CHECK)
        queue = _by_name(results, QUEUE_CHECK)
        assert api["status"] == "ok"
        assert "queue_remaining=0" in api["detail"]
        assert queue["status"] == "ok"
        assert queue["detail"] == "Queue empty"
        assert app._last_queue_value == 0
        assert app._queue_unchanged_since is not None
        assert app._queue_unchanged_since > old
        assert app._unreachable_since is None

    def test_first_nonzero_observation_is_ok(self):
        app = _make_app()
        _init_only(app)
        _mock_client(app, return_value=3)

        results = _run(app._poll_comfyui())

        api = _by_name(results, API_CHECK)
        queue = _by_name(results, QUEUE_CHECK)
        assert api["status"] == "ok"
        assert queue["status"] == "ok"
        assert app._last_queue_value == 3
        assert app._queue_unchanged_since is not None

    def test_queue_movement_resets_timer(self):
        """2 -> 1 counts as movement: the unchanged-since timer resets."""
        app = _make_app()
        _init_only(app)
        client = _mock_client(app, return_value=2)
        _run(app._poll_comfyui())

        # Simulate time passing since the 2-observation
        backdated = datetime.datetime.now() - datetime.timedelta(seconds=1200)
        app._queue_unchanged_since = backdated

        client.fetch_queue_remaining = AsyncMock(return_value=1)
        results = _run(app._poll_comfyui())

        queue = _by_name(results, QUEUE_CHECK)
        assert queue["status"] == "ok"
        assert app._last_queue_value == 1
        assert app._queue_unchanged_since > backdated

    def test_stuck_queue_goes_critical(self):
        """Same non-zero value past queue_stuck_after_s -> Queue critical."""
        app = _make_app()
        _init_only(app)
        _mock_client(app, return_value=2)
        app._last_queue_value = 2
        app._queue_unchanged_since = (
            datetime.datetime.now() - datetime.timedelta(seconds=1900)
        )

        results = _run(app._poll_comfyui())

        api = _by_name(results, API_CHECK)
        queue = _by_name(results, QUEUE_CHECK)
        # API itself is reachable — only the queue is wedged
        assert api["status"] == "ok"
        assert queue["status"] == "critical"
        assert "Queue at 2 with no job completed" in queue["detail"]
        assert "threshold 30m" in queue["detail"]
        assert "human attention" in queue["detail"]

    def test_queue_increase_does_not_reset_stuck_clock(self):
        """The canonical GPU wedge: the web server still answers and the
        detection pipeline keeps submitting, so queue_remaining CLIMBS.
        Increases are producer activity, not progress — the stuck clock
        must keep running and the checker must page."""
        app = _make_app()
        _init_only(app)
        client = _mock_client(app, return_value=3)
        app._last_queue_value = 2
        backdated = datetime.datetime.now() - datetime.timedelta(seconds=1900)
        app._queue_unchanged_since = backdated

        results = _run(app._poll_comfyui())

        queue = _by_name(results, QUEUE_CHECK)
        assert queue["status"] == "critical"
        assert app._queue_unchanged_since == backdated  # not reset
        assert app._last_queue_value == 3

        # Still climbing on the next poll — still critical
        client.fetch_queue_remaining = AsyncMock(return_value=5)
        results = _run(app._poll_comfyui())
        assert _by_name(results, QUEUE_CHECK)["status"] == "critical"
        assert app._queue_unchanged_since == backdated

        # A DECREASE (a job actually finished) clears the stuck state
        client.fetch_queue_remaining = AsyncMock(return_value=4)
        results = _run(app._poll_comfyui())
        assert _by_name(results, QUEUE_CHECK)["status"] == "ok"
        assert app._queue_unchanged_since > backdated

    def test_movement_clears_stuck_state_at_same_nonzero_level(self):
        """After a stuck-critical, a value change resets the timer — a later
        poll at the same non-zero level is ok again."""
        app = _make_app()
        _init_only(app)
        client = _mock_client(app, return_value=2)
        app._last_queue_value = 2
        app._queue_unchanged_since = (
            datetime.datetime.now() - datetime.timedelta(seconds=1900)
        )
        results = _run(app._poll_comfyui())
        assert _by_name(results, QUEUE_CHECK)["status"] == "critical"

        # Movement: 2 -> 1
        client.fetch_queue_remaining = AsyncMock(return_value=1)
        results = _run(app._poll_comfyui())
        assert _by_name(results, QUEUE_CHECK)["status"] == "ok"

        # Same non-zero level shortly after — still ok (timer was reset)
        results = _run(app._poll_comfyui())
        queue = _by_name(results, QUEUE_CHECK)
        assert queue["status"] == "ok"
        assert "Queue at 1" in queue["detail"]

    def test_cold_start_nine_minutes_does_not_false_positive(self):
        """Queue at 1 unchanged for 9 minutes (cold-start model load) must
        stay ok — well inside the 30m threshold."""
        app = _make_app()
        _init_only(app)
        _mock_client(app, return_value=1)
        app._last_queue_value = 1
        app._queue_unchanged_since = (
            datetime.datetime.now() - datetime.timedelta(seconds=540)
        )

        results = _run(app._poll_comfyui())

        api = _by_name(results, API_CHECK)
        queue = _by_name(results, QUEUE_CHECK)
        assert api["status"] == "ok"
        assert queue["status"] == "ok"
        assert "Queue at 1" in queue["detail"]
        assert "9m" in queue["detail"]


# ---------------------------------------------------------------------------
# Tests — Unreachable endpoint
# ---------------------------------------------------------------------------

class TestUnreachable:
    def test_first_failure_is_warning(self):
        app = _make_app()
        _init_only(app)
        _mock_client(app, side_effect=ComfyUIStatusError("connection refused"))

        results = _run(app._poll_comfyui())

        api = _by_name(results, API_CHECK)
        queue = _by_name(results, QUEUE_CHECK)
        assert api["status"] == "warning"
        # Security rule S3: exception text (which can embed the internal
        # service URL) must NOT reach the published detail.
        assert "connection refused" not in api["detail"]
        assert "critical after 15m" in api["detail"]
        assert queue["status"] == "unknown"
        assert queue["detail"] == "Endpoint unreachable"
        assert app._unreachable_since is not None

    def test_unreachable_past_threshold_is_critical(self):
        app = _make_app()
        _init_only(app)
        _mock_client(app, side_effect=ComfyUIStatusError("connection refused"))
        app._unreachable_since = (
            datetime.datetime.now() - datetime.timedelta(seconds=1000)
        )

        results = _run(app._poll_comfyui())

        api = _by_name(results, API_CHECK)
        queue = _by_name(results, QUEUE_CHECK)
        assert api["status"] == "critical"
        assert "threshold 15m" in api["detail"]
        assert "human attention" in api["detail"]
        assert queue["status"] == "unknown"

    def test_recovery_clears_unreachable_since(self):
        app = _make_app()
        _init_only(app)
        _mock_client(app, return_value=0)
        app._unreachable_since = (
            datetime.datetime.now() - datetime.timedelta(seconds=600)
        )

        results = _run(app._poll_comfyui())

        api = _by_name(results, API_CHECK)
        queue = _by_name(results, QUEUE_CHECK)
        assert api["status"] == "ok"
        assert queue["status"] == "ok"
        assert app._unreachable_since is None

    def test_unreachable_since_not_reset_on_repeat_failure(self):
        """A second consecutive failure keeps the original unreachable_since."""
        app = _make_app()
        _init_only(app)
        _mock_client(app, side_effect=ComfyUIStatusError("timeout"))
        original = datetime.datetime.now() - datetime.timedelta(seconds=300)
        app._unreachable_since = original

        _run(app._poll_comfyui())

        assert app._unreachable_since == original


# ---------------------------------------------------------------------------
# Tests — Misconfiguration
# ---------------------------------------------------------------------------

class TestNotConfigured:
    def test_missing_url_is_critical(self):
        app = _make_app({"comfyui_url": ""})
        _init_only(app)
        assert app._client is None

        results = _run(app._poll_comfyui())

        api = _by_name(results, API_CHECK)
        queue = _by_name(results, QUEUE_CHECK)
        assert api["status"] == "critical"
        assert "not configured" in api["detail"]
        assert queue["status"] == "unknown"
        assert "not configured" in queue["detail"]


# ---------------------------------------------------------------------------
# Tests — Status reporting
# ---------------------------------------------------------------------------

class TestRunChecks:
    def test_run_checks_fires_report_status_with_results(self):
        app = _make_app()
        _init_only(app)
        mocked_results = [
            {"name": API_CHECK, "status": "ok", "detail": "queue_remaining=0"},
            {"name": QUEUE_CHECK, "status": "ok", "detail": "Queue empty"},
        ]
        app._poll_comfyui = AsyncMock(return_value=mocked_results)

        _run(app._run_checks())

        report_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
        ]
        assert len(report_calls) == 1
        assert report_calls[0][0][0] == "health_check_command"
        payload = json.loads(report_calls[0][1]["payload"])
        assert payload["checker_id"] == "imagegen"
        assert payload["results"] == mocked_results

    def test_run_checks_end_to_end_with_mocked_client(self):
        """Full _run_checks path with a mocked client (no real HTTP)."""
        app = _make_app()
        _init_only(app)
        _mock_client(app, return_value=0)

        _run(app._run_checks())

        report_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
        ]
        assert len(report_calls) == 1
        payload = json.loads(report_calls[0][1]["payload"])
        statuses = {r["name"]: r["status"] for r in payload["results"]}
        assert statuses == {API_CHECK: "ok", QUEUE_CHECK: "ok"}


# ---------------------------------------------------------------------------
# Tests — Custom metrics (queue_remaining gauge)
# ---------------------------------------------------------------------------

class TestMetricsReporting:
    def test_report_includes_queue_remaining_gauge_when_reachable(self):
        app = _make_app()
        _init_only(app)
        _mock_client(app, return_value=7)

        _run(app._run_checks())

        report_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
        ]
        payload = json.loads(report_calls[0][1]["payload"])
        assert payload["metrics"] == [
            {"name": "queue_remaining", "value": 7, "type": "gauge"}
        ]

    def test_report_includes_queue_remaining_gauge_when_empty(self):
        app = _make_app()
        _init_only(app)
        _mock_client(app, return_value=0)

        _run(app._run_checks())

        report_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
        ]
        payload = json.loads(report_calls[0][1]["payload"])
        assert payload["metrics"] == [
            {"name": "queue_remaining", "value": 0, "type": "gauge"}
        ]

    def test_report_omits_metrics_when_unreachable(self):
        """Queue depth is unknown while the endpoint is down — skip the
        metric rather than reporting a stale/misleading value."""
        app = _make_app()
        _init_only(app)
        _mock_client(app, side_effect=ComfyUIStatusError("connection refused"))

        _run(app._run_checks())

        report_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
        ]
        payload = json.loads(report_calls[0][1]["payload"])
        assert "metrics" not in payload

    def test_report_omits_metrics_when_not_configured(self):
        app = _make_app({"comfyui_url": ""})
        _init_only(app)

        _run(app._run_checks())

        report_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
        ]
        payload = json.loads(report_calls[0][1]["payload"])
        assert "metrics" not in payload

    def test_stale_metric_not_carried_into_next_unreachable_poll(self):
        """A good reading followed by a failure must not resurface the old
        value — the metric should be dropped, not stuck at the last-known
        depth."""
        app = _make_app()
        _init_only(app)
        client = _mock_client(app, return_value=5)
        _run(app._run_checks())

        client.fetch_queue_remaining = AsyncMock(
            side_effect=ComfyUIStatusError("timeout")
        )
        app.fire_event.reset_mock()
        _run(app._run_checks())

        report_calls = [
            c for c in app.fire_event.call_args_list
            if c[1].get("command") == "report_status"
        ]
        payload = json.loads(report_calls[0][1]["payload"])
        assert "metrics" not in payload


# ---------------------------------------------------------------------------
# Tests — Restart seeding (detection state carried across AppDaemon restarts)
# ---------------------------------------------------------------------------

def _published_status(checks: list) -> dict:
    """Build a sensor.health_check_status get_state(attribute='all') payload."""
    return {
        "entity_id": "sensor.health_check_status",
        "state": "critical",
        "attributes": {
            "checkers": {
                "imagegen": {
                    "name": "Image Gen",
                    "status": "critical",
                    "checks": checks,
                }
            }
        },
    }


class TestRestartSeeding:
    def test_seeds_unreachable_from_critical_api_check(self):
        """A pre-restart critical API check must re-arm the unreachable clock
        past the threshold so the next failing poll is critical immediately
        (no false [RESOLVED] + 15-minute re-accumulation window)."""
        app = _make_app()
        _init_only(app)
        ts = (
            datetime.datetime.now() - datetime.timedelta(seconds=120)
        ).isoformat(timespec="seconds")
        app.get_state = AsyncMock(
            return_value=_published_status(
                [{"name": API_CHECK, "status": "critical", "last_changed": ts}]
            )
        )

        _run(app._seed_from_published_status())

        assert app._unreachable_since is not None
        down_for = (
            datetime.datetime.now() - app._unreachable_since
        ).total_seconds()
        assert down_for > app._unreachable_after_s

        # Still down on the first post-restart poll -> critical right away
        _mock_client(app, side_effect=ComfyUIStatusError("still down"))
        results = _run(app._poll_comfyui())
        assert _by_name(results, API_CHECK)["status"] == "critical"

    def test_seeds_unreachable_from_warning_api_check_keeps_transition_time(self):
        app = _make_app()
        _init_only(app)
        transition = datetime.datetime.now() - datetime.timedelta(seconds=300)
        app.get_state = AsyncMock(
            return_value=_published_status(
                [{
                    "name": API_CHECK,
                    "status": "warning",
                    "last_changed": transition.isoformat(timespec="seconds"),
                }]
            )
        )

        _run(app._seed_from_published_status())

        assert app._unreachable_since is not None
        delta = abs((app._unreachable_since - transition).total_seconds())
        assert delta < 2

    def test_seeds_stuck_queue_and_only_progress_clears_it(self):
        app = _make_app()
        _init_only(app)
        ts = (
            datetime.datetime.now() - datetime.timedelta(seconds=60)
        ).isoformat(timespec="seconds")
        app.get_state = AsyncMock(
            return_value=_published_status(
                [{"name": QUEUE_CHECK, "status": "critical", "last_changed": ts}]
            )
        )

        _run(app._seed_from_published_status())

        assert app._last_queue_value == -1
        stuck_for = (
            datetime.datetime.now() - app._queue_unchanged_since
        ).total_seconds()
        assert stuck_for > app._queue_stuck_after_s

        # First post-restart poll at an unknown depth stays critical
        client = _mock_client(app, return_value=5)
        results = _run(app._poll_comfyui())
        assert _by_name(results, QUEUE_CHECK)["status"] == "critical"

        # A genuine decrease (job finished) clears it
        client.fetch_queue_remaining = AsyncMock(return_value=4)
        results = _run(app._poll_comfyui())
        assert _by_name(results, QUEUE_CHECK)["status"] == "ok"

    def test_no_published_state_or_errors_seed_nothing(self):
        app = _make_app()
        _init_only(app)

        app.get_state = AsyncMock(return_value=None)
        _run(app._seed_from_published_status())
        assert app._unreachable_since is None
        assert app._queue_unchanged_since is None

        app.get_state = AsyncMock(side_effect=RuntimeError("HA down"))
        _run(app._seed_from_published_status())
        assert app._unreachable_since is None

    def test_healthy_published_state_seeds_nothing(self):
        app = _make_app()
        _init_only(app)
        app.get_state = AsyncMock(
            return_value=_published_status(
                [
                    {"name": API_CHECK, "status": "ok", "last_changed": None},
                    {"name": QUEUE_CHECK, "status": "ok", "last_changed": None},
                ]
            )
        )

        _run(app._seed_from_published_status())

        assert app._unreachable_since is None
        assert app._queue_unchanged_since is None
        assert app._last_queue_value is None
