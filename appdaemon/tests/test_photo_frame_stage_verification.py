"""Staging verification tests for PhotoFrameViewerApp.

Regression suite for the "broken images on the wall display" defect
(2026-09-18).  The app used to mark a generation ready 3 seconds after
firing ``shell_command/photo_frame_stage_gen``, with no check that the
command had actually produced anything.  When the NFS source stalled
mid-copy, HA killed the shell at its 60s timeout, the atomic ``mv`` never
ran, and the app happily published ``/local/photo-frame/live/<gen>/...``
URLs that 404'd until the next successful generation (10+ minutes later).

The invariant these tests guard: **a generation is never marked pending —
and therefore never published — until HA is verifiably serving it.**
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Mock hassapi before importing the app
mock_hass = MagicMock()
mock_hass.Hass = type("_MockHass", (), {"__init__": lambda self, *a, **kw: None})
sys.modules["hassapi"] = mock_hass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))

from photo_frame_viewer.photo_frame_viewer_app import PhotoFrameViewerApp  # noqa: E402


SOURCE_FILENAMES = ["IMG_001.jpg", "IMG_002.jpg", "IMG_003.jpg"]

_SENSOR_ENTITY_ID = "sensor.wall_display_photo_frame_status"
_PICKER_ENTITY_ID = "input_select.wall_display_photo_frame_image"

# Obviously-fake value (security policy S5) — no real host is ever contacted:
# local_file_status is patched in every test that reaches it.
_FAKE_HA_URL = "http://ha.test:8123"

_PROBE = "photo_frame_viewer.photo_frame_viewer_app.local_file_status"

# Mirrors providers.ha_provisioner.local_file_check.STATUS_UNREACHABLE.
_UNREACHABLE = -1

# The probe timeout the app actually uses, imported rather than duplicated so
# the watchdog-margin tests keep tracking the provider's default.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from providers.ha_provisioner.local_file_check import (  # noqa: E402
    DEFAULT_TIMEOUT_S as _PROBE_TIMEOUT_S,
)


# ----------------------------------------------------------------------
# Fixtures / harness
# ----------------------------------------------------------------------


def _make_app(
    *,
    current_url: str = "",
    picker_value: str = "IMG_001.jpg",
    picker_options: list[str] | None = None,
    source_filenames: list[str] | None = None,
    extra_args: dict | None = None,
) -> PhotoFrameViewerApp:
    """A PhotoFrameViewerApp with mocked AppDaemon methods and a real temp dir."""
    source_dir = tempfile.mkdtemp(prefix="pfv_verify_")
    for fname in source_filenames if source_filenames is not None else SOURCE_FILENAMES:
        Path(os.path.join(source_dir, fname)).write_bytes(b"")

    app = PhotoFrameViewerApp(MagicMock(), MagicMock())

    base_args: dict = {
        "ha_url": _FAKE_HA_URL,
        "source_dir": source_dir,
        "ha_local_url_base": "/local/photo-frame/live",
        "stage_shell_command": "photo_frame_stage_gen",
        "cleanup_shell_command": "photo_frame_cleanup_gen",
        "source_poll_interval_s": 30,
        "stage_settle_delay_s": 3,
        "stage_verify_interval_s": 5,
        "stage_verify_timeout_s": 240,
        "picker_entity_id": _PICKER_ENTITY_ID,
        "fallback_image_path": os.path.join(source_dir, "no-image.jpg"),
        "options_max": 50,
        "auto_cycle": True,
        "state_dir": source_dir,
    }
    if extra_args:
        base_args.update(extra_args)
    app.args = base_args
    app.name = "photo_frame_viewer_wall_display"

    options = list(picker_options or ["IMG_001.jpg", "IMG_002.jpg", "IMG_003.jpg"])

    def fake_get_state(entity_id, attribute=None):
        if entity_id == _SENSOR_ENTITY_ID:
            if attribute == "all":
                return {"state": "playing", "attributes": {"image_url": current_url}}
            return "playing"
        if entity_id == _PICKER_ENTITY_ID:
            if attribute == "all":
                return {"state": picker_value, "attributes": {"options": options}}
            return picker_value
        return None

    app.get_state = MagicMock(side_effect=fake_get_state)
    app.set_state = MagicMock()
    app.call_service = MagicMock()
    app.listen_state = MagicMock()
    app.listen_event = MagicMock()
    app.run_every = MagicMock()
    # Distinct handle per call.  A bare MagicMock returns one shared
    # return_value for every call, which makes the verify timer and the
    # watchdog indistinguishable and silently turns every "this handle was
    # cancelled" assertion vacuous.
    handles = itertools.count()
    app.run_in = MagicMock(side_effect=lambda *a, **k: f"handle-{next(handles)}")
    app.cancel_timer = MagicMock()
    app.timer_running = MagicMock(return_value=False)
    app.datetime = MagicMock()
    app.log = MagicMock()
    # Left as a bare MagicMock on purpose: the drivers below pull the
    # coroutine back out of call_args and await it, so nothing leaks.
    app.create_task = MagicMock()

    return app


def _replace_source_files(source_dir: str, new_filenames: list[str]) -> None:
    for fname in os.listdir(source_dir):
        if os.path.splitext(fname)[1].lower() in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
            os.remove(os.path.join(source_dir, fname))
    for fname in new_filenames:
        Path(os.path.join(source_dir, fname)).write_bytes(b"")


def _service_calls(app: PhotoFrameViewerApp, service: str) -> list:
    return [
        c for c in app.call_service.call_args_list
        if c.args and str(c.args[0]) == service
    ]


def _stage_calls(app: PhotoFrameViewerApp) -> list:
    return _service_calls(app, "shell_command/photo_frame_stage_gen")


def _cleanup_calls(app: PhotoFrameViewerApp) -> list:
    return _service_calls(app, "shell_command/photo_frame_cleanup_gen")


def _last_run_in(app: PhotoFrameViewerApp, fn) -> object:
    matches = [c for c in app.run_in.call_args_list if c.args and c.args[0] == fn]
    assert matches, f"expected a run_in() scheduled for {fn.__name__}"
    return matches[-1]


def _logs(app: PhotoFrameViewerApp, level: str, needle: str) -> list[str]:
    out = []
    for c in app.log.call_args_list:
        if c.kwargs.get("level") != level:
            continue
        message = str(c.args[0]) if c.args else ""
        if needle in message:
            out.append(message)
    return out


def _verify_round(
    app: PhotoFrameViewerApp,
    *,
    exists: bool | None = None,
    status: int | None = None,
    elapsed_s: float | None = None,
) -> AsyncMock:
    """Drive one full verification round: timer -> HTTP probe -> result.

    Mirrors what AppDaemon does at runtime — fire the scheduled ``run_in``
    callback, run the coroutine it hands to ``create_task``, then deliver the
    result through the ``run_in(..., 0)`` bounce back onto the app thread.

    Pass ``exists`` for the two ordinary answers (200 / 404) or ``status`` for
    a specific one (a redirect, ``_UNREACHABLE``, ...).
    """
    if status is None:
        assert exists is not None, "pass exists= or status="
        status = 200 if exists else 404

    if elapsed_s is not None:
        app._staging_started_at = time.monotonic() - elapsed_s

    timer_call = _last_run_in(app, app._on_stage_verify)
    before = len(app.create_task.call_args_list)

    probe = AsyncMock(return_value=status)
    with patch(_PROBE, new=probe):
        app._on_stage_verify(dict(timer_call.kwargs))
        created = app.create_task.call_args_list[before:]
        assert len(created) == 1, (
            f"expected exactly one probe coroutine, got {len(created)}"
        )
        asyncio.run(created[0].args[0])

    result_call = _last_run_in(app, app._on_stage_verify_result)
    app._on_stage_verify_result(dict(result_call.kwargs))
    return probe


def _init_with_current_gen(gen_id: str = "3", **kwargs) -> PhotoFrameViewerApp:
    """Initialize an app that already has a current generation on the sensor.

    ``initialize()`` always forces a re-stage, so the returned app has one
    generation in flight and awaiting verification.
    """
    app = _make_app(
        current_url=f"/local/photo-frame/live/{gen_id}/IMG_001.jpg", **kwargs
    )
    app.initialize()
    return app


# ----------------------------------------------------------------------
# (a) verified on the first check
# ----------------------------------------------------------------------


class TestVerifiedFirstCheck:
    def test_nothing_is_pending_before_verification(self):
        app = _init_with_current_gen()
        assert app._staging_gen_id == "4"
        assert app._staging_in_progress is True
        assert app._pending_gen_id is None, (
            "a generation must not be pending before HA is known to serve it"
        )

    def test_first_check_success_marks_pending_and_clears_latch(self):
        app = _init_with_current_gen()
        gen = app._staging_gen_id

        _verify_round(app, exists=True)

        assert app._pending_gen_id == gen
        assert app._pending_labels == ["IMG_001.jpg", "IMG_002.jpg", "IMG_003.jpg"]
        assert app._staging_in_progress is False
        assert app._staging_gen_id is None

    def test_probe_targets_the_url_the_card_will_load(self):
        app = _init_with_current_gen()
        gen = app._staging_gen_id

        probe = _verify_round(app, exists=True)

        assert probe.await_count == 1
        ha_url, url_path = probe.await_args.args
        assert ha_url == _FAKE_HA_URL
        assert url_path == f"/local/photo-frame/live/{gen}/IMG_001.jpg"

    def test_first_check_does_not_log_a_duration_at_info(self):
        """Only a slow stage (more than one check) is interesting at INFO."""
        app = _init_with_current_gen()
        _verify_round(app, exists=True)
        assert _logs(app, "INFO", "verified staged") == []

    def test_first_check_is_scheduled_at_the_settle_delay(self):
        app = _init_with_current_gen()
        assert _last_run_in(app, app._on_stage_verify).args[1] == app.stage_settle_delay_s

    def test_stage_service_call_is_fire_and_forget(self):
        """The 60s-blocking synchronous call starved the slideshow tick."""
        app = _init_with_current_gen()
        stage = _stage_calls(app)
        assert len(stage) == 1
        assert stage[0].kwargs.get("callback") == app._on_stage_service_result
        assert stage[0].kwargs["gen_id"] == "4"


# ----------------------------------------------------------------------
# (b) appears after several checks
# ----------------------------------------------------------------------


class TestSlowStaging:
    def test_pending_is_set_only_once_the_file_appears(self):
        app = _init_with_current_gen()
        gen = app._staging_gen_id

        for attempt in range(3):
            _verify_round(app, exists=False)
            assert app._pending_gen_id is None, (
                f"published after failed check {attempt + 1}"
            )
            assert app._staging_in_progress is True, "latch released too early"
            assert app._staging_gen_id == gen

        _verify_round(app, exists=True)

        assert app._pending_gen_id == gen
        assert app._staging_in_progress is False

    def test_rechecks_use_the_verify_interval(self):
        app = _init_with_current_gen()
        _verify_round(app, exists=False)
        assert _last_run_in(app, app._on_stage_verify).args[1] == app.stage_verify_interval_s

    def test_slow_stage_logs_duration_and_check_count_at_info(self):
        app = _init_with_current_gen()
        _verify_round(app, exists=False)
        _verify_round(app, exists=False)
        app.log.reset_mock()
        _verify_round(app, exists=True, elapsed_s=42.0)

        messages = _logs(app, "INFO", "verified staged")
        assert len(messages) == 1, messages
        assert "42.0s" in messages[0]
        assert "3 checks" in messages[0]

    def test_no_url_is_published_while_waiting(self):
        app = _init_with_current_gen()
        url_before = app._last_published_local_url
        app.set_state.reset_mock()

        _verify_round(app, exists=False)
        _verify_round(app, exists=False)

        assert app._last_published_local_url == url_before
        for c in app.set_state.call_args_list:
            published = c.kwargs.get("attributes", {}).get("image_url", "")
            assert published in ("", url_before)


# ----------------------------------------------------------------------
# (c) never appears -> deadline
# ----------------------------------------------------------------------


class TestVerificationDeadline:
    def _stage_a_failing_batch(self, app: PhotoFrameViewerApp) -> str:
        """Settle the startup gen, then stage a fresh batch that never lands."""
        _verify_round(app, exists=True)
        app._on_tick({})
        app.call_service.reset_mock()
        app.log.reset_mock()

        _replace_source_files(app.source_dir, ["FLORIDA_1.jpg", "FLORIDA_2.jpg"])
        app._on_batch_ready(
            "immich_fetcher_batch_ready", {"count": 2, "filter": "Florida"}, {}
        )
        failed_gen = app._staging_gen_id
        assert failed_gen is not None
        # The title now lives in the staging snapshot, not the next-stage slot.
        assert app._staging_filter_name == "Florida"
        return failed_gen

    def test_deadline_publishes_nothing_and_cleans_up(self):
        app = _init_with_current_gen()
        failed_gen = self._stage_a_failing_batch(app)
        url_before = app._last_published_local_url

        _verify_round(app, exists=False)
        assert app._staging_in_progress is True

        _verify_round(app, exists=False, elapsed_s=app.stage_verify_timeout_s + 1)

        assert app._pending_gen_id is None
        assert app._pending_fingerprint is None
        assert app._staging_in_progress is False
        assert app._staging_gen_id is None
        assert app._last_published_local_url == url_before

        cleanup = _cleanup_calls(app)
        assert any(c.kwargs.get("gen_id") == failed_gen for c in cleanup), (
            f"the abandoned gen must be cleaned up; cleanup calls: {cleanup}"
        )

    def test_deadline_logs_one_actionable_warning(self):
        app = _init_with_current_gen()
        failed_gen = self._stage_a_failing_batch(app)
        _verify_round(app, exists=False, elapsed_s=app.stage_verify_timeout_s + 1)

        warnings = _logs(app, "WARNING", "FAILED verification")
        assert len(warnings) == 1, warnings
        message = warnings[0]
        assert f"gen={failed_gen}" in message
        assert "/local/photo-frame/live/" in message
        assert "/config/www/photo-frame/live/.stage.log" in message
        # S3/S6: the probe URL is logged, never a credential.
        assert "Bearer" not in message
        assert "token" not in message.lower()

    def test_deadline_preserves_the_album_title_for_the_retry(self):
        app = _init_with_current_gen()
        self._stage_a_failing_batch(app)
        _verify_round(app, exists=False, elapsed_s=app.stage_verify_timeout_s + 1)

        assert app._staged_filter_name == "Florida", (
            "the automatic re-stage must keep the album title"
        )

    def test_next_poll_restages_with_a_new_gen_id(self):
        app = _init_with_current_gen()
        failed_gen = self._stage_a_failing_batch(app)
        _verify_round(app, exists=False, elapsed_s=app.stage_verify_timeout_s + 1)

        app.call_service.reset_mock()
        app._poll_for_changes(reason="poll")

        stage = _stage_calls(app)
        assert len(stage) == 1, "the next poll must re-stage after a failed stage"
        new_gen = stage[0].kwargs["gen_id"]
        assert new_gen != failed_gen
        assert app._staging_gen_id == new_gen

        # ...and the retry, once verified, publishes normally with the title.
        _verify_round(app, exists=True)
        assert app._pending_gen_id == new_gen
        app._on_tick({})
        assert app._displaying_filter_name == "Florida"
        assert f"/local/photo-frame/live/{new_gen}/" in (app._last_published_local_url or "")


# ----------------------------------------------------------------------
# (d) stale / superseded callbacks
# ----------------------------------------------------------------------


class TestStaleCallbacks:
    def test_stale_verify_timer_is_a_noop(self):
        app = _init_with_current_gen()
        in_flight = app._staging_gen_id
        app.create_task.reset_mock()

        app._on_stage_verify({"gen_id": "999"})

        assert app.create_task.call_count == 0, "superseded gen must not probe"
        assert app._staging_gen_id == in_flight
        assert app._staging_in_progress is True
        assert app._pending_gen_id is None

    def test_stale_verify_result_is_a_noop(self):
        app = _init_with_current_gen()
        in_flight = app._staging_gen_id

        app._on_stage_verify_result({"gen_id": "999", "status": 200})

        assert app._pending_gen_id is None, (
            "a superseded generation must never be promoted"
        )
        assert app._staging_gen_id == in_flight
        assert app._staging_in_progress is True
        assert app._staging_checks == 0

    def test_missing_gen_id_is_a_noop(self):
        app = _init_with_current_gen()
        app.create_task.reset_mock()

        app._on_stage_verify({})
        app._on_stage_verify_result({"status": 200})

        assert app.create_task.call_count == 0
        assert app._pending_gen_id is None
        assert app._staging_in_progress is True

    def test_zombie_instance_releases_the_latch(self):
        app = _init_with_current_gen()
        gen = app._staging_gen_id
        app.create_task.reset_mock()

        # An AppDaemon reload constructs a newer instance for the same prefix.
        newer = _make_app()
        newer.initialize()
        assert app._is_active_owner() is False

        app._on_stage_verify({"gen_id": gen})

        assert app.create_task.call_count == 0
        assert app._staging_in_progress is False
        assert app._pending_gen_id is None

    def test_zombie_instance_drops_a_verify_result(self):
        app = _init_with_current_gen()
        gen = app._staging_gen_id
        newer = _make_app()
        newer.initialize()

        app._on_stage_verify_result({"gen_id": gen, "status": 200})

        assert app._pending_gen_id is None
        assert app._staging_in_progress is False

    def test_terminate_releases_the_latch(self):
        app = _init_with_current_gen()
        assert app._staging_in_progress is True

        app.terminate()

        assert app._staging_in_progress is False
        assert app._verify_handle is None


# ----------------------------------------------------------------------
# (e) no ha_url -> legacy behaviour
# ----------------------------------------------------------------------


class TestVerificationDisabled:
    def test_warns_once_at_startup(self):
        app = _make_app(extra_args={"ha_url": ""})
        app.initialize()

        assert app._stage_verification_enabled is False
        assert len(_logs(app, "WARNING", "staging verification is DISABLED")) == 1

    def test_settle_delay_marks_pending_without_probing(self):
        app = _init_with_current_gen(extra_args={"ha_url": ""})
        gen = app._staging_gen_id
        app.create_task.reset_mock()

        timer_call = _last_run_in(app, app._on_stage_verify)
        assert timer_call.args[1] == app.stage_settle_delay_s
        app._on_stage_verify(dict(timer_call.kwargs))

        assert app.create_task.call_count == 0, "must not probe without an ha_url"
        assert app._pending_gen_id == gen
        assert app._staging_in_progress is False

    def test_no_warning_per_generation(self):
        app = _init_with_current_gen(extra_args={"ha_url": ""})
        app._on_stage_verify(dict(_last_run_in(app, app._on_stage_verify).kwargs))
        app._on_tick({})
        app.log.reset_mock()

        _replace_source_files(app.source_dir, ["NEXT_1.jpg"])
        app._poll_for_changes(reason="poll")
        app._on_stage_verify(dict(_last_run_in(app, app._on_stage_verify).kwargs))

        assert _logs(app, "WARNING", "staging verification is DISABLED") == []

    def test_unresolvable_ha_url_env_disables_verification_without_crashing(self):
        """A *_env pointer at an unset variable must not break initialize()."""
        app = _make_app(extra_args={"ha_url": "", "ha_url_env": "PFV_TEST_MISSING_URL"})
        os.environ.pop("PFV_TEST_MISSING_URL", None)

        app.initialize()

        assert app._ha_url == ""
        assert app._stage_verification_enabled is False
        assert len(_stage_calls(app)) == 1


# ----------------------------------------------------------------------
# The staging latch can never be stranded
# ----------------------------------------------------------------------


class TestLatchIsNeverStranded:
    """`_staging_in_progress` blocks every future stage while it is set.

    It is released only by `_on_stage_verify_result`, and the
    `stage_verify_timeout_s` deadline is only evaluated when a result
    arrives — so a verification chain that goes quiet would block staging
    until AppDaemon restarts.  Two independent guards prevent that.
    """

    def test_failed_result_bounce_releases_the_latch(self):
        """If the run_in bounce raises, the latch must not be left set."""
        app = _init_with_current_gen()
        gen = app._staging_gen_id
        timer_call = _last_run_in(app, app._on_stage_verify)

        scheduler = app.run_in

        def raising_run_in(callback, delay, **kwargs):
            if callback == app._on_stage_verify_result:
                raise RuntimeError("scheduler gone")
            return scheduler(callback, delay, **kwargs)

        app.run_in = MagicMock(side_effect=raising_run_in)

        with patch(_PROBE, new=AsyncMock(return_value=True)):
            app._on_stage_verify(dict(timer_call.kwargs))
            asyncio.run(app.create_task.call_args_list[-1].args[0])

        assert app._staging_in_progress is False
        assert app._staging_gen_id is None
        assert app._pending_gen_id is None

        warnings = _logs(app, "WARNING", "could not hand back")
        assert len(warnings) == 1, warnings
        assert f"gen={gen}" in warnings[0]
        assert "RuntimeError" in warnings[0]

    def test_failed_bounce_does_not_clobber_a_newer_staging(self):
        """A late failure must only release the context it owns."""
        app = _init_with_current_gen()
        timer_call = _last_run_in(app, app._on_stage_verify)
        stale_gen = app._staging_gen_id

        scheduler = app.run_in

        def raising_run_in(callback, delay, **kwargs):
            if callback == app._on_stage_verify_result:
                raise RuntimeError("scheduler gone")
            return scheduler(callback, delay, **kwargs)

        app.run_in = MagicMock(side_effect=raising_run_in)

        with patch(_PROBE, new=AsyncMock(return_value=True)):
            app._on_stage_verify(dict(timer_call.kwargs))
            coro = app.create_task.call_args_list[-1].args[0]
            # A newer generation takes over the context before the probe
            # coroutine gets as far as handing its result back.
            app._staging_gen_id = "987"
            app._staging_in_progress = True
            asyncio.run(coro)

        assert app._staging_gen_id == "987", (
            f"a failure for gen={stale_gen} must not clear a newer context"
        )
        assert app._staging_in_progress is True

    def test_watchdog_is_armed_at_the_absolute_deadline(self):
        app = _init_with_current_gen()
        watchdog = _last_run_in(app, app._on_stage_watchdog)
        assert watchdog.args[1] == app._stage_watchdog_delay_s()
        assert watchdog.kwargs["gen_id"] == app._staging_gen_id
        assert app._watchdog_handle is not None

    @pytest.mark.parametrize("interval", [1, 5, 30, 120])
    def test_watchdog_never_pre_empts_the_normal_deadline(self, interval):
        """The margin must scale with `stage_verify_interval_s`, not be fixed.

        The last probe can be scheduled as late as `stage_verify_timeout_s` and
        then take a full probe timeout to answer.  A fixed margin only cleared
        that bar at the default interval; at a larger one the watchdog would
        fire first and abandon a *healthy* generation — deleting a directory
        the in-flight probe was about to see as 200.
        """
        app = _init_with_current_gen(extra_args={"stage_verify_interval_s": interval})
        assert app.stage_verify_interval_s == interval

        delay = app._stage_watchdog_delay_s()
        latest_possible_result = (
            app.stage_verify_timeout_s + interval + _PROBE_TIMEOUT_S
        )
        assert delay > latest_possible_result, (
            f"watchdog at {delay}s would pre-empt a result that can legitimately "
            f"arrive at {latest_possible_result}s (interval={interval})"
        )
        assert _last_run_in(app, app._on_stage_watchdog).args[1] == delay

    def test_watchdog_delay_uses_the_providers_probe_timeout(self):
        """Derived from the real probe timeout, not a duplicated literal."""
        app = _init_with_current_gen()
        assert app._stage_watchdog_delay_s() == (
            app.stage_verify_timeout_s
            + app.stage_verify_interval_s
            + _PROBE_TIMEOUT_S
            + app.STAGE_WATCHDOG_SLACK_S
        )

    def test_watchdog_abandons_when_no_result_ever_arrives(self):
        app = _init_with_current_gen()
        _verify_round(app, exists=True)
        app._on_tick({})
        app.call_service.reset_mock()
        app.log.reset_mock()

        _replace_source_files(app.source_dir, ["FLORIDA_1.jpg", "FLORIDA_2.jpg"])
        app._on_batch_ready(
            "immich_fetcher_batch_ready", {"count": 2, "filter": "Florida"}, {}
        )
        failed_gen = app._staging_gen_id
        url_before = app._last_published_local_url

        # No probe result ever comes back; only the watchdog fires.
        app._on_stage_watchdog(dict(_last_run_in(app, app._on_stage_watchdog).kwargs))

        assert app._pending_gen_id is None
        assert app._staging_in_progress is False
        assert app._staging_gen_id is None
        assert app._last_published_local_url == url_before
        assert app._staged_filter_name == "Florida"
        assert any(c.kwargs.get("gen_id") == failed_gen for c in _cleanup_calls(app))

        warnings = _logs(app, "WARNING", "FAILED verification")
        assert len(warnings) == 1, warnings
        assert "reason=watchdog" in warnings[0]
        assert "/config/www/photo-frame/live/.stage.log" in warnings[0]

        # And the normal recovery still happens.
        app.call_service.reset_mock()
        app._poll_for_changes(reason="poll")
        stage = _stage_calls(app)
        assert len(stage) == 1
        assert stage[0].kwargs["gen_id"] != failed_gen

    def test_deadline_abandon_reports_its_own_reason(self):
        app = _init_with_current_gen()
        _verify_round(app, exists=False, elapsed_s=app.stage_verify_timeout_s + 1)
        warnings = _logs(app, "WARNING", "FAILED verification")
        assert len(warnings) == 1
        assert "reason=deadline" in warnings[0]

    def test_watchdog_for_a_superseded_gen_is_a_noop(self):
        app = _init_with_current_gen()
        in_flight = app._staging_gen_id
        app.call_service.reset_mock()

        app._on_stage_watchdog({"gen_id": "999"})

        assert app._staging_gen_id == in_flight
        assert app._staging_in_progress is True
        assert _cleanup_calls(app) == []

    def test_watchdog_without_a_gen_id_is_a_noop(self):
        app = _init_with_current_gen()
        in_flight = app._staging_gen_id
        app.call_service.reset_mock()

        app._on_stage_watchdog({})

        assert app._staging_gen_id == in_flight
        assert app._staging_in_progress is True
        assert _cleanup_calls(app) == []

    def test_zombie_watchdog_releases_the_latch_without_abandoning(self):
        app = _init_with_current_gen()
        gen = app._staging_gen_id
        newer = _make_app()
        newer.initialize()
        app.call_service.reset_mock()

        app._on_stage_watchdog({"gen_id": gen})

        assert app._staging_in_progress is False
        assert app._pending_gen_id is None
        assert _cleanup_calls(app) == [], "a zombie must not drive cleanup"

    def test_watchdog_is_cancelled_on_successful_verification(self):
        app = _init_with_current_gen()
        handle = app._watchdog_handle
        app.timer_running = MagicMock(return_value=True)
        app.cancel_timer = MagicMock()

        _verify_round(app, exists=True)

        assert app._watchdog_handle is None
        assert handle in [c.args[0] for c in app.cancel_timer.call_args_list]

    def test_watchdog_is_cancelled_on_abandon(self):
        app = _init_with_current_gen()
        handle = app._watchdog_handle
        app.timer_running = MagicMock(return_value=True)
        app.cancel_timer = MagicMock()

        _verify_round(app, exists=False, elapsed_s=app.stage_verify_timeout_s + 1)

        assert app._watchdog_handle is None
        assert handle in [c.args[0] for c in app.cancel_timer.call_args_list]

    def test_watchdog_is_cancelled_on_terminate(self):
        app = _init_with_current_gen()
        handle = app._watchdog_handle
        app.timer_running = MagicMock(return_value=True)
        app.cancel_timer = MagicMock()

        app.terminate()

        assert app._watchdog_handle is None
        assert handle in [c.args[0] for c in app.cancel_timer.call_args_list]

    def test_new_stage_cancels_a_live_watchdog(self):
        """The cancel is unconditional so no timer is ever orphaned."""
        app = _init_with_current_gen()
        handle = app._watchdog_handle
        assert handle is not None
        app.timer_running = MagicMock(return_value=True)
        app.cancel_timer = MagicMock()

        # In production the latch prevents a second stage while one is in
        # flight; call it directly to prove the cancel does not rely on that.
        app._stage_new_generation(app._read_source_file_list(), "fp-xyz")

        assert handle in [c.args[0] for c in app.cancel_timer.call_args_list]
        assert app._watchdog_handle is not None, "a fresh watchdog must be armed"

    def test_watchdog_not_armed_when_verification_is_disabled(self):
        app = _init_with_current_gen(extra_args={"ha_url": ""})
        armed = [
            c for c in app.run_in.call_args_list
            if c.args and c.args[0] == app._on_stage_watchdog
        ]
        assert armed == [], "nothing to watch when there is no chain"
        assert app._watchdog_handle is None


# ----------------------------------------------------------------------
# keep_gens: the HA-side prune's keep list
# ----------------------------------------------------------------------


class TestKeepGens:
    """A generation we abandon can still be completed by the detached worker
    minutes later, after our cleanup already ran against a directory that did
    not exist.  Each successful stage therefore tells the HA-side script which
    generations to keep, and the script prunes everything else.
    """

    def test_builder_keeps_digit_ids_in_order(self):
        assert PhotoFrameViewerApp._build_keep_gens("7", "9") == "7 9"

    def test_builder_drops_duplicates(self):
        assert PhotoFrameViewerApp._build_keep_gens("7", "7") == "7"

    def test_builder_drops_blanks_and_non_digits(self):
        assert PhotoFrameViewerApp._build_keep_gens(
            None, "", "   ", "abc", "1x", "-3", "4.5", "../etc", "8; rm -rf /"
        ) == ""

    def test_builder_keeps_only_the_digit_ids(self):
        assert PhotoFrameViewerApp._build_keep_gens("abc", "12", None, "7") == "12 7"

    def test_stage_call_sends_the_current_gen(self):
        app = _init_with_current_gen("3")
        stage = _stage_calls(app)
        assert len(stage) == 1
        assert stage[0].kwargs["keep_gens"] == "3"

    def test_keep_gens_is_empty_on_a_first_ever_stage(self):
        app = _make_app(current_url="")
        app.initialize()
        stage = _stage_calls(app)
        assert stage[0].kwargs["gen_id"] == "1"
        assert stage[0].kwargs["keep_gens"] == "", (
            "nothing to keep yet — the script must prune nothing"
        )

    def test_replaced_pending_gen_is_not_kept(self):
        """A pending gen this stage supersedes is cleaned up, not kept."""
        app = _init_with_current_gen("3")
        _verify_round(app, exists=True)
        replaced = app._pending_gen_id
        assert replaced is not None

        _replace_source_files(app.source_dir, ["A.jpg", "B.jpg", "C.jpg"])
        app._pending_fingerprint = "force_mismatch"
        app.call_service.reset_mock()
        app._poll_for_changes(reason="poll")

        keep = _stage_calls(app)[0].kwargs["keep_gens"]
        assert keep == "3"
        assert replaced not in keep.split()

    def test_keep_gens_is_logged_with_the_staging_line(self):
        app = _init_with_current_gen("3")
        messages = _logs(app, "INFO", "staging gen=")
        assert len(messages) == 1, messages
        assert "keep_gens='3'" in messages[0]


# ----------------------------------------------------------------------
# shell_command calls are all fire-and-forget
# ----------------------------------------------------------------------


class TestShellCommandsAreNonBlocking:
    """A synchronous `call_service` pins the app's worker thread for up to
    AppDaemon's 60s internal-function timeout.  Nothing reads either shell
    command's result, and both run on latency-sensitive paths — the cleanup
    runs from `_finalize_pending` on the `_on_tick` path and from
    `_abandon_staging` exactly when HA may already be slow to answer.
    """

    def test_cleanup_call_is_fire_and_forget(self):
        app = _init_with_current_gen()
        _verify_round(app, exists=True)
        app.call_service.reset_mock()

        app._call_cleanup("7", reason="test")

        cleanup = _cleanup_calls(app)
        assert len(cleanup) == 1
        callback = cleanup[0].kwargs.get("callback")
        assert callable(callback), "cleanup must not block the app thread"
        assert callback == app._on_cleanup_service_result
        assert cleanup[0].kwargs["gen_id"] == "7"

    def test_cleanup_on_the_tick_path_is_fire_and_forget(self):
        """The gen swap runs inside _on_tick — it must not block there."""
        app = _init_with_current_gen("3")
        _verify_round(app, exists=True)
        app.call_service.reset_mock()

        app._on_tick({})

        cleanup = _cleanup_calls(app)
        assert len(cleanup) == 1
        assert cleanup[0].kwargs["gen_id"] == "3"
        assert callable(cleanup[0].kwargs.get("callback"))

    def test_abandon_cleanup_is_fire_and_forget(self):
        app = _init_with_current_gen()
        _verify_round(app, exists=False, elapsed_s=app.stage_verify_timeout_s + 1)

        cleanup = _cleanup_calls(app)
        assert len(cleanup) == 1
        assert callable(cleanup[0].kwargs.get("callback"))

    def test_every_shell_command_call_passes_a_callback(self):
        app = _init_with_current_gen("3")
        _verify_round(app, exists=True)
        app._on_tick({})
        _replace_source_files(app.source_dir, ["NEXT.jpg"])
        app._poll_for_changes(reason="poll")

        shell_calls = [
            c for c in app.call_service.call_args_list
            if c.args and str(c.args[0]).startswith("shell_command/")
        ]
        assert len(shell_calls) >= 3, shell_calls
        for call in shell_calls:
            assert callable(call.kwargs.get("callback")), (
                f"{call.args[0]} still blocks the app thread"
            )

    def test_input_select_calls_are_left_alone(self):
        """Only shell_command calls are fire-and-forget; state calls are not."""
        app = _init_with_current_gen("3")
        _verify_round(app, exists=True)
        app._on_tick({})

        select_calls = [
            c for c in app.call_service.call_args_list
            if c.args and str(c.args[0]).startswith("input_select/")
        ]
        assert select_calls, "expected the gen swap to drive the picker"
        for call in select_calls:
            assert "callback" not in call.kwargs

    def test_result_callbacks_only_log_at_debug(self):
        app = _init_with_current_gen()
        app.log.reset_mock()

        app._on_stage_service_result({"ok": True})
        app._on_cleanup_service_result(None)

        levels = {c.kwargs.get("level") for c in app.log.call_args_list}
        assert levels == {"DEBUG"}, levels
        messages = [str(c.args[0]) for c in app.log.call_args_list]
        assert any("photo_frame_stage_gen" in m for m in messages)
        assert any("photo_frame_cleanup_gen" in m for m in messages)


# ----------------------------------------------------------------------
# The probe reports WHAT HA answered, not just yes/no
# ----------------------------------------------------------------------


class TestProbeStatusIsReported:
    """`_abandon_staging` used to read identically for "the copy never landed"
    (404 — the next poll fixes it) and "ha_url is wrong / HA is down" (never
    self-heals, display silently frozen, and `.stage.log` says
    `staged 20 files` and sends the operator to the wrong subsystem).
    """

    def test_status_is_remembered_for_the_in_flight_gen(self):
        app = _init_with_current_gen()
        assert app._staging_last_status is None

        _verify_round(app, status=404)
        assert app._staging_last_status == 404

        _verify_round(app, status=_UNREACHABLE)
        assert app._staging_last_status == _UNREACHABLE

    def test_status_is_plumbed_from_the_probe_to_the_result(self):
        app = _init_with_current_gen()
        _verify_round(app, status=301)
        result_call = _last_run_in(app, app._on_stage_verify_result)
        assert result_call.kwargs["status"] == 301

    def test_200_still_promotes_the_generation(self):
        app = _init_with_current_gen()
        gen = app._staging_gen_id
        _verify_round(app, status=200)
        assert app._pending_gen_id == gen

    @pytest.mark.parametrize("status", [404, 301, 401, 500, _UNREACHABLE])
    def test_non_200_never_promotes(self, status):
        app = _init_with_current_gen()
        _verify_round(app, status=status)
        assert app._pending_gen_id is None
        assert app._staging_in_progress is True

    def test_missing_or_unparseable_status_is_treated_as_unreachable(self):
        """Never fall back to "staged" — that is how broken images got published."""
        app = _init_with_current_gen()
        gen = app._staging_gen_id

        app._on_stage_verify_result({"gen_id": gen})
        assert app._pending_gen_id is None
        assert app._staging_last_status == _UNREACHABLE

        app._on_stage_verify_result({"gen_id": gen, "status": "nope"})
        assert app._pending_gen_id is None
        assert app._staging_last_status == _UNREACHABLE

        app._on_stage_verify_result({"gen_id": gen, "status": None})
        assert app._pending_gen_id is None
        assert app._staging_last_status == _UNREACHABLE

    def test_per_check_debug_line_carries_the_status(self):
        app = _init_with_current_gen()
        _verify_round(app, status=403)
        messages = _logs(app, "DEBUG", "not staged yet")
        assert len(messages) == 1, messages
        assert "status=403" in messages[0]

    def test_status_is_reset_between_generations(self):
        app = _init_with_current_gen("3")
        _verify_round(app, status=404)
        assert app._staging_last_status == 404

        _verify_round(app, status=200)
        assert app._staging_last_status is None, (
            "a resolved staging must not leak its status into the next one"
        )

        app._on_tick({})
        _replace_source_files(app.source_dir, ["NEXT.jpg"])
        app._poll_for_changes(reason="poll")
        assert app._staging_last_status is None

    def _abandon_with(self, status: int | None) -> str:
        """Abandon a generation after observing *status* (None = no result)."""
        app = _init_with_current_gen()
        if status is None:
            app._on_stage_watchdog(
                dict(_last_run_in(app, app._on_stage_watchdog).kwargs)
            )
        else:
            _verify_round(
                app, status=status, elapsed_s=app.stage_verify_timeout_s + 1
            )
        warnings = _logs(app, "WARNING", "FAILED verification")
        assert len(warnings) == 1, warnings
        return warnings[0]

    def test_404_warning_blames_staging_and_points_at_the_log(self):
        message = self._abandon_with(404)
        assert "last_status=404" in message
        assert "404" in message and "did not land" in message
        assert "/config/www/photo-frame/live/.stage.log" in message
        assert "ha_url" not in message, (
            "a 404 is a staging problem — do not send the operator after ha_url"
        )

    def test_unreachable_warning_blames_ha_url_and_says_it_will_not_self_heal(self):
        message = self._abandon_with(_UNREACHABLE)
        assert f"last_status={_UNREACHABLE}" in message
        assert "could not reach HA" in message
        assert "ha_url" in message
        assert "NOT self-heal" in message
        assert "merely restarting" in message, (
            "an HA restart is the one unreachable case that does self-heal"
        )
        assert "will not explain it" in message

    def test_other_status_warning_blames_ha_url(self):
        message = self._abandon_with(301)
        assert "last_status=301" in message
        assert "HA answered 301" in message
        assert "ha_url" in message
        assert "NOT self-heal" in message

    def test_watchdog_without_any_result_says_so(self):
        message = self._abandon_with(None)
        assert "last_status=None" in message
        assert "no probe result was ever observed" in message
        assert "/config/www/photo-frame/live/.stage.log" in message

    def test_watchdog_after_a_result_reports_that_status(self):
        app = _init_with_current_gen()
        _verify_round(app, status=502)
        app._on_stage_watchdog(dict(_last_run_in(app, app._on_stage_watchdog).kwargs))

        warnings = _logs(app, "WARNING", "FAILED verification")
        assert len(warnings) == 1
        assert "reason=watchdog" in warnings[0]
        assert "last_status=502" in warnings[0]

    def test_warning_never_leaks_a_credential_or_the_base_url(self):
        for status in (404, 301, _UNREACHABLE):
            message = self._abandon_with(status)
            assert "Bearer" not in message
            assert "token" not in message.lower()
            assert _FAKE_HA_URL not in message, (
                "log the url PATH, not the base url (it could embed credentials)"
            )
            assert "/local/photo-frame/live/" in message


# ----------------------------------------------------------------------
# Album title cannot drift across a long verification window
# ----------------------------------------------------------------------


class TestFilterNameSnapshot:
    """`_on_batch_ready` keeps writing `_staged_filter_name` while a
    generation is being verified, and the latch makes its poll a no-op.  The
    window used to be `stage_settle_delay_s` (3s); verification can now hold it
    for minutes, so without a snapshot the in-flight generation would be
    published under the NEXT album's title.
    """

    def _staged_with_title(self, title: str) -> PhotoFrameViewerApp:
        """An app with one generation in flight, staged under *title*."""
        app = _init_with_current_gen("3")
        _verify_round(app, exists=True)
        app._on_tick({})
        app.log.reset_mock()

        _replace_source_files(app.source_dir, ["A_1.jpg", "A_2.jpg"])
        app._on_batch_ready(
            "immich_fetcher_batch_ready", {"count": 2, "filter": title}, {}
        )
        assert app._staging_gen_id is not None
        return app

    def test_stage_moves_the_title_into_the_snapshot(self):
        app = self._staged_with_title("Album A")
        assert app._staging_filter_name == "Album A"
        assert app._staged_filter_name == "", (
            "_staged_filter_name must be free for the NEXT batch"
        )

    def test_title_arriving_mid_verification_does_not_steal_the_generation(self):
        """The core regression: gen N is album A even if B arrives meanwhile."""
        app = self._staged_with_title("Album A")
        gen_a = app._staging_gen_id

        # A fresh batch lands while gen A is still being verified — the
        # fetcher has already replaced the files on disk, so the event
        # describes a fingerprint gen A knows nothing about.  Its poll is a
        # no-op because the staging latch is held.
        _replace_source_files(app.source_dir, ["B_1.jpg", "B_2.jpg"])
        app._on_batch_ready(
            "immich_fetcher_batch_ready", {"count": 9, "filter": "Album B"}, {}
        )
        assert app._staging_gen_id == gen_a, "the latch must block the new stage"
        assert app._staged_filter_name == "Album B"
        assert app._staging_filter_name == "Album A"

        _verify_round(app, exists=True)

        assert app._pending_gen_id == gen_a
        assert app._pending_filter_name == "Album A", (
            "gen A must not be published under album B's title"
        )

        app._on_tick({})
        assert app._displaying_filter_name == "Album A"

        # ...and the next stage picks up B.
        app._poll_for_changes(reason="poll")
        assert app._staging_filter_name == "Album B"
        _verify_round(app, exists=True)
        app._on_tick({})
        assert app._displaying_filter_name == "Album B"

    def test_abandon_hands_the_title_back_for_the_retry(self):
        app = self._staged_with_title("Album A")
        _verify_round(app, exists=False, elapsed_s=app.stage_verify_timeout_s + 1)

        assert app._staged_filter_name == "Album A"
        assert app._staging_filter_name == ""

        app.call_service.reset_mock()
        app._poll_for_changes(reason="poll")
        assert app._staging_filter_name == "Album A"
        _verify_round(app, exists=True)
        app._on_tick({})
        assert app._displaying_filter_name == "Album A"

    def test_abandon_does_not_clobber_a_newer_title(self):
        app = self._staged_with_title("Album A")
        # A genuinely newer batch: different files, so it is routed to the
        # next stage rather than to the generation being verified.
        _replace_source_files(app.source_dir, ["B_1.jpg"])
        app._on_batch_ready(
            "immich_fetcher_batch_ready", {"count": 9, "filter": "Album B"}, {}
        )

        _verify_round(app, exists=False, elapsed_s=app.stage_verify_timeout_s + 1)

        assert app._staged_filter_name == "Album B", (
            "a batch that arrived during verification describes a newer album"
        )

        app._poll_for_changes(reason="poll")
        assert app._staging_filter_name == "Album B"

    def test_watchdog_abandon_also_hands_the_title_back(self):
        app = self._staged_with_title("Album A")
        app._on_stage_watchdog(dict(_last_run_in(app, app._on_stage_watchdog).kwargs))
        assert app._staged_filter_name == "Album A"

    def test_failed_result_bounce_hands_the_title_back(self):
        app = self._staged_with_title("Album A")
        timer_call = _last_run_in(app, app._on_stage_verify)
        scheduler = app.run_in

        def raising_run_in(callback, delay, **kwargs):
            if callback == app._on_stage_verify_result:
                raise RuntimeError("scheduler gone")
            return scheduler(callback, delay, **kwargs)

        app.run_in = MagicMock(side_effect=raising_run_in)
        with patch(_PROBE, new=AsyncMock(return_value=200)):
            app._on_stage_verify(dict(timer_call.kwargs))
            asyncio.run(app.create_task.call_args_list[-1].args[0])

        assert app._staging_in_progress is False
        assert app._staged_filter_name == "Album A"

    def test_snapshot_is_cleared_between_generations(self):
        app = self._staged_with_title("Album A")
        _verify_round(app, exists=True)
        assert app._staging_filter_name == ""

        app._on_tick({})
        _replace_source_files(app.source_dir, ["C_1.jpg"])
        app._poll_for_changes(reason="poll")
        assert app._staging_filter_name == "", (
            "a stage with no batch_ready must not inherit the previous title"
        )
        _verify_round(app, exists=True)
        assert app._pending_filter_name == ""

    def test_recovered_title_survives_an_untitled_generation(self):
        """A startup re-stage has no batch_ready; the displayed title stays."""
        app = _make_app(current_url="/local/photo-frame/live/10/IMG_001.jpg")
        app.initialize()
        # As if recovered from the sensor / state file on startup.
        app._displaying_filter_name = "Robot"
        assert app._staging_filter_name == "", "the startup stage has no title"

        _verify_round(app, exists=True)
        assert app._pending_filter_name == ""
        app._on_tick({})
        assert app._displaying_filter_name == "Robot", (
            "an untitled generation must not blank the displayed title"
        )

    def test_legacy_no_ha_url_path_uses_the_snapshot_too(self):
        app = _init_with_current_gen("3", extra_args={"ha_url": ""})
        app._on_stage_verify(dict(_last_run_in(app, app._on_stage_verify).kwargs))
        app._on_tick({})

        _replace_source_files(app.source_dir, ["L_1.jpg"])
        app._on_batch_ready(
            "immich_fetcher_batch_ready", {"count": 1, "filter": "Legacy"}, {}
        )
        assert app._staging_filter_name == "Legacy"
        assert app._staged_filter_name == ""

        app._on_stage_verify(dict(_last_run_in(app, app._on_stage_verify).kwargs))
        assert app._pending_filter_name == "Legacy"
        app._on_tick({})
        assert app._displaying_filter_name == "Legacy"

    def test_no_context_settle_leaves_the_next_title_alone(self):
        """A settle with nothing in flight must not eat the next batch's title."""
        app = _init_with_current_gen("3")
        _verify_round(app, exists=True)
        app._on_tick({})
        app._staged_filter_name = "Album Next"

        app._on_stage_settled({})

        assert app._staged_filter_name == "Album Next"


# ----------------------------------------------------------------------
# batch_ready titles are attributed by fingerprint, not arrival time
# ----------------------------------------------------------------------


class TestFilterNameRouting:
    """The fetcher writes its files, THEN publishes a sensor, THEN fires
    `batch_ready`.  Our periodic poll can land in that gap and stage the new
    album's complete files before the event arrives — so a title must be
    attributed to the generation whose files it describes, not to whatever
    happens to be in flight when it lands.
    """

    def _batch_ready(self, app: PhotoFrameViewerApp, title: str) -> None:
        app._on_batch_ready(
            "immich_fetcher_batch_ready", {"count": 2, "filter": title}, {}
        )

    def _routes(self, app: PhotoFrameViewerApp) -> list[str]:
        return [
            m.split("routed to ", 1)[1].split(" ", 1)[0]
            for m in _logs(app, "DEBUG", "routed to")
        ]

    # -- route 1: names the generation being staged/verified ---------------

    def test_route_staging_when_files_match_the_in_flight_gen(self):
        """THE REGRESSION: poll stages the complete batch, event arrives late."""
        app = _init_with_current_gen("3")
        _verify_round(app, exists=True)
        app._on_tick({})
        app.log.reset_mock()

        # The fetcher has finished writing album B; our poll gets there first.
        _replace_source_files(app.source_dir, ["B_1.jpg", "B_2.jpg"])
        app._poll_for_changes(reason="poll")
        gen_b = app._staging_gen_id
        assert gen_b is not None
        assert app._staging_filter_name == "", "staged before the event arrived"

        # ...and only now does batch_ready land.
        self._batch_ready(app, "Album B")

        assert self._routes(app) == ["staging"]
        assert app._staging_filter_name == "Album B"
        assert app._staged_filter_name == "", (
            "the title names the gen in flight, not a future one"
        )

        # End to end: the new photos must appear under their own album name.
        _verify_round(app, exists=True)
        assert app._pending_gen_id == gen_b
        assert app._pending_filter_name == "Album B"
        app._on_tick({})
        assert app._displaying_filter_name == "Album B"

    def test_route_staging_title_survives_an_abandon(self):
        app = _init_with_current_gen("3")
        _verify_round(app, exists=True)
        app._on_tick({})
        _replace_source_files(app.source_dir, ["B_1.jpg"])
        app._poll_for_changes(reason="poll")
        self._batch_ready(app, "Album B")
        assert app._staging_filter_name == "Album B"

        _verify_round(app, exists=False, elapsed_s=app.stage_verify_timeout_s + 1)

        assert app._staged_filter_name == "Album B", "the retry must keep it"
        app._poll_for_changes(reason="poll")
        _verify_round(app, exists=True)
        app._on_tick({})
        assert app._displaying_filter_name == "Album B"

    # -- route 2: names a generation already pending -----------------------

    def test_route_pending_when_files_match_the_pending_gen(self):
        app = _init_with_current_gen("3")
        _verify_round(app, exists=True)
        app._on_tick({})
        _replace_source_files(app.source_dir, ["P_1.jpg", "P_2.jpg"])
        app._poll_for_changes(reason="poll")
        _verify_round(app, exists=True)
        pending = app._pending_gen_id
        assert pending is not None
        app.log.reset_mock()

        self._batch_ready(app, "Album P")

        assert self._routes(app) == ["pending"]
        assert app._pending_filter_name == "Album P"
        assert app._staged_filter_name == ""
        assert app._pending_gen_id == pending

        app._on_tick({})
        assert app._displaying_filter_name == "Album P"

    # -- route 3: names the album already on screen ------------------------

    def test_route_current_retitles_in_place_and_persists(self):
        app = _init_with_current_gen("3")
        _verify_round(app, exists=True)
        app._on_tick({})
        app.log.reset_mock()
        app.set_state.reset_mock()
        url_before = app._last_published_local_url

        self._batch_ready(app, "Album C")

        assert self._routes(app) == ["current"]
        assert app._displaying_filter_name == "Album C"
        assert app._last_published_local_url == url_before, (
            "retitling must not move the displayed image"
        )

        published = [
            c.kwargs["attributes"]["displaying_filter_name"]
            for c in app.set_state.call_args_list
            if "attributes" in c.kwargs
        ]
        assert "Album C" in published, "the sensor must carry the new title"

        state = json.loads(Path(app._state_file).read_text())
        assert state["displaying_filter_name"] == "Album C"

    def test_route_current_is_a_noop_when_the_title_is_unchanged(self):
        app = _init_with_current_gen("3")
        _verify_round(app, exists=True)
        app._on_tick({})
        self._batch_ready(app, "Album C")
        app.log.reset_mock()
        app.set_state.reset_mock()

        self._batch_ready(app, "Album C")

        assert self._routes(app) == ["current_unchanged"]
        assert app.set_state.call_count == 0
        assert app._displaying_filter_name == "Album C"

    # -- route 4: names a batch we have not seen yet -----------------------

    def test_route_next_stage_when_files_match_nothing_known(self):
        app = _init_with_current_gen("3")
        _verify_round(app, exists=True)
        app._on_tick({})
        app.log.reset_mock()

        _replace_source_files(app.source_dir, ["N_1.jpg"])
        self._batch_ready(app, "Album N")

        assert self._routes(app) == ["next_stage"]
        # The poll inside _on_batch_ready stages it and takes the snapshot.
        assert app._staging_filter_name == "Album N"
        assert app._staged_filter_name == ""

    def test_route_next_stage_when_the_source_dir_is_empty(self):
        app = _init_with_current_gen("3")
        _verify_round(app, exists=True)
        app._on_tick({})
        _replace_source_files(app.source_dir, [])
        app.log.reset_mock()

        self._batch_ready(app, "Album E")

        assert app._staged_filter_name == "Album E"
        assert _logs(app, "DEBUG", "no source files on disk")

    # -- general -----------------------------------------------------------

    def test_empty_filter_name_changes_nothing(self):
        app = _init_with_current_gen("3")
        _verify_round(app, exists=True)
        app._on_tick({})
        self._batch_ready(app, "Album C")
        app.log.reset_mock()

        app._on_batch_ready("immich_fetcher_batch_ready", {"count": 2}, {})
        app._on_batch_ready(
            "immich_fetcher_batch_ready", {"count": 2, "filter": "  "}, {}
        )

        assert self._routes(app) == []
        assert app._displaying_filter_name == "Album C"
        assert app._staged_filter_name == ""

    def test_source_is_scanned_once_per_batch_ready(self):
        """The routing fingerprint is handed to the poll, not recomputed."""
        app = _init_with_current_gen("3")
        _verify_round(app, exists=True)
        app._on_tick({})

        with patch.object(
            app, "_read_source_file_list", wraps=app._read_source_file_list
        ) as scan:
            self._batch_ready(app, "Album C")

        assert scan.call_count == 1

    def test_legacy_no_ha_url_path_routes_the_same_way(self):
        app = _init_with_current_gen("3", extra_args={"ha_url": ""})
        app._on_stage_verify(dict(_last_run_in(app, app._on_stage_verify).kwargs))
        app._on_tick({})
        app.log.reset_mock()

        # Poll first, event second — the regression scenario, legacy path.
        _replace_source_files(app.source_dir, ["L_1.jpg"])
        app._poll_for_changes(reason="poll")
        self._batch_ready(app, "Album L")

        assert self._routes(app) == ["staging"]
        app._on_stage_verify(dict(_last_run_in(app, app._on_stage_verify).kwargs))
        app._on_tick({})
        assert app._displaying_filter_name == "Album L"

    def test_after_terminate_a_matching_title_goes_to_the_next_stage(self):
        """`terminate()` clears the latch but keeps the staging context.

        Routing on the fingerprint alone would then hand the title to a
        snapshot nobody will ever consume; the latch check is what sends it to
        the next stage instead.
        """
        app = _init_with_current_gen("3")
        _verify_round(app, exists=True)
        app._on_tick({})
        _replace_source_files(app.source_dir, ["T_1.jpg"])
        app._poll_for_changes(reason="poll")
        assert app._staging_fingerprint is not None

        app.terminate()
        assert app._staging_in_progress is False
        assert app._staging_fingerprint is not None, "context deliberately kept"
        app.log.reset_mock()

        self._batch_ready(app, "Album T")

        assert self._routes(app) == ["next_stage"]
        # The poll inside _on_batch_ready re-stages (the latch is clear) and
        # snapshots the title onto that new generation — routing it to the dead
        # context instead would have lost it when the re-stage overwrote it.
        assert app._staging_filter_name == "Album T"


# ----------------------------------------------------------------------
# Review round 5 — the generation swap publishes the sensor consistently
# ----------------------------------------------------------------------


class TestSwapNeverPublishesNewTitleOverOldImage:
    """The status sensor carries the album title, ``image_url`` and
    ``current_gen`` in ONE attribute set.  Promoting the title through the
    shared helper must not publish before ``_finalize_pending()``, or the card
    shows album B's title over album A's image for the length of two picker
    round trips.
    """

    def _pending_album(self, title: str) -> tuple[PhotoFrameViewerApp, str]:
        app = _init_with_current_gen("3")
        _verify_round(app, exists=True)
        app._on_tick({})

        _replace_source_files(app.source_dir, ["B_1.jpg", "B_2.jpg"])
        app._on_batch_ready(
            "immich_fetcher_batch_ready", {"count": 2, "filter": title}, {}
        )
        _verify_round(app, exists=True)
        assert app._pending_gen_id is not None
        assert app._pending_filter_name == title
        return app, app._pending_gen_id

    def test_every_sensor_publish_during_the_swap_is_self_consistent(self):
        app, pending_gen = self._pending_album("Album B")
        old_gen = app._current_gen_id
        assert old_gen != pending_gen

        seen: list[tuple[str, str]] = []
        real_publish = app._publish_sensor_state

        def recording_publish():
            seen.append((app._displaying_filter_name, str(app._current_gen_id)))
            return real_publish()

        app._publish_sensor_state = recording_publish
        app._on_tick({})

        assert app._current_gen_id == pending_gen
        assert app._displaying_filter_name == "Album B"
        assert seen, "the swap must publish the sensor at least once"
        assert ("Album B", str(old_gen)) not in seen, (
            "published the NEW title while current_gen was still the OLD "
            f"generation: {seen}"
        )
        assert seen[-1] == ("Album B", str(pending_gen))

    def test_swap_with_no_labels_still_publishes_the_promoted_title(self):
        app, pending_gen = self._pending_album("Album B")
        app._pending_labels = []
        app._pending_label_to_path = {}
        app._pending_path_to_label = {}

        seen: list[tuple[str, str]] = []
        real_publish = app._publish_sensor_state

        def recording_publish():
            seen.append((app._displaying_filter_name, str(app._current_gen_id)))
            return real_publish()

        app._publish_sensor_state = recording_publish
        app._apply_pending_gen(reason="tick")

        assert ("Album B", str(pending_gen)) in seen

    def test_late_title_for_the_album_on_screen_still_publishes(self):
        """Route 3 keeps the default publish=True: the URL does not move, so
        the helper's own publish is the only thing that reaches the card."""
        app, _ = self._pending_album("Album B")
        app._on_tick({})
        app._displaying_filter_name = ""

        seen: list[str] = []
        real_publish = app._publish_sensor_state

        def recording_publish():
            seen.append(app._displaying_filter_name)
            return real_publish()

        app._publish_sensor_state = recording_publish
        app._on_batch_ready(
            "immich_fetcher_batch_ready", {"count": 2, "filter": "Album B"}, {}
        )
        assert seen == ["Album B"]
